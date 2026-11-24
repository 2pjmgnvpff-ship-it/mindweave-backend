import AVFoundation
import CoreHaptics
import Foundation
import LinkPresentation
import os
import UIKit

/// Low-latency native CoreHaptics + UIKit tactile engine bridged from `MWNative` / `mwHaptic`.
///
/// Architectural guarantees (`02g` & `01m`):
/// - Single lazy `CHHapticEngine` with `playsHapticsOnly = true` (zero audio-bus contention with
///   `NativeSpeechEngine` / `AVAudioSession`, preventing `capacitor-plugins #2340` daemon exhaustion).
/// - Registers `resetHandler` and `stoppedHandler` for automatic recovery after audio interrupts,
///   Face ID sheets, or background suspension.
/// - All active-recording pulses stay crisp `<= 15ms` transients (`voiceArm`, `voiceCancelCliff`,
///   `voiceRelease`) so microphone ASR is never disrupted by continuous rumble.
@MainActor
final class NativeHaptics {
    static let shared = NativeHaptics()

    private let lightImpact = UIImpactFeedbackGenerator(style: .light)
    private let mediumImpact = UIImpactFeedbackGenerator(style: .medium)
    private let heavyImpact = UIImpactFeedbackGenerator(style: .heavy)
    private let softImpact = UIImpactFeedbackGenerator(style: .soft)
    private let rigidImpact = UIImpactFeedbackGenerator(style: .rigid)
    private let selectionGenerator = UISelectionFeedbackGenerator()
    private let notificationGenerator = UINotificationFeedbackGenerator()

    private let supportsCoreHaptics: Bool
    private var hapticEngine: CHHapticEngine?
    private var engineRunning: Bool = false

    private init() {
        self.supportsCoreHaptics = CHHapticEngine.capabilitiesForHardware().supportsHaptics
    }

    private func ensureEngineStarted() -> CHHapticEngine? {
        guard supportsCoreHaptics else { return nil }

        if hapticEngine == nil {
            do {
                let engine = try CHHapticEngine()
                engine.playsHapticsOnly = true
                engine.isAutoShutdownEnabled = true
                engine.resetHandler = { [weak self] in
                    Task { @MainActor [weak self] in
                        guard let self else { return }
                        self.engineRunning = false
                        try? self.hapticEngine?.start()
                        self.engineRunning = true
                    }
                }
                engine.stoppedHandler = { [weak self] _ in
                    Task { @MainActor [weak self] in
                        self?.engineRunning = false
                    }
                }
                hapticEngine = engine
            } catch {
                return nil
            }
        }

        guard let engine = hapticEngine else { return nil }
        if !engineRunning {
            do {
                try engine.start()
                engineRunning = true
            } catch {
                engineRunning = false
                return nil
            }
        }
        return engine
    }

    private func playCoreHapticEvents(_ events: [CHHapticEvent]) -> Bool {
        guard let engine = ensureEngineStarted() else { return false }
        do {
            let pattern = try CHHapticPattern(events: events, parameters: [])
            let player = try engine.makePlayer(withPattern: pattern)
            try player.start(atTime: CHHapticTimeImmediate)
            return true
        } catch {
            engineRunning = false
            return false
        }
    }

