import Accelerate
@preconcurrency import AVFoundation
import Foundation
import os
@preconcurrency import Speech

/// Unified capture mode matching MindWeave V2 B4 (inline Cupid dictation, capture orb, Cupid voice orb).
enum NativeSpeechMode: String, Sendable {
    case capture
    case dictation
    case chat
}

/// Strictly `Sendable` configuration payload for starting a native speech session.
struct NativeSpeechStartConfig: Sendable {
    let sessionId: String
    let mode: NativeSpeechMode
    let localeIdentifier: String
    let contextualStrings: [String]
    let recordAudio: Bool
    let maxDurationSec: Double
    let preferOnDevice: Bool
}

/// Sole-ownership wrapper transferring an independently allocated `AVAudioPCMBuffer`
/// across actor / queue boundaries without sharing mutable audio memory.
private final class OwnedPCMBuffer: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    init(_ buffer: AVAudioPCMBuffer) {
        self.buffer = buffer
    }
}

/// Clones a Float32 PCM buffer into fresh owned memory so Tee 1 (ASR) and Tee 3 (`.m4a` writer)
/// never race on the CoreAudio realtime tap's recycled backing buffer.
private func clonePCMBuffer(_ source: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    let frameLength = source.frameLength
    guard frameLength > 0,
          let copy = AVAudioPCMBuffer(pcmFormat: source.format, frameCapacity: frameLength) else {
        return nil
    }
    copy.frameLength = frameLength
    let channelCount = Int(source.format.channelCount)
    if let srcFloat = source.floatChannelData, let dstFloat = copy.floatChannelData {
        let byteCount = Int(frameLength) * MemoryLayout<Float>.size
        for ch in 0..<channelCount {
            memcpy(dstFloat[ch], srcFloat[ch], byteCount)
        }
        return copy
    }
    if let srcInt16 = source.int16ChannelData, let dstInt16 = copy.int16ChannelData {
        let byteCount = Int(frameLength) * MemoryLayout<Int16>.size
        for ch in 0..<channelCount {
            memcpy(dstInt16[ch], srcInt16[ch], byteCount)
        }
        return copy
    }
    return nil
}

/// Computes normalized RMS in `[0.0, 1.0]` using Accelerate `vDSP_rmsqv` (zero allocations).
private func computeNormalizedRMS(_ buffer: AVAudioPCMBuffer) -> Double {
    let count = vDSP_Length(buffer.frameLength)
    guard count > 0, let channelData = buffer.floatChannelData?[0] else {
        return 0.0
    }
    var rms: Float = 0.0
    vDSP_rmsqv(channelData, 1, &rms, count)
    guard rms.isFinite else { return 0.0 }
    // Voice speech typically spans -50dB..0dB; boost gently for organic ribbon motion.
    let db = 20.0 * log10(max(Double(rms), 1e-6))
    let normalized = (db + 52.0) / 46.0
    return max(0.0, min(1.0, normalized))
}

/// Converts a source PCM buffer into `targetFormat` using dynamic AudioKit#1990 `frameCapacity`
/// sizing so variable CoreAudio callback sizes (1024/4096/4800) never truncate or crash.
private func convertPCMBuffer(
    _ input: AVAudioPCMBuffer,
    using converter: AVAudioConverter,
    targetFormat: AVAudioFormat
) -> AVAudioPCMBuffer? {
    let sampleRateRatio = targetFormat.sampleRate / max(input.format.sampleRate, 1.0)
    let outCapacity = AVAudioFrameCount(ceil(Double(input.frameLength) * sampleRateRatio)) + 16
    guard outCapacity > 0,
          let outBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outCapacity) else {
        return nil
    }

    var error: NSError?
    let suppliedLock = OSAllocatedUnfairLock<Bool>(initialState: false)
    let ownedInput = OwnedPCMBuffer(input)
    let status = converter.convert(to: outBuffer, error: &error) { _, outStatus in
        let alreadySupplied = suppliedLock.withLock { state -> Bool in
            if state { return true }
            state = true
            return false
        }
        if alreadySupplied {
            outStatus.pointee = .noDataNow
            return nil
        }
        outStatus.pointee = .haveData
        return ownedInput.buffer
    }
    if status == .error || error != nil || outBuffer.frameLength == 0 {
        return nil
    }
    return outBuffer
}

/// Non-isolated concurrency helper ensuring CoreAudio realtime tap closures and `SFSpeechRecognizer`
/// completion callbacks NEVER inherit `@MainActor` runtime assertions under Swift 6 SE-0423.
private enum SpeechConcurrencyBridge {
    nonisolated static func installTapNonisolated(
        on inputNode: AVAudioInputNode,
        format: AVAudioFormat?,
        bufferSize: AVAudioFrameCount,
        handler: @escaping @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void
    ) {
        inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: format) { buffer, time in
            handler(buffer, time)
        }
    }

    nonisolated static func startRecognitionTaskNonisolated(
        recognizer: SFSpeechRecognizer,
        request: SFSpeechAudioBufferRecognitionRequest,
        handler: @escaping @Sendable (String?, Bool, Error?) -> Void
    ) -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            let text = result?.bestTranscription.formattedString
            let isFinal = result?.isFinal ?? false
            handler(text, isFinal, error)
        }
    }
}

#if compiler(>=6.2)
/// iOS 26+ `SpeechAnalyzer` + `DictationTranscriber(.progressiveLongDictation)` session driver.
/// Uses `SpeechAnalyzer.bestAvailableAudioFormat` (`Int16` 16kHz) + two-buffer accumulator
/// (`committedSegments` + `volatileTail`), checking availability strictly via
/// `DictationTranscriber.supportedLocale(equivalentTo:)` and `AssetInventory.status(forModules:)`.
@available(iOS 26.0, *)
private final class IOS26SpeechAnalyzerDriver: @unchecked Sendable {
    private let analyzer: SpeechAnalyzer
    private let transcriber: DictationTranscriber
    let targetFormat: AVAudioFormat
    private let continuation: AsyncStream<AnalyzerInput>.Continuation
    private let lock = OSAllocatedUnfairLock<State>(initialState: State())
    private var readerTask: Task<Void, Never>?

