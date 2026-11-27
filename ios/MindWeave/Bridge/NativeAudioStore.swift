import Foundation
import os

/// Thread-safe, disk-backed `.m4a` + sidecar metadata store for native iOS voice recordings.
///
/// Recordings are stored inside `Application Support/PendingVoiceCaptures/` with
/// `FileProtectionType.completeUntilFirstUserAuthentication` (and excluded from iCloud backup)
/// so unuploaded elevator/subway voice slips survive WKWebView jetsam and WebKit 7-day
/// script-storage eviction until JS confirms HTTP 2xx via `mwSpeech.acknowledgeDraftUploaded`.
final class NativeAudioStore: @unchecked Sendable {
    static let shared = NativeAudioStore()

    private struct Entry: Sendable {
        let fileURL: URL
        let sidecarURL: URL
        let createdAt: Date
    }

    private let lock = OSAllocatedUnfairLock<[String: Entry]>(initialState: [:])
    private let storeDirectory: URL
    private let ttlSeconds: TimeInterval = 7 * 24 * 60 * 60 // 7 days max retention

    private init() {
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.temporaryDirectory
        let dir = appSupport.appendingPathComponent("PendingVoiceCaptures", isDirectory: true)
        self.storeDirectory = dir
        Self.ensureProtectedDirectory(dir)
        bootstrapExistingEntries()
        pruneExpired()
    }

    /// Allocates a protected file URL for writing a new `.m4a` recording.
    func makeRecordingURL(sessionId: String) -> URL {
        let safeID = Self.sanitizeID(sessionId)
        Self.ensureProtectedDirectory(storeDirectory)
        return storeDirectory.appendingPathComponent("\(safeID).m4a")
    }

    /// Registers a finalized `.m4a` recording (and optional sidecar metadata JSON) and returns
    /// its same-origin `mindweave://app/__native_audio/<id>.m4a` fetch URL.
    func register(
        sessionId: String,
        fileURL: URL,
        metadata: [String: Any]? = nil
    ) -> String {
        let safeID = Self.sanitizeID(sessionId)
        let sidecarURL = storeDirectory.appendingPathComponent("\(safeID).json")

        Self.applyFileProtection(fileURL)
        if let metadata,
           let jsonData = try? JSONSerialization.data(withJSONObject: metadata, options: []) {
            try? jsonData.write(to: sidecarURL, options: [.atomic])
            Self.applyFileProtection(sidecarURL)
        }

        let entry = Entry(fileURL: fileURL, sidecarURL: sidecarURL, createdAt: Date())
        lock.withLock { entries in
            entries[safeID] = entry
        }
        return "mindweave://app/__native_audio/\(safeID).m4a"
    }

    /// Reads the `.m4a` bytes for `MindWeaveSchemeHandler` WITHOUT deleting the backstop file.
    /// Deletion happens strictly via `acknowledgeUploaded(sessionId:)` or `discard(sessionId:)`.
    func read(sessionId: String) -> Data? {
        let safeID = Self.sanitizeID(sessionId)
        guard !safeID.isEmpty else { return nil }
        let fileURL: URL = lock.withLock { entries in
            entries[safeID]?.fileURL ?? storeDirectory.appendingPathComponent("\(safeID).m4a")
        }
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            return nil
        }
        return try? Data(contentsOf: fileURL, options: [.mappedIfSafe])
    }

    /// Permanently removes the backstop `.m4a` and sidecar JSON after JS confirms HTTP 2xx receipt.
    func acknowledgeUploaded(sessionId: String) {
        discard(sessionId: sessionId)
    }

    /// Immediately deletes an aborted/cancelled or confirmed recording and its sidecar JSON.
    func discard(sessionId: String) {
        let safeID = Self.sanitizeID(sessionId)
        guard !safeID.isEmpty else { return }
        let removed = lock.withLock { entries in
            entries.removeValue(forKey: safeID)
        }
        let m4aURL = removed?.fileURL ?? storeDirectory.appendingPathComponent("\(safeID).m4a")
        let sidecarURL = removed?.sidecarURL ?? storeDirectory.appendingPathComponent("\(safeID).json")
        try? FileManager.default.removeItem(at: m4aURL)
        try? FileManager.default.removeItem(at: sidecarURL)
    }

    /// Purges expired recordings older than `ttlSeconds` (called at boot and periodically).
    func pruneExpired() {
        let cutoff = Date().addingTimeInterval(-ttlSeconds)
        let staleURLs: [URL] = lock.withLock { entries in
            var collected: [URL] = []
            for (key, entry) in entries where entry.createdAt < cutoff {
                entries.removeValue(forKey: key)
                collected.append(entry.fileURL)
                collected.append(entry.sidecarURL)
            }
            return collected
        }
        for url in staleURLs {
            try? FileManager.default.removeItem(at: url)
        }
    }

    // MARK: - Helpers

    private func bootstrapExistingEntries() {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: storeDirectory,
            includingPropertiesForKeys: [.creationDateKey],
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        var loaded: [String: Entry] = [:]
        for url in urls where url.pathExtension.lowercased() == "m4a" {
            let safeID = Self.sanitizeID(url.deletingPathExtension().lastPathComponent)
            guard !safeID.isEmpty else { continue }
            let created = (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
            let sidecar = storeDirectory.appendingPathComponent("\(safeID).json")
            loaded[safeID] = Entry(fileURL: url, sidecarURL: sidecar, createdAt: created)
        }
        let loadedSnapshot = loaded
        lock.withLock { entries in
            entries.merge(loadedSnapshot) { current, _ in current }
        }
    }

    static func sanitizeID(_ raw: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let cleaned = raw.unicodeScalars.filter { allowed.contains($0) }
        return String(String.UnicodeScalarView(cleaned).prefix(64))
    }

    private static func ensureProtectedDirectory(_ dir: URL) {
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(
                at: dir,
                withIntermediateDirectories: true,
                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
            )
        }
        var mutableDir = dir
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try? mutableDir.setResourceValues(resourceValues)
    }

    private static func applyFileProtection(_ url: URL) {
        try? FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
            ofItemAtPath: url.path
        )
        var mutableURL = url
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try? mutableURL.setResourceValues(resourceValues)
    }
}
