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
    /// Turns started for a `client_request_id`: request → (user item id, start time).
    private var requestTurns: [String: (item: String, started: Date)] = [:]

    private var index: [String: Int] = [:]
    private var toolStarts: [String: Date] = [:]
    /// When each item first appeared, and where turns ended (item count → time):
    /// the durations of the "已处理" rows.
    private var itemTimes: [String: Date] = [:]
    private var turnEnds: [Int: Date] = [:]
    private var eventTime = Date.now
    /// The current turn's thoughts, to drop a partial copy of one already recorded.
    private var turnThoughts: Set<String> = []
    /// Uploaded files by id, to name a turn's attachments.
    private var uploads: [String: MediaFile] = [:]
    private var lastPublication: String?
    private var decided: [String: ApprovalDecision] = [:]
    /// What each Agent/Task tool's subagent did (events with `parent_tool_id`),
    /// shown in that tool's card the way Claude Code nests it.
    private var subagentLogs: [String: SubagentLog] = [:]

    struct SubagentLog: Sendable {
        /// Tool ids in order, with their line and whether they failed.
        var tools: [(id: String, line: String, failed: Bool)] = []
        /// The subagent's latest narration.
        var text = ""
    }

    public init(backend: AgentBackend, cwd: String) {
        self.backend = backend
        self.cwd = cwd
    }

    var agent: Author { .agent(backend.rawValue, backend) }

    public var needsAction: Bool { !pending.isEmpty }

    /// How long each run of work took, by its first step: from the turn's start
    /// (the turn's first run) or the run's first step, to what ended it — the
    /// next item shown or the end of the turn. A run still going has none.
    public var workSeconds: [String: Int] {
        var result: [String: Int] = [:]
        var first: String?
        var start: Date?
        var turnStart: Date?
        func close(_ end: Date?) {
            if let first, let start, let end { result[first] = max(0, Int(end.timeIntervalSince(start).rounded())) }
            first = nil
            start = nil
        }
        for (position, item) in items.enumerated() {
            if let end = turnEnds[position] {
                close(end)
                turnStart = nil
            }
            if case .message(_, let author, _, let time, _, _, _) = item, author.kind == .user {
                close(time)
                turnStart = time
                continue
            }
            if item.isWorkStep {
                if first == nil {
                    first = item.id
                    start = turnStart ?? itemTimes[item.id]
                }
                continue
            }
            if item.floatsPastWork { continue }
            close(itemTimes[item.id])
            turnStart = nil
        }
        close(turnEnds[items.count])
        return result
    }

    public mutating func apply(_ events: [JSON]) {
        for event in events { apply(event) }
    }

    public mutating func apply(_ event: JSON) {
        guard let seq = event["seq"].int, seq > lastSeq else { return }
        lastSeq = seq
        let id = event["id"].string ?? "seq-\(seq)"
        let time = event["ts"].date ?? .now
        eventTime = time
        if let parent = event["parent_tool_id"].string, !parent.isEmpty {
            return applySubagent(event, parent: parent, id: id, time: time)
        }
        switch event["type"].string ?? "" {
        case "turn_started":
            running = true
            lastActivity = time
            turnStartedAt = time
            turnUserItem = id
            turnThoughts = []
            if let request = event["client_request_id"].string {
                acceptedRequests.insert(request)
                requestTurns[request] = (id, time)
            }
            if let queuedID = event["queued_id"].string { queued.removeAll { $0.id == queuedID } }
            let prompt = event["display_prompt"].text ?? event["prompt"].text ?? ""
            if !prompt.isEmpty || event["client_request_id"].string != nil {
                append(.message(id: id, author: .user, text: prompt, time: time))
            }
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
            // Claude's /compact prints "Compacted"; its compaction landmark already says so.
            // A detached output belongs to an earlier priority command (clearing a goal
            // mid-turn) that the goal bar already reflects.
            if event["local_command"].string?.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == "compact"
                || event["local_command_detached"].bool == true {
                lastActivity = time
                return
            }
            lastAssistantText = text
            lastActivity = time
            append(.message(id: id, author: agent, text: text, time: time))
        case "reasoning_summary", "reasoning_text":
            guard let text = event["text"].text else { return }
            // An older server kept the turn's last thought a second time, as partial.
            let thought = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if event["partial"].bool == true, turnThoughts.contains(thought) { return }
            turnThoughts.insert(thought)
            if case .thinking(let previousID, let seconds, let previous)? = items.last {
                replace(previousID, with: .thinking(id: previousID, seconds: seconds, text: previous + "\n\n" + text))
            } else {
                append(.thinking(id: id, seconds: 0, text: text))
            }
        case "chat_consult_asked":
            // The user asked another chat from here (@ in the composer); its card reads the live consult.
            guard let consultID = event["consult_id"].string else { return }
            append(.consult(id: "consult-" + consultID, call: ConsultCall(
                target: event["target"]["title"].string ?? "聊天", deep: event["mode"].string == "deep",
                question: event["question"].string ?? "", running: false, consultID: consultID, byUser: true)))
        case "tool_started":
            let tool = event["tool"]
            guard let toolID = tool["id"].string else { return }
            toolStarts[toolID] = time
            // A question to another chat is shown as its own card, not folded into the steps.
            if let call = ConsultCall.parse(tool: tool) {
                append(.consult(id: toolID, call: call))
                return
            }
            let info = Self.describe(tool: tool, cwd: cwd)
            append(.tool(id: toolID, kind: info.kind, title: info.title, output: "", status: .running, seconds: 0))
        case "tool_finished":
            let tool = event["tool"]
            guard let toolID = event["tool_id"].string ?? tool["id"].string else { return }
            let started = index[toolID].map { items[$0] }
            if case .consult(_, var call)? = started ?? ConsultCall.parse(tool: tool).map({ .consult(id: toolID, call: $0) }) {
                call.running = false
                call.receipt = ConsultReceipt.parse(output: Self.toolText(event["output"]),
                                                    failed: event["is_error"].bool == true)
                replace(toolID, with: .consult(id: toolID, call: call))
                return
            }
            let info = Self.describe(tool: tool, cwd: cwd)
            let seconds = toolStarts[toolID].map { max(0, time.timeIntervalSince($0)) } ?? 0
            let failed = event["is_error"].bool == true || (event["exit_code"].int ?? 0) != 0
            let output = subagentOutput(toolID, result: Self.clip(event["output"].string ?? ""))
            let item = TimelineItem.tool(id: toolID, kind: info.kind, title: info.title, output: output,
                                         status: failed ? .failed : .succeeded, seconds: seconds)
            if index[toolID] != nil { replace(toolID, with: item) } else { append(item) }
        case "claude_interaction_requested", "codex_interaction_requested":
            let interaction = event["interaction"]
            guard let interactionID = interaction["id"].string else { return }
            let method = interaction["method"].string ?? ""
            let params = interaction["params"]
            let decisions = params["availableDecisions"].array.compactMap(\.string)
            pending[interactionID] = PendingApproval(id: interactionID, decisions: decisions)
            if let form = InteractionForm.parse(method: method, params: params) {
                append(.form(id: interactionID, author: agent, form: form, decision: .pending))
                return
            }
            let request = Self.describe(interaction: interaction, cwd: cwd)
            append(.approval(id: interactionID, author: agent, title: request.title, command: request.detail,
                             reason: request.reason, decision: .pending,
                             choices: ApprovalChoice.choices(method: method, params: params)))
        case "claude_interaction_resolved", "codex_interaction_resolved":
            guard let interactionID = event["interaction_id"].string else { return }
            pending[interactionID] = nil
            setDecision(decided[interactionID] ?? .resolved, for: interactionID)
        case "code_diff":
            let files = event["diff_files"].array.compactMap { file -> FileChange? in
                guard let path = file["path"].string else { return nil }
                return FileChange(path: path, added: file["additions"].int ?? 0, removed: file["deletions"].int ?? 0, diff: "",
                                  root: event["repository_root"].string ?? cwd)
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
        case "turn_unqueued", "turn_queue_run_now":
            if let queuedID = event["queued_id"].string { queued.removeAll { $0.id == queuedID } }
        case "turn_queue_paused":
            let ids = Set(event["queued_ids"].array.compactMap(\.string))
            for index in queued.indices where ids.contains(queued[index].id) && queued[index].hold == nil {
                queued[index].hold = .stopped
            }
        case "turn_queue_delivery_fenced":
            if let queuedID = event["queued_id"].string, let index = queued.firstIndex(where: { $0.id == queuedID }) {
                queued[index].hold = .deliveryUncertain
            }
        case "turn_queue_resumed":
            let ids = Set(event["queued_ids"].array.compactMap(\.string))
            for index in queued.indices where ids.contains(queued[index].id) && queued[index].hold == .stopped {
                queued[index].hold = nil
            }
        case "turn_queue_updated":
            if let queuedID = event["queued_id"].string, let position = queued.firstIndex(where: { $0.id == queuedID }),
               let text = event["display_prompt"].text ?? event["prompt"].text {
                queued[position].text = text
            }
        case "turn_queue_reordered":
            let order = Dictionary(event["positions"].array.compactMap { entry -> (String, Int)? in
                guard let id = entry["queued_id"].string, let position = entry["position"].int else { return nil }
                return (id, position)
            }, uniquingKeysWith: { $1 })
            queued.sort { (order[$0.id] ?? .max) < (order[$1.id] ?? .max) }
        case "turn_steered":
            // Sent into the running turn: the user's words join it where they arrived.
            if let queuedID = event["queued_id"].string { queued.removeAll { $0.id == queuedID } }
            if let request = event["client_request_id"].string { acceptedRequests.insert(request) }
            if let prompt = event["display_prompt"].text ?? event["prompt"].text, !prompt.isEmpty {
                append(.message(id: id, author: .user, text: prompt, time: time))
            }
        case "agent_notification":
            if let message = event["message"].text {
                let important = event["notification_level"].string == "important"
                append(.divider(id: id, text: (important ? "重要通知：" : "通知：") + message))
            }
        case "emergency_alert_raised":
            append(.divider(id: id, text: "紧急提醒：" + (event["message"].text ?? "")))
        case "emergency_alert_acknowledged":
            append(.divider(id: id, text: "已确认紧急提醒"))
        case "session_forked":
            let copied = event["copied_events"].int.map { "（\($0) 条记录）" } ?? ""
            append(.divider(id: id, text: "已分叉出一个新聊天" + copied))
        case "history_imported" where event["copied_events"].int != nil:
            append(.divider(id: id, text: "从原聊天分叉而来，以上是复制的对话"))
        case "claude_compaction_completed":
            append(.divider(id: id, text: "上下文已压缩"))
        case "codex_compaction_started":
            append(.divider(id: id, text: "正在压缩上下文…"))
        case "codex_compaction_completed":
            let failed = event["status"].string.map { $0 != "completed" } ?? false
            append(.divider(id: id, text: failed ? "上下文压缩失败" : "上下文已压缩"))
        default:
            break
        }
    }

    /// One turn's work, for the turn started with `request` (a group dispatch id).
    public struct TurnSlice: Hashable, Sendable {
        /// Thinking, tool calls and answered approvals, in order.
        public var steps: [TimelineItem] = []
        /// Approvals and questions still waiting for the user.
        public var pendingApprovals: [TimelineItem] = []
        public var changes: FileChangeSet?
        public var seconds: Int?
        public var running = false
        public var startedAt: Date?
    }

    public func turn(forRequest request: String) -> TurnSlice? {
        guard let (userItem, started) = requestTurns[request], let start = index[userItem] else { return nil }
        var slice = TurnSlice(seconds: turnSeconds[userItem], startedAt: started)
        var position = start + 1
        var files: [FileChange] = []
        while position < items.count {
            let item = items[position]
            if case .message(_, let author, _, _, _, _, _) = item, author.kind == .user { break }
            switch item {
            case .thinking, .tool, .consult:
                slice.steps.append(item)
            case .approval(let id, _, _, _, _, let decision, _), .form(let id, _, _, let decision):
                if decision == .pending && pending[id] != nil { slice.pendingApprovals.append(item) } else { slice.steps.append(item) }
            case .changes(_, let set):
                files += set.files
            default:
                break
            }
            position += 1
        }
        slice.changes = files.isEmpty ? nil : FileChangeSet(files: files)
        slice.running = running && turnUserItem == userItem
        return slice
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
            let hold: QueuedMessage.Hold? = turn["paused"].bool == true
                ? QueuedMessage.Hold(rawValue: turn["pause_reason"].string ?? "") ?? .stopped : nil
            return QueuedMessage(id: id, text: turn["display_prompt"].text ?? turn["prompt"].text ?? "", hold: hold)
        }
    }

    /// Keeps a queue already known when the timeline is rebuilt.
    public mutating func replaceQueue(_ messages: [QueuedMessage]) {
        queued = messages
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
            FileChange(path: file.path, added: file.added, removed: file.removed, diff: byPath[file.path] ?? file.diff,
                       root: file.root)
        }
        items[position] = .changes(id: id, set: FileChangeSet(files: files))
    }

    // MARK: Subagents

    /// A subagent's tool calls and narration fold into its Agent tool's card;
    /// they never become the main agent's messages. Without a known parent
    /// (history paged in mid-turn) they stay with the turn's folded work.
    private mutating func applySubagent(_ event: JSON, parent: String, id: String, time: Date) {
        guard let position = index[parent], case .tool(_, let kind, let title, _, let status, let seconds) = items[position]
        else {
            switch event["type"].string {
            case "assistant_text", "reasoning_summary", "reasoning_text":
                if let text = event["text"].text { append(.thinking(id: id, seconds: 0, text: text)) }
            case "tool_started":
                let tool = event["tool"]
                guard let toolID = tool["id"].string else { return }
                let info = Self.describe(tool: tool, cwd: cwd)
                append(.tool(id: toolID, kind: info.kind, title: info.title, output: "", status: .running, seconds: 0))
            case "tool_finished":
                guard let toolID = event["tool_id"].string ?? event["tool"]["id"].string, index[toolID] != nil,
                      case .tool(_, let kind, let title, _, _, let seconds)? = index[toolID].map({ items[$0] }) else { return }
                let failed = event["is_error"].bool == true
                replace(toolID, with: .tool(id: toolID, kind: kind, title: title,
                                            output: Self.clip(event["output"].string ?? ""),
                                            status: failed ? .failed : .succeeded, seconds: seconds))
            default:
                break
            }
            return
        }
        var log = subagentLogs[parent] ?? SubagentLog()
        switch event["type"].string {
        case "tool_started":
            let tool = event["tool"]
            guard let toolID = tool["id"].string, !log.tools.contains(where: { $0.id == toolID }) else { return }
            log.tools.append((toolID, Self.describe(tool: tool, cwd: cwd).title, false))
        case "tool_finished":
            guard let toolID = event["tool_id"].string ?? event["tool"]["id"].string,
                  let entry = log.tools.firstIndex(where: { $0.id == toolID }) else { return }
            log.tools[entry].failed = event["is_error"].bool == true
        case "assistant_text":
            guard let text = event["text"].text else { return }
            log.text = text
        default:
            return
        }
        subagentLogs[parent] = log
        lastActivity = time
        if case .tool(_, _, _, let output, _, _) = items[position] {
            let result = status == .running ? "" : (output.components(separatedBy: Self.subagentResultMarker).last ?? output)
            items[position] = .tool(id: parent, kind: kind, title: title, output: subagentOutput(parent, result: result),
                                    status: status, seconds: seconds)
        }
    }

    private static let subagentResultMarker = "\n\n— 结果 —\n"

    /// A subagent tool's card: its recent steps, then its result once it finished.
    private func subagentOutput(_ toolID: String, result: String) -> String {
        guard let log = subagentLogs[toolID] else { return result }
        var lines = log.tools.suffix(8).map { "· " + $0.line + ($0.failed ? "（失败）" : "") }
        if log.tools.count > 8 { lines.insert("…还有 \(log.tools.count - 8) 次工具调用", at: 0) }
        if !log.text.isEmpty, result.isEmpty { lines.append(Self.clip(log.text, limit: 400)) }
        let steps = "子代理用了 \(log.tools.count) 次工具\n" + lines.joined(separator: "\n")
        return result.isEmpty ? steps : steps + Self.subagentResultMarker + result
    }

    // MARK: Helpers

    private mutating func closeTurn(at time: Date) {
        turnEnds[items.count] = time
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
        itemTimes[item.id] = eventTime
        index[item.id] = items.count
        items.append(item)
    }

    private mutating func replace(_ id: String, with item: TimelineItem) {
        guard let position = index[id] else { return append(item) }
        items[position] = item
    }

    private mutating func setDecision(_ decision: ApprovalDecision, for interactionID: String) {
        guard let position = index[interactionID] else { return }
        switch items[position] {
        case .approval(let id, let author, let title, let command, let reason, _, let choices):
            items[position] = .approval(id: id, author: author, title: title, command: command, reason: reason,
                                        decision: decision, choices: choices)
        case .form(let id, let author, let form, _):
            items[position] = .form(id: id, author: author, form: form, decision: decision)
        default:
            break
        }
    }

    private mutating func settleRunningTools(as status: ToolStatus) {
        for position in items.indices {
            if case .tool(let id, let kind, let title, let output, .running, let seconds) = items[position] {
                items[position] = .tool(id: id, kind: kind, title: title, output: output, status: status, seconds: seconds)
            }
        }
    }

    /// A tool's output as text: a string, or the text parts of an MCP result.
    static func toolText(_ output: JSON) -> String {
        if let text = output.string { return text }
        let parts = output["content"].array.isEmpty ? output.array : output["content"].array
        return parts.compactMap { $0["text"].string ?? $0.string }.joined(separator: "\n")
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
        case "ImageGeneration":
            return (.mcp, "生成图片")
        case "Task", "Agent":
            return (.mcp, input["description"].string ?? "子代理")
        default:
            if ConsultCall.isHyadexTool(name) {
                // Hyadex's own helpers (jobs, publish, notify, ask list…), by what they do.
                let helper = input["helper"].string ?? "helper"
                if helper == "ask" { return (.mcp, "查看可以询问的聊天") }
                let arguments = input["arguments"].array.compactMap(\.string).prefix(2).joined(separator: " ")
                return (.mcp, "Hyadex · \(helper) \(arguments)".trimmingCharacters(in: .whitespaces))
            }
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
        if method == "execCommandApproval" {
            let command = abbreviateHome(unwrapShell(params["command"].array.compactMap(\.string).joined(separator: " ")))
            return ("运行命令", command, reason.isEmpty ? "在 \(relative(params["cwd"].string ?? cwd, to: cwd)) 中运行" : reason)
        }
        if method == "applyPatchApproval" {
            let paths = (params["fileChanges"].object ?? [:]).keys.sorted().map { relative($0, to: cwd) }
            return ("修改文件", paths.joined(separator: "\n"), reason)
        }
        if method == "item/permissions/requestApproval" {
            let permissions = params["permissions"]
            var lines: [String] = []
            if permissions["network"]["enabled"].bool == true { lines.append("访问网络") }
            let files = permissions["fileSystem"]
            lines += files["read"].array.compactMap(\.string).map { "读取 " + relative($0, to: cwd) }
            lines += files["write"].array.compactMap(\.string).map { "写入 " + relative($0, to: cwd) }
            for entry in files["entries"].array {
                let path = entry["path"]
                let target = path["path"].string.map { relative($0, to: cwd) } ?? path["pattern"].string
                    ?? path["value"]["kind"].string ?? "?"
                let access = switch entry["access"].string {
                case "write": "写入"
                case "deny": "禁止访问"
                default: "读取"
                }
                lines.append("\(access) \(target)")
            }
            return ("更多权限", lines.joined(separator: "\n"), reason)
        }
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
    /// Agent message item id → the dispatch that produced it.
    public private(set) var dispatchOfMessage: [String: String] = [:]
    /// Dispatches whose member has not answered yet, in dispatch order.
    public private(set) var activeDispatches: [GroupDispatch] = []
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
                if let dispatch = event["dispatch_id"].string {
                    dispatchOfMessage[id] = dispatch
                    activeDispatches.removeAll { $0.id == dispatch }
                }
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
        case "dispatch":
            guard let dispatch = event["dispatch_id"].string, let handle = event["handle"].string,
                  !activeDispatches.contains(where: { $0.id == dispatch }) else { return }
            let state = event["state"].string ?? "running"
            if ["running", "queued", "dispatched"].contains(state) {
                activeDispatches.append(GroupDispatch(id: dispatch, handle: handle, sessionID: event["session_id"].string, time: time))
            }
        case "member_status":
            guard let handle = event["handle"].string else { return }
            let state = Self.state(event["status"].string ?? event["state"].string)
            memberStates[handle] = state
            if let dispatch = event["dispatch_id"].string, state == .idle || state == .failed {
                activeDispatches.removeAll { $0.id == dispatch }
            }
        case "round_finished":
            activeDispatches.removeAll()
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

/// A member asked to answer in a group round.
public struct GroupDispatch: Identifiable, Hashable, Sendable {
    public let id: String
    public let handle: String
    public let sessionID: String?
    public let time: Date
}
