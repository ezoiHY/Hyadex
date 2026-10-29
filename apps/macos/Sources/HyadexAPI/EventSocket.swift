import Foundation

/// One event stream (a chat's or a group's). Replays everything after the
/// last applied `seq`, then delivers live events; on disconnect it reconnects
/// with backoff and resumes after the last `seq` it delivered, so events are
/// never repeated or skipped.
public final class EventSocket: @unchecked Sendable {
    public enum Frame: Sendable {
        case event(JSON)    // has a `seq`
        case control(JSON)  // stream_ready, liveness, reasoning lanes…
    }

    private let client: HyadexClient
    private let path: String
    private let protocolName: String
    private let extraQuery: [String: String]
    private let lock = NSLock()
    private var lastSeq: Int
    private var task: Task<Void, Never>?
    /// The server bounds each live snapshot to about a million characters.
    static let maximumMessageSize = 16 * 1024 * 1024

    /// - Parameters:
    ///   - path: e.g. `api/sessions/<id>/events` or `api/groups/<id>/events/stream`.
    ///   - after: the last `seq` already applied by the caller.
    public init(client: HyadexClient, path: String, after: Int, query: [String: String] = [:],
                protocolName: String = "hyadex-events-v1") {
        self.client = client
        self.path = path
        self.lastSeq = after
        self.extraQuery = query
        self.protocolName = protocolName
    }

    deinit { task?.cancel() }

    public func start(onFrame: @escaping @Sendable (Frame) async -> Void) {
        task?.cancel()
        task = Task { [weak self] in
            var delay: Double = 0.5
            while !Task.isCancelled {
                guard let self else { return }
                let delivered = await self.runOnce(onFrame: onFrame)
                if Task.isCancelled { return }
                delay = delivered ? 0.5 : min(delay * 2, 30)
                try? await Task.sleep(for: .seconds(Double.random(in: delay / 2...delay)))
            }
        }
    }

    public func stop() {
        task?.cancel()
        task = nil
    }

    private var resumeSeq: Int {
        lock.lock(); defer { lock.unlock() }
        return lastSeq
    }

    /// Runs one connection until it closes. Returns whether any frame arrived.
    private func runOnce(onFrame: @escaping @Sendable (Frame) async -> Void) async -> Bool {
        var components = URLComponents(url: client.endpoint.baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.queryItems = [URLQueryItem(name: "after", value: String(resumeSeq))]
            + extraQuery.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        if let token = client.token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue(protocolName, forHTTPHeaderField: "Sec-WebSocket-Protocol")
        let socket = URLSession.shared.webSocketTask(with: request)
        // Live reasoning and answer snapshots can exceed the 1 MiB default.
        socket.maximumMessageSize = Self.maximumMessageSize
        socket.resume()
        defer { socket.cancel(with: .goingAway, reason: nil) }
        var received = false
        while !Task.isCancelled {
            let message: URLSessionWebSocketTask.Message
            do {
                // receive() does not observe Task cancellation. Closing the
                // socket ends it, so stop() releases the server subscription
                // at once instead of at the next frame.
                message = try await withTaskCancellationHandler {
                    try await socket.receive()
                } onCancel: {
                    socket.cancel(with: .goingAway, reason: nil)
                }
            } catch {
                return received
            }
            let data: Data
            switch message {
            case .string(let text): data = Data(text.utf8)
            case .data(let bytes): data = bytes
            @unknown default: continue
            }
            guard let json = try? JSONDecoder().decode(JSON.self, from: data) else { continue }
            received = true
            if let seq = json["seq"].int {
                let fresh: Bool = {
                    lock.lock(); defer { lock.unlock() }
                    guard seq > lastSeq else { return false }
                    lastSeq = seq
                    return true
                }()
                if fresh { await onFrame(.event(json)) }
            } else {
                await onFrame(.control(json))
            }
        }
        return received
    }
}
