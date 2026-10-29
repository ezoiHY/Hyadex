import Foundation
import HyadexAPI

public enum AgentBackend: String, CaseIterable, Codable, Hashable, Sendable {
    case claude, codex

    public static func named(_ raw: String) -> AgentBackend? { AgentBackend(rawValue: raw) }

    public var displayName: String {
        switch self {
        case .claude: "Claude Code"
        case .codex: "Codex"
        }
    }

}

/// One state vocabulary for every chat, member, run and step, so the
/// sidebar, headers and timeline always use the same colours and words.
public enum ActivityState: String, Codable, Hashable, Sendable {
    case idle, queued, running, needsAction, failed, done, paused

    public var label: String {
        switch self {
        case .idle: "空闲"
        case .queued: "排队中"
        case .running: "运行中"
        case .needsAction: "需要你处理"
        case .failed: "失败"
        case .done: "已完成"
        case .paused: "已暂停"
        }
    }
}

public enum SidebarItem: Hashable, Sendable {
    case chat(String)
    case group(String)
    case autopilot(String)
    case jobs

    public var mode: SidebarMode {
        switch self {
        case .chat, .jobs: .work
        case .group: .group
        case .autopilot: .autopilot
        }
    }
}

/// The three kinds of work the sidebar switches between.
public enum SidebarMode: String, CaseIterable, Hashable, Sendable {
    case work, group, autopilot

    public var title: String {
        switch self {
        case .work: "Work"
        case .group: "Group"
        case .autopilot: "Autopilot"
        }
    }

    public var symbol: String {
        switch self {
        case .work: "bubble.left.and.text.bubble.right"
        case .group: "person.2"
        case .autopilot: "arrow.triangle.2.circlepath"
        }
    }

    public var newItemTitle: String {
        switch self {
        case .work: "新建聊天"
        case .group: "新建讨论组"
        case .autopilot: "新建自动接力"
        }
    }

    public var searchPrompt: String {
        switch self {
        case .work: "搜索聊天和消息"
        case .group: "搜索讨论组"
        case .autopilot: "搜索自动接力"
        }
    }
}

// MARK: - Chats

/// Tokens a chat has used across all its turns, as the provider reported them.
/// Input includes cached input.
public struct TokenTotals: Hashable, Sendable {
    public var total: Int
    public var input: Int
    public var cachedInput: Int
    public var output: Int

    public init(total: Int, input: Int, cachedInput: Int, output: Int) {
        self.total = total
        self.input = input
        self.cachedInput = cachedInput
        self.output = output
    }
}

public struct ChatSummary: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var backend: AgentBackend
    public var model: String
    public var effort: String
    public var permission: String
    public var cwd: String
    public var preview: String
    public var lastActivity: Date
    public var state: ActivityState
    public var pinned: Bool
    public var unread: Bool
    public var tokensUsed: Int
    /// The provider's token counts across all turns; nil until the server has recorded one.
    public var tokenTotals: TokenTotals?
    public var archived: Bool
    /// The project the chat was moved into (the server's `folder`); nil when in none.
    public var folder: String?

    public init(id: String, title: String, backend: AgentBackend, model: String, effort: String = "",
                permission: String, cwd: String, preview: String, lastActivity: Date,
                state: ActivityState = .idle, pinned: Bool = false, unread: Bool = false, tokensUsed: Int = 0,
                tokenTotals: TokenTotals? = nil, archived: Bool = false, folder: String? = nil) {
        self.folder = folder
        self.tokenTotals = tokenTotals
        self.id = id
        self.title = title
        self.backend = backend
        self.model = model
        self.effort = effort
        self.permission = permission
        self.cwd = cwd
        self.preview = preview
        self.lastActivity = lastActivity
        self.state = state
        self.pinned = pinned
        self.unread = unread
        self.tokensUsed = tokensUsed
        self.archived = archived
    }
}

// MARK: - Groups

public struct Participant: Identifiable, Hashable, Sendable {
    public var id: String { handle }
    public var handle: String
    public var backend: AgentBackend
    public var role: String
    public var model: String
    public var state: ActivityState
    /// The member chat that runs this agent's turns (live servers only).
    public var sessionID: String?
    public var effort: String
    public var permission: String

    public init(handle: String, backend: AgentBackend, role: String, model: String, state: ActivityState = .idle,
                sessionID: String? = nil, effort: String = "", permission: String? = nil) {
        self.handle = handle
        self.backend = backend
        self.role = role
        self.model = model
        self.state = state
        self.sessionID = sessionID
        self.effort = effort
        self.permission = permission ?? (backend == .claude ? "Manual" : "Custom (workspace-write, never)")
    }
}