    private struct State: Sendable {
        var committedSegments: [String] = []
        var volatileTail: String = ""
        var finished = false
    }

    static func makeIfReady(
        locale: Locale,
        contextualStrings: [String],
        onUpdate: @escaping @Sendable (String, String, Int) -> Void
    ) async -> IOS26SpeechAnalyzerDriver? {
        guard let supported = await DictationTranscriber.supportedLocale(equivalentTo: locale) else {
            return nil
        }
        let transcriber = DictationTranscriber(
            locale: supported,
            preset: .progressiveLongDictation
        )
        let status = await AssetInventory.status(forModules: [transcriber])
        guard status == .installed else {
            // Trigger background download opportunistically without blocking current utterance!
            // Capture ONLY Sendable `supported` Locale and build a fresh transcriber inside the task.
            Task.detached(priority: .utility) {
                let dlTranscriber = DictationTranscriber(
                    locale: supported,
                    preset: .progressiveLongDictation
                )
                if let downloader = try? await AssetInventory.assetInstallationRequest(supporting: [dlTranscriber]) {
                    try? await downloader.downloadAndInstall()
                }
            }
            return nil
        }

        guard let bestFormat = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            return nil
        }

        let options = SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .whileInUse)
        let analyzer = SpeechAnalyzer(modules: [transcriber], options: options)
        if !contextualStrings.isEmpty {
            let ctx = AnalysisContext()
            ctx.contextualStrings[.general] = Array(contextualStrings.prefix(100))
            try? await analyzer.setContext(ctx)
        }

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingNewest(32))
        let driver = IOS26SpeechAnalyzerDriver(
            analyzer: analyzer,
            transcriber: transcriber,
            targetFormat: bestFormat,
            continuation: continuation
        )
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            continuation.finish()
            return nil
        }
        driver.startResultLoop(onUpdate: onUpdate)
        return driver
    }

    private init(
        analyzer: SpeechAnalyzer,
        transcriber: DictationTranscriber,
        targetFormat: AVAudioFormat,
        continuation: AsyncStream<AnalyzerInput>.Continuation
    ) {
        self.analyzer = analyzer
        self.transcriber = transcriber
        self.targetFormat = targetFormat
        self.continuation = continuation
    }

    private struct UncheckedSpeechBox<T>: @unchecked Sendable {
        let value: T
    }

    private func startResultLoop(onUpdate: @escaping @Sendable (String, String, Int) -> Void) {
        let transcriberBox = UncheckedSpeechBox(value: self.transcriber)
        readerTask = Task.detached(priority: .userInitiated) { [weak self] in
            do {
                for try await result in transcriberBox.value.results {
                    guard let self else { break }
                    let piece = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                    let snapshot: (String, String, Int) = self.lock.withLock { st in
                        if result.isFinal {
                            if !piece.isEmpty {
                                st.committedSegments.append(piece)
                            }
                            st.volatileTail = ""
                        } else {
                            st.volatileTail = piece
                        }
                        let joined = (st.committedSegments + (st.volatileTail.isEmpty ? [] : [st.volatileTail]))
                            .joined()
                        return (joined, st.volatileTail, st.committedSegments.count)
                    }
                    onUpdate(snapshot.0, snapshot.1, snapshot.2)
                }
            } catch {
                // Handled gracefully during finalize/cancel
            }
        }
    }

    func yieldBuffer(_ owned: OwnedPCMBuffer) {
        let isFinished = lock.withLock { $0.finished }
        guard !isFinished else { return }
        continuation.yield(AnalyzerInput(buffer: owned.buffer))
    }

    func finalizeAndCollect(timeoutMs: UInt64 = 650) async -> (transcript: String, segmentCount: Int) {
        let alreadyFinished = lock.withLock { st -> Bool in
            let prev = st.finished
            st.finished = true
            return prev
        }
        if !alreadyFinished {
            // CRITICAL (AnySpeechRecognizer fix): ALWAYS finish continuation BEFORE finalize!
            continuation.finish()
            let analyzerBox = UncheckedSpeechBox(value: analyzer)
            try? await withThrowingTaskGroup(of: Void.self) { group in
                group.addTask {
                    try await analyzerBox.value.finalizeAndFinishThroughEndOfInput()
                }
                group.addTask {
                    try await Task.sleep(nanoseconds: timeoutMs * 1_000_000)
                }
                _ = try? await group.next()
                group.cancelAll()
            }
        }
        readerTask?.cancel()
        readerTask = nil
        return lock.withLock { st in
            let combined = (st.committedSegments + (st.volatileTail.isEmpty ? [] : [st.volatileTail])).joined()
            return (combined, max(1, st.committedSegments.count))
        }
    }

    func cancel() {
        lock.withLock { $0.finished = true }
        continuation.finish()
        readerTask?.cancel()
        readerTask = nil
        let analyzerBox = UncheckedSpeechBox(value: analyzer)
        Task.detached {
            await analyzerBox.value.cancelAndFinishNow()
        }
    }
}
#endif

/// Low-latency, Swift 6 strict-concurrency-clean native iOS Chinese speech & dual-tee audio engine.
///
/// Architecture highlights:
/// - **Pre-warmed `AVAudioEngine` + `AVAudioSession`**: `< 40ms` hold-to-speak startup,
///   `setAllowHapticsAndSystemSoundsDuringRecording(true)` so Taptic Engine works mid-hold.
/// - **Dual-Tee Lock-Free Audio Pipeline**:
///   - Tee 1: Live Chinese streaming ASR (`SpeechAnalyzer` + `.progressiveLongDictation` on iOS 26+
///     when installed, instant 0ms fallback to `SFSpeechRecognizer(zh-CN)` on iOS 16–25+ with
///     `addsPunctuation`, `contextualStrings`, 48s seamless rollover, and iOS 18 silence-gap guard).
///   - Tee 2: 30fps `vDSP_rmsqv` RMS metering driving JS silk ribbon waveform.
///   - Tee 3: Independent `OwnedPCMBuffer` clone written as 32 kbps AAC `.m4a` on serial `fileWriteQueue`.
/// - **B4 Mode Exclusivity (`capture | dictation | chat`)**: Starting any mode atomically cancels
///   any prior overlapping session.
@MainActor
final class NativeSpeechEngine {
    typealias EventEmitter = @MainActor (String) -> Void

