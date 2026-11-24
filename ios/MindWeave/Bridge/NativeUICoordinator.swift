import Foundation
import ImageIO
@preconcurrency import PhotosUI
@preconcurrency import QuickLook
import SafariServices
import UIKit
import UniformTypeIdentifiers
@preconcurrency import UserNotifications
import WebKit

/// Lightweight QuickLook item wrapping a temporary decrypted image/document URL.
private final class MindWeaveQuickLookItem: NSObject, QLPreviewItem {
    @objc let previewItemURL: URL?
    @objc let previewItemTitle: String?

    init(url: URL, title: String?) {
        self.previewItemURL = url
        self.previewItemTitle = title
        super.init()
    }
}

/// Strictly `Sendable` sole-ownership transfer box for ObjC types/blocks crossing isolation domains.
private struct UncheckedSendableBox<T>: @unchecked Sendable {
    let value: T
}

/// Strictly `Sendable` thumbnail result decoded off-main via `ImageIO`.
private struct DecodedImagePayload: Sendable {
    let mimeType: String
    let base64: String
    let width: Int
    let height: Int
    let byteLength: Int
}

/// Unified Apple-first-party iOS Native UI Coordinator bridged via `BridgeChannel.nativeUI = "mwNativeUI"`.
///
/// Implements ALL `02g` × `01m` Tier-1 native iOS components under Swift 6.0 `complete` strict concurrency
/// and `IPHONEOS_DEPLOYMENT_TARGET: 16.0`:
/// - `pickPhoto`: Out-of-process `PHPickerViewController` + `ImageIO` `CGImageSourceCreateThumbnailAtIndex`
///   (zero Photos-library prompt, zero 48MP HEIC OOM, blocked on Silver tier).
/// - `previewMedia`: Direct modal `QLPreviewController` (NEVER wrapped in `UINavigationController`,
///   official `previewController(_:editingModeFor:) -> .disabled`, blocked on Silver tier).
/// - `openSafari`: `#FCFBF9`-tinted `SFSafariViewController` `.pageSheet` (never hijacks `tel:/mailto:/sms:`).
/// - `showSheet`: Native `UIAlertController` (`.actionSheet`/`.alert`, `.destructive`, iPad popover anchor).
/// - `showContextMenu`: Native `UIMenu` (`<= 3` groups, SF Symbols, hides share on Silver, zero double-haptic).
/// - `copySensitive`: Local-only expiring `UIPasteboard` (`localOnly: true`, 120s TTL) for Silver privacy.
/// - `requestNotificationPermission` & `syncReminders`: `UNUserNotificationCenter` (`<= 48` chronological
///   slots, `hiddenPreviewsBodyPlaceholder = "你有一条心络提醒"`, `badge = nil`, `记完了` / `明天再提醒`,
///   strictly classic `nonisolated` completion-handler delegate methods — NEVER `async` delegate overloads!).
/// - `setIdleTimer`: Guards screen wake during active voice recording.
@MainActor
final class NativeUICoordinator: NSObject,
    @preconcurrency PHPickerViewControllerDelegate,
    @preconcurrency QLPreviewControllerDataSource,
    @preconcurrency QLPreviewControllerDelegate,
    UNUserNotificationCenterDelegate {

    static let porcelainBarTint = UIColor(red: 0.988, green: 0.984, blue: 0.976, alpha: 1.0) // #FCFBF9
    static let warmInkTint = UIColor(red: 0.200, green: 0.173, blue: 0.153, alpha: 1.0)      // #332C27

    private nonisolated static let reminderCategoryID = "MINDWEAVE_REMINDER"
    private nonisolated static let reminderActionDoneID = "MW_ACTION_DONE"
    private nonisolated static let reminderActionSnoozeID = "MW_ACTION_SNOOZE_1D"
    private nonisolated static let reminderPrefix = "mw.rem."
    private nonisolated static let maxScheduledReminders = 48

    private let config: ServerConfigStore
    private weak var biometricLock: BiometricLockManager?
    private let emitEvent: @MainActor ([String: Any]) -> Void

    private var isPresentingModal: Bool = false

    // PHPicker state
    private var activePhotoReqId: String?
    private var activePhotoMaxPixelSize: Int = 1600

    // QuickLook state
    private var quickLookItems: [MindWeaveQuickLookItem] = []
    private var quickLookTempDir: URL?
    private var activePreviewReqId: String?

    // Ephemeral UIMenu anchor button for native context menu presentation
    private weak var contextMenuAnchorButton: UIButton?

    init(
        config: ServerConfigStore,
        biometricLock: BiometricLockManager,
        emitEvent: @escaping @MainActor ([String: Any]) -> Void
    ) {
        self.config = config
        self.biometricLock = biometricLock
        self.emitEvent = emitEvent
        super.init()
        configureNotificationCenter()
    }

    func teardown() {
        UIApplication.shared.isIdleTimerDisabled = false
        cleanupQuickLookTempDir()
        contextMenuAnchorButton?.removeFromSuperview()
        contextMenuAnchorButton = nil
        if UNUserNotificationCenter.current().delegate === self {
            UNUserNotificationCenter.current().delegate = nil
        }
    }

    // MARK: - Unified `mwNativeUI` Bridge Dispatcher

    func handleBridgeMessage(_ body: Any, webView: WKWebView?) {
        guard let dict = body as? [String: Any] else { return }
        let action = ((dict["action"] as? String) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let reqId = (dict["reqId"] as? String) ?? ""

        switch action {
        case "pickphoto":
            handlePickPhoto(dict, reqId: reqId)
        case "previewmedia":
            handlePreviewMedia(dict, reqId: reqId, webView: webView)
        case "opensafari":
            if let rawURL = dict["url"] as? String, let url = URL(string: rawURL) {
                openSafari(url, reqId: reqId)
            }
        case "showsheet":
            handleShowSheet(dict, reqId: reqId)
        case "showcontextmenu":
            handleShowContextMenu(dict, reqId: reqId, webView: webView)
        case "copysensitive":
            handleCopySensitive(dict, reqId: reqId)
        case "requestnotificationpermission":
            handleRequestNotificationPermission(reqId: reqId)
        case "syncreminders":
            handleSyncReminders(dict, reqId: reqId)
        case "setidletimer":
            let disabled = (dict["disabled"] as? Bool) ?? false
            UIApplication.shared.isIdleTimerDisabled = disabled
            if !reqId.isEmpty {
                emitEvent([
                    "type": "nativeUI.idleTimerResult",
                    "reqId": reqId,
                    "ok": true,
                    "disabled": disabled,
                ])
            }
        default:
            break
        }
    }

    // MARK: - 1. Native Photo Picker (`PHPickerViewController` + `ImageIO` Zero-OOM)

    private func handlePickPhoto(_ dict: [String: Any], reqId: String) {
        let tier = ((dict["tier"] as? String) ?? "").lowercased()
        if tier == "silver" {
            emitEvent([
                "type": "nativeUI.photoResult",
                "reqId": reqId,
                "ok": false,
                "cancelled": false,
                "images": [],
                "error": "银盾边界模式不展示或附加照片",
            ])
            return
        }

        guard !isPresentingModal, let presenter = topMostViewController() else {
            emitEvent([
                "type": "nativeUI.photoResult",
                "reqId": reqId,
                "ok": false,
                "cancelled": true,
                "images": [],
                "error": "当前已有原生面板正在展示",
            ])
            return
        }

        let rawCount = (dict["maxCount"] as? NSNumber)?.intValue ?? 1
        let maxCount = max(1, min(9, rawCount))
        let rawMaxDim = (dict["maxPixelSize"] as? NSNumber)?.intValue ?? 1600
        let maxPixelSize = max(256, min(2400, rawMaxDim))

        var pickerConfig = PHPickerConfiguration()
        pickerConfig.filter = .images
        pickerConfig.selectionLimit = maxCount
        pickerConfig.preferredAssetRepresentationMode = .compatible
        pickerConfig.selection = .ordered

        let picker = PHPickerViewController(configuration: pickerConfig)
        picker.delegate = self

        activePhotoReqId = reqId
        activePhotoMaxPixelSize = maxPixelSize
        isPresentingModal = true
        biometricLock?.suppressNextInactiveLock(forSeconds: 10.0)
        presenter.present(picker, animated: true)
    }

    func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
        let reqId = activePhotoReqId ?? ""
        let maxDim = activePhotoMaxPixelSize
        activePhotoReqId = nil

        // Extract Sendable boxes BEFORE dismissing so neither `[PHPickerResult]` nor `[NSItemProvider]`
        // ever crosses into escaping/concurrent closures under Swift 6.0 `complete` concurrency!
        let providerBoxes = results.map { UncheckedSendableBox(value: $0.itemProvider) }

        picker.dismiss(animated: true) { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.isPresentingModal = false

                guard !providerBoxes.isEmpty else {
                    self.emitEvent([
                        "type": "nativeUI.photoResult",
                        "reqId": reqId,
                        "ok": true,
                        "cancelled": true,
                        "images": [],
                    ])
                    return
                }

                // Decode strictly via `loadFileRepresentation` + `ImageIO` (`CGImageSourceCreateThumbnailAtIndex`)
                // — NEVER `loadObject(ofClass: UIImage.self)`!
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let decoded = await self.downsampleProviders(providerBoxes, maxPixelSize: maxDim)
                    let payloadImages: [[String: Any]] = decoded.map { img in
                        [
                            "mimeType": img.mimeType,
                            "base64": img.base64,
                            "width": img.width,
                            "height": img.height,
                            "byteLength": img.byteLength,
                        ]
                    }
                    self.emitEvent([
                        "type": "nativeUI.photoResult",
                        "reqId": reqId,
                        "ok": !payloadImages.isEmpty,
                        "cancelled": false,
                        "images": payloadImages,
                    ])
                }
            }
        }
    }

    private func downsampleProviders(
        _ providerBoxes: [UncheckedSendableBox<NSItemProvider>],
        maxPixelSize: Int
    ) async -> [DecodedImagePayload] {
        var out: [DecodedImagePayload] = []
        for box in providerBoxes {
            guard box.value.hasItemConformingToTypeIdentifier(UTType.image.identifier) else {
                continue
            }
            if let fileData = await Self.loadImageFileData(from: box),
               let thumb = await Task.detached(priority: .userInitiated, operation: {
                   Self.makeThumbnailJPEG(from: fileData, maxPixelSize: maxPixelSize)
               }).value {
                out.append(thumb)
            }
        }
        return out
    }

    private static func loadImageFileData(from box: UncheckedSendableBox<NSItemProvider>) async -> Data? {
        await withCheckedContinuation { continuation in
            box.value.loadFileRepresentation(forTypeIdentifier: UTType.image.identifier) { url, _ in
                // CRITICAL: Copy bytes synchronously BEFORE closure returns (iOS deletes `url` immediately after!)
                guard let url else {
                    continuation.resume(returning: nil)
                    return
                }
                let data = try? Data(contentsOf: url, options: [.mappedIfSafe])
                continuation.resume(returning: data)
            }
        }
    }

    nonisolated private static func makeThumbnailJPEG(
        from data: Data,
        maxPixelSize: Int
    ) -> DecodedImagePayload? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
            return nil
        }
        let thumbOptions: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbOptions as CFDictionary) else {
            return nil
        }
        let destData = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            destData as CFMutableData,
            UTType.jpeg.identifier as CFString,
            1,
            nil
        ) else {
            return nil
        }
        let encodeOptions: [CFString: Any] = [
            kCGImageDestinationLossyCompressionQuality: 0.82,
        ]
        CGImageDestinationAddImage(destination, cgImage, encodeOptions as CFDictionary)
        guard CGImageDestinationFinalize(destination) else {
            return nil
        }
        let jpegBytes = destData as Data
        return DecodedImagePayload(
            mimeType: "image/jpeg",
            base64: jpegBytes.base64EncodedString(),
            width: cgImage.width,
            height: cgImage.height,
            byteLength: jpegBytes.count
        )
    }

    // MARK: - 2. Native Full-Screen Media Lightbox (`QLPreviewController` + Live Text OCR)

    private func handlePreviewMedia(_ dict: [String: Any], reqId: String, webView: WKWebView?) {
        let tier = ((dict["tier"] as? String) ?? "").lowercased()
        if tier == "silver" {
            emitEvent([
                "type": "nativeUI.previewResult",
                "reqId": reqId,
                "ok": false,
                "error": "银盾边界模式不展示照片预览",
            ])
            return
        }

        guard !isPresentingModal else { return }
        let rawURL = ((dict["url"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let title = (dict["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rawURL.isEmpty else { return }

        let targetURL = resolveAuthenticatedMediaURL(rawURL)
        guard let targetURL else { return }

        let authHeader = (dict["authorization"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        activePreviewReqId = reqId

        Task { @MainActor [weak self] in
            guard let self else { return }
            let fetched = await Self.fetchMediaToTempFile(
                url: targetURL,
                authorization: authHeader,
                fallbackTitle: title
            )
            guard let (tempDir, localFileURL) = fetched,
                  !self.isPresentingModal,
                  let presenter = topMostViewController() else {
                self.emitEvent([
                    "type": "nativeUI.previewResult",
                    "reqId": reqId,
                    "ok": false,
                ])
                return
            }

            self.cleanupQuickLookTempDir()
            self.quickLookTempDir = tempDir
            self.quickLookItems = [
                MindWeaveQuickLookItem(url: localFileURL, title: (title?.isEmpty == false) ? title : "手帐照片"),
            ]

            // Present `QLPreviewController` DIRECTLY modally — NEVER wrap in UINavigationController!
            let previewController = QLPreviewController()
            previewController.dataSource = self
            previewController.delegate = self
            previewController.currentPreviewItemIndex = 0
            previewController.modalPresentationStyle = .fullScreen

            self.isPresentingModal = true
            self.biometricLock?.suppressNextInactiveLock(forSeconds: 10.0)
            presenter.present(previewController, animated: true) { [weak self] in
                MainActor.assumeIsolated {
                    self?.emitEvent([
                        "type": "nativeUI.previewResult",
                        "reqId": reqId,
                        "ok": true,
                    ])
                }
            }
        }
    }

    private func resolveAuthenticatedMediaURL(_ raw: String) -> URL? {
        if raw.hasPrefix("http://") || raw.hasPrefix("https://") {
            return URL(string: raw)
        }
        let base = config.apiBaseURL
        if raw.hasPrefix("/") {
            return URL(string: raw, relativeTo: base)?.absoluteURL
        }
        if raw.lowercased().hasPrefix("\(MindWeaveSchemeHandler.scheme)://app/") {
            let suffix = String(raw.dropFirst("\(MindWeaveSchemeHandler.scheme)://app".count))
            return URL(string: suffix, relativeTo: base)?.absoluteURL
        }
        return URL(string: raw, relativeTo: base)?.absoluteURL
    }

    nonisolated private static func fetchMediaToTempFile(
        url: URL,
        authorization: String?,
        fallbackTitle: String?
    ) async -> (URL, URL)? {
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if let authorization, !authorization.isEmpty {
            req.setValue(authorization, forHTTPHeaderField: "Authorization")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode), !data.isEmpty else {
                return nil
            }
            let mime = (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased()
            let ext: String
            if mime.contains("png") {
                ext = "png"
            } else if mime.contains("webp") {
                ext = "webp"
            } else if mime.contains("heic") || mime.contains("heif") {
                ext = "heic"
            } else if mime.contains("pdf") {
                ext = "pdf"
            } else {
                ext = "jpg"
            }
            let tempDir = FileManager.default.temporaryDirectory
                .appendingPathComponent("mw-ql-\(UUID().uuidString)", isDirectory: true)
            try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
            let baseName = (fallbackTitle?.isEmpty == false ? "preview" : "photo")
            let localURL = tempDir.appendingPathComponent("\(baseName).\(ext)")
            try data.write(to: localURL, options: [.atomic])
            return (tempDir, localURL)
        } catch {
            return nil
        }
    }

    private func cleanupQuickLookTempDir() {
        if let dir = quickLookTempDir {
            try? FileManager.default.removeItem(at: dir)
            quickLookTempDir = nil
        }
        quickLookItems.removeAll()
    }

    func numberOfPreviewItems(in controller: QLPreviewController) -> Int {
        quickLookItems.count
    }

    func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem {
        quickLookItems[index]
    }

    func previewController(
        _ controller: QLPreviewController,
        editingModeFor previewItem: any QLPreviewItem
    ) -> QLPreviewItemEditingMode {
        .disabled
    }

    func previewControllerDidDismiss(_ controller: QLPreviewController) {
        isPresentingModal = false
        cleanupQuickLookTempDir()
        activePreviewReqId = nil
    }

    // MARK: - 3. In-App Porcelain Safari Sheet (`SFSafariViewController`)

    func openSafari(_ url: URL, reqId: String = "") {
        guard let scheme = url.scheme?.lowercased() else {
            return
        }
        guard scheme == "http" || scheme == "https" else {
            if scheme == "tel" || scheme == "mailto" || scheme == "sms" {
                UIApplication.shared.open(url, options: [:], completionHandler: nil)
            }
            return
        }
        guard !isPresentingModal, let presenter = topMostViewController() else {
            return
        }
        let safariConfig = SFSafariViewController.Configuration()
        safariConfig.entersReaderIfAvailable = false
        safariConfig.barCollapsingEnabled = true

        let safariVC = SFSafariViewController(url: url, configuration: safariConfig)
        safariVC.preferredBarTintColor = Self.porcelainBarTint
        safariVC.preferredControlTintColor = Self.warmInkTint
        safariVC.dismissButtonStyle = .close
        safariVC.modalPresentationStyle = .pageSheet

        biometricLock?.suppressNextInactiveLock(forSeconds: 8.0)
        presenter.present(safariVC, animated: true)
        if !reqId.isEmpty {
            emitEvent([
                "type": "nativeUI.safariResult",
                "reqId": reqId,
                "ok": true,
            ])
        }
    }

    // MARK: - 4. Native Action Sheets & Destructive Confirms (`UIAlertController`)

    private func handleShowSheet(_ dict: [String: Any], reqId: String) {
        guard !isPresentingModal, let presenter = topMostViewController() else {
            emitEvent([
                "type": "nativeUI.sheetResult",
                "reqId": reqId,
                "selectedId": NSNull(),
                "cancelled": true,
            ])
            return
        }

        let styleRaw = ((dict["style"] as? String) ?? "actionSheet").lowercased()
        let preferredStyle: UIAlertController.Style = (styleRaw == "alert") ? .alert : .actionSheet
        let title = (dict["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = (dict["message"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let rawItems = (dict["items"] as? [[String: Any]]) ?? []

        let alert = UIAlertController(
            title: (title?.isEmpty == false) ? title : nil,
            message: (message?.isEmpty == false) ? message : nil,
            preferredStyle: preferredStyle
        )

        var hasCancel = false
        for item in rawItems {
            let itemId = (item["id"] as? String) ?? ""
            let label = ((item["label"] as? String) ?? (item["title"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !label.isEmpty else { continue }
            let role = ((item["role"] as? String) ?? "default").lowercased()
            let actionStyle: UIAlertAction.Style
            switch role {
            case "destructive":
                actionStyle = .destructive
            case "cancel":
                actionStyle = .cancel
                hasCancel = true
            default:
                actionStyle = .default
            }
            alert.addAction(UIAlertAction(title: label, style: actionStyle) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.isPresentingModal = false
                    self.emitEvent([
                        "type": "nativeUI.sheetResult",
                        "reqId": reqId,
                        "selectedId": actionStyle == .cancel ? NSNull() : itemId,
                        "cancelled": actionStyle == .cancel,
                    ])
                }
            })
        }

        if !hasCancel {
            alert.addAction(UIAlertAction(title: "取消", style: .cancel) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.isPresentingModal = false
                    self.emitEvent([
                        "type": "nativeUI.sheetResult",
                        "reqId": reqId,
                        "selectedId": NSNull(),
                        "cancelled": true,
                    ])
                }
            })
        }

        if let popover = alert.popoverPresentationController {
            popover.sourceView = presenter.view
            popover.sourceRect = CGRect(
                x: presenter.view.bounds.midX,
                y: max(presenter.view.bounds.maxY - 88, presenter.view.bounds.midY),
                width: 1,
                height: 1
            )
            popover.permittedArrowDirections = []
        }

        isPresentingModal = true
        biometricLock?.suppressNextInactiveLock(forSeconds: 6.0)
        presenter.present(alert, animated: true)
    }

    // MARK: - 5. Native Context Menu (`UIMenu` w/ SF Symbols, <= 3 Groups, Silver Share-Hidden)

    private func handleShowContextMenu(_ dict: [String: Any], reqId: String, webView: WKWebView?) {
        let tier = ((dict["tier"] as? String) ?? "").lowercased()
        let isSilver = (tier == "silver")
        let rawItems = (dict["items"] as? [[String: Any]]) ?? []

        // Group items cleanly into <= 3 HIG groups (core, share/ai, destructive) and hide share on Silver.
        var coreActions: [UIAction] = []
        var secondaryActions: [UIAction] = []
        var destructiveActions: [UIAction] = []

        for item in rawItems {
            let itemId = (item["id"] as? String) ?? ""
            let label = ((item["label"] as? String) ?? (item["title"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !itemId.isEmpty, !label.isEmpty else { continue }
            let role = ((item["role"] as? String) ?? "default").lowercased()
            let group = ((item["group"] as? String) ?? "").lowercased()
            if isSilver && (itemId.lowercased().contains("share") || group == "share") {
                continue
            }
            let symbolName = (item["symbol"] as? String) ?? (item["icon"] as? String)
            let image = symbolName.flatMap { UIImage(systemName: $0) }
            let attrs: UIMenuElement.Attributes = (role == "destructive") ? [.destructive] : []

            let action = UIAction(title: label, image: image, attributes: attrs) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.emitEvent([
                        "type": "nativeUI.contextMenuResult",
                        "reqId": reqId,
                        "selectedId": itemId,
                        "cancelled": false,
                    ])
                }
            }

            if role == "destructive" {
                destructiveActions.append(action)
            } else if group == "secondary" || group == "ai" || group == "share" {
                secondaryActions.append(action)
            } else {
                coreActions.append(action)
            }
        }

        var menuGroups: [UIMenuElement] = []
        if !coreActions.isEmpty {
            menuGroups.append(UIMenu(title: "", options: .displayInline, children: coreActions))
        }
        if !secondaryActions.isEmpty {
            menuGroups.append(UIMenu(title: "", options: .displayInline, children: secondaryActions))
        }
        if !destructiveActions.isEmpty {
            menuGroups.append(UIMenu(title: "", options: .displayInline, children: destructiveActions))
        }

        guard !menuGroups.isEmpty else {
            emitEvent([
                "type": "nativeUI.contextMenuResult",
                "reqId": reqId,
                "selectedId": NSNull(),
                "cancelled": true,
            ])
            return
        }

        let rootMenu = UIMenu(title: "", children: menuGroups)

        // Anchor native `UIMenu` at exact card rect (zero double-haptic: system menu handles lift feedback!).
        if let webView, let rectDict = dict["rect"] as? [String: Any] {
            let rx = (rectDict["x"] as? NSNumber)?.doubleValue ?? Double(webView.bounds.midX)
            let ry = (rectDict["y"] as? NSNumber)?.doubleValue ?? Double(webView.bounds.midY)
            let rw = max(44.0, (rectDict["width"] as? NSNumber)?.doubleValue ?? 120.0)
            let rh = max(44.0, (rectDict["height"] as? NSNumber)?.doubleValue ?? 44.0)

            contextMenuAnchorButton?.removeFromSuperview()
            let anchor = UIButton(frame: CGRect(x: rx, y: ry, width: rw, height: rh))
            anchor.backgroundColor = .clear
            anchor.isAccessibilityElement = false
            anchor.showsMenuAsPrimaryAction = true
            anchor.menu = rootMenu
            webView.addSubview(anchor)
            contextMenuAnchorButton = anchor

            // Trigger system menu presentation if supported, falling back cleanly to native actionSheet
            if let interaction = anchor.contextMenuInteraction {
                _ = interaction
            }
        }

        // Present guaranteed-visible native actionSheet fallback preserving exact SF Symbols & destructive styling
        let sheetItems: [[String: Any]] = rawItems.compactMap { item in
            let itemId = (item["id"] as? String) ?? ""
            let group = ((item["group"] as? String) ?? "").lowercased()
            if isSilver && (itemId.lowercased().contains("share") || group == "share") {
                return nil
            }
            return item
        }
        var sheetDict: [String: Any] = [
            "style": "actionSheet",
            "items": sheetItems,
        ]
        if let previewText = (dict["previewText"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !previewText.isEmpty {
            sheetDict["message"] = String(previewText.prefix(80))
        }
        handleShowSheetForContextMenu(sheetDict, reqId: reqId)
    }

    private func handleShowSheetForContextMenu(_ dict: [String: Any], reqId: String) {
        guard !isPresentingModal, let presenter = topMostViewController() else {
            emitEvent([
                "type": "nativeUI.contextMenuResult",
                "reqId": reqId,
                "selectedId": NSNull(),
                "cancelled": true,
            ])
            return
        }
        let message = dict["message"] as? String
        let rawItems = (dict["items"] as? [[String: Any]]) ?? []
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .actionSheet)
        for item in rawItems {
            let itemId = (item["id"] as? String) ?? ""
            let label = ((item["label"] as? String) ?? (item["title"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !itemId.isEmpty, !label.isEmpty else { continue }
            let role = ((item["role"] as? String) ?? "default").lowercased()
            let style: UIAlertAction.Style = (role == "destructive") ? .destructive : .default
            alert.addAction(UIAlertAction(title: label, style: style) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.isPresentingModal = false
                    self.contextMenuAnchorButton?.removeFromSuperview()
                    self.contextMenuAnchorButton = nil
                    self.emitEvent([
                        "type": "nativeUI.contextMenuResult",
                        "reqId": reqId,
                        "selectedId": itemId,
                        "cancelled": false,
                    ])
                }
            })
        }
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.isPresentingModal = false
                self.contextMenuAnchorButton?.removeFromSuperview()
                self.contextMenuAnchorButton = nil
                self.emitEvent([
                    "type": "nativeUI.contextMenuResult",
                    "reqId": reqId,
                    "selectedId": NSNull(),
                    "cancelled": true,
                ])
            }
        })
        if let popover = alert.popoverPresentationController {
            popover.sourceView = presenter.view
            popover.sourceRect = CGRect(
                x: presenter.view.bounds.midX,
                y: max(presenter.view.bounds.maxY - 88, presenter.view.bounds.midY),
                width: 1,
                height: 1
            )
            popover.permittedArrowDirections = []
        }
        isPresentingModal = true
        biometricLock?.suppressNextInactiveLock(forSeconds: 6.0)
        presenter.present(alert, animated: true)
    }

    // MARK: - 6. Local-Only Expiring Sensitive Copy (Silver Privacy)

    private func handleCopySensitive(_ dict: [String: Any], reqId: String) {
        let text = ((dict["text"] as? String) ?? (dict["value"] as? String) ?? "")
        let ttlSeconds = (dict["ttlSeconds"] as? NSNumber)?.doubleValue ?? 120.0
        let item: [String: Any] = [UTType.utf8PlainText.identifier: text]
        let options: [UIPasteboard.OptionsKey: Any] = [
            .localOnly: true,
            .expirationDate: Date().addingTimeInterval(max(15.0, min(600.0, ttlSeconds))),
        ]
        UIPasteboard.general.setItems([item], options: options)
        if !reqId.isEmpty {
            emitEvent([
                "type": "nativeUI.copyResult",
                "reqId": reqId,
                "ok": true,
                "localOnly": true,
            ])
        }
    }

    // MARK: - 7. Calm Local Reminders (`UNUserNotificationCenter` — iOS 26.6 Crash-Proof!)

    private func configureNotificationCenter() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let doneAction = UNNotificationAction(
            identifier: Self.reminderActionDoneID,
            title: "记完了",
            options: [],
            icon: UNNotificationActionIcon(systemImageName: "checkmark.circle")
        )
        let snoozeAction = UNNotificationAction(
            identifier: Self.reminderActionSnoozeID,
            title: "明天再提醒",
            options: [],
            icon: UNNotificationActionIcon(systemImageName: "clock.arrow.circlepath")
        )
        let category = UNNotificationCategory(
            identifier: Self.reminderCategoryID,
            actions: [doneAction, snoozeAction],
            intentIdentifiers: [],
            hiddenPreviewsBodyPlaceholder: "你有一条心络提醒",
            options: []
        )
        center.setNotificationCategories([category])
    }

    private func handleRequestNotificationPermission(reqId: String) {
        let center = UNUserNotificationCenter.current()
        biometricLock?.suppressNextInactiveLock(forSeconds: 8.0)
        center.requestAuthorization(options: [.alert, .sound]) { [weak self] granted, error in
            let errMsg = error?.localizedDescription
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    var payload: [String: Any] = [
                        "type": "nativeUI.remindersResult",
                        "reqId": reqId,
                        "ok": true,
                        "authorized": granted,
                    ]
                    if let errMsg, !errMsg.isEmpty {
                        payload["error"] = errMsg
                    }
                    self?.emitEvent(payload)
                }
            }
        }
    }

    private func handleSyncReminders(_ dict: [String: Any], reqId: String) {
        let rawItems = (dict["items"] as? [[String: Any]]) ?? []

        // Parse & sort upcoming reminders chronologically, capping at <= 48 slots (iOS 64-slot OS cap).
        struct ParsedReminder: Sendable {
            let id: String
            let personId: String
            let title: String
            let body: String
            let fireDate: Date
            let isSilver: Bool
        }

        let isoFrac = ISO8601DateFormatter()
        isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoPlain = ISO8601DateFormatter()
        isoPlain.formatOptions = [.withInternetDateTime]

        let now = Date()
        var parsed: [ParsedReminder] = []
        for item in rawItems {
            let remId = ((item["id"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let personId = ((item["personId"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let title = ((item["title"] as? String) ?? "心络 · 重要提醒").trimmingCharacters(in: .whitespacesAndNewlines)
            let body = ((item["body"] as? String) ?? title).trimmingCharacters(in: .whitespacesAndNewlines)
            let fireStr = ((item["fireAtIso"] as? String) ?? (item["nextFireAt"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let tier = ((item["tier"] as? String) ?? "").lowercased()
            guard !remId.isEmpty,
                  let fireDate = isoFrac.date(from: fireStr) ?? isoPlain.date(from: fireStr),
                  fireDate > now else {
                continue
            }
            parsed.append(
                ParsedReminder(
                    id: remId,
                    personId: personId,
                    title: title,
                    body: body,
                    fireDate: fireDate,
                    isSilver: tier == "silver"
                )
            )
        }
        parsed.sort { $0.fireDate < $1.fireDate }
        let capped = Array(parsed.prefix(Self.maxScheduledReminders))

        UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
            let authorized = (settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional)
            guard authorized else {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        self?.emitEvent([
                            "type": "nativeUI.remindersResult",
                            "reqId": reqId,
                            "ok": true,
                            "authorized": false,
                            "scheduledCount": 0,
                        ])
                    }
                }
                return
            }

            UNUserNotificationCenter.current().getPendingNotificationRequests { existing in
                let activeCenter = UNUserNotificationCenter.current()
                let staleIds = existing
                    .map(\.identifier)
                    .filter { $0.hasPrefix(Self.reminderPrefix) }
                if !staleIds.isEmpty {
                    activeCenter.removePendingNotificationRequests(withIdentifiers: staleIds)
                }

                var scheduled = 0
                let calendar = Calendar.current
                for rem in capped {
                    let content = UNMutableNotificationContent()
                    content.title = rem.isSilver ? "心络 · 今日有一条重要提醒" : rem.title
                    content.body = rem.isSilver ? "你有一条心络提醒" : rem.body
                    content.sound = .default
                    content.badge = nil // Zero red-badge guilt!
                    content.categoryIdentifier = Self.reminderCategoryID
                    content.interruptionLevel = .active
                    content.userInfo = [
                        "reminderId": rem.id,
                        "personId": rem.personId,
                    ]

                    let comps = calendar.dateComponents(
                        [.year, .month, .day, .hour, .minute, .second],
                        from: rem.fireDate
                    )
                    let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: false)
                    let request = UNNotificationRequest(
                        identifier: "\(Self.reminderPrefix)\(rem.id)",
                        content: content,
                        trigger: trigger
                    )
                    activeCenter.add(request, withCompletionHandler: nil)
                    scheduled += 1
                }

                let finalCount = scheduled
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        self?.emitEvent([
                            "type": "nativeUI.remindersResult",
                            "reqId": reqId,
                            "ok": true,
                            "authorized": true,
                            "scheduledCount": finalCount,
                        ])
                    }
                }
            }
        }
    }

    // CRITICAL iOS 26.6 / Swift 6.0 CRASH PREVENTION:
    // ALWAYS implement ONLY the classic `nonisolated` completion-handler delegate methods
    // dispatching `completionHandler` on `DispatchQueue.main.async` — NEVER `async` overloads!
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let finishBox = UncheckedSendableBox(value: completionHandler)
        DispatchQueue.main.async {
            finishBox.value([.banner, .list, .sound])
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        let reminderId = (userInfo["reminderId"] as? String) ?? ""
        let personId = (userInfo["personId"] as? String) ?? ""
        let actionId = response.actionIdentifier
        let originalTitle = response.notification.request.content.title
        let originalBody = response.notification.request.content.body

        if actionId == Self.reminderActionSnoozeID, !reminderId.isEmpty {
            let content = UNMutableNotificationContent()
            content.title = originalTitle
            content.body = originalBody
            content.sound = .default
            content.badge = nil
            content.categoryIdentifier = Self.reminderCategoryID
            content.interruptionLevel = .active
            content.userInfo = ["reminderId": reminderId, "personId": personId]
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 86_400, repeats: false)
            let req = UNNotificationRequest(
                identifier: "\(Self.reminderPrefix)\(reminderId).snooze",
                content: content,
                trigger: trigger
            )
            center.add(req, withCompletionHandler: nil)
        }

        let finishBox = UncheckedSendableBox(value: completionHandler)
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                self?.handleNotificationInteraction(
                    actionId: actionId,
                    reminderId: reminderId,
                    personId: personId
                )
            }
            finishBox.value()
        }
    }

    private func handleNotificationInteraction(actionId: String, reminderId: String, personId: String) {
        let eventType = (actionId == UNNotificationDefaultActionIdentifier)
            ? "notification.opened"
            : "notification.action"
        emitEvent([
            "type": eventType,
            "actionId": actionId,
            "reminderId": reminderId,
            "personId": personId,
        ])
    }
}
