import SwiftUI
import UIKit
import WebKit

/// Named JavaScript-to-Swift bridge channels matching `web/scripts/native-bridge.js`.
private enum BridgeChannel: String, CaseIterable {
    case haptic = "mwHaptic"
    case share = "mwShare"
    case clipboard = "mwClipboard"
    case audio = "mwAudio"
    case config = "mwConfig"
}

/// Weak wrapper preventing the `WKUserContentController -> WKScriptMessageHandler` retain cycle.
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: (any WKScriptMessageHandler)?

    init(delegate: any WKScriptMessageHandler) {
        self.delegate = delegate
        super.init()
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        delegate?.userContentController(userContentController, didReceive: message)
    }
}

/// Full-screen `WKWebView` container hosting the bundled MindWeave web experience
/// with native iOS haptics, audio session, Share Sheet, clipboard, media permissions,
/// and same-origin `mindweave://app/` API + SSE proxying.
@MainActor
struct WebContainerView: UIViewRepresentable {
    @ObservedObject var config: ServerConfigStore

    private static let cottonPaperUIColor = UIColor(
        red: 0.969,
        green: 0.937,
        blue: 0.890,
        alpha: 1.0
    )

    /// Encodes an arbitrary Swift string as a safe RFC 8259 JSON string literal for JS injection.
    static func jsonStringLiteral(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value], options: []),
              let encodedArray = String(data: data, encoding: .utf8),
              encodedArray.count >= 2 else {
            return "\"\""
        }
        return String(encodedArray.dropFirst().dropLast())
    }

    /// Document-start bootstrap script keeping all API requests same-origin on `mindweave://app/v1/*`
    /// while exposing the live configured backend URL in `window.__MW_SERVER_URL__`.
    static func bootstrapScriptSource(serverURL: String) -> String {
        let escapedServerURL = jsonStringLiteral(serverURL)
        return """
        try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {}
        window.__MW_NATIVE_IOS__ = true;
        window.__MW_IOS_NATIVE__ = true;
        window.__MW_API_BASE__ = "";
        window.__MW_SERVER_URL__ = \(escapedServerURL);
        """
    }

    private static func makeBootstrapUserScript(serverURL: String) -> WKUserScript {
        WKUserScript(
            source: bootstrapScriptSource(serverURL: serverURL),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(config: config)
    }

    func makeUIView(context: Context) -> WKWebView {
        let coordinator = context.coordinator
        let userContentController = WKUserContentController()
        userContentController.addUserScript(
            Self.makeBootstrapUserScript(serverURL: config.apiBaseURLString)
        )

        let weakBridge = WeakScriptMessageHandler(delegate: coordinator)
        for channel in BridgeChannel.allCases {
            userContentController.add(weakBridge, name: channel.rawValue)
        }

        let configuration = WKWebViewConfiguration()
        configuration.userContentController = userContentController
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.setURLSchemeHandler(
            coordinator.schemeHandler,
            forURLScheme: MindWeaveSchemeHandler.scheme
        )

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = coordinator
        webView.navigationDelegate = coordinator
        webView.isOpaque = false
        webView.backgroundColor = Self.cottonPaperUIColor
        webView.scrollView.backgroundColor = Self.cottonPaperUIColor
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.bounces = false
        webView.scrollView.alwaysBounceVertical = false
        webView.scrollView.alwaysBounceHorizontal = false
        webView.scrollView.showsVerticalScrollIndicator = false
        webView.scrollView.showsHorizontalScrollIndicator = false
        webView.allowsBackForwardNavigationGestures = false

        #if DEBUG
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        #endif

        // Unobtrusive 3-finger double-tap shortcut so testers can open ServerConfigSheet from any screen.
        let serverConfigTap = UITapGestureRecognizer(
            target: coordinator,
            action: #selector(Coordinator.handleServerConfigGesture)
        )
        serverConfigTap.numberOfTouchesRequired = 3
        serverConfigTap.numberOfTapsRequired = 2
        serverConfigTap.cancelsTouchesInView = false
        serverConfigTap.delegate = coordinator
        webView.addGestureRecognizer(serverConfigTap)

        coordinator.webView = webView
        webView.load(URLRequest(url: MindWeaveSchemeHandler.entryURL))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        let coordinator = context.coordinator

        if coordinator.lastServerURL != config.apiBaseURLString {
            coordinator.lastServerURL = config.apiBaseURLString
            let controller = uiView.configuration.userContentController
            controller.removeAllUserScripts()
            controller.addUserScript(
                Self.makeBootstrapUserScript(serverURL: config.apiBaseURLString)
            )
            coordinator.syncServerURLGlobal(in: uiView)
        }

        if coordinator.lastReloadToken != config.reloadToken {
            coordinator.lastReloadToken = config.reloadToken
            coordinator.schemeHandler.cancelAllTasks()
            uiView.load(URLRequest(url: MindWeaveSchemeHandler.entryURL))
        }
    }

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        coordinator.teardown(webView: uiView)
    }

    // MARK: - Coordinator

    @MainActor
    final class Coordinator: NSObject,
        WKScriptMessageHandler,
        WKUIDelegate,
        WKNavigationDelegate,
        UIGestureRecognizerDelegate {
        let config: ServerConfigStore
        let schemeHandler: MindWeaveSchemeHandler
        var lastReloadToken: UUID
        var lastServerURL: String
        weak var webView: WKWebView?

        init(config: ServerConfigStore) {
            self.config = config
            self.schemeHandler = MindWeaveSchemeHandler(config: config)
            self.lastReloadToken = config.reloadToken
            self.lastServerURL = config.apiBaseURLString
            super.init()
        }

        func teardown(webView: WKWebView) {
            let controller = webView.configuration.userContentController
            for channel in BridgeChannel.allCases {
                controller.removeScriptMessageHandler(forName: channel.rawValue)
            }
            controller.removeAllUserScripts()
            schemeHandler.cancelAllTasks()
            webView.uiDelegate = nil
            webView.navigationDelegate = nil
        }

        func syncServerURLGlobal(in webView: WKWebView) {
            let escaped = WebContainerView.jsonStringLiteral(config.apiBaseURLString)
            let js = "try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {} window.__MW_API_BASE__ = \"\"; window.__MW_SERVER_URL__ = \(escaped);"
            webView.evaluateJavaScript(js, completionHandler: nil)
        }

        @objc func handleServerConfigGesture() {
            NativeHaptics.shared.trigger("selection")
            config.showConfigSheet = true
        }

        // MARK: - UIGestureRecognizerDelegate

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            true
        }

        // MARK: - WKScriptMessageHandler

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard let channel = BridgeChannel(rawValue: message.name) else {
                return
            }

            switch channel {
            case .haptic:
                handleHapticMessage(message.body)
            case .share:
                handleShareMessage(message.body)
            case .clipboard:
                handleClipboardMessage(message.body)
            case .audio:
                handleAudioMessage(message.body)
            case .config:
                handleConfigMessage(message.body)
            }
        }

        private func handleHapticMessage(_ body: Any) {
            if let style = body as? String {
                NativeHaptics.shared.trigger(style)
                return
            }
            guard let dict = body as? [String: Any] else {
                NativeHaptics.shared.trigger("light")
                return
            }
            let style = (dict["style"] as? String)
                ?? (dict["type"] as? String)
                ?? (dict["kind"] as? String)
                ?? "light"
            if (dict["prepare"] as? Bool) == true {
                NativeHaptics.shared.prepare(style)
                return
            }
            let intensity = (dict["intensity"] as? NSNumber).map { CGFloat($0.doubleValue) }
            NativeHaptics.shared.trigger(style, intensity: intensity)
        }

        private func handleShareMessage(_ body: Any) {
            if let text = body as? String {
                presentNativeShare(title: nil, text: text, filename: nil)
                return
            }
            guard let dict = body as? [String: Any] else {
                return
            }
            let title = dict["title"] as? String
            let text = (dict["text"] as? String) ?? (dict["content"] as? String)
            let filename = dict["filename"] as? String
            let urlString = dict["url"] as? String
            presentNativeShare(title: title, text: text, filename: filename, urlString: urlString)
        }

        private func handleClipboardMessage(_ body: Any) {
            if let text = body as? String {
                UIPasteboard.general.string = text
                return
            }
            if let dict = body as? [String: Any],
               let text = (dict["text"] as? String) ?? (dict["value"] as? String) {
                UIPasteboard.general.string = text
            }
        }

        private func handleAudioMessage(_ body: Any) {
            if let command = body as? String {
                NativeAudioSession.handleCommand(command)
                return
            }
            if let dict = body as? [String: Any] {
                let command = (dict["action"] as? String)
                    ?? (dict["mode"] as? String)
                    ?? (dict["category"] as? String)
                    ?? "playAndRecord"
                NativeAudioSession.handleCommand(command)
                return
            }
            NativeAudioSession.activateForVoiceAndPlayback()
        }

        private func handleConfigMessage(_ body: Any) {
            if let str = body as? String {
                let trimmed = str.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.lowercased() == "reload" {
                    config.triggerReload()
                } else if trimmed.lowercased().hasPrefix("http://") || trimmed.lowercased().hasPrefix("https://") {
                    _ = config.updateBaseURL(trimmed)
                } else {
                    config.showConfigSheet = true
                }
                return
            }

            if let dict = body as? [String: Any] {
                if let action = (dict["action"] as? String)?.lowercased() {
                    if action == "open" || action == "show" {
                        config.showConfigSheet = true
                        return
                    }
                    if action == "reload" {
                        config.triggerReload()
                        return
                    }
                }
                if dict.keys.contains("apiBase") {
                    let apiBase = (dict["apiBase"] as? String)?
                        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    if apiBase.isEmpty {
                        config.resetToDefault()
                    } else {
                        _ = config.updateBaseURL(apiBase)
                    }
                    if (dict["open"] as? Bool) == true {
                        config.showConfigSheet = true
                    }
                    return
                }
                if let urlStr = (dict["url"] as? String)
                    ?? (dict["apiBaseUrl"] as? String)
                    ?? (dict["baseUrl"] as? String),
                   !urlStr.isEmpty {
                    _ = config.updateBaseURL(urlStr)
                    if (dict["open"] as? Bool) == true {
                        config.showConfigSheet = true
                    }
                    return
                }
            }

            config.showConfigSheet = true
        }

        // MARK: - WKUIDelegate

        @available(iOS 15.0, *)
        func webView(
            _ webView: WKWebView,
            requestMediaCapturePermissionFor origin: WKSecurityOrigin,
            initiatedByFrame frame: WKFrameInfo,
            type: WKMediaCaptureType,
            decisionHandler: @escaping @MainActor (WKPermissionDecision) -> Void
        ) {
            if origin.`protocol`.lowercased() == MindWeaveSchemeHandler.scheme {
                if type == .microphone || type == .cameraAndMicrophone {
                    NativeAudioSession.activateForVoiceAndPlayback()
                }
                decisionHandler(.grant)
            } else {
                decisionHandler(.prompt)
            }
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url {
                openExternallyIfSupported(url)
            }
            return nil
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor () -> Void
        ) {
            guard let presenter = topMostViewController() else {
                completionHandler()
                return
            }
            let alert = UIAlertController(title: "心络", message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "好", style: .default) { _ in
                MainActor.assumeIsolated {
                    completionHandler()
                }
            })
            presenter.present(alert, animated: true)
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor (Bool) -> Void
        ) {
            guard let presenter = topMostViewController() else {
                completionHandler(false)
                return
            }
            let alert = UIAlertController(title: "心络", message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in
                MainActor.assumeIsolated {
                    completionHandler(false)
                }
            })
            alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in
                MainActor.assumeIsolated {
                    completionHandler(true)
                }
            })
            presenter.present(alert, animated: true)
        }

        // MARK: - WKNavigationDelegate

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }

            let scheme = url.scheme?.lowercased() ?? ""
            if scheme == MindWeaveSchemeHandler.scheme
                || scheme == "about"
                || scheme == "blob"
                || scheme == "data" {
                decisionHandler(.allow)
                return
            }

            // Route 24h crisis hotlines (`tel:`), mail/SMS, and external links directly to iOS.
            if scheme == "tel" || scheme == "mailto" || scheme == "sms" {
                openExternallyIfSupported(url)
                decisionHandler(.cancel)
                return
            }

            if (scheme == "http" || scheme == "https"),
               navigationAction.navigationType == .linkActivated {
                openExternallyIfSupported(url)
                decisionHandler(.cancel)
                return
            }

            decisionHandler(.allow)
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            syncServerURLGlobal(in: webView)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            syncServerURLGlobal(in: webView)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            schemeHandler.cancelAllTasks()
            webView.load(URLRequest(url: MindWeaveSchemeHandler.entryURL))
        }

        private func openExternallyIfSupported(_ url: URL) {
            guard let scheme = url.scheme?.lowercased(),
                  scheme == "tel" || scheme == "mailto" || scheme == "sms" || scheme == "http" || scheme == "https" else {
                return
            }
            UIApplication.shared.open(url, options: [:], completionHandler: nil)
        }
    }
}
