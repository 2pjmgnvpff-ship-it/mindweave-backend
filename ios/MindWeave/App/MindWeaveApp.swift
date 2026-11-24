import Combine
import Foundation
import SwiftUI
import UIKit

/// Thread-safe lock-backed value container so configuration state can be read
/// safely from any isolation domain without Swift 6 concurrency violations.
final class LockedValue<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T

    init(_ value: T) {
        self.value = value
    }

    func get() -> T {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ newValue: T) {
        lock.lock()
        value = newValue
        lock.unlock()
    }
}

/// Persists and broadcasts the active MindWeave API backend URL and (in Debug builds only)
/// sheet presentation state. Release IPAs always use `MWDefaultAPIBaseURL` and purge any
/// stale UserDefaults server overrides so end users are never exposed to raw backend URLs.
@MainActor
final class ServerConfigStore: ObservableObject {
    static let storageKey = "app.mindweave.apiBaseURL"
    static let fallbackBaseURLString = "https://mindweave-backend-kou46qyjxa-de.a.run.app"
    private static let legacyPlaceholderURLString = "https://mindweave-backend.run.app"

    /// Build-time default URL from Info.plist (`MWDefaultAPIBaseURL`), falling back to Cloud Run.
    static var defaultBaseURLString: String {
        if let configured = Bundle.main.object(forInfoDictionaryKey: "MWDefaultAPIBaseURL") as? String,
           let normalized = normalizeURLString(configured) {
            return normalized
        }
        return fallbackBaseURLString
    }

    /// Validates and canonicalizes an HTTP(S) base URL string (strips whitespace and trailing slashes).
    static func normalizeURLString(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host,
              !host.isEmpty else {
            return nil
        }
        components.scheme = scheme
        while components.path.hasSuffix("/") {
            components.path.removeLast()
        }
        components.query = nil
        components.fragment = nil
        return components.url?.absoluteString
    }

    private let userDefaults: UserDefaults
    private let urlBox: LockedValue<URL>

    @Published var apiBaseURLString: String
    #if DEBUG
    @Published var showConfigSheet: Bool = false
    #endif
    @Published var reloadToken: UUID = UUID()

    /// Thread-safe snapshot of the current backend base URL for `MindWeaveSchemeHandler`.
    nonisolated var apiBaseURL: URL {
        urlBox.get()
    }

    init(userDefaults: UserDefaults = .standard) {
        self.userDefaults = userDefaults
        let resolvedString: String
        #if DEBUG
        let saved = userDefaults.string(forKey: Self.storageKey).flatMap(Self.normalizeURLString)
        if let saved, saved != Self.legacyPlaceholderURLString {
            resolvedString = saved
        } else {
            resolvedString = Self.defaultBaseURLString
            if saved == Self.legacyPlaceholderURLString {
                userDefaults.removeObject(forKey: Self.storageKey)
            }
        }
        #else
        // Release builds strictly lock to Info.plist MWDefaultAPIBaseURL and purge stale overrides.
        userDefaults.removeObject(forKey: Self.storageKey)
        resolvedString = Self.defaultBaseURLString
        #endif
        let resolvedURL = URL(string: resolvedString) ?? URL(string: Self.fallbackBaseURLString)!
        self.apiBaseURLString = resolvedString
        self.urlBox = LockedValue(resolvedURL)
    }

    /// Requests a clean reload of `mindweave://app/` without changing the URL.
    func triggerReload() {
        reloadToken = UUID()
    }

    #if DEBUG
    /// Updates and persists the backend base URL, then triggers a clean WebView reload.
    @discardableResult
    func updateBaseURL(_ raw: String) -> Bool {
        guard let normalized = Self.normalizeURLString(raw),
              let url = URL(string: normalized) else {
            return false
        }
        userDefaults.set(normalized, forKey: Self.storageKey)
        urlBox.set(url)
        apiBaseURLString = normalized
        reloadToken = UUID()
        return true
    }

    /// Restores the build-time default server URL and reloads the WebView.
    func resetToDefault() {
        let fallback = Self.defaultBaseURLString
        userDefaults.removeObject(forKey: Self.storageKey)
        if let url = URL(string: fallback) {
            urlBox.set(url)
        }
        apiBaseURLString = fallback
        reloadToken = UUID()
    }