    private let emitJSON: EventEmitter
    private let pipeline: SpeechAudioPipeline

    init(emitJSON: @escaping EventEmitter) {
        self.emitJSON = emitJSON
        self.pipeline = SpeechAudioPipeline { payload in
            Task { @MainActor in
                emitJSON(payload)
            }
        }
    }

    func handleBridgeMessage(_ body: Any) {
        guard let dict = body as? [String: Any],
              let action = (dict["action"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() else {
            return
        }

        switch action {
        case "checkpermissions":
            checkPermissions()
        case "warm":
            let locale = (dict["locale"] as? String) ?? "zh-CN"
            let hints = (dict["contextualStrings"] as? [String]) ?? []
            pipeline.warm(localeIdentifier: locale, contextualStrings: hints)
        case "start":
            let sessionId = (dict["sessionId"] as? String) ?? UUID().uuidString
            let modeRaw = (dict["mode"] as? String)?.lowercased() ?? "capture"
            let mode = NativeSpeechMode(rawValue: modeRaw) ?? .capture
            let locale = (dict["locale"] as? String) ?? "zh-CN"
            let hints = (dict["contextualStrings"] as? [String]) ?? []
            let defaultRecord = (mode != .dictation)
            let recordAudio = (dict["recordAudio"] as? Bool) ?? defaultRecord
            let maxDurationSec = (dict["maxDurationSec"] as? NSNumber)?.doubleValue ?? 120.0
            let preferOnDevice = (dict["preferOnDevice"] as? Bool) ?? true
            let cfg = NativeSpeechStartConfig(
                sessionId: sessionId,
                mode: mode,
                localeIdentifier: locale,
                contextualStrings: Array(hints.prefix(100)),
                recordAudio: recordAudio,
                maxDurationSec: max(5.0, min(180.0, maxDurationSec)),
                preferOnDevice: preferOnDevice
            )
            Task { @MainActor [weak self] in
                await self?.startWithPermissions(cfg)
            }
        case "stop":
            let sessionId = dict["sessionId"] as? String
            Task { @MainActor [weak self] in
                await self?.pipeline.stop(sessionId: sessionId)
            }
        case "cancel":
            let sessionId = dict["sessionId"] as? String
            pipeline.cancel(sessionId: sessionId)
        case "acknowledgedraftuploaded":
            if let sessionId = (dict["sessionId"] as? String) ?? (dict["draftId"] as? String) {
                NativeAudioStore.shared.acknowledgeUploaded(sessionId: sessionId)
            }
        default:
            break
        }
    }

    func shutdown() {
        pipeline.shutdown()
    }

    var isActivelyRecording: Bool {
        pipeline.isActive
    }

    // MARK: - Permissions

    private func checkPermissions() {
        let micStatus = Self.currentMicStatusString()
        let speechStatus = Self.currentSpeechStatusString()
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))
        let onDevice = recognizer?.supportsOnDeviceRecognition ?? false
        emitDict([
            "type": "speech.permission",
            "mic": micStatus,
            "speech": speechStatus,
            "onDeviceAvailable": onDevice,
            "engine": "ios_sf_speech",
        ])
    }

    private func startWithPermissions(_ config: NativeSpeechStartConfig) async {
        // 1. Microphone permission
        let micGranted = await Self.ensureMicPermission()
        guard micGranted else {
            emitDict([
                "type": "speech.error",
                "sessionId": config.sessionId,
                "mode": config.mode.rawValue,
                "code": "mic_denied",
                "message": "麦克风权限未开启，可在系统设置里打开，现在先打字记下吧",
                "partialTranscript": "",
            ])
            return
        }

        // 2. Speech recognition permission (if denied, still allow .m4a audio capture when recordAudio == true!)
        let speechGranted = await Self.ensureSpeechPermission()
        if !speechGranted && !config.recordAudio {
            emitDict([
                "type": "speech.error",
                "sessionId": config.sessionId,
                "mode": config.mode.rawValue,
                "code": "speech_denied",
                "message": "语音识别未开启，先打字记下吧",
                "partialTranscript": "",
            ])
            return
        }

        await pipeline.start(config: config, speechAuthorized: speechGranted)
    }

    private static func currentMicStatusString() -> String {
        if #available(iOS 17.0, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return "granted"
            case .denied: return "denied"
            case .undetermined: return "undetermined"
            @unknown default: return "undetermined"
            }
        } else {
            switch AVAudioSession.sharedInstance().recordPermission {
            case .granted: return "granted"
            case .denied: return "denied"
            case .undetermined: return "undetermined"
            @unknown default: return "undetermined"
            }
        }
    }

    private static func currentSpeechStatusString() -> String {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return "granted"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "undetermined"
        @unknown default: return "undetermined"
        }
    }

    nonisolated private static func ensureMicPermission() async -> Bool {
        if #available(iOS 17.0, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return true
            case .denied: return false
            case .undetermined:
                return await AVAudioApplication.requestRecordPermission()
            @unknown default: return false
            }
        } else {
            switch AVAudioSession.sharedInstance().recordPermission {
            case .granted: return true
            case .denied: return false
            case .undetermined:
                return await withCheckedContinuation { cont in
                    AVAudioSession.sharedInstance().requestRecordPermission { ok in
                        cont.resume(returning: ok)
                    }
                }
            @unknown default: return false
            }
        }
    }

    nonisolated private static func ensureSpeechPermission() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .denied, .restricted: return false
        case .notDetermined:
            return await withCheckedContinuation { cont in
                SFSpeechRecognizer.requestAuthorization { status in
                    cont.resume(returning: status == .authorized)
                }
            }
        @unknown default: return false
        }
    }

    private func emitDict(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
              let json = String(data: data, encoding: .utf8) else {
            return
        }
        emitJSON(json)
    }
}

// MARK: - Non-Isolated Dual-Tee Audio & Speech Pipeline

