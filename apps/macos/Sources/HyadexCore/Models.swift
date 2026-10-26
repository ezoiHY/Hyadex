import Foundation

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
        case .work: "搜索聊天"
        case .group: "搜索讨论组"
        case .autopilot: "搜索自动接力"
        }
    }
}

// MARK: - Chats

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

    public init(id: String, title: String, backend: AgentBackend, model: String, effort: String = "",
                permission: String, cwd: String, preview: String, lastActivity: Date,
                state: ActivityState = .idle, pinned: Bool = false, unread: Bool = false, tokensUsed: Int = 0) {
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

    public init(id: String, title: String, cwd: String, participants: [Participant], defaultResponder: String,
                preview: String, lastActivity: Date, unread: Bool = false, promptChars: Int = 0, dispatches: Int = 0) {
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
    public var lastRun: Date?
    public var enabled: Bool

    public init(id: String, title: String, prompt: String, target: String, schedule: String, nextRun: Date?,
                lastResult: ActivityState?, lastRun: Date?, enabled: Bool) {
        self.id = id
        self.title = title
        self.prompt = prompt
        self.target = target
        self.schedule = schedule
        self.nextRun = nextRun
        self.lastResult = lastResult
        self.lastRun = lastRun
        self.enabled = enabled
    }
}

// MARK: - Queue

public struct QueuedMessage: Identifiable, Hashable, Sendable {
    public var id: String
    public var text: String

    public init(id: String = UUID().uuidString, text: String) {
        self.id = id
        self.text = text
    }
}

/// An approval a group member is waiting for. Members run in hidden member
/// chats, so the group view surfaces their requests.
public struct GroupApproval: Identifiable, Hashable, Sendable {
    public var id: String { interactionID }
    public let interactionID: String
    public let handle: String
    public let backend: AgentBackend
    public let sessionID: String
    public let title: String
    public let detail: String
    public let reason: String
    public let allowsSession: Bool

    public init(interactionID: String, handle: String, backend: AgentBackend, sessionID: String,
                title: String, detail: String, reason: String, allowsSession: Bool) {
        self.interactionID = interactionID
        self.handle = handle
        self.backend = backend
        self.sessionID = sessionID
        self.title = title
        self.detail = detail
        self.reason = reason
        self.allowsSession = allowsSession
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
    /// Live reasoning text or the running command, when known.
    public let detail: String?
    public let since: Date

    public init(kind: Kind, label: String, detail: String? = nil, since: Date) {
        self.kind = kind
        self.label = label
        self.detail = detail
        self.since = since
    }
}
