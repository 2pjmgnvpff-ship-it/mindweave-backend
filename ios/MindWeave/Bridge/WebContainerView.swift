import Network
import SwiftUI
import UIKit
import WebKit

/// Named JavaScript-to-Swift bridge channels matching `web/scripts/native-bridge.js`.
private enum BridgeChannel: String, CaseIterable {
    case haptic = "mwHaptic"
    case share = "mwShare"
    case clipboard = "mwClipboard"
    case audio = "mwAudio"
    case speech = "mwSpeech"
    case biometrics = "mwBiometrics"
    case nativeUI = "mwNativeUI"
    #if DEBUG
    case config = "mwConfig"
    #endif
}

/// Clean public-API `WKWebView` subclass suppressing the gray Safari keyboard accessory toolbar
/// (`inputAccessoryView = nil` + empty iPad `inputAssistantItem` groups) with ZERO private
/// `WKContentView` ObjC runtime swizzling (App Store Review Guideline 2.5.1 safe!).
@MainActor
private final class MindWeaveWKWebView: WKWebView {
    override var inputAccessoryView: UIView? {
        nil
    }

    override var inputAssistantItem: UITextInputAssistantItem {
        let item = super.inputAssistantItem
        item.leadingBarButtonGroups = []
        item.trailingBarButtonGroups = []
        return item
    }
}

/// Weak wrapper preventing the `WKUserContentController -> WKScriptMessageHandler` retain cycle.
/// Annotated `@MainActor` for Swift 6.0 `complete` strict-concurrency conformance to `WKScriptMessageHandler`.
@MainActor
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
/// with native iOS speech recognition (`mwSpeech`), Face ID lock (`mwBiometrics`),
/// unified native UIKit/Apple SDK components (`mwNativeUI`), CoreHaptics, audio session,
/// binary Share Sheet + `LPLinkMetadata`, clipboard, 1pt status-bar tap-to-top proxy,
/// frame-locked keyboard spring curve sync, live `UIAccessibility` trait sync,
/// public-API zoom lock, `NWPathMonitor` wake hooks, and same-origin proxying.
@MainActor
struct WebContainerView: UIViewRepresentable {
    @ObservedObject var config: ServerConfigStore
    @ObservedObject var biometricLock: BiometricLockManager

