import Foundation

/// A Hyadex server the app can talk to (LAN or Tailscale).
public struct ServerEndpoint: Identifiable, Hashable, Codable, Sendable {
    public var id: UUID
    public var name: String
    public var baseURL: URL

    public init(id: UUID = UUID(), name: String, baseURL: URL) {
        self.id = id
        self.name = name
        self.baseURL = baseURL
    }
}

/// The subset of `GET /api/health` the client relies on.
public struct HealthResponse: Sendable {
    public let raw: JSON

    public var version: String? { raw["server_version"].string }
    public var apiContractVersion: Int? { raw["api_contract_version"].int }
    public var serverName: String? { raw["server_name"].string }
    public var defaultCwd: String? { raw["default_cwd"].string }
    public var supportsGroups: Bool { !raw["capabilities"]["group_discussions_v1"].isNull }
    public var supportsAutopilot: Bool { !raw["capabilities"]["autopilot_v1"].isNull }
}

public enum HyadexClientError: LocalizedError, Sendable {
    case unauthorized
    case http(Int, String?)
    case unreachable(String)
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .unauthorized: "令牌无效或缺失"
        case .http(let code, let detail): detail.map { "服务器返回 \(code)：\($0)" } ?? "服务器返回 HTTP \(code)"
        case .unreachable(let reason): "无法连接：\(reason)"
        case .invalidResponse: "服务器返回了无法解析的内容"
        }
    }
}

/// REST client. The token travels only in the Authorization header.
public struct HyadexClient: Sendable {
    public let endpoint: ServerEndpoint
    let token: String?
    private let session: URLSession

    public init(endpoint: ServerEndpoint, token: String?, session: URLSession = .shared) {
        self.endpoint = endpoint
        self.token = token
        self.session = session
    }

    // MARK: Transport

