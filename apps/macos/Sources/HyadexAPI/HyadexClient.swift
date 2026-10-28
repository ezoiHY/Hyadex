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

    /// Sends a message. `command` is the opaque id and inventory revision of
    /// the slash command or skill the message starts with.
    public func sendTurn(_ id: String, prompt: String, requestID: String, capabilities: [String],
                         command: (id: String, revision: String)? = nil, fileIDs: [String] = []) async throws -> JSON {
        var body: [String: JSON] = [
            "prompt": .string(prompt),
            "client_request_id": .string(requestID),
            "client_capabilities": .array(capabilities.map(JSON.string)),
        ]
        if !fileIDs.isEmpty { body["file_ids"] = .array(fileIDs.map(JSON.string)) }
        if let command {
            body["skill_selection"] = ["id": .string(command.id), "revision": .string(command.revision)]
        }
        return try await post("api/sessions/\(id)/turns", .object(body))
    }

    // MARK: Native goals, commands and MCP

    /// Commands and skills the chat's agent offers in its folder.
    public func providerCommands(_ id: String, refresh: Bool = false) async throws -> JSON {
        try await request("GET", "api/sessions/\(id)/provider-commands",
                          query: refresh ? ["refresh": "true"] : [:], timeout: 60)
    }

    /// Sets Claude Code's `/goal` completion condition; returns the Claude runtime.
    public func setClaudeGoal(_ id: String, condition: String) async throws -> JSON {
        try await request("PUT", "api/sessions/\(id)/claude/goal", body: ["condition": .string(condition)], timeout: 60)
    }

    public func clearClaudeGoal(_ id: String) async throws -> JSON {
        try await request("DELETE", "api/sessions/\(id)/claude/goal", timeout: 60)
    }

    public func codexGoal(_ id: String) async throws -> JSON {
        try await get("api/sessions/\(id)/codex/goal")
    }

    /// Creates or changes Codex's persistent goal. Only the given fields
    /// change; `.null` removes a budget.
    public func setCodexGoal(_ id: String, _ fields: JSON) async throws -> JSON {
        try await request("PUT", "api/sessions/\(id)/codex/goal", body: fields, timeout: 60)
    }

    public func clearCodexGoal(_ id: String) async throws -> JSON {
        try await request("DELETE", "api/sessions/\(id)/codex/goal", timeout: 60)
    }

    /// The MCP servers the chat's agent loaded. `backend` is "claude" or "codex".
    public func mcpStatus(_ id: String, backend: String) async throws -> JSON {
        try await request("GET", "api/sessions/\(id)/\(backend)/mcp", timeout: 60)
    }

    /// `action` is reconnect, reconnect_all, enable or disable (Claude only).
    public func controlClaudeMCP(_ id: String, action: String, server: String?, generation: String) async throws -> JSON {
        var body: [String: JSON] = ["version": 1, "action": .string(action), "expected_generation": .string(generation)]
        if let server { body["server_name"] = .string(server) }
        // Reconnecting every failed server takes ~10 s each; the server bounds it at 135 s.
        return try await request("POST", "api/sessions/\(id)/claude/mcp", body: .object(body),
                                 timeout: action == "reconnect_all" ? 150 : 60)
    }

    public func stop(_ id: String) async throws {
        _ = try await post("api/sessions/\(id)/stop")
    }

    /// Events before `before` (exclusive seq), newest last; `events_omitted_before` counts what is older still.
    public func sessionPage(_ id: String, before: Int, limit: Int = 400) async throws -> JSON {
        try await get("api/sessions/\(id)", query: ["before": String(before), "limit": String(limit)])
    }

    /// Deletes a chat: stops its turn, clears its queue, terminal and jobs.
    public func deleteSession(_ id: String) async throws {
        _ = try await request("DELETE", "api/sessions/\(id)", timeout: 60)
    }

    /// Uploads one file to the chat; returns its record (`id`, `filename`, `size`, `content_type`).
    public func uploadFile(_ id: String, fileURL: URL) async throws -> JSON {
        let boundary = "hyadex-" + UUID().uuidString
        var request = URLRequest(url: endpoint.baseURL.appending(path: "api/sessions/\(id)/files"))
        request.httpMethod = "POST"
        request.timeoutInterval = 600
        if let token, !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let name = fileURL.lastPathComponent.replacingOccurrences(of: "\"", with: "_")
        let head = "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\n"
            + "Content-Type: application/octet-stream\r\n\r\n"
        let bodyFile = FileManager.default.temporaryDirectory.appending(path: boundary)
        FileManager.default.createFile(atPath: bodyFile.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: bodyFile) }
        // Stream the file into the body on disk so large files never sit in memory.
        let writer = try FileHandle(forWritingTo: bodyFile)
        writer.write(Data(head.utf8))
        let reader = try FileHandle(forReadingFrom: fileURL)
        while let chunk = try reader.read(upToCount: 1 << 20), !chunk.isEmpty { writer.write(chunk) }
        try reader.close()
        writer.write(Data("\r\n--\(boundary)--\r\n".utf8))
        try writer.close()
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.upload(for: request, fromFile: bodyFile)
        } catch {
            throw HyadexClientError.unreachable(error.localizedDescription)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONDecoder().decode(JSON.self, from: data)) ?? .null
        if status == 401 || status == 403 { throw HyadexClientError.unauthorized }
        guard (200..<300).contains(status) else { throw HyadexClientError.http(status, json["detail"].string) }
        return json["file"]
    }

    /// Server-side folders under (or starting with) `path`, for picking a working directory.
    public func completeDirectory(_ path: String, limit: Int = 50) async throws -> JSON {
        try await get("api/working-directories/complete", query: ["path": path, "limit": String(limit)])
    }

    /// Force Send: steers the running turn natively, or stops it and runs this message next.
    public func runQueuedNow(_ id: String, queuedID: String) async throws -> JSON {
        try await request("POST", "api/sessions/\(id)/queue/\(queuedID)/run-now",
                          body: ["accept_deferred_queue_response": true], timeout: 60)
    }

    /// Releases the hold Stop put on queued messages, so the queue continues in order.
    public func resumeQueue(_ id: String) async throws -> JSON {
        try await post("api/sessions/\(id)/queue/resume")
    }

    /// Moves a queued message one place up or down.
    public func moveQueued(_ id: String, queuedID: String, direction: String, expectedAdjacent: String?) async throws {
        var body: [String: JSON] = ["direction": .string(direction)]
        if let expectedAdjacent { body["expected_adjacent_queued_id"] = .string(expectedAdjacent) }
        _ = try await post("api/sessions/\(id)/queue/\(queuedID)/move", .object(body))
    }

    /// Starts Codex's native context compaction (idle chats only).
    public func compactCodex(_ id: String) async throws -> JSON {
        try await post("api/sessions/\(id)/codex/compact")
    }

    /// Acknowledges an emergency alert and every earlier one of the chat.
    public func acknowledgeEmergency(_ id: String, throughAlertID: String) async throws {
        _ = try await post("api/sessions/\(id)/emergency/acknowledge", ["through_alert_id": .string(throughAlertID)])
    }

    /// Marks a chat unread until it is read again.
    public func markUnread(_ id: String) async throws {
        _ = try await post("api/sessions/\(id)/unread")
    }

    /// Forks a chat with its history (up to its last finished turn); returns the new chat.
    public func fork(_ id: String, title: String? = nil) async throws -> JSON {
        try await request("POST", "api/sessions/\(id)/fork",
                          body: title.map { ["title": .string($0)] } ?? [:], timeout: 120)["session"]
    }

    /// Full-text search over every chat's messages (newest hit per chat).
    public func search(_ query: String, limit: Int = 30) async throws -> JSON {
        try await get("api/search", query: ["q": query, "limit": String(limit)])
    }

    /// The chat's subagents: Claude's Task agents and Codex's collaborators.
    public func subagents(_ id: String) async throws -> JSON {
        try await get("api/sessions/\(id)/subagents")
    }

    // MARK: Side chat (native: Claude Code's /btw, a Codex fork); privileged header only

    public func sideChat(_ id: String) async throws -> JSON {
        try await request("GET", "api/sessions/\(id)/side-chat", nativeToken: true)
    }

    public func askSideChat(_ id: String, requestID: String, question: String, sideChatID: String,
                            after: String?) async throws -> JSON {
        try await request("POST", "api/sessions/\(id)/side-chat", body: [
            "request_id": .string(requestID), "question": .string(question),
            "side_chat_id": .string(sideChatID), "after_request_id": after.map(JSON.string) ?? .null,
        ], timeout: 30, nativeToken: true)
    }

    public func cancelSideQuestion(_ id: String, requestID: String) async throws -> JSON {
        try await request("DELETE", "api/sessions/\(id)/side-chat/requests/\(requestID)", nativeToken: true)
    }

    public func clearSideChat(_ id: String, sideChatID: String) async throws -> JSON {
        try await request("DELETE", "api/sessions/\(id)/side-chat/\(sideChatID)", nativeToken: true)
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

    public func groups(includeArchived: Bool = false) async throws -> [JSON] {
        try await get("api/groups", query: includeArchived ? ["include_archived": "true"] : [:])["groups"].array
    }

    /// Renames (`title`) or archives (`archived`) a group; returns the group.
    public func updateGroup(_ id: String, _ body: JSON) async throws -> JSON {
        try await request("PATCH", "api/groups/\(id)", body: body)["group"]
    }

    /// Deletes a group and its jobs; its member chats are archived.
    public func deleteGroup(_ id: String) async throws {
        _ = try await request("DELETE", "api/groups/\(id)", timeout: 60)
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

    /// Sets a native goal on one member (Claude `/goal` or Codex thread goal).
    public func setGroupGoal(_ id: String, handle: String, objective: String) async throws -> JSON {
        try await request("PUT", "api/groups/\(id)/goal",
                          body: ["handle": .string(handle), "objective": .string(objective)], timeout: 60)
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

    /// Creates a job for a chat (`session_id` in the body) or a group (`groupID`).
    public func createJob(_ body: JSON, groupID: String? = nil) async throws -> JSON {
        try await post(groupID.map { "api/groups/\($0)/jobs" } ?? "api/jobs", body)["job"]
    }

    public func deleteJob(_ id: String, groupID: String? = nil) async throws {
        _ = try await request("DELETE", groupID.map { "api/groups/\($0)/jobs/\(id)" } ?? "api/jobs/\(id)")
    }

    public func runJob(_ id: String) async throws {
        _ = try await post("api/jobs/\(id)/run")
    }
}
