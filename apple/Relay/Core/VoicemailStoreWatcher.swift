import Foundation
#if canImport(CoreServices)
import CoreServices
#endif

/// A coalescing wake-up: any number of `signal()` calls before a `wait`
/// collapse into one wake. One waiter at a time (the relay run loop).
public final class WakeSignal: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = false
    private var waiter: (id: UUID, continuation: CheckedContinuation<Bool, Never>)?

    public init() {}

    public func signal() {
        lock.lock()
        if let w = waiter {
            waiter = nil
            lock.unlock()
            w.continuation.resume(returning: true)
        } else {
            pending = true
            lock.unlock()
        }
    }

    /// Returns true when signalled, false on timeout.
    public func wait(timeout: TimeInterval) async -> Bool {
        let id = UUID()
        return await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            lock.lock()
            if pending {
                pending = false
                lock.unlock()
                cont.resume(returning: true)
                return
            }
            waiter = (id, cont)
            lock.unlock()
            DispatchQueue.global().asyncAfter(deadline: .now() + max(0, timeout)) { [weak self] in
                self?.expire(id)
            }
        }
    }

    private func expire(_ id: UUID) {
        lock.lock()
        guard let w = waiter, w.id == id else { lock.unlock(); return }
        waiter = nil
        lock.unlock()
        w.continuation.resume(returning: false)
    }
}

/// Watches the voicemail store's directory with FSEvents and signals when a
/// file of the store family changes (`<name>`, `<name>-wal`, `<name>-shm`).
///
/// FSEvents on the directory is used instead of a `DispatchSource` on one file
/// because Core Data checkpoints and recreates the write-ahead log, so a
/// per-file watcher would lose its target. Events are coalesced by the stream
/// latency. Without CoreServices the watcher is a no-op and the relay's poll
/// interval still drives capture.
public final class VoicemailStoreWatcher: @unchecked Sendable {
    public let directory: URL
    public let filePrefix: String
    public let signal: WakeSignal
    private let latency: TimeInterval
    private let queue = DispatchQueue(label: "nervecenter.relay.voicemail-watcher")
    #if canImport(CoreServices)
    private var stream: FSEventStreamRef?
    #endif

    public init(
        storeURL: URL, signal: WakeSignal = WakeSignal(), latency: TimeInterval = 1.0
    ) {
        self.directory = storeURL.deletingLastPathComponent()
        self.filePrefix = storeURL.lastPathComponent
        self.signal = signal
        self.latency = latency
    }

    deinit { stop() }

    /// True when a changed path belongs to the store family.
    func matches(_ path: String) -> Bool {
        let name = (path as NSString).lastPathComponent
        return name == filePrefix || name == filePrefix + "-wal" || name == filePrefix + "-shm"
    }

    /// Start watching. Returns false when watching is unavailable.
    @discardableResult
    public func start() -> Bool {
        #if canImport(CoreServices)
        guard stream == nil else { return true }
        var context = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, _, _ in
            guard let info else { return }
            let watcher = Unmanaged<VoicemailStoreWatcher>.fromOpaque(info).takeUnretainedValue()
            let list = unsafeBitCast(paths, to: NSArray.self) as? [String] ?? []
            if list.prefix(count).contains(where: { watcher.matches($0) }) {
                watcher.signal.signal()
            }
        }
        let flags = UInt32(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagUseCFTypes)
        guard let s = FSEventStreamCreate(
            nil, callback, &context, [directory.path] as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow), latency, flags
        ) else { return false }
        FSEventStreamSetDispatchQueue(s, queue)
        guard FSEventStreamStart(s) else {
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
            return false
        }
        stream = s
        return true
        #else
        return false
        #endif
    }

    public func stop() {
        #if canImport(CoreServices)
        guard let s = stream else { return }
        stream = nil
        FSEventStreamStop(s)
        FSEventStreamInvalidate(s)
        FSEventStreamRelease(s)
        #endif
    }
}