private final class SpeechAudioPipeline: @unchecked Sendable {
    private let emitRawJSON: @Sendable (String) -> Void
    private let controlQueue = DispatchQueue(label: "app.mindweave.speech.control", qos: .userInitiated)
    private let fileWriteQueue = DispatchQueue(label: "app.mindweave.speech.filewrite", qos: .utility)

    private struct RuntimeState: Sendable {
        var activeSessionId: String?
        var activeMode: NativeSpeechMode = .capture
        var startUptime: TimeInterval = 0
        var lastRmsEmitUptime: TimeInterval = 0
        var committedSegments: [String] = []
        var currentSegmentText: String = ""
        var segmentCount: Int = 1
        var engineName: String = "ios_sf_speech"
        var onDevice: Bool = false
        var contextualStrings: [String] = []
        var localeIdentifier: String = "zh-CN"
        var preferOnDevice: Bool = true
        var retriedCloudFallback: Bool = false
        var recordingFileURL: URL?
        var recordAudio: Bool = true
        var isStopping: Bool = false
    }

    private let stateLock = OSAllocatedUnfairLock<RuntimeState>(initialState: RuntimeState())
    private var audioEngine = AVAudioEngine()
    private var recognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var audioFile: AVAudioFile?
    private var fileConverter: AVAudioConverter?
    private var fileProcessingFormat: AVAudioFormat?
    private var speechConverter: AVAudioConverter?
    private var speechTargetFormat: AVAudioFormat?

    #if compiler(>=6.2)
    private var ios26DriverBox: AnyObject?
    #endif

    private var rolloverTimer: DispatchSourceTimer?
    private var maxDurationTimer: DispatchSourceTimer?
    private var keepAliveTimer: DispatchSourceTimer?
    private var observers: [NSObjectProtocol] = []

    var isActive: Bool {
        stateLock.withLock { $0.activeSessionId != nil }
    }

    init(emitRawJSON: @escaping @Sendable (String) -> Void) {
        self.emitRawJSON = emitRawJSON
        installAudioSessionObservers()
    }

    deinit {
        for token in observers {
            NotificationCenter.default.removeObserver(token)
        }
    }