    private func makeTransient(
        at time: TimeInterval = 0,
        intensity: Float,
        sharpness: Float
    ) -> CHHapticEvent {
        CHHapticEvent(
            eventType: .hapticTransient,
            parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: intensity),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: sharpness),
            ],
            relativeTime: time
        )
    }

    /// Primes the Taptic Engine right before an interactive gesture (e.g., `pointerdown` on hold-to-speak or spine scrub).
    func prepare(_ style: String) {
        let key = style.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch key {
        case "porcelaintick", "yeardetent", "voicearm", "voicebloom", "voicecancelcliff",
             "voicerelease", "memoryseal", "memorysealsilver", "gentlewarn":
            _ = ensureEngineStarted()
            selectionGenerator.prepare()
            softImpact.prepare()
            mediumImpact.prepare()
        case "light":
            lightImpact.prepare()
        case "medium":
            mediumImpact.prepare()
        case "heavy":
            heavyImpact.prepare()
        case "soft":
            softImpact.prepare()
        case "rigid":
            rigidImpact.prepare()
        case "selection", "tick":
            selectionGenerator.prepare()
        case "success", "warning", "error":
            notificationGenerator.prepare()
        default:
            lightImpact.prepare()
        }
    }

    /// Fires a native iOS tactile pattern matching MindWeave's semantic Gleb porcelain haptic palette.
    func trigger(_ style: String, intensity: CGFloat? = nil) {
        let clampedIntensity = intensity.map { max(0.0, min(1.0, $0)) }
        let key = style.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        switch key {
        case "porcelaintick":
            let amp = Float(clampedIntensity ?? 0.36)
            if !playCoreHapticEvents([makeTransient(intensity: amp, sharpness: 0.72)]) {
                selectionGenerator.prepare()
                selectionGenerator.selectionChanged()
            }
        case "yeardetent":
            let amp = Float(clampedIntensity ?? 0.68)
            if !playCoreHapticEvents([makeTransient(intensity: amp, sharpness: 0.72)]) {
                lightImpact.prepare()
                lightImpact.impactOccurred(intensity: 0.75)
            }
        case "voicearm", "voicebloom":
            // Strictly crisp <= 15ms transient before speech onset (zero mic acoustic interference!)
            let amp = Float(clampedIntensity ?? 0.58)
            if !playCoreHapticEvents([makeTransient(intensity: amp, sharpness: 0.40)]) {
                mediumImpact.prepare()
                mediumImpact.impactOccurred(intensity: 0.65)
            }
        case "voicecancelcliff":
            let events = [
                makeTransient(at: 0.0, intensity: 0.55, sharpness: 0.80),
                makeTransient(at: 0.055, intensity: 0.45, sharpness: 0.85),
            ]
            if !playCoreHapticEvents(events) {
                notificationGenerator.prepare()
                notificationGenerator.notificationOccurred(.warning)
            }
        case "voicerelease":
            let amp = Float(clampedIntensity ?? 0.46)
            if !playCoreHapticEvents([makeTransient(intensity: amp, sharpness: 0.25)]) {
                softImpact.prepare()
                softImpact.impactOccurred(intensity: 0.55)
            }
        case "memoryseal":
            if UIAccessibility.isReduceMotionEnabled {
                softImpact.prepare()
                softImpact.impactOccurred(intensity: 0.48)
                return
            }
            let events = [
                makeTransient(at: 0.0, intensity: 0.42, sharpness: 0.28),
                makeTransient(at: 0.085, intensity: 0.68, sharpness: 0.62),
            ]
            if !playCoreHapticEvents(events) {
                notificationGenerator.prepare()
                notificationGenerator.notificationOccurred(.success)
            }
        case "memorysealsilver":
            // Factual archival stamp for Silver tier (crisp single transient, zero warm bloom)
            if !playCoreHapticEvents([makeTransient(intensity: 0.42, sharpness: 0.65)]) {
                rigidImpact.prepare()
                rigidImpact.impactOccurred(intensity: 0.55)
            }
        case "gentlewarn":
            let events = [
                makeTransient(at: 0.0, intensity: 0.40, sharpness: 0.30),
                makeTransient(at: 0.11, intensity: 0.55, sharpness: 0.35),
            ]
            if !playCoreHapticEvents(events) {
                notificationGenerator.prepare()
                notificationGenerator.notificationOccurred(.warning)
            }
        case "light":
            lightImpact.prepare()
            if let clampedIntensity {
                lightImpact.impactOccurred(intensity: clampedIntensity)
            } else {
                lightImpact.impactOccurred()
            }
        case "medium":
            mediumImpact.prepare()
            if let clampedIntensity {
                mediumImpact.impactOccurred(intensity: clampedIntensity)
            } else {
                mediumImpact.impactOccurred()
            }
        case "heavy":
            heavyImpact.prepare()
            if let clampedIntensity {
                heavyImpact.impactOccurred(intensity: clampedIntensity)
            } else {
                heavyImpact.impactOccurred()
            }
        case "soft":
            softImpact.prepare()
            if let clampedIntensity {
                softImpact.impactOccurred(intensity: clampedIntensity)
            } else {
                softImpact.impactOccurred()
            }
        case "rigid":
            rigidImpact.prepare()
            if let clampedIntensity {
                rigidImpact.impactOccurred(intensity: clampedIntensity)
            } else {
                rigidImpact.impactOccurred()
            }
        case "selection", "tick":
            selectionGenerator.prepare()
            selectionGenerator.selectionChanged()
        case "success":
            notificationGenerator.prepare()
            notificationGenerator.notificationOccurred(.success)
        case "warning":
            notificationGenerator.prepare()
            notificationGenerator.notificationOccurred(.warning)
        case "error":
            notificationGenerator.prepare()
            notificationGenerator.notificationOccurred(.error)
        default:
            lightImpact.prepare()
            lightImpact.impactOccurred()
        }
    }
}

