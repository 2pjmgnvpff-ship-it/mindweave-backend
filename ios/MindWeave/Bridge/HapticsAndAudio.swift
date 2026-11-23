import AVFoundation
import Foundation
import UIKit

/// Low-latency native UIKit haptic engine bridged from `MWNative` / `mwHaptic`.
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

    private init() {}

    /// Primes the Taptic Engine right before an interactive gesture (e.g., hold-to-speak or scrub).
    func prepare(_ style: String) {
        switch style.lowercased() {
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

    /// Fires a native iOS haptic pulse matching the requested semantic style.
    func trigger(_ style: String, intensity: CGFloat? = nil) {
        let clampedIntensity = intensity.map { max(0.0, min(1.0, $0)) }
        switch style.lowercased() {
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

/// Manages `AVAudioSession` routing so WebKit `MediaRecorder` capture and WAV playback
/// route through the full-volume main speaker (or Bluetooth headset) instead of the earpiece.
enum NativeAudioSession {
    static func activateForVoiceAndPlayback() {
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
            try session.setActive(true, options: [])
        } catch {
            // Non-fatal on Simulator or restricted audio hardware; WebKit continues gracefully.
        }
    }

    static func deactivate() {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            // Ignore deactivation errors if another audio graph is still active.
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

/// Presents the native iOS Share Sheet (`UIActivityViewController`), optionally exporting
/// text content into a temporary file when `filename` is provided, with full iPad popover safety.
@MainActor
func presentNativeShare(
    title: String?,
    text: String?,
    filename: String?,
    urlString: String? = nil
) {
    guard let presenter = topMostViewController() else {
        return
    }

    var activityItems: [Any] = []
    var tempDirURL: URL?

    if let rawFilename = filename?.trimmingCharacters(in: .whitespacesAndNewlines),
       !rawFilename.isEmpty,
       let text {
        let sanitized = (rawFilename as NSString).lastPathComponent
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: ":", with: "_")
        let safeName = sanitized.isEmpty || sanitized == "." || sanitized == ".."
            ? "MindWeave-Export.txt"
            : sanitized
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let fileURL = tempDir.appendingPathComponent(safeName)
        do {
            try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
            try text.write(to: fileURL, atomically: true, encoding: .utf8)
            activityItems.append(fileURL)
            tempDirURL = tempDir
        } catch {
            try? FileManager.default.removeItem(at: tempDir)
            activityItems.append(text)
        }
    } else {
        if let text, !text.isEmpty {
            activityItems.append(text)
        }
        if let urlString, let url = URL(string: urlString) {
            activityItems.append(url)
        }
    }

    if activityItems.isEmpty, let title, !title.isEmpty {
        activityItems.append(title)
    }
    guard !activityItems.isEmpty else {
        return
    }

    let activityController = UIActivityViewController(
        activityItems: activityItems,
        applicationActivities: nil
    )

    if let title, !title.isEmpty {
        activityController.setValue(title, forKey: "subject")
    }

    if let tempDirURL {
        activityController.completionWithItemsHandler = { _, _, _, _ in
            try? FileManager.default.removeItem(at: tempDirURL)
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

    presenter.present(activityController, animated: true)
}
