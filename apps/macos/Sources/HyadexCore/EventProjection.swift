import Foundation
import HyadexAPI

/// Turns a chat's raw event log into timeline items, incrementally.
/// Feed events in `seq` order with `apply`; duplicates and stale events are ignored.
public struct SessionProjection: Sendable {
    public struct PendingApproval: Hashable, Sendable {
        public let id: String
        public let decisions: [String]
    }

    public let backend: AgentBackend
    public let cwd: String
    public private(set) var items: [TimelineItem] = []
    public private(set) var lastSeq = 0
    public private(set) var running = false
    public private(set) var pending: [String: PendingApproval] = [:]
    public private(set) var queued: [QueuedMessage] = []
    public private(set) var lastAssistantText: String?
    public private(set) var lastActivity: Date?
    /// `code_diff` items whose unified diff has not been fetched yet: item id → run id.
    public private(set) var diffsToLoad: [String: String] = [:]
    /// When the running turn started (nil when idle).
    public private(set) var turnStartedAt: Date?
    /// How long each finished turn took, keyed by the id of its user message.
    public private(set) var turnSeconds: [String: Int] = [:]
    /// `client_request_id`s the server has accepted (turn started or queued).
    public private(set) var acceptedRequests: Set<String> = []
    private var turnUserItem: String?

    private var index: [String: Int] = [:]
    private var toolStarts: [String: Date] = [:]
    /// Uploaded files by id, to name a turn's attachments.
    private var uploads: [String: MediaFile] = [:]
    private var lastPublication: String?
    private var decided: [String: ApprovalDecision] = [:]

    public init(backend: AgentBackend, cwd: String) {
        self.backend = backend
        self.cwd = cwd
    }

    var agent: Author { .agent(backend.rawValue, backend) }

    public var needsAction: Bool { !pending.isEmpty }

    public mutating func apply(_ events: [JSON]) {
        for event in events { apply(event) }
    }

