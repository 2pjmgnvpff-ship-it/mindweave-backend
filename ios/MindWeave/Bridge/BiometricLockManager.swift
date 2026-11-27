import Combine
import Foundation
import LocalAuthentication
import SwiftUI

/// Strictly `Sendable` result crossing from `LocalAuthentication` back to `@MainActor`.
enum BiometricEvalResult: Sendable {
    case authenticated
    case cancelled
    case unavailable(String)
    case failed(String)
}

/// Strictly `Sendable` capability snapshot bridged to Settings JS via `mwBiometrics`.
struct BiometricCapabilities: Sendable {
    let available: Bool
    let enabled: Bool
    let locked: Bool
    let biometryKind: String // "faceID" | "touchID" | "opticID" | "passcode" | "none"
    let labelCn: String
    let graceSeconds: Int    // 0 | 60 | 300
}

/// Swift 6 strict-concurrency-safe, iOS 16+ compatible Face ID / Touch ID / Passcode App Lock
/// & App-Switcher Porcelain Privacy Veil manager (User Locked C2).
///
/// Design invariants:
/// - Uses `ObservableObject` + `@Published` (NEVER iOS 17-only `@Observable` since deployment target is 16.0).
/// - Fresh `LAContext()` is instantiated ONLY inside `nonisolated static func evaluatePolicyAsync`
///   so non-`Sendable` `LAContext` never crosses actor boundaries.
/// - Anti-loop `scenePhase` FSM: `.inactive` toggles ONLY `isPrivacyShieldVisible` (blocking App
///   Switcher screenshots) without mutating `isLocked`; `.background` records `backgroundUptime`
///   and locks when returning after `graceSeconds` (`0`, `60`, or `300`).
@MainActor
final class BiometricLockManager: ObservableObject {
    static let storageKey = "app.mindweave.biometricLockEnabled"
    static let graceStorageKey = "app.mindweave.biometricLockGraceSeconds"
    private static let allowedGraceValues: Set<Int> = [0, 60, 300]

    @Published private(set) var isEnabled: Bool
    @Published private(set) var isLocked: Bool
    @Published private(set) var isPrivacyShieldVisible: Bool = false
    @Published private(set) var isAuthenticating: Bool = false
    @Published private(set) var graceSeconds: Int
    @Published private(set) var lastErrorMessage: String?

    private let userDefaults: UserDefaults
    private var userCancelledCurrentSession: Bool = false
    private var backgroundUptime: TimeInterval?
    private var suppressInactiveUntilUptime: TimeInterval = 0
    private var lastAuthTask: Task<Void, Never>?

    /// Cached biometry descriptor so SwiftUI `body` redraws never invoke fresh `LAContext` IPC.
    @Published private(set) var cachedBiometryKind: String = "passcode"
    @Published private(set) var cachedBiometryLabelCn: String = "设备密码"

    /// Callback consulted before locking so an active voice recording is never interrupted.
    var isVoiceRecordingActive: (() -> Bool)?

    /// Suppresses spurious App-Switcher / Face ID re-locks while presenting native iOS modals
    /// (Photo Picker, Quick Look, Share Sheet, or in-app Safari sheet).
    func suppressNextInactiveLock(forSeconds duration: TimeInterval = 4.0) {
        let target = ProcessInfo.processInfo.systemUptime + max(0.5, duration)
        if target > suppressInactiveUntilUptime {
            suppressInactiveUntilUptime = target
        }
    }

    /// Show opaque porcelain privacy veil whenever locked OR when iOS snapshots App Switcher.
    var shouldCoverContent: Bool {
        isLocked || isPrivacyShieldVisible
    }

    init(userDefaults: UserDefaults = .standard) {
        self.userDefaults = userDefaults
        let savedEnabled = userDefaults.bool(forKey: Self.storageKey)
        let rawGrace = (userDefaults.object(forKey: Self.graceStorageKey) as? Int) ?? 60
        self.isEnabled = savedEnabled
        self.isLocked = savedEnabled
        self.graceSeconds = Self.allowedGraceValues.contains(rawGrace) ? rawGrace : 60
        let initial = Self.queryBiometryInfo()
        self.cachedBiometryKind = initial.kind
        self.cachedBiometryLabelCn = initial.label
    }