/// Manages `AVAudioSession` routing off the main thread so WebKit capture and `.m4a` playback
/// route through the full-volume main speaker (or Bluetooth headset) without blocking UI frames
/// or disrupting an active/warm `NativeSpeechEngine` session.
enum NativeAudioSession {
    private static let sessionQueue = DispatchQueue(
        label: "app.mindweave.audio.session",
        qos: .userInitiated
    )
    private static let holdingSpeechSession = OSAllocatedUnfairLock<Bool>(initialState: false)

    static func setSpeechEngineHoldingSession(_ holding: Bool) {
        holdingSpeechSession.withLock { $0 = holding }
    }

    static var isSpeechEngineHoldingSession: Bool {
        holdingSpeechSession.withLock { $0 }
    }

    static func activateForVoiceAndPlayback() {
        sessionQueue.async {
            if isSpeechEngineHoldingSession {
                return
            }
            let session = AVAudioSession.sharedInstance()
            do {
                #if compiler(>=6.2)
                let options: AVAudioSession.CategoryOptions = [.defaultToSpeaker, .allowBluetoothHFP, .mixWithOthers]
                #else
                let options: AVAudioSession.CategoryOptions = [.defaultToSpeaker, .allowBluetooth, .mixWithOthers]
                #endif
                try session.setCategory(
                    .playAndRecord,
                    mode: .default,
                    options: options
                )
                try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
                try session.setActive(true, options: [])
            } catch {
                // Non-fatal on Simulator or restricted audio hardware; WebKit continues gracefully.
            }
        }
    }

    static func deactivate() {
        sessionQueue.async {
            if isSpeechEngineHoldingSession {
                // Defense-in-depth: never tear down AVAudioSession while NativeSpeechEngine is active or warm.
                return
            }
            let session = AVAudioSession.sharedInstance()
            do {
                try session.setActive(false, options: .notifyOthersOnDeactivation)
            } catch {
                // Ignore deactivation errors if another audio graph is still active.
            }
        }
    }

    static func handleCommand(_ command: String) {
        switch command.lowercased() {
        case "deactivate", "stop", "idle", "off":
            deactivate()
        default:
            activateForVoiceAndPlayback()
        }
    }
}

/// Resolves the active foreground key window's top-most presented `UIViewController`
/// using modern iOS 16+ `UIWindowScene` APIs.
@MainActor
func topMostViewController() -> UIViewController? {
    let windowScenes = UIApplication.shared.connectedScenes
        .compactMap { $0 as? UIWindowScene }
    let activeScene = windowScenes.first(where: { $0.activationState == .foregroundActive })
        ?? windowScenes.first(where: { $0.activationState == .foregroundInactive })
        ?? windowScenes.first
    let keyWindow = activeScene?.windows.first(where: { $0.isKeyWindow })
        ?? activeScene?.windows.first

    var current = keyWindow?.rootViewController
    while let presented = current?.presentedViewController {
        current = presented
    }
    return current
}

