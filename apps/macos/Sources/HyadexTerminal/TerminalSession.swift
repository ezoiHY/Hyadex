import Foundation
import Observation

/// One live attachment to a chat's tmux session over the server's PTY socket.
///
/// Output arrives as raw binary frames and goes straight into the emulator.
/// Keystrokes go back as binary frames; resize and history scrolling are JSON
/// text frames. Closing the socket only detaches: the tmux session and its
/// processes keep running on the server.
@MainActor @Observable
public final class TerminalSession {
    public enum Phase: Equatable, Sendable {
        case idle
        case connecting
        case connected
        /// The shell exited, so tmux ended the session.
        case ended
        case failed(String)
    }

    public private(set) var phase: Phase = .idle
    @ObservationIgnored public let emulator: TerminalEmulator
    /// Called after new output was applied; the view redraws.
    @ObservationIgnored public var onUpdate: (() -> Void)?

    @ObservationIgnored private let makeRequest: (_ columns: Int, _ rows: Int) -> URLRequest?
    @ObservationIgnored private var socket: URLSessionWebSocketTask?
    @ObservationIgnored private var loop: Task<Void, Never>?
    @ObservationIgnored private var resizeTask: Task<Void, Never>?
    @ObservationIgnored private var size = (columns: 100, rows: 30)

    public init(makeRequest: @escaping (_ columns: Int, _ rows: Int) -> URLRequest?) {
        self.makeRequest = makeRequest
        emulator = TerminalEmulator(columns: 100, rows: 30)
        emulator.onResponse = { [weak self] data in self?.send(data) }
    }

    public var isActive: Bool { phase == .connecting || phase == .connected }

    public func connect(columns: Int, rows: Int) {
        size = (max(columns, 2), max(rows, 1))
        guard loop == nil else { return }
        loop = Task { [weak self] in await self?.run() }
    }

    public func disconnect() {
        loop?.cancel()
        loop = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        if phase != .ended { phase = .idle }
    }

    /// Reattach after the session ended or failed; tmux starts a fresh shell
    /// only when the previous session is gone.
    public func reconnect() {
        disconnect()
        connect(columns: size.columns, rows: size.rows)
    }

    public func send(_ data: Data) {
        guard phase == .connected, let socket, !data.isEmpty else { return }
        // The server accepts at most 1 MiB per frame.
        var offset = 0
        while offset < data.count {
            let chunk = data.subdata(in: offset..<min(offset + 512 * 1024, data.count))
            socket.send(.data(chunk)) { _ in }
            offset += chunk.count
        }
    }

    public func send(_ text: String) { send(Data(text.utf8)) }

    /// Debounced: live window drags would otherwise send a resize per frame.
    public func resize(columns: Int, rows: Int) {
        let next = (max(columns, 2), max(rows, 1))
        guard next != size else { return }
        size = next
        emulator.resize(columns: next.0, rows: next.1)
        onUpdate?()
        resizeTask?.cancel()
        resizeTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(100))
            guard !Task.isCancelled, let self else { return }
            self.control(["type": "resize", "columns": next.0, "rows": next.1])
        }
    }

    /// Scrolls tmux history on the server; negative is up.
    public func scroll(lines: Int) {
        guard lines != 0 else { return }
        control(["type": "scroll", "delta": max(-80, min(80, lines))])
    }

    private func control(_ object: [String: Any]) {
        guard phase == .connected, let socket,
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        socket.send(.string(text)) { _ in }
    }

    private func run() async {
        var delay = 0.5
        while !Task.isCancelled {
            guard let request = makeRequest(size.columns, size.rows) else {
                phase = .failed("终端不可用")
                return
            }
            phase = .connecting
            let task = URLSession.shared.webSocketTask(with: request)
            task.maximumMessageSize = 4 * 1024 * 1024
            socket = task
            task.resume()
            var attached = false
            var serverError: String?
            while !Task.isCancelled {
                let message: URLSessionWebSocketTask.Message
                do { message = try await task.receive() } catch { break }
                switch message {
                case .data(let bytes):
                    emulator.feed(bytes)
                    onUpdate?()
                case .string(let text):
                    let object = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any]
                    switch object?["type"] as? String {
                    case "ready":
                        attached = true
                        delay = 0.5
                        phase = .connected
                        emulator.restart(columns: size.columns, rows: size.rows)
                        // The view may have measured itself while connecting.
                        if object?["columns"] as? Int != size.columns || object?["rows"] as? Int != size.rows {
                            control(["type": "resize", "columns": size.columns, "rows": size.rows])
                        }
                        onUpdate?()
                    case "error":
                        serverError = object?["message"] as? String
                    default: break
                    }
                @unknown default: break
                }
            }
            let code = task.closeCode.rawValue
            if socket === task { socket = nil }
            if Task.isCancelled { return }
            switch code {
            case 1000 where attached:
                phase = .ended
                loop = nil
                return
            case 4401:
                phase = .failed("没有权限打开这个终端"); loop = nil; return
            case 4404:
                phase = .failed("这个聊天已不存在"); loop = nil; return
            case 4409:
                phase = .failed("已归档的聊天不能打开终端"); loop = nil; return
            default:
                phase = .failed(serverError ?? "终端连接已断开，正在重新连接…")
            }
            try? await Task.sleep(for: .seconds(Double.random(in: delay / 2...delay)))
            delay = min(delay * 2, 10)
        }
    }
}