    public mutating func apply(_ event: JSON) {
        guard let seq = event["seq"].int, seq > lastSeq else { return }
        lastSeq = seq
        let id = event["id"].string ?? "seq-\(seq)"
        let time = event["ts"].date ?? .now
        switch event["type"].string ?? "" {
        case "turn_started":
            running = true
            lastActivity = time
            turnStartedAt = time
            turnUserItem = id
            if let request = event["client_request_id"].string { acceptedRequests.insert(request) }
            if let queuedID = event["queued_id"].string { queued.removeAll { $0.id == queuedID } }
            let prompt = event["display_prompt"].text ?? event["prompt"].text ?? ""
            if !prompt.isEmpty { append(.message(id: id, author: .user, text: prompt, time: time)) }
            let attached = (event["display_file_ids"].array.isEmpty ? event["file_ids"] : event["display_file_ids"])
                .array.compactMap(\.string)
            if !attached.isEmpty {
                append(.media(id: id + "-files", author: .user, files: attached.map {
                    uploads[$0] ?? MediaFile(id: $0, name: $0, contentType: "")
                }))
            }
        case "file_uploaded":
            if let file = Self.mediaFile(event["file"]) { uploads[file.id] = file }
        case "artifact_created":
            guard let file = Self.mediaFile(event["artifact"]) else { return }
            lastActivity = time
            // One publication of several files shows as one group.
            if case .media(let previousID, let author, let files)? = items.last, author.kind == .agent,
               let publication = event["publication_id"].string, publication == lastPublication {
                replace(previousID, with: .media(id: previousID, author: author, files: files + [file]))
            } else {
                append(.media(id: id, author: agent, files: [file]))
            }
            lastPublication = event["publication_id"].string
        case "assistant_text":
            guard let text = event["text"].text else { return }
            lastAssistantText = text
            lastActivity = time
            append(.message(id: id, author: agent, text: text, time: time))
        case "reasoning_summary", "reasoning_text":
            guard let text = event["text"].text else { return }
            if case .thinking(let previousID, let seconds, let previous)? = items.last {
                replace(previousID, with: .thinking(id: previousID, seconds: seconds, text: previous + "\n\n" + text))
            } else {
                append(.thinking(id: id, seconds: 0, text: text))
            }
        case "tool_started":
            let tool = event["tool"]
            guard let toolID = tool["id"].string else { return }
            toolStarts[toolID] = time
            let info = Self.describe(tool: tool, cwd: cwd)
            append(.tool(id: toolID, kind: info.kind, title: info.title, output: "", status: .running, seconds: 0))
        case "tool_finished":
            let tool = event["tool"]
            guard let toolID = event["tool_id"].string ?? tool["id"].string else { return }
            let info = Self.describe(tool: tool, cwd: cwd)
            let seconds = toolStarts[toolID].map { max(0, time.timeIntervalSince($0)) } ?? 0
            let failed = event["is_error"].bool == true || (event["exit_code"].int ?? 0) != 0
            let output = Self.clip(event["output"].string ?? "")
            let item = TimelineItem.tool(id: toolID, kind: info.kind, title: info.title, output: output,
                                         status: failed ? .failed : .succeeded, seconds: seconds)
            if index[toolID] != nil { replace(toolID, with: item) } else { append(item) }
        case "claude_interaction_requested", "codex_interaction_requested":
            let interaction = event["interaction"]
            guard let interactionID = interaction["id"].string else { return }
            let params = interaction["params"]
            let decisions = params["availableDecisions"].array.compactMap(\.string)
            pending[interactionID] = PendingApproval(id: interactionID, decisions: decisions)
            let request = Self.describe(interaction: interaction, cwd: cwd)
            append(.approval(id: interactionID, author: agent, title: request.title, command: request.detail,
                             reason: request.reason, decision: .pending,
                             allowsSession: decisions.isEmpty || decisions.contains("acceptForSession")))
        case "claude_interaction_resolved", "codex_interaction_resolved":
            guard let interactionID = event["interaction_id"].string else { return }
            pending[interactionID] = nil
            setDecision(decided[interactionID] ?? .resolved, for: interactionID)
        case "code_diff":
            let files = event["diff_files"].array.compactMap { file -> FileChange? in
                guard let path = file["path"].string else { return nil }
                return FileChange(path: path, added: file["additions"].int ?? 0, removed: file["deletions"].int ?? 0, diff: "")
            }
            guard !files.isEmpty else { return }
            append(.changes(id: id, set: FileChangeSet(files: files)))
            if let runID = event["run_id"].string { diffsToLoad[id] = runID }
        case "turn_finished":
            running = false
            closeTurn(at: time)
            pending.removeAll()
            lastActivity = time
            if lastAssistantText == nil, let text = event["result_text"].text { lastAssistantText = text }
            settleRunningTools(as: .succeeded)
        case "turn_stopped", "turn_aborted":
            running = false
            closeTurn(at: time)
            pending.removeAll()
            settleRunningTools(as: .failed)
            append(.divider(id: id, text: "已停止"))
        case "turn_failed", "error":
            running = false
            closeTurn(at: time)
            settleRunningTools(as: .failed)
            let message = event["message"].text ?? event["error"].text ?? "运行出错"
            append(.divider(id: id, text: "出错：" + String(message.prefix(160))))
        case "turn_queued":
            if let request = event["client_request_id"].string { acceptedRequests.insert(request) }
            guard let queuedID = event["queued_id"].string, !queued.contains(where: { $0.id == queuedID }) else { return }
            queued.append(QueuedMessage(id: queuedID, text: event["display_prompt"].text ?? event["prompt"].text ?? ""))
        case "turn_unqueued":
            if let queuedID = event["queued_id"].string { queued.removeAll { $0.id == queuedID } }
        default:
            break
        }
    }

    static func mediaFile(_ record: JSON) -> MediaFile? {
        guard let id = record["id"].string else { return nil }
        let name = record["filename"].text ?? record["title"].text ?? id
        return MediaFile(id: id, name: name, contentType: record["content_type"].string ?? "")
    }

    /// Replaces the queue with the server's current list (from `GET /api/sessions/{id}`).
    public mutating func setQueue(_ turns: [JSON]) {
        queued = turns.compactMap { turn in
            guard let id = turn["id"].string ?? turn["queued_id"].string else { return nil }
            return QueuedMessage(id: id, text: turn["display_prompt"].text ?? turn["prompt"].text ?? "")
        }
    }

    /// Records the user's own answer so the resolved card says what was chosen.
    public mutating func noteDecision(_ decision: ApprovalDecision, for interactionID: String) {
        decided[interactionID] = decision
        setDecision(decision, for: interactionID)
    }

    public mutating func setDiff(_ unified: String, forItem itemID: String) {
        diffsToLoad[itemID] = nil
        guard let position = index[itemID], case .changes(let id, let set) = items[position] else { return }
        let byPath = Self.splitDiff(unified)
        let files = set.files.map { file in
            FileChange(path: file.path, added: file.added, removed: file.removed, diff: byPath[file.path] ?? file.diff)
        }
        items[position] = .changes(id: id, set: FileChangeSet(files: files))
    }

    // MARK: Helpers

    private mutating func closeTurn(at time: Date) {
        if let start = turnStartedAt, let item = turnUserItem {
            turnSeconds[item] = max(0, Int(time.timeIntervalSince(start).rounded()))
        }
        turnStartedAt = nil
        turnUserItem = nil
    }