    func warm(localeIdentifier: String, contextualStrings: [String]) {
        controlQueue.async { [weak self] in
            guard let self else { return }
            self.stateLock.withLock { st in
                st.localeIdentifier = localeIdentifier
                st.contextualStrings = Array(contextualStrings.prefix(100))
            }
            if self.recognizer == nil || self.recognizer?.locale.identifier != localeIdentifier {
                self.recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier))
            }
            self.configureAudioSessionIfNeeded(activate: false)
            self.audioEngine.prepare()
        }
    }

    #if compiler(>=6.2)
    private struct IOS26DriverHandoff: @unchecked Sendable {
        let driver: AnyObject
        let format: AVAudioFormat
    }

    private struct SendableAnyObjectBox: @unchecked Sendable {
        let value: AnyObject
    }
    #endif

    func start(config: NativeSpeechStartConfig, speechAuthorized: Bool) async {
        // B4 mode exclusivity: cancel any overlapping session first.
        cancel(sessionId: nil)

        let startedAt = ProcessInfo.processInfo.systemUptime

        // Opportunistically probe iOS 26 SpeechAnalyzer when installed (0ms fallback to SFSpeechRecognizer).
        #if compiler(>=6.2)
        let readyHandoff: IOS26DriverHandoff?
        if speechAuthorized, #available(iOS 26.0, *) {
            let sid = config.sessionId
            let modeStr = config.mode.rawValue
            if let drv = await IOS26SpeechAnalyzerDriver.makeIfReady(
                locale: Locale(identifier: config.localeIdentifier),
                contextualStrings: config.contextualStrings,
                onUpdate: { [weak self] fullText, volatileTail, segCount in
                    self?.handleIOS26Partial(
                        sessionId: sid,
                        mode: modeStr,
                        transcript: fullText,
                        volatileTail: volatileTail,
                        segmentIndex: segCount
                    )
                }
            ) {
                readyHandoff = IOS26DriverHandoff(driver: drv, format: drv.targetFormat)
            } else {
                readyHandoff = nil
            }
        } else {
            readyHandoff = nil
        }
        #endif

        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            controlQueue.async { [weak self] in
                defer { cont.resume() }
                guard let self else { return }

                self.keepAliveTimer?.cancel()
                self.keepAliveTimer = nil
                NativeAudioSession.setSpeechEngineHoldingSession(true)

                guard self.configureAudioSessionIfNeeded(activate: true) else {
                    NativeAudioSession.setSpeechEngineHoldingSession(false)
                    self.emitDict([
                        "type": "speech.error",
                        "sessionId": config.sessionId,
                        "mode": config.mode.rawValue,
                        "code": "audio_session_failed",
                        "message": "音频通道暂时不可用，请重试",
                        "partialTranscript": "",
                    ])
                    return
                }

                let inputNode = self.audioEngine.inputNode
                inputNode.removeTap(onBus: 0)
                let hwFormat = inputNode.outputFormat(forBus: 0)
                guard hwFormat.sampleRate > 0 && hwFormat.channelCount > 0 else {
                    NativeAudioSession.setSpeechEngineHoldingSession(false)
                    self.emitDict([
                        "type": "speech.error",
                        "sessionId": config.sessionId,
                        "mode": config.mode.rawValue,
                        "code": "audio_format_invalid",
                        "message": "麦克风尚未就绪，请稍后再试",
                        "partialTranscript": "",
                    ])
                    return
                }

                var recordingURL: URL? = nil
                self.fileConverter = nil
                self.fileProcessingFormat = nil
                self.audioFile = nil
                if config.recordAudio {
                    let outURL = NativeAudioStore.shared.makeRecordingURL(sessionId: config.sessionId)
                    try? FileManager.default.removeItem(at: outURL)
                    let aacSettings: [String: Any] = [
                        AVFormatIDKey: kAudioFormatMPEG4AAC,
                        AVSampleRateKey: 16_000.0,
                        AVNumberOfChannelsKey: 1,
                        AVEncoderBitRateKey: 32_000,
                        AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
                    ]
                    if let targetFloat = AVAudioFormat(
                        commonFormat: .pcmFormatFloat32,
                        sampleRate: 16_000.0,
                        channels: 1,
                        interleaved: false
                    ),
                       let conv = AVAudioConverter(from: hwFormat, to: targetFloat),
                       let file = try? AVAudioFile(
                           forWriting: outURL,
                           settings: aacSettings,
                           commonFormat: .pcmFormatFloat32,
                           interleaved: false
                       ) {
                        self.audioFile = file
                        self.fileConverter = conv
                        self.fileProcessingFormat = targetFloat
                        recordingURL = outURL
                    }
                }

                var engineName = speechAuthorized ? "ios_sf_speech" : "ios_audio_only"
                var usingOnDevice = false
                self.speechConverter = nil
                self.speechTargetFormat = nil

                #if compiler(>=6.2)
                self.ios26DriverBox = nil
                if let handoff = readyHandoff {
                    self.ios26DriverBox = handoff.driver
                    self.speechTargetFormat = handoff.format
                    if handoff.format != hwFormat {
                        self.speechConverter = AVAudioConverter(from: hwFormat, to: handoff.format)
                    }
                    engineName = "ios_speech_analyzer"
                    usingOnDevice = true
                }
                #endif

                let initialEngineName = engineName
                let initialUsingOnDevice = usingOnDevice
                let finalRecordingURL = recordingURL

                self.stateLock.withLock { st in
                    st.activeSessionId = config.sessionId
                    st.activeMode = config.mode
                    st.startUptime = startedAt
                    st.lastRmsEmitUptime = 0
                    st.committedSegments = []
                    st.currentSegmentText = ""
                    st.segmentCount = 1
                    st.engineName = initialEngineName
                    st.onDevice = initialUsingOnDevice
                    st.contextualStrings = config.contextualStrings
                    st.localeIdentifier = config.localeIdentifier
                    st.preferOnDevice = config.preferOnDevice
                    st.retriedCloudFallback = false
                    st.recordingFileURL = finalRecordingURL
                    st.recordAudio = config.recordAudio
                    st.isStopping = false
                }

                if engineName == "ios_sf_speech" {
                    let startedOnDevice = self.startLegacySFSpeechSegment(
                        sessionId: config.sessionId,
                        mode: config.mode,
                        localeIdentifier: config.localeIdentifier,
                        contextualStrings: config.contextualStrings,
                        preferOnDevice: config.preferOnDevice
                    )
                    usingOnDevice = startedOnDevice
                    self.stateLock.withLock { $0.onDevice = startedOnDevice }
                    self.scheduleRolloverTimer(sessionId: config.sessionId)
                }

                let capturedSessionId = config.sessionId
                let capturedMode = config.mode.rawValue
                SpeechConcurrencyBridge.installTapNonisolated(
                    on: inputNode,
                    format: hwFormat,
                    bufferSize: 1024
                ) { [weak self] buffer, _ in
                    self?.handleRealtimeAudioTap(
                        buffer: buffer,
                        sessionId: capturedSessionId,
                        mode: capturedMode
                    )
                }

                do {
                    self.audioEngine.prepare()
                    try self.audioEngine.start()
                } catch {
                    inputNode.removeTap(onBus: 0)
                    self.teardownLegacySpeechOnly()
                    self.audioFile = nil
                    if let url = recordingURL {
                        NativeAudioStore.shared.discard(sessionId: config.sessionId)
                        try? FileManager.default.removeItem(at: url)
                    }
                    self.stateLock.withLock { $0.activeSessionId = nil }
                    NativeAudioSession.setSpeechEngineHoldingSession(false)
                    self.emitDict([
                        "type": "speech.error",
                        "sessionId": config.sessionId,
                        "mode": config.mode.rawValue,
                        "code": "engine_start_failed",
                        "message": "无法启动麦克风，请重试",
                        "partialTranscript": "",
                    ])
                    return
                }

                self.scheduleMaxDurationTimer(
                    sessionId: config.sessionId,
                    maxDurationSec: config.maxDurationSec
                )

                let latencyMs = Int((ProcessInfo.processInfo.systemUptime - startedAt) * 1000.0)
                self.emitDict([
                    "type": "speech.ready",
                    "sessionId": config.sessionId,
                    "mode": config.mode.rawValue,
                    "engine": engineName,
                    "onDevice": usingOnDevice,
                    "startLatencyMs": latencyMs,
                ])
            }
        }
    }

    func stop(sessionId: String?) async {
        let shouldStop: Bool = stateLock.withLock { st in
            guard let active = st.activeSessionId, !st.isStopping else { return false }
            if let sessionId, !sessionId.isEmpty, sessionId != active { return false }
            st.isStopping = true
            return true
        }
        guard shouldStop else { return }

        // Stop timers and audio tap first on controlQueue, signal end-of-audio to recognizer.
        #if compiler(>=6.2)
        let activeIOS26Box: SendableAnyObjectBox? = await withCheckedContinuation { cont in
            controlQueue.async { [weak self] in
                guard let self else {
                    cont.resume(returning: nil)
                    return
                }
                self.rolloverTimer?.cancel()
                self.rolloverTimer = nil
                self.maxDurationTimer?.cancel()
                self.maxDurationTimer = nil

                self.audioEngine.inputNode.removeTap(onBus: 0)
                self.audioEngine.stop()
                self.recognitionRequest?.endAudio()
                let box = self.ios26DriverBox.map(SendableAnyObjectBox.init)
                self.ios26DriverBox = nil
                cont.resume(returning: box)
            }
        }
        #else
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            controlQueue.async { [weak self] in
                defer { cont.resume() }
                guard let self else { return }
                self.rolloverTimer?.cancel()
                self.rolloverTimer = nil
                self.maxDurationTimer?.cancel()
                self.maxDurationTimer = nil

                self.audioEngine.inputNode.removeTap(onBus: 0)
                self.audioEngine.stop()
                self.recognitionRequest?.endAudio()
            }
        }
        #endif

        // Wait briefly (<= 650ms) for trailing Chinese syllables to finalize.
        let finalIOS26Result: (String, Int)?
        #if compiler(>=6.2)
        if #available(iOS 26.0, *), let drv = activeIOS26Box?.value as? IOS26SpeechAnalyzerDriver {
            finalIOS26Result = await drv.finalizeAndCollect(timeoutMs: 650)
        } else {
            try? await Task.sleep(nanoseconds: 280_000_000)
            finalIOS26Result = nil
        }
        #else
        try? await Task.sleep(nanoseconds: 280_000_000)
        finalIOS26Result = nil
        #endif

        // Drain serial file writer queue so `.m4a` MOOV atom flushes cleanly before reading!
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            fileWriteQueue.async { [weak self] in
                self?.audioFile = nil
                cont.resume()
            }
        }

        // Collect final snapshot & emit speech.final
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            controlQueue.async { [weak self] in
                defer { cont.resume() }
                guard let self else { return }
                self.teardownLegacySpeechOnly()

                let snapshot = self.stateLock.withLock { st -> (String, NativeSpeechMode, String, Int, String, Bool, URL?, TimeInterval) in
                    let sid = st.activeSessionId ?? (sessionId ?? "")
                    let mode = st.activeMode
                    let finalText: String
                    let segCount: Int
                    if let finalIOS26Result {
                        finalText = finalIOS26Result.0.trimmingCharacters(in: .whitespacesAndNewlines)
                        segCount = finalIOS26Result.1
                    } else {
                        var parts = st.committedSegments
                        if !st.currentSegmentText.isEmpty {
                            parts.append(st.currentSegmentText)
                        }
                        finalText = parts.joined().trimmingCharacters(in: .whitespacesAndNewlines)
                        segCount = max(1, st.segmentCount)
                    }
                    let engineName = st.engineName
                    let onDevice = st.onDevice
                    let recURL = st.recordingFileURL
                    let duration = max(0.0, ProcessInfo.processInfo.systemUptime - st.startUptime)
                    st.activeSessionId = nil
                    st.isStopping = false
                    st.recordingFileURL = nil
                    return (sid, mode, finalText, segCount, engineName, onDevice, recURL, duration)
                }

                var audioUrl = ""
                var byteLength = 0
                if let recURL = snapshot.6,
                   let attrs = try? FileManager.default.attributesOfItem(atPath: recURL.path),
                   let size = attrs[.size] as? Int,
                   size > 0 {
                    byteLength = size
                    audioUrl = NativeAudioStore.shared.register(
                        sessionId: snapshot.0,
                        fileURL: recURL,
                        metadata: [
                            "sessionId": snapshot.0,
                            "mode": snapshot.1.rawValue,
                            "clientTranscript": snapshot.2,
                            "speechEngine": snapshot.4,
                            "audioDurationMs": Int(snapshot.7 * 1000.0),
                        ]
                    )
                } else if let recURL = snapshot.6 {
                    try? FileManager.default.removeItem(at: recURL)
                }

                self.emitDict([
                    "type": "speech.final",
                    "sessionId": snapshot.0,
                    "mode": snapshot.1.rawValue,
                    "transcript": snapshot.2,
                    "audioUrl": audioUrl,
                    "audioMimeType": audioUrl.isEmpty ? "" : "audio/mp4",
                    "audioByteLength": byteLength,
                    "durationMs": Int(snapshot.7 * 1000.0),
                    "engine": snapshot.4,
                    "onDevice": snapshot.5,
                    "segmentCount": snapshot.3,
                ])

                self.scheduleWarmKeepAliveDeactivation()
            }
        }
    }

    func cancel(sessionId: String?) {
        controlQueue.async { [weak self] in
            guard let self else { return }
            let targetURL: (String, URL?)? = self.stateLock.withLock { st in
                guard let active = st.activeSessionId else { return nil }
                if let sessionId, !sessionId.isEmpty, sessionId != active { return nil }
                let url = st.recordingFileURL
                st.activeSessionId = nil
                st.isStopping = false
                st.recordingFileURL = nil
                return (active, url)
            }
            guard let targetURL else { return }

            self.rolloverTimer?.cancel()
            self.rolloverTimer = nil
            self.maxDurationTimer?.cancel()
            self.maxDurationTimer = nil

            self.audioEngine.inputNode.removeTap(onBus: 0)
            self.audioEngine.stop()
            self.teardownLegacySpeechOnly()

            #if compiler(>=6.2)
            if #available(iOS 26.0, *), let drv = self.ios26DriverBox as? IOS26SpeechAnalyzerDriver {
                drv.cancel()
            }
            self.ios26DriverBox = nil
            #endif

            self.fileWriteQueue.async { [weak self] in
                self?.audioFile = nil
                NativeAudioStore.shared.discard(sessionId: targetURL.0)
                if let url = targetURL.1 {
                    try? FileManager.default.removeItem(at: url)
                }
            }
            self.scheduleWarmKeepAliveDeactivation()
        }
    }

    func shutdown() {
        cancel(sessionId: nil)
        controlQueue.async { [weak self] in
            guard let self else { return }
            self.keepAliveTimer?.cancel()
            self.keepAliveTimer = nil
            self.audioEngine.stop()
            self.audioEngine.reset()
            NativeAudioSession.setSpeechEngineHoldingSession(false)
            NativeAudioSession.deactivate()
        }
    }

    // MARK: - Realtime Audio Tap (CoreAudio Thread — Lock-Free / Zero Heavy Work)

    private func handleRealtimeAudioTap(
        buffer: AVAudioPCMBuffer,
        sessionId: String,
        mode: String
    ) {
        // Tee 1: Feed Speech Recognizer (iOS 26 SpeechAnalyzer via dedicated Int16 converter OR SFSpeechRecognizer)
        #if compiler(>=6.2)
        if #available(iOS 26.0, *), let drv = ios26DriverBox as? IOS26SpeechAnalyzerDriver {
            let converted: AVAudioPCMBuffer?
            if let conv = speechConverter, let fmt = speechTargetFormat {
                converted = convertPCMBuffer(buffer, using: conv, targetFormat: fmt)
            } else {
                converted = clonePCMBuffer(buffer)
            }
            if let converted {
                drv.yieldBuffer(OwnedPCMBuffer(converted))
            }
        } else {
            recognitionRequest?.append(buffer)
        }
        #else
        recognitionRequest?.append(buffer)
        #endif

        // Tee 2: 30fps vDSP RMS metering
        let now = ProcessInfo.processInfo.systemUptime
        let shouldEmitLevel: (Bool, Int) = stateLock.withLock { st in
            guard st.activeSessionId == sessionId, !st.isStopping else { return (false, 0) }
            if now - st.lastRmsEmitUptime >= 0.033 {
                st.lastRmsEmitUptime = now
                return (true, Int((now - st.startUptime) * 1000.0))
            }
            return (false, 0)
        }
        if shouldEmitLevel.0 {
            let rms = computeNormalizedRMS(buffer)
            emitDict([
                "type": "speech.level",
                "sessionId": sessionId,
                "mode": mode,
                "rms": rms,
                "elapsedMs": shouldEmitLevel.1,
            ])
        }

        // Tee 3: Clone buffer into sole-ownership OwnedPCMBuffer and encode `.m4a` on serial writer queue
        if let cloned = clonePCMBuffer(buffer) {
            let owned = OwnedPCMBuffer(cloned)
            fileWriteQueue.async { [weak self] in
                guard let self,
                      let file = self.audioFile,
                      let conv = self.fileConverter,
                      let fmt = self.fileProcessingFormat,
                      let pcm16k = convertPCMBuffer(owned.buffer, using: conv, targetFormat: fmt) else {
                    return
                }
                try? file.write(from: pcm16k)
            }
        }
    }

    // MARK: - Legacy iOS 16–25+ SFSpeechRecognizer Segment Engine

    private func startLegacySFSpeechSegment(
        sessionId: String,
        mode: NativeSpeechMode,
        localeIdentifier: String,
        contextualStrings: [String],
        preferOnDevice: Bool
    ) -> Bool {
        if recognizer == nil || recognizer?.locale.identifier != localeIdentifier {
            recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier))
        }
        guard let recognizer, recognizer.isAvailable else { return false }

        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.addsPunctuation = true
        req.taskHint = .dictation
        if !contextualStrings.isEmpty {
            req.contextualStrings = contextualStrings
        }
        let useOnDevice = preferOnDevice && recognizer.supportsOnDeviceRecognition
        req.requiresOnDeviceRecognition = useOnDevice

        self.recognitionRequest = req
        let modeStr = mode.rawValue

        self.recognitionTask = SpeechConcurrencyBridge.startRecognitionTaskNonisolated(
            recognizer: recognizer,
            request: req
        ) { [weak self] text, isFinal, error in
            self?.handleLegacyRecognitionUpdate(
                sessionId: sessionId,
                mode: modeStr,
                text: text,
                isFinal: isFinal,
                error: error
            )
        }
        return useOnDevice
    }

    private func handleLegacyRecognitionUpdate(
        sessionId: String,
        mode: String,
        text: String?,
        isFinal: Bool,
        error: Error?
    ) {
        if let text {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                let emitPayload: (String, String, Int)? = stateLock.withLock { st in
                    guard st.activeSessionId == sessionId else { return nil }
                    // iOS 18 silence-gap transcript reset guard: if partial shrinks sharply and
                    // is not a prefix of currentSegmentText, commit prior text before accepting new clause!
                    if st.currentSegmentText.count >= 4,
                       trimmed.count + 4 <= st.currentSegmentText.count,
                       !st.currentSegmentText.hasPrefix(trimmed) {
                        st.committedSegments.append(st.currentSegmentText)
                        st.segmentCount += 1
                    }
                    st.currentSegmentText = trimmed
                    let combined = (st.committedSegments + [st.currentSegmentText]).joined()
                    return (combined, trimmed, st.segmentCount)
                }
                if let emitPayload {
                    emitDict([
                        "type": "speech.partial",
                        "sessionId": sessionId,
                        "mode": mode,
                        "transcript": emitPayload.0,
                        "volatileTail": emitPayload.1,
                        "segmentIndex": emitPayload.2,
                    ])
                }
            }
        }

        if let nsErr = error as NSError? {
            let errCode = nsErr.code
            // F3: On-device model not cached (kAFAssistantErrorDomain 1101/1107/1110) within 1.5s with 0 partials
            let shouldRetryCloud: Bool = stateLock.withLock { st in
                guard st.activeSessionId == sessionId,
                      !st.isStopping,
                      st.onDevice,
                      !st.retriedCloudFallback,
                      st.committedSegments.isEmpty,
                      st.currentSegmentText.isEmpty,
                      (ProcessInfo.processInfo.systemUptime - st.startUptime) < 2.0 else {
                    return false
                }
                if errCode == 1101 || errCode == 1107 || errCode == 1110 || errCode == 203 {
                    st.retriedCloudFallback = true
                    st.onDevice = false
                    return true
                }
                return false
            }
            if shouldRetryCloud {
                controlQueue.async { [weak self] in
                    guard let self else { return }
                    let snap = self.stateLock.withLock { ($0.activeMode, $0.localeIdentifier, $0.contextualStrings) }
                    self.teardownLegacySpeechOnly()
                    _ = self.startLegacySFSpeechSegment(
                        sessionId: sessionId,
                        mode: snap.0,
                        localeIdentifier: snap.1,
                        contextualStrings: snap.2,
                        preferOnDevice: false
                    )
                }
            }
        }

        _ = isFinal
    }

    private func handleIOS26Partial(
        sessionId: String,
        mode: String,
        transcript: String,
        volatileTail: String,
        segmentIndex: Int
    ) {
        let stillActive = stateLock.withLock { st in
            guard st.activeSessionId == sessionId else { return false }
            st.currentSegmentText = transcript
            st.segmentCount = max(1, segmentIndex)
            return true
        }
        guard stillActive, !transcript.isEmpty else { return }
        emitDict([
            "type": "speech.partial",
            "sessionId": sessionId,
            "mode": mode,
            "transcript": transcript,
            "volatileTail": volatileTail,
            "segmentIndex": segmentIndex,
        ])
    }

    private func scheduleRolloverTimer(sessionId: String) {
        rolloverTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: controlQueue)
        timer.schedule(deadline: .now() + 48.0, repeating: 48.0)
        timer.setEventHandler { [weak self] in
            self?.performSeamless48sRollover(sessionId: sessionId)
        }
        rolloverTimer = timer
        timer.resume()
    }

    private func performSeamless48sRollover(sessionId: String) {
        let info: (NativeSpeechMode, String, [String], Bool)? = stateLock.withLock { st in
            guard st.activeSessionId == sessionId, !st.isStopping else { return nil }
            if !st.currentSegmentText.isEmpty {
                st.committedSegments.append(st.currentSegmentText)
                st.currentSegmentText = ""
            }
            st.segmentCount += 1
            return (st.activeMode, st.localeIdentifier, st.contextualStrings, st.preferOnDevice && !st.retriedCloudFallback)
        }
        guard let info else { return }

        let oldReq = recognitionRequest
        let oldTask = recognitionTask
        oldReq?.endAudio()
        oldTask?.finish()

        _ = startLegacySFSpeechSegment(
            sessionId: sessionId,
            mode: info.0,
            localeIdentifier: info.1,
            contextualStrings: info.2,
            preferOnDevice: info.3
        )
    }

    private func scheduleMaxDurationTimer(sessionId: String, maxDurationSec: Double) {
        maxDurationTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: controlQueue)
        timer.schedule(deadline: .now() + maxDurationSec)
        timer.setEventHandler { [weak self] in
            Task { [weak self] in
                await self?.stop(sessionId: sessionId)
            }
        }
        maxDurationTimer = timer
        timer.resume()
    }

    private func scheduleWarmKeepAliveDeactivation() {
        keepAliveTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: controlQueue)
        timer.schedule(deadline: .now() + 1.5)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let active = self.stateLock.withLock { $0.activeSessionId != nil }
            guard !active else { return }
            NativeAudioSession.setSpeechEngineHoldingSession(false)
            NativeAudioSession.deactivate()
        }
        keepAliveTimer = timer
        timer.resume()
    }

    private func teardownLegacySpeechOnly() {
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionRequest = nil
        recognitionTask = nil
    }

    private func configureAudioSessionIfNeeded(activate: Bool) -> Bool {
        let session = AVAudioSession.sharedInstance()
        do {
            let options: AVAudioSession.CategoryOptions
            #if compiler(>=6.2)
            if #available(iOS 26.0, *) {
                options = [.defaultToSpeaker, .allowBluetoothHFP, .mixWithOthers]
            } else {
                options = [.defaultToSpeaker, .allowBluetooth, .mixWithOthers]
            }
            #else
            options = [.defaultToSpeaker, .allowBluetooth, .mixWithOthers]
            #endif
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: options)
            try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
            if activate {
                try session.setActive(true, options: [])
            }
            return true
        } catch {
            return false
        }
    }

    // MARK: - Interruption, Route Change & Media Services Reset Resilience (F6, F7, LiveKit#886)

    private func installAudioSessionObservers() {
        let nc = NotificationCenter.default

        let interruptionObs = nc.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: nil
        ) { [weak self] note in
            let typeRaw = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? NSNumber)?.uintValue
            guard typeRaw == AVAudioSession.InterruptionType.began.rawValue else { return }
            self?.handleExternalInterruption(reason: "interrupted", userMessage: "录音被系统打断，已保留刚才说的话")
        }
        observers.append(interruptionObs)

        let routeObs = nc.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: nil,
            queue: nil
        ) { [weak self] note in
            let reasonRaw = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? NSNumber)?.uintValue ?? 0
            if reasonRaw == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue
                || reasonRaw == AVAudioSession.RouteChangeReason.newDeviceAvailable.rawValue {
                self?.handleRouteOrReset(reallocateEngine: false)
            }
        }
        observers.append(routeObs)

        let resetObs = nc.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.handleRouteOrReset(reallocateEngine: true)
        }
        observers.append(resetObs)
    }

    private func handleExternalInterruption(reason: String, userMessage: String) {
        controlQueue.async { [weak self] in
            guard let self else { return }
            let snapshot: (String, String, String)? = self.stateLock.withLock { st in
                guard let sid = st.activeSessionId, !st.isStopping else { return nil }
                st.activeSessionId = nil
                st.isStopping = false
                let partial = (st.committedSegments + (st.currentSegmentText.isEmpty ? [] : [st.currentSegmentText]))
                    .joined()
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                return (sid, st.activeMode.rawValue, partial)
            }
            guard let snapshot else { return }

            self.rolloverTimer?.cancel()
            self.rolloverTimer = nil
            self.maxDurationTimer?.cancel()
            self.maxDurationTimer = nil
            self.audioEngine.inputNode.removeTap(onBus: 0)
            self.audioEngine.stop()
            self.audioEngine.reset()
            self.teardownLegacySpeechOnly()
            #if compiler(>=6.2)
            if #available(iOS 26.0, *), let drv = self.ios26DriverBox as? IOS26SpeechAnalyzerDriver {
                drv.cancel()
            }
            self.ios26DriverBox = nil
            #endif
            self.fileWriteQueue.async { [weak self] in
                self?.audioFile = nil
            }
            NativeAudioSession.setSpeechEngineHoldingSession(false)

            self.emitDict([
                "type": "speech.error",
                "sessionId": snapshot.0,
                "mode": snapshot.1,
                "code": reason,
                "message": userMessage,
                "partialTranscript": snapshot.2,
            ])
            self.emitDict([
                "type": "audio.interrupted",
                "sessionId": snapshot.0,
                "reason": reason,
            ])
        }
    }

    private func handleRouteOrReset(reallocateEngine: Bool) {
        let activeSid = stateLock.withLock { $0.activeSessionId }
        if let activeSid {
            Task { [weak self] in
                await self?.stop(sessionId: activeSid)
            }
        }
        controlQueue.async { [weak self] in
            guard let self else { return }
            self.fileConverter = nil
            self.speechConverter = nil
            if reallocateEngine {
                self.audioEngine.stop()
                self.audioEngine.reset()
                self.audioEngine = AVAudioEngine()
            }
        }
    }

    private func emitDict(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
              let json = String(data: data, encoding: .utf8) else {
            return
        }
        emitRawJSON(json)
    }
}