    private static func queryBiometryInfo() -> (available: Bool, kind: String, label: String) {
        let context = LAContext()
        var authError: NSError?
        let canAuth = context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &authError)
        if context.biometryType == .faceID {
            return (canAuth, "faceID", "面容 ID")
        } else if context.biometryType == .touchID {
            return (canAuth, "touchID", "触控 ID")
        } else if #available(iOS 17.0, *), context.biometryType == .opticID {
            return (canAuth, "opticID", "视控 ID")
        } else {
            return (canAuth, canAuth ? "passcode" : "none", canAuth ? "设备密码" : "未开启设备锁")
        }
    }

    func capabilities() -> BiometricCapabilities {
        let info = Self.queryBiometryInfo()
        cachedBiometryKind = info.kind
        cachedBiometryLabelCn = info.label
        return BiometricCapabilities(
            available: info.available,
            enabled: isEnabled,
            locked: isLocked,
            biometryKind: info.kind,
            labelCn: info.label,
            graceSeconds: graceSeconds
        )
    }

    /// Updates background re-lock grace window (`0` immediate, `60` 1 min, `300` 5 min).
    @discardableResult
    func setGraceSeconds(_ seconds: Int) -> Int {
        let clamped = Self.allowedGraceValues.contains(seconds) ? seconds : 0
        graceSeconds = clamped
        userDefaults.set(clamped, forKey: Self.graceStorageKey)
        return clamped
    }

    /// Observe SwiftUI `@Environment(\.scenePhase)` here — immune to Face ID `.inactive` loops.
    func handleScenePhaseChange(_ phase: ScenePhase) {
        guard isEnabled else {
            isPrivacyShieldVisible = false
            isLocked = false
            backgroundUptime = nil
            return
        }

        let nowUptime = ProcessInfo.processInfo.systemUptime
        switch phase {
        case .background:
            // Record monotonic uptime for grace window evaluation; never interrupt active recording.
            lastAuthTask?.cancel()
            isAuthenticating = false
            userCancelledCurrentSession = false
            backgroundUptime = nowUptime
            isPrivacyShieldVisible = true
            if graceSeconds == 0
                && nowUptime > suppressInactiveUntilUptime
                && !(isVoiceRecordingActive?() ?? false) {
                isLocked = true
            }
        case .inactive:
            // Cover App Switcher snapshot immediately WITHOUT mutating `isLocked` (prevents Face ID loop!).
            if !isAuthenticating && nowUptime > suppressInactiveUntilUptime {
                isPrivacyShieldVisible = true
            }
        case .active:
            isPrivacyShieldVisible = false
            if let bgTime = backgroundUptime {
                let elapsed = nowUptime - bgTime
                backgroundUptime = nil
                if elapsed >= Double(graceSeconds)
                    && nowUptime > suppressInactiveUntilUptime
                    && !(isVoiceRecordingActive?() ?? false) {
                    isLocked = true
                }
            }
            if isLocked && !isAuthenticating && !userCancelledCurrentSession {
                triggerUnlock(userInitiated: false)
            }
        @unknown default:
            isPrivacyShieldVisible = true
        }
    }

    /// Trigger Face ID / Touch ID / Passcode unlock. `userInitiated: true` resets cancel suppression.
    func triggerUnlock(userInitiated: Bool) {
        guard isLocked, !isAuthenticating else { return }
        if userInitiated {
            userCancelledCurrentSession = false
            lastErrorMessage = nil
        }
        isAuthenticating = true
        lastAuthTask?.cancel()
        lastAuthTask = Task { @MainActor [weak self] in
            let result = await Self.evaluatePolicyAsync(reason: "验证身份以翻开你的心络手帐")
            guard let self, !Task.isCancelled else { return }
            self.isAuthenticating = false
            switch result {
            case .authenticated:
                NativeHaptics.shared.trigger("memorySeal")
                self.lastErrorMessage = nil
                self.userCancelledCurrentSession = false
                self.isLocked = false
            case .cancelled:
                self.userCancelledCurrentSession = true
                self.lastErrorMessage = nil
            case .unavailable(let msg), .failed(let msg):
                NativeHaptics.shared.trigger("warning")
                self.userCancelledCurrentSession = true
                self.lastErrorMessage = msg
            }
        }
    }

    /// Settings JS toggle (`mwBiometrics`): verifies owner identity BEFORE enabling or disabling lock.
    func setLockEnabled(_ enable: Bool) async -> (ok: Bool, enabled: Bool, error: String?) {
        if enable == isEnabled {
            return (true, isEnabled, nil)
        }
        let reason = enable ? "开启应用锁，保护你的私密手帐" : "验证身份以关闭应用锁"
        isAuthenticating = true
        let result = await Self.evaluatePolicyAsync(reason: reason)
        isAuthenticating = false
        switch result {
        case .authenticated:
            isEnabled = enable
            userDefaults.set(enable, forKey: Self.storageKey)
            if !enable {
                isLocked = false
                isPrivacyShieldVisible = false
                backgroundUptime = nil
            }
            NativeHaptics.shared.trigger("memorySeal")
            return (true, isEnabled, nil)
        case .cancelled:
            return (false, isEnabled, "已取消验证")
        case .unavailable(let msg), .failed(let msg):
            return (false, isEnabled, msg)
        }
    }

    /// Fresh non-isolated `LAContext` per evaluation: zero `Sendable` / actor-isolation violations in Swift 6!
    nonisolated private static func evaluatePolicyAsync(reason: String) async -> BiometricEvalResult {
        let context = LAContext()
        context.localizedCancelTitle = "稍后再看"
        var canErr: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &canErr) else {
            return .unavailable("请先在系统设置中开启面容 ID 或设备密码")
        }
        do {
            let ok = try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)
            return ok ? .authenticated : .failed("验证未通过，请轻点重试")
        } catch let error as LAError {
            switch error.code {
            case .userCancel, .systemCancel, .appCancel:
                return .cancelled
            case .passcodeNotSet, .biometryNotEnrolled, .biometryNotAvailable:
                return .unavailable("请先在系统设置中开启面容 ID 或设备密码")
            default:
                return .failed("暂时无法验证身份，请轻点重试")
            }
        } catch {
            return .failed("暂时无法验证身份，请轻点重试")
        }
    }
}