    private mutating func append(_ item: TimelineItem) {
        if let position = index[item.id] {
            items[position] = item
            return
        }
        index[item.id] = items.count
        items.append(item)
    }

    private mutating func replace(_ id: String, with item: TimelineItem) {
        guard let position = index[id] else { return append(item) }
        items[position] = item
    }

    private mutating func setDecision(_ decision: ApprovalDecision, for interactionID: String) {
        guard let position = index[interactionID],
              case .approval(let id, let author, let title, let command, let reason, _, let allowsSession) = items[position]
        else { return }
        items[position] = .approval(id: id, author: author, title: title, command: command, reason: reason,
                                    decision: decision, allowsSession: allowsSession)
    }

    private mutating func settleRunningTools(as status: ToolStatus) {
        for position in items.indices {
            if case .tool(let id, let kind, let title, let output, .running, let seconds) = items[position] {
                items[position] = .tool(id: id, kind: kind, title: title, output: output, status: status, seconds: seconds)
            }
        }
    }

    static func clip(_ text: String, limit: Int = 4_000) -> String {
        text.count > limit ? String(text.prefix(limit)) + "\n…" : text
    }

    /// Shows paths relative to the chat's folder.
    static func relative(_ path: String, to cwd: String) -> String {
        var path = path
        var base = cwd
        for prefix in ["/private"] {
            if path.hasPrefix(prefix + "/") { path.removeFirst(prefix.count) }
            if base.hasPrefix(prefix + "/") { base.removeFirst(prefix.count) }
        }
        if !base.isEmpty, path.hasPrefix(base + "/") { return String(path.dropFirst(base.count + 1)) }
        let home = NSHomeDirectory()
        if path.hasPrefix(home + "/") { return "~" + path.dropFirst(home.count) }
        return path
    }

    static func abbreviateHome(_ text: String) -> String {
        text.replacingOccurrences(of: NSHomeDirectory() + "/", with: "~/")
    }

    /// Codex wraps commands as `/bin/zsh -lc "…"`; show the command itself.
    static func unwrapShell(_ command: String) -> String {
        for prefix in ["/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc ", "/bin/sh -c "] where command.hasPrefix(prefix) {
            var inner = String(command.dropFirst(prefix.count))
            if inner.count >= 2, let first = inner.first, first == inner.last, first == "\"" || first == "'" {
                inner = String(inner.dropFirst().dropLast())
            }
            return inner.replacingOccurrences(of: "\\\\", with: "\\")
        }
        return command
    }

    static func describe(tool: JSON, cwd: String) -> (kind: ToolKind, title: String) {
        let name = tool["name"].string ?? "tool"
        let input = tool["input"]
        let path = (input["file_path"].string ?? input["path"].string ?? input["notebook_path"].string)
            .map { relative($0, to: cwd) }
        switch name {
        case "Bash", "shell", "exec_command", "local_shell", "BashOutput":
            let command = abbreviateHome(unwrapShell(input["command"].string ?? input["cmd"].string ?? name))
            return (.command, command.components(separatedBy: "\n").first ?? command)
        case "Read", "NotebookRead":
            return (.read, path ?? name)
        case "Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "fileChange":
            return (.edit, path ?? "补丁")
        case "Grep", "Glob", "LS":
            return (.search, input["pattern"].string ?? path ?? name)
        case "WebFetch", "WebSearch", "web_search":
            return (.web, input["url"].string ?? input["query"].string ?? name)
        case "TodoWrite":
            return (.edit, "更新待办清单")
        case "Task", "Agent":
            return (.mcp, input["description"].string ?? "子代理")
        default:
            if name.hasPrefix("mcp__") {
                let parts = name.split(separator: "_", omittingEmptySubsequences: true)
                return (.mcp, parts.dropFirst().joined(separator: " · "))
            }
            return (.mcp, name)
        }
    }

    static func describe(interaction: JSON, cwd: String) -> (title: String, detail: String, reason: String) {
        let method = interaction["method"].string ?? ""
        let params = interaction["params"]
        let reason = params["reason"].text ?? ""
        if method.contains("commandExecution") || params["kind"].string == "command" {
            let command = abbreviateHome(unwrapShell(params["command"].string ?? ""))
            return ("运行命令", command, reason.isEmpty ? "在 \(relative(params["cwd"].string ?? cwd, to: cwd)) 中运行" : reason)
        }
        if method.contains("fileChange") {
            let changes = params["changes"].array
            let paths = changes.compactMap { ($0["file_path"].string ?? $0["path"].string).map { relative($0, to: cwd) } }
            var detail = paths.joined(separator: "\n")
            if changes.count == 1, let content = changes[0]["content"].string, !content.isEmpty {
                detail += "\n\n" + clip(content, limit: 1_200)
            }
            if detail.isEmpty, let name = params["displayName"].string { detail = name }
            return ("修改文件", detail, reason)
        }
        let name = params["displayName"].string ?? params["toolName"].string ?? method
        let input = params["toolInput"]
        let detail = unwrapShell(input["command"].string ?? input["file_path"].string ?? params["description"].string ?? name)
        return ("使用 \(name)", detail, reason.isEmpty ? (params["description"].text ?? "") : reason)
    }