/// Rich `UIActivityItemSource` providing a porcelain `LPLinkMetadata` preview header
/// (Chinese title, file URL badge & app icon) for binary `.zip` / image / text exports.
/// Intentionally nonisolated: UIKit may invoke `UIActivityItemSource` methods off-main.
private final class MindWeaveShareItemSource: NSObject, UIActivityItemSource {
    private nonisolated(unsafe) let item: Any
    private let displayTitle: String
    private let fileURL: URL?
    private let appIconImage: UIImage?

    init(item: Any, displayTitle: String, fileURL: URL?) {
        self.item = item
        self.displayTitle = displayTitle
        self.fileURL = fileURL
        self.appIconImage = Self.loadAppIconImage()
        super.init()
    }

    func activityViewControllerPlaceholderItem(_ activityViewController: UIActivityViewController) -> Any {
        item
    }

    func activityViewController(
        _ activityViewController: UIActivityViewController,
        itemForActivityType activityType: UIActivity.ActivityType?
    ) -> Any? {
        item
    }

    func activityViewController(
        _ activityViewController: UIActivityViewController,
        subjectForActivityType activityType: UIActivity.ActivityType?
    ) -> String {
        displayTitle
    }

    func activityViewControllerLinkMetadata(
        _ activityViewController: UIActivityViewController
    ) -> LPLinkMetadata? {
        let metadata = LPLinkMetadata()
        metadata.title = displayTitle
        if let fileURL {
            metadata.originalURL = fileURL
            metadata.url = fileURL
        }
        if let appIcon = appIconImage {
            metadata.iconProvider = NSItemProvider(object: appIcon)
        }
        return metadata
    }

    private static func loadAppIconImage() -> UIImage? {
        if let icons = Bundle.main.infoDictionary?["CFBundleIcons"] as? [String: Any],
           let primary = icons["CFBundlePrimaryIcon"] as? [String: Any],
           let files = primary["CFBundleIconFiles"] as? [String],
           let last = files.last,
           let img = UIImage(named: last) {
            return img
        }
        return UIImage(systemName: "book.closed.fill")
    }
}

private func formattedBackupDateCn() -> String {
    let formatter = DateFormatter()
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "yyyy年MM月dd日"
    return formatter.string(from: Date())
}