    public func request(_ method: String, _ path: String, query: [String: String] = [:],
                        body: JSON? = nil, timeout: TimeInterval = 20, nativeToken: Bool = false) async throws -> JSON {
        var components = URLComponents(url: endpoint.baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = timeout
        if let token, !token.isEmpty {
            // Privileged native controls accept only their own header, never a bearer.
            if nativeToken {
                request.setValue(token, forHTTPHeaderField: "X-Hyadex-Token")
            } else {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw HyadexClientError.unreachable(error.localizedDescription)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONDecoder().decode(JSON.self, from: data)) ?? .null
        if status == 401 || status == 403 { throw HyadexClientError.unauthorized }
        guard (200..<300).contains(status) else {
            let detail = json["detail"].string ?? json["detail"]["message"].string
            throw HyadexClientError.http(status, detail)
        }
        return json
    }

    /// An authenticated WebSocket request; the token stays in a header.
    public func webSocketRequest(_ path: String, query: [String: String] = [:], protocols: [String]) -> URLRequest {
        var components = URLComponents(url: endpoint.baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        if !query.isEmpty {
            components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue(protocols.joined(separator: ", "), forHTTPHeaderField: "Sec-WebSocket-Protocol")
        return request
    }

    func get(_ path: String, query: [String: String] = [:]) async throws -> JSON {
        try await request("GET", path, query: query)
    }

    func post(_ path: String, _ body: JSON = [:]) async throws -> JSON {
        try await request("POST", path, body: body)
    }

    // MARK: Server

    public func health() async throws -> HealthResponse {
        HealthResponse(raw: try await get("api/health"))
    }

    /// Native models and reasoning efforts per backend (probes the CLIs; may take seconds).
    public func runtimeCatalog(refresh: Bool = false) async throws -> JSON {
        try await request("GET", "api/runtime/catalog", query: refresh ? ["refresh": "true"] : [:], timeout: 90)
    }

    /// The provider account's plan usage. Claude's first read starts a short
    /// native CLI connection, so allow it some time.
    public func providerUsage(backend: String, refresh: Bool = false) async throws -> JSON {
        var query = ["backend": backend]
        if refresh { query["refresh"] = "true" }
        return try await request("GET", "api/runtime/usage", query: query, timeout: 60, nativeToken: true)
    }

    // MARK: Chats

    /// Provider runtime state of a chat (context usage, permission modes …).
    public func runtime(_ id: String, backend: String) async throws -> JSON {
        try await get("api/sessions/\(id)/\(backend)/runtime")
    }

    /// Bytes of a file the chat uploaded or published (`file_id`), or of a
    /// registered artifact a message links to (`target`).
    public func fileData(_ id: String, fileID: String? = nil, target: String? = nil) async throws -> Data {
        var components: URLComponents
        if let fileID {
            components = URLComponents(url: endpoint.baseURL.appending(path: "api/sessions/\(id)/files/\(fileID)"),
                                       resolvingAgainstBaseURL: false)!
        } else {
            components = URLComponents(url: endpoint.baseURL.appending(path: "api/sessions/\(id)/links/file"),
                                       resolvingAgainstBaseURL: false)!
            components.queryItems = [URLQueryItem(name: "target", value: target ?? "")]
        }
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 30
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw HyadexClientError.invalidResponse }
        return data
    }

    public func sessions() async throws -> [JSON] {
        try await get("api/sessions")["sessions"].array
    }

    public func createSession(_ body: JSON) async throws -> JSON {
        try await post("api/sessions", body)["session"]
    }

    public func updateSession(_ id: String, _ body: JSON) async throws -> JSON {
        try await request("PATCH", "api/sessions/\(id)", body: body)["session"]
    }

    /// Session record, queued turns and a page of events after `after`.
    public func session(_ id: String, after: Int = 0, limit: Int = 500) async throws -> JSON {
        try await get("api/sessions/\(id)", query: ["after": String(after), "limit": String(limit), "tail": "false"])
    }

    /// The most recent events of a session (for opening long chats quickly).
    public func sessionTail(_ id: String, limit: Int = 400) async throws -> JSON {
        try await get("api/sessions/\(id)", query: ["limit": String(limit)])
    }

    public func sendTurn(_ id: String, prompt: String, requestID: String, capabilities: [String]) async throws -> JSON {
        try await post("api/sessions/\(id)/turns", [
            "prompt": .string(prompt),
            "client_request_id": .string(requestID),
            "client_capabilities": .array(capabilities.map(JSON.string)),
        ])
    }

    public func stop(_ id: String) async throws {
        _ = try await post("api/sessions/\(id)/stop")
    }

    public func markRead(_ id: String) async throws {
        _ = try await post("api/sessions/\(id)/read")
    }

    public func deleteQueued(_ id: String, queuedID: String) async throws {
        _ = try await request("DELETE", "api/sessions/\(id)/queue/\(queuedID)")
    }

    /// Answers a pending approval. `backend` is "claude" or "codex".
    public func resolveInteraction(_ id: String, backend: String, interactionID: String, decision: String) async throws {
        _ = try await post("api/sessions/\(id)/\(backend)/interactions/\(interactionID)/resolve",
                           ["response": ["decision": .string(decision)]])
    }

    /// Unified diff of the files a run changed.
    public func diff(_ id: String, runID: String) async throws -> String {
        var request = URLRequest(url: endpoint.baseURL.appending(path: "api/sessions/\(id)/diffs/\(runID)"))
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw HyadexClientError.invalidResponse }
        return String(decoding: data, as: UTF8.self)
    }

    // MARK: Terminal (one persistent tmux session per chat)

    public func terminalSocketRequest(_ id: String, columns: Int, rows: Int) -> URLRequest {
        webSocketRequest("api/sessions/\(id)/terminal/ws",
                         query: ["columns": String(columns), "rows": String(rows)],
                         protocols: ["hyadex-terminal-v1"])
    }

    public func terminalWindows(_ id: String) async throws -> JSON {
        try await get("api/sessions/\(id)/terminal/windows")
    }

    public func terminalAction(_ id: String, action: String, target: String? = nil) async throws -> JSON {
        var body: [String: JSON] = ["action": .string(action)]
        if let target { body["target"] = .string(target) }
        return try await post("api/sessions/\(id)/terminal/action", .object(body))
    }

    public func killTerminal(_ id: String) async throws {
        _ = try await request("DELETE", "api/sessions/\(id)/terminal")
    }

    // MARK: Groups

    public func groups() async throws -> [JSON] {
        try await get("api/groups")["groups"].array
    }

    public func group(_ id: String) async throws -> JSON {
        try await get("api/groups/\(id)")["group"]
    }

    public func createGroup(_ body: JSON) async throws -> JSON {
        try await post("api/groups", body)["group"]
    }

    public func groupEvents(_ id: String, after: Int = 0, limit: Int = 500) async throws -> [JSON] {
        try await get("api/groups/\(id)/events", query: ["after": String(after), "limit": String(limit)])["events"].array
    }

    public func postGroupMessage(_ id: String, text: String, mode: String, analysisOnly: Bool) async throws -> JSON {
        try await post("api/groups/\(id)/messages",
                       ["text": .string(text), "mode": .string(mode), "analysis_only": .bool(analysisOnly)])
    }

    /// Adds a member; returns the updated group.
    public func addParticipant(_ id: String, _ spec: JSON) async throws -> JSON {
        try await post("api/groups/\(id)/participants", spec)["group"]
    }

    public func removeParticipant(_ id: String, handle: String) async throws {
        _ = try await request("DELETE", "api/groups/\(id)/participants/\(handle)")
    }

    public func stopGroup(_ id: String) async throws {
        _ = try await post("api/groups/\(id)/stop")
    }

    // MARK: Autopilot

    public func runs() async throws -> [JSON] {
        try await get("api/autopilot/runs")["runs"].array
    }

    public func run(_ id: String) async throws -> JSON {
        try await get("api/autopilot/runs/\(id)")["run"]
    }

    public func createRun(_ body: JSON) async throws -> JSON {
        try await post("api/autopilot/runs", body)["run"]
    }

    /// `action` is approve, pause, resume or abort.
    public func runAction(_ id: String, _ action: String) async throws -> JSON {
        try await post("api/autopilot/runs/\(id)/\(action)")["run"]
    }

    public func runFeedback(_ id: String, text: String) async throws -> JSON {
        try await post("api/autopilot/runs/\(id)/feedback", ["text": .string(text)])["run"]
    }

    // MARK: Jobs

    public func jobs() async throws -> [JSON] {
        try await get("api/jobs")["jobs"].array
    }

    public func updateJob(_ id: String, _ body: JSON) async throws -> JSON {
        try await request("PATCH", "api/jobs/\(id)", body: body)
    }

    public func runJob(_ id: String) async throws {
        _ = try await post("api/jobs/\(id)/run")
    }
}
