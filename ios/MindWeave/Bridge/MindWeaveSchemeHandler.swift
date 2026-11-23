import Foundation
import WebKit

/// Custom `WKURLSchemeHandler` serving offline bundled `WebBundle/` assets on `mindweave://app/`
/// and transparently proxying `/v1/*` and `/healthz` (including real-time SSE streams)
/// to the configured MindWeave backend server on the same origin.
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
            let disposition = MainActor.assumeIsolated { () -> URLSession.ResponseDisposition in
                guard let owner else { return .cancel }
                return owner.handleDidReceive(response: response, forTaskIdentifier: taskID)
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

    /// Cancels all in-flight upstream proxy tasks safely (e.g., on server switch or teardown).
    func cancelAllTasks() {
        let tasksToCancel: [URLSessionDataTask]
        lock.lock()
        for (schemeID, dataTask) in activeTasks {
            stoppedTasks.insert(schemeID)
            schemeTasksByDataTask.removeValue(forKey: dataTask.taskIdentifier)
            receivedResponseTasks.remove(schemeID)
        }
        tasksToCancel = Array(activeTasks.values)
        activeTasks.removeAll()
        lock.unlock()

        for task in tasksToCancel {
            task.cancel()
        }
    }

    // MARK: - WKURLSchemeHandler

    func webView(_ webView: WKWebView, start urlSchemeTask: any WKURLSchemeTask) {
        guard let requestURL = urlSchemeTask.request.url else {
            let error = URLError(.badURL)
            urlSchemeTask.didFailWithError(error)
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
        let dataTaskToCancel: URLSessionDataTask?

        lock.lock()
        dataTaskToCancel = activeTasks.removeValue(forKey: schemeID)
        if let dataTask = dataTaskToCancel {
            stoppedTasks.insert(schemeID)
            schemeTasksByDataTask.removeValue(forKey: dataTask.taskIdentifier)
            receivedResponseTasks.remove(schemeID)
        }
        lock.unlock()

        dataTaskToCancel?.cancel()
    }

    // MARK: - Local WebBundle Serving

    private func serveBundledAsset(for urlSchemeTask: any WKURLSchemeTask, requestURL: URL) {
        guard let bundleRoot = Bundle.main.url(forResource: "WebBundle", withExtension: nil)?.standardizedFileURL else {
            respondWithLocalStatus(
                urlSchemeTask: urlSchemeTask,
                url: requestURL,
                statusCode: 500,
                mimeType: "text/plain; charset=utf-8",
                body: Data("WebBundle resource missing from app bundle.".utf8)
            )
            return
        }

        var relativePath = requestURL.path
        if relativePath.isEmpty || relativePath == "/" {
            relativePath = "index.html"
        } else {
            while relativePath.hasPrefix("/") {
                relativePath.removeFirst()
            }
        }

        let candidateURL = bundleRoot.appendingPathComponent(relativePath).standardizedFileURL
        let rootPrefix = bundleRoot.path.hasSuffix("/") ? bundleRoot.path : bundleRoot.path + "/"

        // Prevent any path traversal outside WebBundle.
        guard candidateURL.path == bundleRoot.path || candidateURL.path.hasPrefix(rootPrefix) else {
            respondWithLocalStatus(
                urlSchemeTask: urlSchemeTask,
                url: requestURL,
                statusCode: 403,
                mimeType: "text/plain; charset=utf-8",
                body: Data("Forbidden".utf8)
            )
            return
        }

        var targetFileURL = candidateURL
        var isDirectory: ObjCBool = false
        if FileManager.default.fileExists(atPath: targetFileURL.path, isDirectory: &isDirectory) {
            if isDirectory.boolValue {
                targetFileURL = targetFileURL.appendingPathComponent("index.html")
            }
        } else {
            respondWithLocalStatus(
                urlSchemeTask: urlSchemeTask,
                url: requestURL,
                statusCode: 404,
                mimeType: "text/plain; charset=utf-8",
                body: Data("Not Found".utf8)
            )
            return
        }

        do {
            let fileData = try Data(contentsOf: targetFileURL)
            let mimeType = Self.mimeType(for: targetFileURL.pathExtension)
            respondWithLocalStatus(
                urlSchemeTask: urlSchemeTask,
                url: requestURL,
                statusCode: 200,
                mimeType: mimeType,
                body: fileData
            )
        } catch {
            urlSchemeTask.didFailWithError(error)
        }
    }

    private func respondWithLocalStatus(
        urlSchemeTask: any WKURLSchemeTask,
        url: URL,
        statusCode: Int,
        mimeType: String,
        body: Data
    ) {
        let headers: [String: String] = [
            "Content-Type": mimeType,
            "Content-Length": "\(body.count)",
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        ]
        if let response = HTTPURLResponse(
            url: url,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        ) {
            urlSchemeTask.didReceive(response)
            urlSchemeTask.didReceive(body)
            urlSchemeTask.didFinish()
        } else {
            urlSchemeTask.didFailWithError(URLError(.cannotParseResponse))
        }
    }

    // MARK: - Upstream API & SSE Streaming Proxy

    private func startUpstreamProxy(for urlSchemeTask: any WKURLSchemeTask, requestURL: URL) {
        guard let session = sessionHolder.session,
              let targetURL = Self.buildUpstreamURL(for: requestURL, baseURL: config.apiBaseURL) else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }

        let upstreamRequest = Self.buildUpstreamRequest(
            from: urlSchemeTask.request,
            targetURL: targetURL
        )
        let dataTask = session.dataTask(with: upstreamRequest)
        let schemeID = ObjectIdentifier(urlSchemeTask as AnyObject)
        let taskID = dataTask.taskIdentifier

        lock.lock()
        stoppedTasks.remove(schemeID)
        receivedResponseTasks.remove(schemeID)
        activeTasks[schemeID] = dataTask
        schemeTasksByDataTask[taskID] = urlSchemeTask
        schemeTaskIDsByDataTask[taskID] = schemeID
        originalURLsByDataTask[taskID] = requestURL
        lock.unlock()

        dataTask.resume()
    }

    // MARK: - URLSessionDataDelegate Handlers (MainActor)

    func handleDidReceive(
        response: URLResponse,
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
        if let httpResponse = response as? HTTPURLResponse {
            let headers = Self.filteredResponseHeaders(from: httpResponse)
            proxyResponse = HTTPURLResponse(
                url: originalURL,
                statusCode: httpResponse.statusCode,
                httpVersion: "HTTP/1.1",
                headerFields: headers
            ) ?? URLResponse(
                url: originalURL,
                mimeType: httpResponse.mimeType,
                expectedContentLength: -1,
                textEncodingName: httpResponse.textEncodingName
            )
        } else {
            proxyResponse = URLResponse(
                url: originalURL,
                mimeType: response.mimeType,
                expectedContentLength: Int(clamping: response.expectedContentLength),
                textEncodingName: response.textEncodingName
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
        var acceptsEventStream = targetURL.path.hasSuffix("/chat/stream")
            || targetURL.path.hasSuffix("/chat")
            || targetURL.path.hasSuffix("/events")

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
                upstream.setValue(value, forHTTPHeaderField: name)
            }
        }

        if !hasClientPlatform {
            upstream.setValue("ios-wkwebview", forHTTPHeaderField: "X-Client-Platform")
        }

        if acceptsEventStream {
            // Prevent gzip chunk buffering so SSE events arrive instantaneously.
            upstream.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
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
        // stripped when a non-identity Content-Encoding header is present. Preserving Content-Length
        // on uncompressed audio/* and 206 Partial Content responses keeps iOS CoreMedia seeking intact.
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