public enum GroupMode: String, CaseIterable, Hashable, Sendable {
    case sequential, parallel

    public var label: String { self == .sequential ? "依次回答" : "同时回答" }
}

public struct GroupSummary: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var cwd: String
    public var participants: [Participant]
    public var defaultResponder: String
    public var preview: String
    public var lastActivity: Date
    public var unread: Bool
    public var promptChars: Int
    public var dispatches: Int
    /// A native goal set on one member, announced to the group.
    public var goal: GroupGoal?
    public var archived = false
    /// Responder, hand-off limit and brief, as the server stores them.
    public var settings = GroupSettings()

    public init(id: String, title: String, cwd: String, participants: [Participant], defaultResponder: String,
                preview: String, lastActivity: Date, unread: Bool = false, promptChars: Int = 0, dispatches: Int = 0,
                goal: GroupGoal? = nil) {
        self.id = id
        self.title = title
        self.cwd = cwd
        self.participants = participants
        self.defaultResponder = defaultResponder
        self.preview = preview
        self.lastActivity = lastActivity
        self.unread = unread
        self.promptChars = promptChars
        self.dispatches = dispatches
        self.goal = goal
    }

    /// The most urgent member state wins: needs action > running > queued > idle.
    public var state: ActivityState {
        let states = participants.map(\.state)
        for candidate in [ActivityState.needsAction, .failed, .running, .queued] where states.contains(candidate) {
            return candidate
        }
        return .idle
    }
}

// MARK: - Autopilot

public enum AutopilotStatus: String, CaseIterable, Hashable, Sendable {
    case planning, awaitingApproval, executing, verifying, reviewing, paused, needsAttention, done, aborted

    public var label: String {
        switch self {
        case .planning: "规划中"
        case .awaitingApproval: "等待批准"
        case .executing: "执行中"
        case .verifying: "验证中"
        case .reviewing: "审查中"
        case .paused: "已暂停"
        case .needsAttention: "需要处理"
        case .done: "已完成"
        case .aborted: "已中止"
        }
    }

    public var activity: ActivityState {
        switch self {
        case .planning, .executing, .verifying, .reviewing: .running
        case .awaitingApproval, .needsAttention: .needsAction
        case .paused: .paused
        case .done: .done
        case .aborted: .failed
        }
    }

    public var isActive: Bool { activity == .running }
}

public enum ApprovalOption: String, CaseIterable, Hashable, Sendable {
    case plan, step, none

    public var label: String {
        switch self {
        case .plan: "批准计划"
        case .step: "逐步批准"
        case .none: "全自动"
        }
    }

    public var explanation: String {
        switch self {
        case .plan: "你批准计划后自动执行到底。"
        case .step: "计划和每一步完成后都等你批准。"
        case .none: "不需要你批准，验证和审查通过即继续。"
        }
    }
}

public enum IsolationOption: String, CaseIterable, Hashable, Sendable {
    case worktree, inPlace

    public var label: String { self == .worktree ? "独立 worktree" : "直接修改" }

    public var explanation: String {
        switch self {
        case .worktree: "在新分支和独立目录里工作，每个任务一个提交，你的工作区不受影响。"
        case .inPlace: "直接改当前目录，不创建提交。"
        }
    }
}

public enum ReviewerOption: String, CaseIterable, Hashable, Sendable {
    case planner, fresh, none

    public var label: String {
        switch self {
        case .planner: "规划者审查"
        case .fresh: "独立审查者"
        case .none: "只看验证"
        }
    }

    public var explanation: String {
        switch self {
        case .planner: "由做规划的会话审查，最省 token，也最了解计划。"
        case .fresh: "另开一个与计划无关的审查者，更客观。"
        case .none: "只由验证命令决定是否通过。"
        }
    }
}

public enum TaskState: String, Hashable, Sendable {
    case pending, executing, verifying, reviewing, revising, approved, blocked
}

public struct Verification: Hashable, Sendable {
    public var command: String
    public var passed: Bool
    public var summary: String
    public var duration: TimeInterval

    public init(command: String, passed: Bool, summary: String, duration: TimeInterval) {
        self.command = command
        self.passed = passed
        self.summary = summary
        self.duration = duration
    }
}