/// Presents the native iOS Share Sheet (`UIActivityViewController`), supporting BOTH binary
/// `base64` payloads (e.g. Data-Rights `.zip` archives and PNG cards without UTF-8 corruption!)
/// and plain text/URL sharing, wrapped in rich `LPLinkMetadata` with temp-file cleanup and
/// C2 Face ID modal-grace protection.
@MainActor
func presentNativeShare(
    title: String?,
    text: String?,
    filename: String?,
    urlString: String? = nil,
    base64Data: String? = nil,
    mimeType: String? = nil,
    biometricLock: BiometricLockManager? = nil
) {
    guard let presenter = topMostViewController() else {
        return
    }

    let trimmedTitle = title?.trimmingCharacters(in: .whitespacesAndNewlines)
    let isArchive = (filename?.lowercased().hasSuffix(".zip") == true)
        || (mimeType?.lowercased().contains("zip") == true)
    let resolvedTitle: String
    if let trimmedTitle, !trimmedTitle.isEmpty {
        resolvedTitle = trimmedTitle
    } else if isArchive {
        resolvedTitle = "心络手帐备份 · \(formattedBackupDateCn())"
    } else {
        resolvedTitle = "心络手帐分享"
    }

    var activityItems: [Any] = []
    var tempDirURL: URL?

    let sanitizedFilename: String? = {
        guard let rawFilename = filename?.trimmingCharacters(in: .whitespacesAndNewlines),
              !rawFilename.isEmpty else {
            return nil
        }
        let cleaned = (rawFilename as NSString).lastPathComponent
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: ":", with: "_")
        if cleaned.isEmpty || cleaned == "." || cleaned == ".." {
            return isArchive ? "mindweave-export.zip" : "MindWeave-Export.txt"
        }
        return cleaned
    }()

    // 1. Binary base64 payload (fixes UTF-8 `.zip` / image corruption!)
    if let rawBase64 = base64Data?.trimmingCharacters(in: .whitespacesAndNewlines),
       !rawBase64.isEmpty {
        let strippedBase64: String
        if let commaIdx = rawBase64.firstIndex(of: ","), rawBase64.hasPrefix("data:") {
            strippedBase64 = String(rawBase64[rawBase64.index(after: commaIdx)...])
        } else {
            strippedBase64 = rawBase64
        }
        if let decoded = Data(base64Encoded: strippedBase64, options: .ignoreUnknownCharacters),
           !decoded.isEmpty {
            let safeName = sanitizedFilename ?? (isArchive ? "mindweave-export.zip" : "mindweave-share.bin")
            let tempDir = FileManager.default.temporaryDirectory
                .appendingPathComponent("mw-share-\(UUID().uuidString)", isDirectory: true)
            let fileURL = tempDir.appendingPathComponent(safeName)
            do {
                try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
                try decoded.write(to: fileURL, options: [.atomic])
                tempDirURL = tempDir
                activityItems.append(
                    MindWeaveShareItemSource(
                        item: fileURL,
                        displayTitle: resolvedTitle,
                        fileURL: fileURL
                    )
                )
            } catch {
                try? FileManager.default.removeItem(at: tempDir)
            }
        }
    }

    // 2. UTF-8 text file export (when `filename` + `text` supplied without binary base64)
    if activityItems.isEmpty, let safeName = sanitizedFilename, let text {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("mw-share-\(UUID().uuidString)", isDirectory: true)
        let fileURL = tempDir.appendingPathComponent(safeName)
        do {
            try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
            try text.write(to: fileURL, atomically: true, encoding: .utf8)
            tempDirURL = tempDir
            activityItems.append(
                MindWeaveShareItemSource(
                    item: fileURL,
                    displayTitle: resolvedTitle,
                    fileURL: fileURL
                )
            )
        } catch {
            try? FileManager.default.removeItem(at: tempDir)
            activityItems.append(
                MindWeaveShareItemSource(item: text, displayTitle: resolvedTitle, fileURL: nil)
            )
        }
    }

    // 3. Plain text / URL share
    if activityItems.isEmpty {
        if let text, !text.isEmpty {
            activityItems.append(
                MindWeaveShareItemSource(item: text, displayTitle: resolvedTitle, fileURL: nil)
            )
        }
        if let urlString, let url = URL(string: urlString) {
            activityItems.append(url)
        }
    }

    if activityItems.isEmpty {
        activityItems.append(resolvedTitle)
    }

    let activityController = UIActivityViewController(
        activityItems: activityItems,
        applicationActivities: nil
    )
    activityController.setValue(resolvedTitle, forKey: "subject")

    if isArchive {
        activityController.excludedActivityTypes = [
            .assignToContact,
            .addToReadingList,
            .postToFacebook,
            .postToTwitter,
        ]
    }

    let cleanupDirURL = tempDirURL
    activityController.completionWithItemsHandler = { [cleanupDirURL] _, _, _, _ in
        if let cleanupDirURL {
            try? FileManager.default.removeItem(at: cleanupDirURL)
        }
    }

    if let popover = activityController.popoverPresentationController {
        popover.sourceView = presenter.view
        popover.sourceRect = CGRect(
            x: presenter.view.bounds.midX,
            y: max(presenter.view.bounds.maxY - 64, presenter.view.bounds.midY),
            width: 1,
            height: 1
        )
        popover.permittedArrowDirections = []
    }

    biometricLock?.suppressNextInactiveLock(forSeconds: 8.0)
    presenter.present(activityController, animated: true)
}