    /// Splits `git diff` output into one chunk per file path.
    static func splitDiff(_ unified: String) -> [String: String] {
        var result: [String: String] = [:]
        var currentPath: String?
        var lines: [Substring] = []
        func flush() {
            if let currentPath { result[currentPath] = lines.joined(separator: "\n") }
            lines = []
        }
        for line in unified.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.hasPrefix("diff --git ") {
                flush()
                currentPath = line.components(separatedBy: " b/").last
                continue
            }
            if line.hasPrefix("index ") || line.hasPrefix("--- ") || line.hasPrefix("+++ ")
                || line.hasPrefix("new file mode") || line.hasPrefix("deleted file mode") {
                continue
            }
            lines.append(line)
        }
        flush()
        return result
    }
}

/// Turns a group's (or an Autopilot run's) timeline into items.
public struct GroupProjection: Sendable {
    public private(set) var items: [TimelineItem] = []
    public private(set) var lastSeq = 0
    public private(set) var memberStates: [String: ActivityState] = [:]
    /// Orchestrator progress notes (Autopilot runs show these as their activity log).
    public private(set) var notes: [TimelineItem] = []
    private var backends: [String: AgentBackend]

    public init(backends: [String: AgentBackend]) {
        self.backends = backends
    }

    public mutating func apply(_ events: [JSON]) {
        for event in events { apply(event) }
    }

    public mutating func apply(_ event: JSON) {
        guard let seq = event["seq"].int, seq > lastSeq else { return }
        lastSeq = seq
        let id = "g\(seq)"
        let time = event["ts"].date ?? event["created_at"].date ?? .now
        switch event["type"].string ?? "" {
        case "group_created":
            items.append(.divider(id: id, text: "讨论组已创建"))
        case "message":
            let text = event["text"].string ?? ""
            let name = event["author"].string ?? "?"
            let mentions = event["mentions"].array.compactMap(\.string)
            let receipt = event["receipt"]["changed_files"].array.compactMap(\.string)
            let changes = receipt.isEmpty ? nil
                : FileChangeSet(files: receipt.map { FileChange(path: $0, added: 0, removed: 0, diff: "") })
            switch event["author_kind"].string {
            case "user":
                items.append(.message(id: id, author: .user, text: text, time: time, mentions: mentions))
            case "agent":
                let backend = backends[name] ?? (name.hasPrefix("codex") ? .codex : .claude)
                items.append(.message(id: id, author: .agent(name, backend), text: text, time: time,
                                      mentions: mentions, receipt: changes))
            default:
                let note = TimelineItem.message(id: id, author: Author(name: name, kind: .orchestrator), text: text,
                                                time: time, mentions: mentions)
                items.append(note)
                // Orchestrator messages addressed to agents are prompts, not progress notes.
                let audience = event["audience"].array.compactMap(\.string)
                if audience.isEmpty || audience.contains("user") { notes.append(note) }
            }
        case "member_status":
            guard let handle = event["handle"].string else { return }
            memberStates[handle] = Self.state(event["status"].string ?? event["state"].string)
        case "round_finished":
            let reason = event["reason"].string ?? event["status"].string
            items.append(.divider(id: id, text: reason == "stopped" ? "本轮已停止" : "本轮结束"))
        case "participant_added":
            if let handle = event["handle"].string ?? event["participant"]["handle"].string {
                if let backend = (event["backend"].string ?? event["participant"]["backend"].string).flatMap(AgentBackend.named) {
                    backends[handle] = backend
                }
                items.append(.divider(id: id, text: "@\(handle) 加入了讨论组"))
            }
        case "participant_removed":
            if let handle = event["handle"].string { items.append(.divider(id: id, text: "@\(handle) 离开了讨论组")) }
        case "goal_set":
            if let handle = event["handle"].string { items.append(.divider(id: id, text: "已为 @\(handle) 设置目标")) }
        default:
            break
        }
    }

    static func state(_ raw: String?) -> ActivityState {
        switch raw {
        case "running": .running
        case "queued": .queued
        case "failed": .failed
        case "needs_action": .needsAction
        default: .idle
        }
    }
}