public struct AutopilotTask: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var detail: String
    public var files: [String]
    public var acceptance: String
    public var state: TaskState
    public var revisions: Int
    public var commit: String?
    public var verification: Verification?
    public var reviewNote: String?
    public var changes: FileChangeSet?

    public init(id: String, title: String, detail: String, files: [String], acceptance: String,
                state: TaskState = .pending, revisions: Int = 0, commit: String? = nil,
                verification: Verification? = nil, reviewNote: String? = nil, changes: FileChangeSet? = nil) {
        self.id = id
        self.title = title
        self.detail = detail
        self.files = files
        self.acceptance = acceptance
        self.state = state
        self.revisions = revisions
        self.commit = commit
        self.verification = verification
        self.reviewNote = reviewNote
        self.changes = changes
    }
}

public struct AutopilotRun: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var goal: String
    public var cwd: String
    public var branch: String?
    public var status: AutopilotStatus
    public var approval: ApprovalOption
    public var isolation: IsolationOption
    public var reviewer: ReviewerOption
    public var verifyCommand: String
    public var planner: AgentBackend
    public var executor: AgentBackend
    public var planSummary: String
    public var tasks: [AutopilotTask]
    public var notes: [TimelineItem]
    public var lastActivity: Date
    public var promptChars: Int

    public init(id: String, title: String, goal: String, cwd: String, branch: String?, status: AutopilotStatus,
                approval: ApprovalOption, isolation: IsolationOption, reviewer: ReviewerOption, verifyCommand: String,
                planner: AgentBackend = .claude, executor: AgentBackend = .codex, planSummary: String,
                tasks: [AutopilotTask], notes: [TimelineItem] = [], lastActivity: Date, promptChars: Int = 0) {
        self.id = id
        self.title = title
        self.goal = goal
        self.cwd = cwd
        self.branch = branch
        self.status = status
        self.approval = approval
        self.isolation = isolation
        self.reviewer = reviewer
        self.verifyCommand = verifyCommand
        self.planner = planner
        self.executor = executor
        self.planSummary = planSummary
        self.tasks = tasks
        self.notes = notes
        self.lastActivity = lastActivity
        self.promptChars = promptChars
    }

    public var completedTasks: Int { tasks.filter { $0.state == .approved }.count }
}

// MARK: - Jobs

public struct ScheduledJob: Identifiable, Hashable, Sendable {
    public var id: String
    public var title: String
    public var prompt: String
    public var target: String
    public var schedule: String
    public var nextRun: Date?
    public var lastResult: ActivityState?
    /// How the last run ended in words (成功, 失败, 已停止, 已派发 …); nil uses the state's label.
    public var lastResultText: String?
    /// Why the last run failed, from the server.
    public var lastError: String?
    public var lastRun: Date?
    public var enabled: Bool
    /// The chat it posts to (a group job: the group's anchor member chat).
    public var sessionID: String?
    public var groupID: String?
    /// The raw schedule, for editing: interval, cron or rrule.
    public var spec: JobSchedule?

    public init(id: String, title: String, prompt: String, target: String, schedule: String, nextRun: Date?,
                lastResult: ActivityState?, lastRun: Date?, enabled: Bool, sessionID: String? = nil,
                groupID: String? = nil, spec: JobSchedule? = nil, lastResultText: String? = nil,
                lastError: String? = nil) {
        self.id = id
        self.lastResultText = lastResultText
        self.lastError = lastError
        self.title = title
        self.prompt = prompt
        self.target = target
        self.schedule = schedule
        self.nextRun = nextRun
        self.lastResult = lastResult
        self.lastRun = lastRun
        self.enabled = enabled
        self.sessionID = sessionID
        self.groupID = groupID
        self.spec = spec
    }
}

/// When a job runs, in the server's terms. Cron expressions have seconds first.
public enum JobSchedule: Hashable, Sendable {
    /// Every `seconds`; `repeats == false` runs once at `firstRun`.
    case interval(seconds: Int, repeats: Bool, firstRun: Date?)
    case cron(String)
    case rrule(String)