    /// Handles `mindweave://server?url=...` and `mindweave://config` deep links in Debug builds only.
    func handleIncomingURL(_ url: URL) {
        guard url.scheme?.lowercased() == MindWeaveSchemeHandler.scheme else {
            return
        }
        let host = url.host?.lowercased() ?? ""
        let path = url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")).lowercased()

        if host == "config" || path == "config" {
            showConfigSheet = true
            return
        }

        guard host == "server" || path == "server" else {
            return
        }

        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let rawURL = components?.queryItems?.first(where: { $0.name == "url" })?.value
        let shouldOpenSheet = components?.queryItems?.first(where: { $0.name == "open" })?.value == "1"

        if let rawURL, updateBaseURL(rawURL) {
            NativeHaptics.shared.trigger("success")
            if shouldOpenSheet {
                showConfigSheet = true
            }
        } else {
            showConfigSheet = true
        }
    }
    #endif
}

extension Notification.Name {
    static let mindWeaveShortcutAction = Notification.Name("app.mindweave.shortcutAction")
}

/// Captures Home Screen Quick Action (`UIApplicationShortcutItem`) launches and taps,
/// forwarding them cleanly to `WebContainerView` as `shortcut.action` bridge events.
@MainActor
final class MindWeaveAppDelegate: NSObject, UIApplicationDelegate {
    private static let pendingShortcutBox = LockedValue<String?>(nil)

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        if let shortcut = options.shortcutItem {
            Self.dispatchShortcut(shortcut.type)
        }
        let config = UISceneConfiguration(name: nil, sessionRole: connectingSceneSession.role)
        config.delegateClass = MindWeaveSceneDelegate.self
        return config
    }

    static func dispatchShortcut(_ shortcutType: String) {
        pendingShortcutBox.set(shortcutType)
        NotificationCenter.default.post(
            name: .mindWeaveShortcutAction,
            object: nil,
            userInfo: ["shortcutType": shortcutType]
        )
    }

    static func consumePendingShortcut() -> String? {
        let value = pendingShortcutBox.get()
        pendingShortcutBox.set(nil)
        return value
    }
}

@MainActor
final class MindWeaveSceneDelegate: NSObject, UIWindowSceneDelegate {
    func windowScene(
        _ windowScene: UIWindowScene,
        performActionFor shortcutItem: UIApplicationShortcutItem,
        completionHandler: @escaping (Bool) -> Void
    ) {
        MindWeaveAppDelegate.dispatchShortcut(shortcutItem.type)
        completionHandler(true)
    }
}

@main
struct MindWeaveApp: App {
    @UIApplicationDelegateAdaptor(MindWeaveAppDelegate.self) private var appDelegate
    @StateObject private var config = ServerConfigStore()
    @StateObject private var biometricLock = BiometricLockManager()
    @Environment(\.scenePhase) private var scenePhase

    /// Warm porcelain backdrop (`#FCFBF9`) matching MindWeave V2's seamless journal paper.
    private static let porcelainBackdrop = Color(red: 0.988, green: 0.984, blue: 0.976)

    var body: some Scene {
        WindowGroup {
            ZStack {
                Self.porcelainBackdrop
                    .ignoresSafeArea()

                WebContainerView(config: config, biometricLock: biometricLock)
                    .ignoresSafeArea()

                if biometricLock.shouldCoverContent {
                    PorcelainLockCurtainView(lockManager: biometricLock)
                        .ignoresSafeArea()
                        .transition(.opacity)
                        .zIndex(100)
                }
            }
            .preferredColorScheme(.light)
            .animation(.easeInOut(duration: 0.16), value: biometricLock.shouldCoverContent)
            // Single-parameter `.onChange` keeps 100% iOS 16.0 deployment-target compatibility!
            .onChange(of: scenePhase) { newPhase in
                biometricLock.handleScenePhaseChange(newPhase)
            }
            .task {
                if biometricLock.isLocked {
                    biometricLock.triggerUnlock(userInitiated: false)
                }
            }
            #if DEBUG
            .sheet(isPresented: $config.showConfigSheet) {
                ServerConfigSheet(config: config)
            }
            .onOpenURL { url in
                config.handleIncomingURL(url)
            }
            #endif
        }
    }
}