    /// Warm porcelain backdrop (`#FCFBF9`) eliminating transparent compositing & FOUC.
    private static let porcelainUIColor = UIColor(
        red: 0.988,
        green: 0.984,
        blue: 0.976,
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

    /// Document-start bootstrap script keeping all API requests same-origin on `mindweave://app/v1/*`,
    /// declaring native speech & native UI readiness, and gating debug surfaces strictly via `#if DEBUG`.
    static func bootstrapScriptSource(serverURL: String) -> String {
        #if DEBUG
        let escapedServerURL = jsonStringLiteral(serverURL)
        return """
        try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {}
        window.__MW_NATIVE_IOS__ = true;
        window.__MW_IOS_NATIVE__ = true;
        window.__MW_SPEECH_NATIVE__ = true;
        window.__MW_NATIVE_UI__ = true;
        window.__MW_DEBUG_BUILD__ = true;
        window.__MW_API_BASE__ = "";
        window.__MW_SERVER_URL__ = \(escapedServerURL);
        """
        #else
        _ = serverURL
        return """
        try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {}
        window.__MW_NATIVE_IOS__ = true;
        window.__MW_IOS_NATIVE__ = true;
        window.__MW_SPEECH_NATIVE__ = true;
        window.__MW_NATIVE_UI__ = true;
        window.__MW_DEBUG_BUILD__ = false;
        window.__MW_API_BASE__ = "";
        """
        #endif
    }

    private static func makeBootstrapUserScript(serverURL: String) -> WKUserScript {
        WKUserScript(
            source: bootstrapScriptSource(serverURL: serverURL),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(config: config, biometricLock: biometricLock)
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
        configuration.ignoresViewportScaleLimits = false
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.setURLSchemeHandler(
            coordinator.schemeHandler,
            forURLScheme: MindWeaveSchemeHandler.scheme
        )

        let webView = MindWeaveWKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = coordinator
        webView.navigationDelegate = coordinator
        webView.isOpaque = true
        webView.backgroundColor = Self.porcelainUIColor
        webView.scrollView.backgroundColor = Self.porcelainUIColor
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.bounces = false
        webView.scrollView.alwaysBounceVertical = false
        webView.scrollView.alwaysBounceHorizontal = false
        webView.scrollView.showsVerticalScrollIndicator = false
        webView.scrollView.showsHorizontalScrollIndicator = false
        // Status-bar tap is intercepted by our 1x1pt parked `statusBarTapProxy` (`01m`/`02g`).
        webView.scrollView.scrollsToTop = false
        webView.scrollView.keyboardDismissMode = .interactive
        // Public-API zoom lock (preserves native <textarea> double-tap text selection!)
        webView.scrollView.delegate = coordinator
        webView.scrollView.minimumZoomScale = 1.0
        webView.scrollView.maximumZoomScale = 1.0
        webView.scrollView.pinchGestureRecognizer?.isEnabled = false
        webView.allowsBackForwardNavigationGestures = false
        webView.isFindInteractionEnabled = true

        #if DEBUG
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        // Unobtrusive 3-finger double-tap shortcut (Debug builds ONLY).
        let serverConfigTap = UITapGestureRecognizer(
            target: coordinator,
            action: #selector(Coordinator.handleServerConfigGesture)
        )
        serverConfigTap.numberOfTouchesRequired = 3
        serverConfigTap.numberOfTapsRequired = 2
        serverConfigTap.cancelsTouchesInView = false
        serverConfigTap.delegate = coordinator
        webView.addGestureRecognizer(serverConfigTap)
        #endif

        coordinator.attach(webView: webView)
        webView.load(URLRequest(url: MindWeaveSchemeHandler.entryURL))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        let coordinator = context.coordinator

        #if DEBUG
        if coordinator.lastServerURL != config.apiBaseURLString {
            coordinator.lastServerURL = config.apiBaseURLString
            let controller = uiView.configuration.userContentController
            controller.removeAllUserScripts()
            controller.addUserScript(
                Self.makeBootstrapUserScript(serverURL: config.apiBaseURLString)
            )
            coordinator.syncRuntimeGlobals(in: uiView)
        }
        #endif

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
        UIScrollViewDelegate,
        UIGestureRecognizerDelegate {
        let config: ServerConfigStore
        let biometricLock: BiometricLockManager
        let schemeHandler: MindWeaveSchemeHandler
        private var speechEngine: NativeSpeechEngine?
        private var nativeUI: NativeUICoordinator?
        private var statusBarTapProxy: UIScrollView?
        private let pathMonitor = NWPathMonitor()
        private let pathMonitorQueue = DispatchQueue(label: "app.mindweave.netpath", qos: .utility)
        private var lastPathSatisfied: Bool = false
        private var notificationObservers: [NSObjectProtocol] = []
        var lastReloadToken: UUID
        var lastServerURL: String
        weak var webView: WKWebView?

        init(config: ServerConfigStore, biometricLock: BiometricLockManager) {
            self.config = config
            self.biometricLock = biometricLock
            self.schemeHandler = MindWeaveSchemeHandler(config: config)
            self.lastReloadToken = config.reloadToken
            self.lastServerURL = config.apiBaseURLString
            super.init()

            let engine = NativeSpeechEngine { [weak self] json in
                self?.emitNativeEventJSON(json)
            }
            self.speechEngine = engine
            self.biometricLock.isVoiceRecordingActive = { [weak engine] in
                engine?.isActivelyRecording ?? false
            }

            self.nativeUI = NativeUICoordinator(
                config: config,
                biometricLock: biometricLock
            ) { [weak self] dict in
                self?.emitNativeEventDict(dict)
            }
        }

        func attach(webView: WKWebView) {
            self.webView = webView
            installStatusBarTapProxy(on: webView)
            startSystemObservers()
            startNetworkPathMonitor()
        }

        func teardown(webView: WKWebView) {
            for observer in notificationObservers {
                NotificationCenter.default.removeObserver(observer)
            }
            notificationObservers.removeAll()
            pathMonitor.cancel()

            statusBarTapProxy?.delegate = nil
            statusBarTapProxy?.removeFromSuperview()
            statusBarTapProxy = nil

            nativeUI?.teardown()
            nativeUI = nil

            speechEngine?.shutdown()
            speechEngine = nil
            UIApplication.shared.isIdleTimerDisabled = false

            let controller = webView.configuration.userContentController
            for channel in BridgeChannel.allCases {
                controller.removeScriptMessageHandler(forName: channel.rawValue)
            }
            controller.removeAllUserScripts()
            schemeHandler.cancelAllTasks()
            webView.scrollView.delegate = nil
            webView.uiDelegate = nil
            webView.navigationDelegate = nil
        }

        /// Transparent `1x1pt` parked scroll view (`contentSize = (1, 2)`, `contentOffset.y = 1`,
        /// `scrollsToTop = true`) so tapping the iPhone status bar ALWAYS fires `statusbar.tap`
        /// even while outer `webView.scrollView.contentOffset.y == 0` (`01m` & `02g`).
        private func installStatusBarTapProxy(on webView: WKWebView) {
            statusBarTapProxy?.removeFromSuperview()
            let proxy = UIScrollView(frame: CGRect(x: 0, y: 0, width: 1, height: 1))
            proxy.backgroundColor = .clear
            proxy.isUserInteractionEnabled = false
            proxy.isAccessibilityElement = false
            proxy.showsVerticalScrollIndicator = false
            proxy.showsHorizontalScrollIndicator = false
            proxy.contentInsetAdjustmentBehavior = .never
            proxy.contentSize = CGSize(width: 1, height: 2)
            proxy.contentOffset = CGPoint(x: 0, y: 1)
            proxy.scrollsToTop = true
            proxy.delegate = self
            webView.addSubview(proxy)
            statusBarTapProxy = proxy
        }

        /// Single unified Swift→JS push channel (`window.__mwNativeEvent?.(<json>)`).
        func emitNativeEventJSON(_ json: String) {
            guard let webView else { return }
            let js = "window.__mwNativeEvent && window.__mwNativeEvent(\(json));"
            webView.evaluateJavaScript(js, completionHandler: nil)
        }

        private func emitNativeEventDict(_ dict: [String: Any]) {
            guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
                  let json = String(data: data, encoding: .utf8) else {
                return
            }
            emitNativeEventJSON(json)
        }

        func syncRuntimeGlobals(in webView: WKWebView) {
            #if DEBUG
            let escaped = WebContainerView.jsonStringLiteral(config.apiBaseURLString)
            let js = """
            try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {}
            window.__MW_SPEECH_NATIVE__ = true;
            window.__MW_NATIVE_UI__ = true;
            window.__MW_DEBUG_BUILD__ = true;
            window.__MW_API_BASE__ = "";
            window.__MW_SERVER_URL__ = \(escaped);
            """
            #else
            let js = """
            try { window.localStorage.removeItem('mw.apiBase'); } catch (e) {}
            window.__MW_SPEECH_NATIVE__ = true;
            window.__MW_NATIVE_UI__ = true;
            window.__MW_DEBUG_BUILD__ = false;
            window.__MW_API_BASE__ = "";
            """
            #endif
            webView.evaluateJavaScript(js, completionHandler: nil)
        }

        #if DEBUG
        @objc func handleServerConfigGesture() {
            NativeHaptics.shared.trigger("selection")
            config.showConfigSheet = true
        }
        #endif

        // MARK: - Keyboard Spring Sync, Accessibility Trait Sync & Quick Actions Observers

        private func startSystemObservers() {
            for observer in notificationObservers {
                NotificationCenter.default.removeObserver(observer)
            }
            notificationObservers.removeAll()

            let kbChangeObserver = NotificationCenter.default.addObserver(
                forName: UIResponder.keyboardWillChangeFrameNotification,
                object: nil,
                queue: .main
            ) { [weak self] note in
                MainActor.assumeIsolated {
                    let endFrame = (note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? NSValue)?.cgRectValue
                    let durationSec = (note.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? NSNumber)?.doubleValue ?? 0.25
                    let curveRaw = (note.userInfo?[UIResponder.keyboardAnimationCurveUserInfoKey] as? NSNumber)?.intValue ?? 7
                    self?.handleKeyboardFrame(
                        endFrame: endFrame,
                        durationSec: durationSec,
                        curveRaw: curveRaw,
                        isHide: false
                    )
                }
            }

            let kbHideObserver = NotificationCenter.default.addObserver(
                forName: UIResponder.keyboardWillHideNotification,
                object: nil,
                queue: .main
            ) { [weak self] note in
                MainActor.assumeIsolated {
                    let durationSec = (note.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? NSNumber)?.doubleValue ?? 0.25
                    let curveRaw = (note.userInfo?[UIResponder.keyboardAnimationCurveUserInfoKey] as? NSNumber)?.intValue ?? 7
                    self?.handleKeyboardFrame(
                        endFrame: nil,
                        durationSec: durationSec,
                        curveRaw: curveRaw,
                        isHide: true
                    )
                }
            }

            let a11yNames: [Notification.Name] = [
                UIContentSizeCategory.didChangeNotification,
                UIAccessibility.reduceMotionStatusDidChangeNotification,
                UIAccessibility.voiceOverStatusDidChangeNotification,
                UIAccessibility.boldTextStatusDidChangeNotification,
            ]
            var observers: [NSObjectProtocol] = [kbChangeObserver, kbHideObserver]
            for name in a11yNames {
                let obs = NotificationCenter.default.addObserver(
                    forName: name,
                    object: nil,
                    queue: .main
                ) { [weak self] _ in
                    MainActor.assumeIsolated {
                        self?.syncAccessibilityTraits()
                    }
                }
                observers.append(obs)
            }

            let shortcutObs = NotificationCenter.default.addObserver(
                forName: .mindWeaveShortcutAction,
                object: nil,
                queue: .main
            ) { [weak self] note in
                let shortcutType = (note.userInfo?["shortcutType"] as? String) ?? ""
                MainActor.assumeIsolated {
                    guard !shortcutType.isEmpty else { return }
                    self?.emitShortcutAction(shortcutType)
                }
            }
            observers.append(shortcutObs)

            notificationObservers = observers
        }

        private static func cssCurve(for curveRaw: Int) -> String {
            switch curveRaw {
            case 0: return "ease-in-out"
            case 1: return "ease-in"
            case 2: return "ease-out"
            case 3: return "linear"
            default:
                // Apple's standard iOS 7-26 keyboard spring curve (`rawValue == 7`)
                return "cubic-bezier(0.38, 0.7, 0.125, 1)"
            }
        }

        private func handleKeyboardFrame(
            endFrame: CGRect?,
            durationSec: Double,
            curveRaw: Int,
            isHide: Bool
        ) {
            guard let webView else { return }
            let insetPx: Int
            if isHide {
                insetPx = 0
            } else if let endFrame {
                let webFrameInWindow = webView.convert(webView.bounds, to: nil)
                let overlap = max(0.0, webFrameInWindow.maxY - endFrame.minY)
                insetPx = Int(overlap.rounded())
            } else {
                return
            }
            let durationMs = max(0, Int((durationSec * 1000.0).rounded()))
            let curveCss = Self.cssCurve(for: curveRaw)

            if insetPx == 0 {
                // Prevent iOS WKWebView from leaving a ghost vertical scroll offset when keyboard dismisses.
                webView.scrollView.setContentOffset(.zero, animated: false)
            }

            let kbOpenJS = insetPx > 0 ? "true" : "false"
            let escapedCurve = WebContainerView.jsonStringLiteral(curveCss)
            let cssJS = """
            (function(){
              var r = document.documentElement.style;
              r.setProperty('--mw-kb-inset', '\(insetPx)px');
              r.setProperty('--kb-inset', '\(insetPx)px');
              r.setProperty('--mw-kb-dur', '\(durationMs)ms');
              r.setProperty('--mw-kb-curve', \(escapedCurve));
              document.documentElement.classList.toggle('is-kb-open', \(kbOpenJS));
            })();
            """
            webView.evaluateJavaScript(cssJS, completionHandler: nil)
            emitNativeEventDict([
                "type": "keyboard.change",
                "height": insetPx,
                "durationMs": durationMs,
                "curve": curveRaw,
                "curveCss": curveCss,
            ])
        }

        private static func dynamicTypeScale(for category: UIContentSizeCategory) -> Double {
            switch category {
            case .extraSmall: return 0.85
            case .small: return 0.90
            case .medium: return 0.95
            case .large: return 1.00
            case .extraLarge: return 1.12
            case .extraExtraLarge: return 1.24
            case .extraExtraExtraLarge: return 1.35
            case .accessibilityMedium: return 1.48
            case .accessibilityLarge: return 1.60
            case .accessibilityExtraLarge: return 1.72
            case .accessibilityExtraExtraLarge: return 1.80
            case .accessibilityExtraExtraExtraLarge: return 1.85
            default: return 1.00
            }
        }

        private func syncAccessibilityTraits() {
            guard let webView else { return }
            let category = UIApplication.shared.preferredContentSizeCategory
            let scale = Self.dynamicTypeScale(for: category)
            let reduceMotion = UIAccessibility.isReduceMotionEnabled
            let voiceOver = UIAccessibility.isVoiceOverRunning
            let boldText = UIAccessibility.isBoldTextEnabled

            let scaleStr = String(format: "%.2f", scale)
            let reduceMotionJS = reduceMotion ? "true" : "false"
            let cssJS = """
            (function(){
              document.documentElement.style.setProperty('--mw-dynamic-type-scale', '\(scaleStr)');
              document.documentElement.classList.toggle('mw-reduce-motion', \(reduceMotionJS));
            })();
            """
            webView.evaluateJavaScript(cssJS, completionHandler: nil)
            emitNativeEventDict([
                "type": "a11y.change",
                "preferredContentSize": category.rawValue,
                "dynamicTypeScale": scale,
                "fontScale": scale,
                "reduceMotion": reduceMotion,
                "voiceOver": voiceOver,
                "boldText": boldText,
            ])
        }

        private func emitShortcutAction(_ shortcutType: String) {
            let shortAction: String
            switch shortcutType {
            case "app.mindweave.shortcut.capture":
                shortAction = "capture"
            case "app.mindweave.shortcut.chat":
                shortAction = "chat"
            case "app.mindweave.shortcut.archives":
                shortAction = "archives"
            default:
                shortAction = shortcutType
            }
            emitNativeEventDict([
                "type": "shortcut.action",
                "shortcutType": shortcutType,
                "action": shortAction,
            ])
        }

        // MARK: - NWPathMonitor Subway/Elevator Recovery Wake Hook (BUG-SQ-5)

        private func startNetworkPathMonitor() {
            pathMonitor.pathUpdateHandler = { [weak self] path in
                let satisfied = (path.status == .satisfied)
                let iface: String
                if path.usesInterfaceType(.wifi) {
                    iface = "wifi"
                } else if path.usesInterfaceType(.cellular) {
                    iface = "cellular"
                } else {
                    iface = "other"
                }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let wasSatisfied = self.lastPathSatisfied
                    self.lastPathSatisfied = satisfied
                    if satisfied && !wasSatisfied {
                        self.emitNativeEventDict([
                            "type": "network.ready",
                            "interface": iface,
                        ])
                        self.webView?.evaluateJavaScript(
                            "window.__mwOnNetworkPathReady && window.__mwOnNetworkPathReady();",
                            completionHandler: nil
                        )
                    }
                }
            }
            pathMonitor.start(queue: pathMonitorQueue)
        }

        // MARK: - UIScrollViewDelegate (Status-Bar Tap-to-Top Proxy + Public-API Zoom Lock)

        func scrollViewShouldScrollToTop(_ scrollView: UIScrollView) -> Bool {
            if scrollView === statusBarTapProxy {
                NativeHaptics.shared.trigger("porcelainTick")
                emitNativeEventDict(["type": "statusbar.tap"])
                // Keep proxy parked at y=1 so every subsequent status-bar tap fires reliably!
                scrollView.setContentOffset(CGPoint(x: 0, y: 1), animated: false)
                return false
            }
            return false
        }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? {
            nil
        }

        func scrollViewWillBeginZooming(_ scrollView: UIScrollView, with view: UIView?) {
            scrollView.pinchGestureRecognizer?.isEnabled = false
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
            case .speech:
                speechEngine?.handleBridgeMessage(message.body)
            case .biometrics:
                handleBiometricsMessage(message.body)
            case .nativeUI:
                nativeUI?.handleBridgeMessage(message.body, webView: webView)
            #if DEBUG
            case .config:
                handleConfigMessage(message.body)
            #endif
            }
        }

        private func handleBiometricsMessage(_ body: Any) {
            guard let dict = body as? [String: Any] else { return }
            let reqId = (dict["reqId"] as? String) ?? ""
            let action = ((dict["action"] as? String) ?? "status").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

            switch action {
            case "status", "getstatus":
                let caps = biometricLock.capabilities()
                emitNativeEventDict([
                    "type": "biometrics.result",
                    "reqId": reqId,
                    "ok": true,
                    "available": caps.available,
                    "enabled": caps.enabled,
                    "locked": caps.locked,
                    "biometryKind": caps.biometryKind,
                    "labelCn": caps.labelCn,
                    "graceSeconds": caps.graceSeconds,
                ])
            case "setgrace":
                let secs = (dict["graceSeconds"] as? NSNumber)?.intValue ?? 0
                let applied = biometricLock.setGraceSeconds(secs)
                let caps = biometricLock.capabilities()
                emitNativeEventDict([
                    "type": "biometrics.result",
                    "reqId": reqId,
                    "ok": true,
                    "available": caps.available,
                    "enabled": caps.enabled,
                    "locked": caps.locked,
                    "biometryKind": caps.biometryKind,
                    "labelCn": caps.labelCn,
                    "graceSeconds": applied,
                ])
            case "setenabled":
                let targetEnabled = (dict["enabled"] as? Bool) ?? false
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let outcome = await self.biometricLock.setLockEnabled(targetEnabled)
                    let caps = self.biometricLock.capabilities()
                    var payload: [String: Any] = [
                        "type": "biometrics.result",
                        "reqId": reqId,
                        "ok": outcome.ok,
                        "available": caps.available,
                        "enabled": outcome.enabled,
                        "locked": caps.locked,
                        "biometryKind": caps.biometryKind,
                        "labelCn": caps.labelCn,
                        "graceSeconds": caps.graceSeconds,
                    ]
                    if let err = outcome.error {
                        payload["error"] = err
                    }
                    self.emitNativeEventDict(payload)
                }
            case "unlock":
                biometricLock.triggerUnlock(userInitiated: true)
            default:
                break
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
            if (dict["prepare"] as? Bool) == true
                || ((dict["action"] as? String)?.lowercased() == "prepare") {
                NativeHaptics.shared.prepare(style)
                return
            }
            let tier = (dict["tier"] as? String)?.lowercased()
            let resolvedStyle = (tier == "silver" && style.lowercased() == "memoryseal")
                ? "memorySealSilver"
                : style
            let intensity = (dict["intensity"] as? NSNumber).map { CGFloat($0.doubleValue) }
            NativeHaptics.shared.trigger(resolvedStyle, intensity: intensity)
        }

        private func handleShareMessage(_ body: Any) {
            if let text = body as? String {
                presentNativeShare(
                    title: nil,
                    text: text,
                    filename: nil,
                    biometricLock: biometricLock
                )
                return
            }
            guard let dict = body as? [String: Any] else {
                return
            }
            let title = dict["title"] as? String
            let text = (dict["text"] as? String) ?? (dict["content"] as? String)
            let filename = dict["filename"] as? String
            let urlString = dict["url"] as? String
            let base64Data = (dict["base64"] as? String) ?? (dict["binaryBase64"] as? String)
            let mimeType = dict["mimeType"] as? String
            presentNativeShare(
                title: title,
                text: text,
                filename: filename,
                urlString: urlString,
                base64Data: base64Data,
                mimeType: mimeType,
                biometricLock: biometricLock
            )
        }

        private func handleClipboardMessage(_ body: Any) {
            if let text = body as? String {
                UIPasteboard.general.string = text
                return
            }
            if let dict = body as? [String: Any],
               let text = (dict["text"] as? String) ?? (dict["value"] as? String) {
                if (dict["localOnly"] as? Bool) == true {
                    nativeUI?.handleBridgeMessage(
                        ["action": "copySensitive", "text": text],
                        webView: webView
                    )
                } else {
                    UIPasteboard.general.string = text
                }
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

        #if DEBUG
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
        #endif

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
            biometricLock.suppressNextInactiveLock(forSeconds: 4.0)
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
            biometricLock.suppressNextInactiveLock(forSeconds: 4.0)
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

            // Route 24h crisis hotlines (`tel:`), mail/SMS directly to iOS (NEVER hijack with Safari!).
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
            syncRuntimeGlobals(in: webView)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            syncRuntimeGlobals(in: webView)
            syncAccessibilityTraits()
            if let pendingShortcut = MindWeaveAppDelegate.consumePendingShortcut() {
                emitShortcutAction(pendingShortcut)
            }
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            schemeHandler.cancelAllTasks()
            webView.load(URLRequest(url: MindWeaveSchemeHandler.entryURL))
        }

        private func openExternallyIfSupported(_ url: URL) {
            guard let scheme = url.scheme?.lowercased() else { return }
            if scheme == "tel" || scheme == "mailto" || scheme == "sms" {
                UIApplication.shared.open(url, options: [:], completionHandler: nil)
            } else if scheme == "http" || scheme == "https" {
                nativeUI?.openSafari(url)
            }
        }
    }
}