    /// The create/update fields for `POST /api/jobs` and `PATCH /api/jobs/{id}`.
    public var fields: [String: JSON] {
        let zone: JSON = .string(TimeZone.current.identifier)
        switch self {
        case .interval(let seconds, let repeats, let firstRun):
            var fields: [String: JSON] = ["schedule_kind": "interval", "interval_seconds": .number(Double(seconds)),
                                          "loop": .bool(repeats), "timezone": zone,
                                          "cron_expression": .null, "rrule": .null]
            if !repeats { fields["max_runs"] = 1 }
            if let firstRun { fields["first_run_at"] = .string(ISO8601DateFormatter().string(from: firstRun)) }
            return fields
        case .cron(let expression):
            return ["schedule_kind": "cron", "cron_expression": .string(expression), "timezone": zone,
                    "interval_seconds": .null, "rrule": .null]
        case .rrule(let rule):
            return ["schedule_kind": "rrule", "rrule": .string(rule), "timezone": zone,
                    "interval_seconds": .null, "cron_expression": .null]
        }
    }
}

// MARK: - Attachments, alerts and folders

/// A file added to the composer: uploading, uploaded (with the server's id) or failed.
public struct DraftAttachment: Identifiable, Hashable, Sendable {
    public enum State: Hashable, Sendable {
        case uploading
        case ready(fileID: String)
        case failed(String)
    }

    public let id: String
    public let name: String
    public let size: Int64
    public var state: State
    /// The file on this Mac, for its thumbnail and preview.
    public let localURL: URL?

    public init(id: String = UUID().uuidString, name: String, size: Int64, state: State = .uploading,
                localURL: URL? = nil) {
        self.id = id
        self.name = name
        self.size = size
        self.state = state
        self.localURL = localURL
    }

    public var fileID: String? { if case .ready(let id) = state { id } else { nil } }
}

/// An agent's unacknowledged emergency alert for one chat.
public struct EmergencyAlert: Identifiable, Hashable, Sendable {
    public var id: String { sessionID }
    public let sessionID: String
    public let alertID: String
    public let chatTitle: String
    public let message: String
    public let raisedAt: Date?
    /// Unacknowledged alerts of this chat, this one included.
    public let count: Int

    public init(sessionID: String, alertID: String, chatTitle: String, message: String, raisedAt: Date?, count: Int) {
        self.sessionID = sessionID
        self.alertID = alertID
        self.chatTitle = chatTitle
        self.message = message
        self.raisedAt = raisedAt
        self.count = count
    }

    /// One `public_emergency_session` from the emergency stream.
    public static func parse(_ session: JSON) -> EmergencyAlert? {
        let alert = session["emergency_alert"]
        guard let sessionID = session["id"].string, let alertID = alert["id"].string,
              alert["status"].string.map({ $0 == "active" }) ?? true else { return nil }
        return EmergencyAlert(sessionID: sessionID, alertID: alertID, chatTitle: session["title"].text ?? "聊天",
                              message: alert["message"].text ?? "", raisedAt: alert["raised_at"].date,
                              count: max(1, session["unacknowledged_emergency_count"].int ?? 1))
    }
}

/// A folder on the server, offered while picking a working directory.
public struct DirectorySuggestion: Identifiable, Hashable, Sendable {
    public var id: String { path }
    public let name: String
    public let path: String

    public init(name: String, path: String) {
        self.name = name
        self.path = path
    }
}

// MARK: - Queue

public struct QueuedMessage: Identifiable, Hashable, Sendable {
    public enum Hold: String, Hashable, Sendable {
        /// Kept after the user stopped the turn; waits for 继续发送 or 立即发送.
        case stopped
        /// The provider may already have it; the user should check before resending.
        case deliveryUncertain = "delivery_uncertain"
    }

    public var id: String
    public var text: String
    public var hold: Hold?

    public init(id: String = UUID().uuidString, text: String, hold: Hold? = nil) {
        self.id = id
        self.text = text
        self.hold = hold
    }
}

/// An approval or question a group member is waiting for. Members run in
/// hidden member chats, so the group view surfaces their requests.
public struct GroupApproval: Identifiable, Hashable, Sendable {
    public var id: String { interactionID }
    public let interactionID: String
    public let handle: String
    public let backend: AgentBackend
    public let sessionID: String
    /// The request as the member's own chat shows it: `.approval` or `.form`.
    public let request: TimelineItem

    public init(interactionID: String, handle: String, backend: AgentBackend, sessionID: String,
                request: TimelineItem) {
        self.interactionID = interactionID
        self.handle = handle
        self.backend = backend
        self.sessionID = sessionID
        self.request = request
    }

    public var title: String {
        switch request {
        case .approval(_, _, let title, _, _, _, _): title
        case .form(_, _, let form, _): form.title
        default: ""
        }
    }

    public var detail: String {
        switch request {
        case .approval(_, _, _, let command, _, _, _): command
        case .form(_, _, let form, _): form.fields.map(\.prompt).joined(separator: "\n")
        default: ""
        }
    }

