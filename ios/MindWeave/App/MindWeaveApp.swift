import Foundation
import SwiftUI

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

/// Persists and broadcasts the active MindWeave API backend URL and sheet presentation state.
@MainActor
final class ServerConfigStore: ObservableObject {
    static let storageKey = "app.mindweave.apiBaseURL"
    static let fallbackBaseURLString = "https://mindweave-backend.run.app"

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
    @Published var showConfigSheet: Bool = false
    @Published var reloadToken: UUID = UUID()

    /// Thread-safe snapshot of the current backend base URL for `MindWeaveSchemeHandler`.
    nonisolated var apiBaseURL: URL {
        urlBox.get()
    }

    init(userDefaults: UserDefaults = .standard) {
        self.userDefaults = userDefaults
        let saved = userDefaults.string(forKey: Self.storageKey)
        let resolvedString = saved.flatMap(Self.normalizeURLString) ?? Self.defaultBaseURLString
        let resolvedURL = URL(string: resolvedString) ?? URL(string: Self.fallbackBaseURLString)!
        self.apiBaseURLString = resolvedString
        self.urlBox = LockedValue(resolvedURL)
    }

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

    /// Requests a clean reload of `mindweave://app/` without changing the URL.
    func triggerReload() {
        reloadToken = UUID()
    }

    /// Handles `mindweave://server?url=...` and `mindweave://config` deep links.
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
}

@main
struct MindWeaveApp: App {
    @StateObject private var config = ServerConfigStore()

    /// Warm cotton-paper backdrop (`#F7EFE3`) matching MindWeave's default journal edition.
    private static let cottonPaper = Color(red: 0.969, green: 0.937, blue: 0.890)

    var body: some Scene {
        WindowGroup {
            ZStack {
                Self.cottonPaper
                    .ignoresSafeArea()

                WebContainerView(config: config)
                    .ignoresSafeArea()
            }
            .preferredColorScheme(.light)
            .sheet(isPresented: $config.showConfigSheet) {
                ServerConfigSheet(config: config)
            }
            .onOpenURL { url in
                config.handleIncomingURL(url)
            }
        }
    }
}
