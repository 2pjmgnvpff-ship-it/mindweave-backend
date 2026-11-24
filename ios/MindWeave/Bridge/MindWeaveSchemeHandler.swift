import Foundation
import os
import WebKit

/// Strictly `Sendable` immutable cached bundle asset payload for off-main-thread serving.
fileprivate struct CachedBundleAsset: Sendable {
    let data: Data
    let mimeType: String
    let cacheControl: String
}

/// Strictly `Sendable` local HTTP response payload computed off `@MainActor` and handed back
/// to `@MainActor` where non-`Sendable` `any WKURLSchemeTask` is invoked safely.
fileprivate struct LocalSchemePayload: Sendable {
    let statusCode: Int
    let headers: [String: String]
    let body: Data
}

/// Strictly `Sendable` HTTP/URL response snapshot extracted off `@MainActor` inside `URLSessionDataDelegate`
/// so non-`Sendable` `URLResponse` never crosses into `MainActor.assumeIsolated` under Swift 6.0 `complete`.
fileprivate struct UpstreamResponseSnapshot: Sendable {
    let isHTTP: Bool
    let statusCode: Int
    let headers: [String: String]
    let mimeType: String?
    let expectedContentLength: Int
    let textEncodingName: String?
}

/// Custom `WKURLSchemeHandler` serving offline bundled `WebBundle/` assets on `mindweave://app/`,
/// native voice `.m4a` clips on `mindweave://app/__native_audio/<id>.m4a` (with RFC 9110 `206`
/// Range support), and transparently proxying `/v1/*` and `/healthz` (including SSE streams and
/// HTTP `206 Partial Content` media streaming) to the configured MindWeave backend server.
@MainActor
final class MindWeaveSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "mindweave"
    static let appHost = "app"
    static let entryURL = URL(string: "mindweave://app/")!

    /// Non-isolated owner of `URLSession` so `deinit` cleanly invalidates sockets without
    /// touching `@MainActor`-isolated properties or introducing retain cycles.
    private final class SessionHolder: @unchecked Sendable {
        var session: URLSession?

        deinit {
            session?.invalidateAndCancel()
        }
    }

    /// Weak delegate proxy that breaks the `URLSession -> delegate` retain cycle and
    /// forwards `URLSessionDataDelegate` callbacks synchronously on `OperationQueue.main`.
    private final class SessionDelegateProxy: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        weak var owner: MindWeaveSchemeHandler?

        func urlSession(
            _ session: URLSession,
            dataTask: URLSessionDataTask,
            didReceive response: URLResponse,
            completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
        ) {
            let taskID = dataTask.taskIdentifier
            let snapshot: UpstreamResponseSnapshot
            if let httpResponse = response as? HTTPURLResponse {
                snapshot = UpstreamResponseSnapshot(
                    isHTTP: true,
                    statusCode: httpResponse.statusCode,
                    headers: MindWeaveSchemeHandler.filteredResponseHeaders(from: httpResponse),
                    mimeType: httpResponse.mimeType,
                    expectedContentLength: -1,
                    textEncodingName: httpResponse.textEncodingName
                )
            } else {
                snapshot = UpstreamResponseSnapshot(
                    isHTTP: false,
                    statusCode: 200,
                    headers: [:],
                    mimeType: response.mimeType,
                    expectedContentLength: Int(clamping: response.expectedContentLength),
                    textEncodingName: response.textEncodingName
                )
            }
            let disposition = MainActor.assumeIsolated { () -> URLSession.ResponseDisposition in
                guard let owner else { return .cancel }
                return owner.handleDidReceive(responseSnapshot: snapshot, forTaskIdentifier: taskID)
            }
            completionHandler(disposition)
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            let taskID = dataTask.taskIdentifier
            MainActor.assumeIsolated {
                owner?.handleDidReceive(data: data, forTaskIdentifier: taskID)
            }
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            let taskID = task.taskIdentifier
            MainActor.assumeIsolated {
                owner?.handleDidComplete(taskIdentifier: taskID, error: error)
            }
        }
    }

    private let config: ServerConfigStore
    private let bundleRootURL: URL?
    private let localIOQueue = DispatchQueue(
        label: "app.mindweave.scheme.io",
        qos: .userInitiated,
        attributes: .concurrent
    )
    private let assetCache = OSAllocatedUnfairLock<[String: CachedBundleAsset]>(initialState: [:])
    private static let maxCachedAssetBytes = 256 * 1024

    // Strictly @MainActor-pinned non-Sendable WKURLSchemeTask maps (zero cross-actor capture!).
    private var pendingLocalTasks: [ObjectIdentifier: any WKURLSchemeTask] = [:]
    private var pendingProxyPreTasks: [ObjectIdentifier: any WKURLSchemeTask] = [:]

    private let lock = NSLock()
    private var activeTasks: [ObjectIdentifier: URLSessionDataTask] = [:]
    private var schemeTasksByDataTask: [Int: any WKURLSchemeTask] = [:]
    private var schemeTaskIDsByDataTask: [Int: ObjectIdentifier] = [:]
    private var originalURLsByDataTask: [Int: URL] = [:]
    private var stoppedTasks: Set<ObjectIdentifier> = []
    private var receivedResponseTasks: Set<ObjectIdentifier> = []

    private let delegateProxy: SessionDelegateProxy
    private let sessionHolder = SessionHolder()

    init(config: ServerConfigStore) {
        self.config = config
        self.bundleRootURL = Bundle.main.url(forResource: "WebBundle", withExtension: nil)?.standardizedFileURL
        self.delegateProxy = SessionDelegateProxy()
        super.init()
        self.delegateProxy.owner = self

        let sessionConfig = URLSessionConfiguration.default
        sessionConfig.requestCachePolicy = .reloadIgnoringLocalCacheData
        sessionConfig.urlCache = nil
        sessionConfig.waitsForConnectivity = false
        sessionConfig.timeoutIntervalForRequest = 120
        sessionConfig.timeoutIntervalForResource = 300
        sessionConfig.httpShouldSetCookies = true
        sessionConfig.httpCookieAcceptPolicy = .always

        self.sessionHolder.session = URLSession(
            configuration: sessionConfig,
            delegate: delegateProxy,
            delegateQueue: .main
        )
    }

    /// Cancels all in-flight upstream and local tasks and purges all 8 tracking maps (`02d LK-9`, `BC5 BOMB-IOS-3`).
    func cancelAllTasks() {
        pendingLocalTasks.removeAll()
        pendingProxyPreTasks.removeAll()

        let tasksToCancel: [URLSessionDataTask]
        lock.lock()
        tasksToCancel = Array(activeTasks.values)
        activeTasks.removeAll()
        schemeTasksByDataTask.removeAll()
        schemeTaskIDsByDataTask.removeAll()
        originalURLsByDataTask.removeAll()
        stoppedTasks.removeAll()
        receivedResponseTasks.removeAll()
        lock.unlock()

        for task in tasksToCancel {
            task.cancel()
        }
    }

    // MARK: - WKURLSchemeHandler

    func webView(_ webView: WKWebView, start urlSchemeTask: any WKURLSchemeTask) {
        guard let requestURL = urlSchemeTask.request.url else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }

        if requestURL.path.hasPrefix("/__native_audio/") {
            serveNativeAudio(for: urlSchemeTask, requestURL: requestURL)
            return
        }

        if Self.shouldProxy(path: requestURL.path) {
            startUpstreamProxy(for: urlSchemeTask, requestURL: requestURL)
        } else {
            serveBundledAsset(for: urlSchemeTask, requestURL: requestURL)
        }
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: any WKURLSchemeTask) {
        let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
        pendingLocalTasks.removeValue(forKey: schemeID)
        pendingProxyPreTasks.removeValue(forKey: schemeID)

        let dataTaskToCancel: URLSessionDataTask?
        lock.lock()
        dataTaskToCancel = activeTasks.removeValue(forKey: schemeID)
        if let dataTask = dataTaskToCancel {
            stoppedTasks.insert(schemeID)
            schemeTasksByDataTask.removeValue(forKey: dataTask.taskIdentifier)
            schemeTaskIDsByDataTask.removeValue(forKey: dataTask.taskIdentifier)
            originalURLsByDataTask.removeValue(forKey: dataTask.taskIdentifier)
            receivedResponseTasks.remove(schemeID)
        }
        lock.unlock()

        dataTaskToCancel?.cancel()
    }

    // MARK: - Local Native Audio Serving (/__native_audio/<id>.m4a, RFC 9110 Range 206 Support)

    private func serveNativeAudio(for urlSchemeTask: any WKURLSchemeTask, requestURL: URL) {
        let filename = String(requestURL.path.dropFirst("/__native_audio/".count))
        guard filename.hasSuffix(".m4a") else {
            deliverLocalPayload(
                LocalSchemePayload(
                    statusCode: 400,
                    headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"],
                    body: Data("Invalid audio path".utf8)
                ),
                to: urlSchemeTask,
                url: requestURL
            )
            return
        }
        let rawID = String(filename.dropLast(4))
        let safeID = NativeAudioStore.sanitizeID(rawID)
        guard !safeID.isEmpty, safeID == rawID else {
            deliverLocalPayload(
                LocalSchemePayload(
                    statusCode: 400,
                    headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"],
                    body: Data("Invalid session ID".utf8)
                ),
                to: urlSchemeTask,
                url: requestURL
            )
            return
        }

        let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
        let rangeHeader = urlSchemeTask.request.value(forHTTPHeaderField: "Range")
        pendingLocalTasks[schemeID] = urlSchemeTask

        localIOQueue.async { [weak self] in
            let payload: LocalSchemePayload
            if let audioData = NativeAudioStore.shared.read(sessionId: safeID) {
                payload = Self.makeRangeAwarePayload(
                    data: audioData,
                    mimeType: "audio/mp4",
                    cacheControl: "no-store",
                    rangeHeader: rangeHeader
                )
            } else {
                payload = LocalSchemePayload(
                    statusCode: 404,
                    headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"],
                    body: Data("Not Found".utf8)
                )
            }
            Task { @MainActor [weak self] in
                guard let self,
                      let liveTask = self.pendingLocalTasks.removeValue(forKey: schemeID) else {
                    return
                }
                self.deliverLocalPayload(payload, to: liveTask, url: requestURL)
            }
        }
    }

    // MARK: - Off-Main-Thread Local WebBundle Serving (02d CB-1/CB-2 + 02f CI-4)

    private func serveBundledAsset(for urlSchemeTask: any WKURLSchemeTask, requestURL: URL) {
        guard let bundleRoot = bundleRootURL else {
            deliverLocalPayload(
                LocalSchemePayload(
                    statusCode: 500,
                    headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"],
                    body: Data("WebBundle resource missing from app bundle.".utf8)
                ),
                to: urlSchemeTask,
                url: requestURL
            )
            return
        }

        let rawPath = requestURL.path
        let normalizedPath: String
        if rawPath.isEmpty || rawPath == "/" {
            normalizedPath = "index.html"
        } else {
            let stripped = String(rawPath.drop(while: { $0 == "/" }))
            normalizedPath = stripped.isEmpty ? "index.html" : stripped
        }

        // Fast synchronous cache hit (0 disk syscalls, 0 actor hops!).
        if let cached = assetCache.withLock({ $0[normalizedPath] }) {
            let payload = LocalSchemePayload(
                statusCode: 200,
                headers: [
                    "Content-Type": cached.mimeType,
                    "Content-Length": "\(cached.data.count)",
                    "Cache-Control": cached.cacheControl,
                    "Access-Control-Allow-Origin": "*",
                ],
                body: cached.data
            )
            deliverLocalPayload(payload, to: urlSchemeTask, url: requestURL)
            return
        }

        // Cache miss: pin non-Sendable WKURLSchemeTask on @MainActor and read disk on I/O queue.
        let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
        pendingLocalTasks[schemeID] = urlSchemeTask
        let cache = assetCache
        let maxCached = Self.maxCachedAssetBytes

        localIOQueue.async { [weak self] in
            let payload = Self.loadBundledAssetOffMain(
                bundleRoot: bundleRoot,
                relativePath: normalizedPath,
                cache: cache,
                maxCachedBytes: maxCached
            )
            Task { @MainActor [weak self] in
                guard let self,
                      let liveTask = self.pendingLocalTasks.removeValue(forKey: schemeID) else {
                    return
                }
                self.deliverLocalPayload(payload, to: liveTask, url: requestURL)
            }
        }
    }

    nonisolated private static func loadBundledAssetOffMain(
        bundleRoot: URL,
        relativePath: String,
        cache: OSAllocatedUnfairLock<[String: CachedBundleAsset]>,
        maxCachedBytes: Int
    ) -> LocalSchemePayload {
        let candidateURL = bundleRoot.appendingPathComponent(relativePath).standardizedFileURL
        let rootPrefix = bundleRoot.path.hasSuffix("/") ? bundleRoot.path : bundleRoot.path + "/"

        guard candidateURL.path == bundleRoot.path || candidateURL.path.hasPrefix(rootPrefix) else {
            return LocalSchemePayload(
                statusCode: 403,
                headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"],
                body: Data("Forbidden".utf8)
            )
        }

        var targetFileURL = candidateURL
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: targetFileURL.path, isDirectory: &isDirectory) else {
            return LocalSchemePayload(
                statusCode: 404,
                headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"],
                body: Data("Not Found".utf8)
            )
        }
        if isDirectory.boolValue {
            targetFileURL = targetFileURL.appendingPathComponent("index.html")
        }

        guard let fileData = try? Data(contentsOf: targetFileURL) else {
            return LocalSchemePayload(
                statusCode: 500,
                headers: ["Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"],
                body: Data("Failed to read bundled asset.".utf8)
            )
        }

        let mime = mimeType(for: targetFileURL.pathExtension)
        let isImmutable = relativePath.hasPrefix("scripts/")
            || relativePath.hasPrefix("styles/")
            || relativePath.hasPrefix("assets/")
        let cacheControl = isImmutable ? "public, max-age=31536000, immutable" : "no-cache"

        if fileData.count <= maxCachedBytes {
            let entry = CachedBundleAsset(data: fileData, mimeType: mime, cacheControl: cacheControl)
            cache.withLock { dict in
                dict[relativePath] = entry
            }
        }

        return LocalSchemePayload(
            statusCode: 200,
            headers: [
                "Content-Type": mime,
                "Content-Length": "\(fileData.count)",
                "Cache-Control": cacheControl,
                "Access-Control-Allow-Origin": "*",
            ],
            body: fileData
        )
    }

    private func deliverLocalPayload(
        _ payload: LocalSchemePayload,
        to urlSchemeTask: any WKURLSchemeTask,
        url: URL
    ) {
        if let response = HTTPURLResponse(
            url: url,
            statusCode: payload.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: payload.headers
        ) {
            urlSchemeTask.didReceive(response)
            if !payload.body.isEmpty {
                urlSchemeTask.didReceive(payload.body)
            }
            urlSchemeTask.didFinish()
        } else {
            urlSchemeTask.didFailWithError(URLError(.cannotParseResponse))
        }
    }

    /// Slices an in-memory media buffer per RFC 9110 `Range: bytes=start-end` (`206` / `416` / `200`).
    nonisolated fileprivate static func makeRangeAwarePayload(
        data: Data,
        mimeType: String,
        cacheControl: String,
        rangeHeader: String?
    ) -> LocalSchemePayload {
        let total = data.count
        var baseHeaders: [String: String] = [
            "Content-Type": mimeType,
            "Accept-Ranges": "bytes",
            "Cache-Control": cacheControl,
            "Access-Control-Allow-Origin": "*",
        ]

        guard let rangeHeader = rangeHeader?.trimmingCharacters(in: .whitespacesAndNewlines),
              rangeHeader.lowercased().hasPrefix("bytes="),
              total > 0 else {
            baseHeaders["Content-Length"] = "\(total)"
            return LocalSchemePayload(statusCode: 200, headers: baseHeaders, body: data)
        }

        let spec = String(rangeHeader.dropFirst(6)).split(separator: ",").first.map(String.init) ?? ""
        let parts = spec.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 2 else {
            baseHeaders["Content-Length"] = "\(total)"
            return LocalSchemePayload(statusCode: 200, headers: baseHeaders, body: data)
        }

        let start: Int
        let end: Int
        if parts[0].isEmpty {
            guard let suffixLen = Int(parts[1]), suffixLen > 0 else {
                baseHeaders["Content-Range"] = "bytes */\(total)"
                baseHeaders["Content-Length"] = "0"
                return LocalSchemePayload(statusCode: 416, headers: baseHeaders, body: Data())
            }
            start = max(0, total - suffixLen)
            end = total - 1
        } else {
            guard let parsedStart = Int(parts[0]), parsedStart >= 0 else {
                baseHeaders["Content-Range"] = "bytes */\(total)"
                baseHeaders["Content-Length"] = "0"
                return LocalSchemePayload(statusCode: 416, headers: baseHeaders, body: Data())
            }
            start = parsedStart
            if parts[1].isEmpty {
                end = total - 1
            } else if let parsedEnd = Int(parts[1]) {
                end = min(parsedEnd, total - 1)
            } else {
                baseHeaders["Content-Range"] = "bytes */\(total)"
                baseHeaders["Content-Length"] = "0"
                return LocalSchemePayload(statusCode: 416, headers: baseHeaders, body: Data())
            }
        }

        guard start < total && start <= end else {
            baseHeaders["Content-Range"] = "bytes */\(total)"
            baseHeaders["Content-Length"] = "0"
            return LocalSchemePayload(statusCode: 416, headers: baseHeaders, body: Data())
        }

        let slice = data.subdata(in: start..<(end + 1))
        baseHeaders["Content-Range"] = "bytes \(start)-\(end)/\(total)"
        baseHeaders["Content-Length"] = "\(slice.count)"
        return LocalSchemePayload(statusCode: 206, headers: baseHeaders, body: slice)
    }

    // MARK: - Upstream API & SSE Streaming Proxy (Off-Main Body Drain — BC5 BOMB-IOS-2 + 02f CI-4)

    private func startUpstreamProxy(for urlSchemeTask: any WKURLSchemeTask, requestURL: URL) {
        guard let targetURL = Self.buildUpstreamURL(for: requestURL, baseURL: config.apiBaseURL) else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }

        let reqSnapshot: URLRequest = urlSchemeTask.request
        if reqSnapshot.httpBody == nil && reqSnapshot.httpBodyStream != nil {
            // Pin non-Sendable WKURLSchemeTask on @MainActor; copy Sendable URLRequest value to drain stream off-main!
            let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
            pendingProxyPreTasks[schemeID] = urlSchemeTask
            localIOQueue.async { [weak self] in
                let upstreamRequest = Self.buildUpstreamRequest(from: reqSnapshot, targetURL: targetURL)
                Task { @MainActor [weak self] in
                    guard let self,
                          let liveTask = self.pendingProxyPreTasks.removeValue(forKey: schemeID) else {
                        return
                    }
                    self.launchUpstreamDataTask(
                        upstreamRequest: upstreamRequest,
                        for: liveTask,
                        originalURL: requestURL
                    )
                }
            }
            return
        }

        let upstreamRequest = Self.buildUpstreamRequest(from: reqSnapshot, targetURL: targetURL)
        launchUpstreamDataTask(
            upstreamRequest: upstreamRequest,
            for: urlSchemeTask,
            originalURL: requestURL
        )
    }

    private func launchUpstreamDataTask(
        upstreamRequest: URLRequest,
        for urlSchemeTask: any WKURLSchemeTask,
        originalURL: URL
    ) {
        guard let session = sessionHolder.session else {
            urlSchemeTask.didFailWithError(URLError(.cancelled))
            return
        }
        let dataTask = session.dataTask(with: upstreamRequest)
        let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
        let taskID = dataTask.taskIdentifier

        lock.lock()
        stoppedTasks.remove(schemeID)
        receivedResponseTasks.remove(schemeID)
        activeTasks[schemeID] = dataTask
        schemeTasksByDataTask[taskID] = urlSchemeTask
        schemeTaskIDsByDataTask[taskID] = schemeID
        originalURLsByDataTask[taskID] = originalURL
        lock.unlock()

        dataTask.resume()
    }

    // MARK: - URLSessionDataDelegate Handlers (MainActor)

    fileprivate func handleDidReceive(
        responseSnapshot: UpstreamResponseSnapshot,
        forTaskIdentifier taskIdentifier: Int
    ) -> URLSession.ResponseDisposition {
        let schemeTask: (any WKURLSchemeTask)?
        let originalURL: URL?

        lock.lock()
        if let schemeID = schemeTaskIDsByDataTask[taskIdentifier],
           !stoppedTasks.contains(schemeID),
           activeTasks[schemeID] != nil,
           let task = schemeTasksByDataTask[taskIdentifier] {
            receivedResponseTasks.insert(schemeID)
            schemeTask = task
            originalURL = originalURLsByDataTask[taskIdentifier]
        } else {
            schemeTask = nil
            originalURL = nil
        }
        lock.unlock()

        guard let schemeTask, let originalURL else {
            return .cancel
        }

        let proxyResponse: URLResponse
        if responseSnapshot.isHTTP {
            proxyResponse = HTTPURLResponse(
                url: originalURL,
                statusCode: responseSnapshot.statusCode,
                httpVersion: "HTTP/1.1",
                headerFields: responseSnapshot.headers
            ) ?? URLResponse(
                url: originalURL,
                mimeType: responseSnapshot.mimeType,
                expectedContentLength: -1,
                textEncodingName: responseSnapshot.textEncodingName
            )
        } else {
            proxyResponse = URLResponse(
                url: originalURL,
                mimeType: responseSnapshot.mimeType,
                expectedContentLength: responseSnapshot.expectedContentLength,
                textEncodingName: responseSnapshot.textEncodingName
            )
        }

        schemeTask.didReceive(proxyResponse)
        return .allow
    }

    func handleDidReceive(data: Data, forTaskIdentifier taskIdentifier: Int) {
        guard !data.isEmpty else { return }

        let schemeTask: (any WKURLSchemeTask)?
        lock.lock()
        if let schemeID = schemeTaskIDsByDataTask[taskIdentifier],
           !stoppedTasks.contains(schemeID),
           activeTasks[schemeID] != nil,
           receivedResponseTasks.contains(schemeID) {
            schemeTask = schemeTasksByDataTask[taskIdentifier]
        } else {
            schemeTask = nil
        }
        lock.unlock()

        schemeTask?.didReceive(data)
    }

    func handleDidComplete(taskIdentifier: Int, error: Error?) {
        let schemeTask: (any WKURLSchemeTask)?
        let wasStopped: Bool

        lock.lock()
        let schemeID = schemeTaskIDsByDataTask.removeValue(forKey: taskIdentifier)
        originalURLsByDataTask.removeValue(forKey: taskIdentifier)
        let candidateTask = schemeTasksByDataTask.removeValue(forKey: taskIdentifier)
        if let schemeID {
            wasStopped = stoppedTasks.remove(schemeID) != nil
            activeTasks.removeValue(forKey: schemeID)
            receivedResponseTasks.remove(schemeID)
        } else {
            wasStopped = true
        }
        schemeTask = wasStopped ? nil : candidateTask
        lock.unlock()

        guard let schemeTask else {
            return
        }

        if let error {
            schemeTask.didFailWithError(error)
        } else {
            schemeTask.didFinish()
        }
    }

    // MARK: - Pure Request/Response & MIME Helpers

    nonisolated static func shouldProxy(path: String) -> Bool {
        return path == "/v1"
            || path.hasPrefix("/v1/")
            || path == "/healthz"
            || path == "/readyz"
            || path.hasPrefix("/api/")
            || path.hasPrefix("/dev/")
    }

    nonisolated static func buildUpstreamURL(for requestURL: URL, baseURL: URL) -> URL? {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            return nil
        }
        var basePath = components.path
        while basePath.hasSuffix("/") {
            basePath.removeLast()
        }
        let incomingPath = requestURL.path.hasPrefix("/") ? requestURL.path : "/" + requestURL.path
        components.path = basePath + incomingPath
        components.percentEncodedQuery = URLComponents(
            url: requestURL,
            resolvingAgainstBaseURL: false
        )?.percentEncodedQuery
        return components.url
    }

    nonisolated static func buildUpstreamRequest(from originalRequest: URLRequest, targetURL: URL) -> URLRequest {
        var upstream = URLRequest(url: targetURL)
        upstream.httpMethod = originalRequest.httpMethod ?? "GET"
        upstream.cachePolicy = .reloadIgnoringLocalCacheData

        let skippedRequestHeaders: Set<String> = [
            "host",
            "origin",
            "referer",
            "connection",
            "content-length",
            "transfer-encoding",
            "accept-encoding",
        ]

        var hasClientPlatform = false
        var hasRangeOrMediaHeader = false
        let pathLower = targetURL.path.lowercased()
        var acceptsEventStream = pathLower.hasSuffix("/chat/stream")
            || pathLower.hasSuffix("/chat")
            || pathLower.hasSuffix("/events")
        let isBinaryMediaEndpoint = pathLower.hasSuffix("/audio")
            || pathLower.hasSuffix("/media")
            || pathLower.hasSuffix("/download")

        if let headers = originalRequest.allHTTPHeaderFields {
            for (name, value) in headers {
                let lower = name.lowercased()
                if skippedRequestHeaders.contains(lower) {
                    continue
                }
                if lower == "x-client-platform" {
                    hasClientPlatform = true
                }
                if lower == "accept" && value.lowercased().contains("text/event-stream") {
                    acceptsEventStream = true
                }
                if lower == "range" || lower == "if-range" {
                    hasRangeOrMediaHeader = true
                }
                upstream.setValue(value, forHTTPHeaderField: name)
            }
        }

        if !hasClientPlatform {
            upstream.setValue("ios-wkwebview", forHTTPHeaderField: "X-Client-Platform")
        }

        // BC5 BOMB-IOS-1a: Force `Accept-Encoding: identity` for SSE, `/audio`, `/media`, and ANY
        // `Range`/`If-Range` request so URLSession auto-gzip never strips `Content-Length` or breaks `206`!
        if acceptsEventStream || isBinaryMediaEndpoint || hasRangeOrMediaHeader {
            upstream.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        }

        if acceptsEventStream {
            upstream.timeoutInterval = 300
        } else {
            upstream.timeoutInterval = 60
        }

        if let bodyData = extractBodyData(from: originalRequest) {
            upstream.httpBody = bodyData
        }

        return upstream
    }

    nonisolated static func extractBodyData(from request: URLRequest) -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }
        stream.open()
        defer { stream.close() }

        var data = Data()
        let chunkSize = 65_536
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: chunkSize)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let bytesRead = stream.read(buffer, maxLength: chunkSize)
            if bytesRead > 0 {
                data.append(buffer, count: bytesRead)
            } else {
                break
            }
        }
        return data
    }

    nonisolated static func filteredResponseHeaders(from httpResponse: HTTPURLResponse) -> [String: String] {
        // URLSession transparently decompresses encoded bodies, so Content-Length must ONLY be
        // stripped when a non-identity Content-Encoding header is present. Preserving Content-Length,
        // Content-Range, and Accept-Ranges on uncompressed audio/* and 206 Partial Content responses
        // keeps iOS CoreMedia seeking intact.
        var hasNonIdentityEncoding = false
        for (rawKey, rawValue) in httpResponse.allHeaderFields {
            guard let key = rawKey as? String, let value = rawValue as? String else {
                continue
            }
            if key.lowercased() == "content-encoding" {
                let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                if !normalized.isEmpty && normalized != "identity" {
                    hasNonIdentityEncoding = true
                }
            }
        }

        var strippedHeaders: Set<String> = [
            "content-encoding",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "upgrade",
        ]
        if hasNonIdentityEncoding {
            strippedHeaders.insert("content-length")
        }

        var result: [String: String] = [:]
        for (rawKey, rawValue) in httpResponse.allHeaderFields {
            guard let key = rawKey as? String, let value = rawValue as? String else {
                continue
            }
            if strippedHeaders.contains(key.lowercased()) {
                continue
            }
            result[key] = value
        }
        return result
    }

    nonisolated static func mimeType(for pathExtension: String) -> String {
        switch pathExtension.lowercased() {
        case "html", "htm":
            return "text/html; charset=utf-8"
        case "js", "mjs":
            return "application/javascript; charset=utf-8"
        case "css":
            return "text/css; charset=utf-8"
        case "webp":
            return "image/webp"
        case "png":
            return "image/png"
        case "svg":
            return "image/svg+xml"
        case "jpg", "jpeg":
            return "image/jpeg"
        case "gif":
            return "image/gif"
        case "ico":
            return "image/x-icon"
        case "webmanifest":
            return "application/manifest+json; charset=utf-8"
        case "json":
            return "application/json; charset=utf-8"
        case "wav":
            return "audio/wav"
        case "webm":
            return "audio/webm"
        case "mp3":
            return "audio/mpeg"
        case "m4a", "mp4":
            return "audio/mp4"
        case "ogg":
            return "audio/ogg"
        case "woff2":
            return "font/woff2"
        case "woff":
            return "font/woff"
        case "ttf":
            return "font/ttf"
        case "txt":
            return "text/plain; charset=utf-8"
        default:
            return "application/octet-stream"
        }
    }
}