    /// The approval answer meaning `decision`, if the request offers one.
    public func reply(for decision: ApprovalDecision) -> InteractionReply? {
        request.reply(for: decision)
    }
}

/// A group member's answer in progress: the work it has done so far in its
/// own chat, what it is doing now, and approvals it is waiting for.
public struct MemberTurn: Identifiable, Hashable, Sendable {
    public let id: String  // the dispatch id
    public let author: Author
    public var steps: [TimelineItem]
    public var approvals: [GroupApproval]
    public var activity: LiveActivity?
    /// The answer it is writing now, before it is posted to the group.
    public var liveText: String?

    public init(id: String, author: Author, steps: [TimelineItem] = [], approvals: [GroupApproval] = [],
                activity: LiveActivity? = nil, liveText: String? = nil) {
        self.id = id
        self.author = author
        self.steps = steps
        self.approvals = approvals
        self.activity = activity
        self.liveText = liveText
    }
}

/// A message the user sent that the server has not acknowledged yet.
public struct PendingSend: Identifiable, Hashable, Sendable {
    public let id: String  // the turn's client_request_id
    public let text: String
    public let time: Date

    public init(id: String, text: String, time: Date = .now) {
        self.id = id
        self.text = text
        self.time = time
    }
}

/// What the agent is doing right now, for the live status line.
public struct LiveActivity: Hashable, Sendable {
    public enum Kind: Hashable, Sendable { case starting, thinking, tool, waiting }

    public let kind: Kind
    public let label: String
    /// The latest thought (see `ThoughtSummary`) or the running command, when known.
    public let detail: String?
    public let since: Date
    /// The chat's provider, whose mark the status shows; nil in group rows,
    /// which name the member above.
    public let backend: AgentBackend?

    public init(kind: Kind, label: String, detail: String? = nil, since: Date, backend: AgentBackend? = nil) {
        self.kind = kind
        self.label = label
        self.detail = detail
        self.since = since
        self.backend = backend
    }
}

/// The one line a live status shows for a provider's thinking: Codex's
/// newest bold step heading, otherwise the last sentence written so far.
public enum ThoughtSummary {
    public static func line(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let lines = trimmed.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }
        if let heading = lines.last(where: { $0.count > 4 && $0.hasPrefix("**") && $0.hasSuffix("**") }) {
            return plain(String(heading.dropFirst(2).dropLast(2)))
        }
        let paragraph = lines.last { !$0.isEmpty } ?? trimmed
        var sentences: [String] = []
        var current = ""
        var previous: Character?
        for character in paragraph {
            // A full stop ends a sentence only before a space ("3.5" and "file.swift" do not).
            if previous == ".", character == " " {
                sentences.append(current)
                current = ""
                previous = character
                continue
            }
            current.append(character)
            if "。！？!?；".contains(character) {
                sentences.append(current)
                current = ""
            }
            previous = character
        }
        sentences.append(current)
        let parts = sentences.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard var last = parts.last else { return nil }
        // A sentence just begun says too little on its own.
        if last.count < 8, parts.count > 1 { last = parts[parts.count - 2] + " " + last }
        return plain(last)
    }

    /// Without Markdown emphasis and code marks.
    private static func plain(_ text: String) -> String {
        text.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "")
            .trimmingCharacters(in: .whitespaces)
    }

    /// The turn's latest thinking, unless the answer has started since.
    public static func latest(in items: some BidirectionalCollection<TimelineItem>) -> String? {
        for item in items.reversed() {
            switch item {
            case .thinking(_, _, let text): return text
            case .message: return nil
            default: continue
            }
        }
        return nil
    }
}


/// One tmux window of a chat's terminal.
public struct TerminalWindow: Identifiable, Hashable, Sendable {
    public let id: String
    public let index: Int
    public let name: String
    public let active: Bool
    public let panes: Int

    public init(id: String, index: Int, name: String, active: Bool, panes: Int) {
        self.id = id
        self.index = index
        self.name = name
        self.active = active
        self.panes = panes
    }

    static func parse(_ json: JSON) -> [TerminalWindow] {
        json["windows"].array.compactMap { item in
            guard let index = item["index"].int else { return nil }
            return TerminalWindow(id: item["id"].string ?? "@\(index)", index: index,
                                  name: item["name"].string ?? "", active: item["active"].bool ?? false,
                                  panes: item["panes"].int ?? 1)
        }.sorted { $0.index < $1.index }
    }
}
