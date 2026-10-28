import Foundation
import HyadexAPI

// Goals, slash commands and MCP servers come from each provider's own
// runtime: Claude Code's `/goal`, Codex's persistent thread goal, the
// commands and skills the agent discovers in the chat's folder, and the MCP
// servers it loaded. The app only shows and drives them.

// MARK: - Goals

/// A chat's native goal: Claude Code's `/goal` completion condition or
/// Codex's persistent thread objective.
public struct ChatGoal: Hashable, Sendable {
    public enum Status: String, Hashable, Sendable {
        // Claude Code
        case achieved, cleared, failed
        // Codex
        case paused, blocked, usageLimited, budgetLimited, complete
        // Both
        case active
        /// Accepted by the server; the provider has not confirmed it yet.
        case starting
    }

    public var backend: AgentBackend
    public var text: String
    public var status: Status
    /// Claude: how many times the goal check ran.
    public var iterations: Int?
    /// Claude: the evaluator's last explanation.
    public var reason: String?
    public var tokensUsed: Int?
    public var tokenBudget: Int?
    public var secondsUsed: Int?
    /// Codex: the server-enforced wall-clock limit.
    public var timeBudgetSeconds: Int?
    public var timeBudgetExhausted: Bool

    public init(backend: AgentBackend, text: String, status: Status, iterations: Int? = nil, reason: String? = nil,
                tokensUsed: Int? = nil, tokenBudget: Int? = nil, secondsUsed: Int? = nil,
                timeBudgetSeconds: Int? = nil, timeBudgetExhausted: Bool = false) {
        self.backend = backend
        self.text = text
        self.status = status
        self.iterations = iterations
        self.reason = reason
        self.tokensUsed = tokensUsed
        self.tokenBudget = tokenBudget
        self.secondsUsed = secondsUsed
        self.timeBudgetSeconds = timeBudgetSeconds
        self.timeBudgetExhausted = timeBudgetExhausted
    }

    /// The goal still drives the agent (or can be resumed).
    public var isOpen: Bool {
        switch status {
        case .active, .starting, .paused, .blocked, .usageLimited, .budgetLimited: true
        case .achieved, .cleared, .failed, .complete: false
        }
    }

    public var canPause: Bool { backend == .codex && status == .active }
    public var canResume: Bool {
        backend == .codex && [.paused, .blocked, .usageLimited, .budgetLimited].contains(status) && !timeBudgetExhausted
    }

    public var statusLabel: String {
        switch status {
        case .active: "进行中"
        case .starting: "正在设定"
        case .achieved, .complete: "已达成"
        case .cleared: "已清除"
        case .failed: "未达成"
        case .paused: timeBudgetExhausted ? "时间用完" : "已暂停"
        case .blocked: "受阻"
        case .usageLimited: "额度受限"
        case .budgetLimited: "token 用完"
        }
    }

    public var activity: ActivityState {
        switch status {
        case .active, .starting: .running
        case .paused: .paused
        case .blocked, .usageLimited, .budgetLimited: .needsAction
        case .achieved, .complete: .done
        case .cleared: .idle
        case .failed: .failed
        }
    }

    /// Claude's goal from `GET …/claude/runtime` (`goal`, `goal_starting`).
    public static func claude(runtime json: JSON) -> ChatGoal? {
        let goal = json["goal"]
        if let condition = goal["condition"].text {
            let status = Status(rawValue: goal["status"].string ?? "") ?? .active
            return ChatGoal(backend: .claude, text: condition,
                            status: json["goal_starting"].bool == true ? .starting : status,
                            iterations: goal["iterations"].int, reason: goal["last_reason"].text,
                            tokensUsed: goal["tokens"].int,
                            secondsUsed: goal["duration_ms"].int.map { $0 / 1000 })
        }
        return nil
    }

    /// Codex's goal from `GET …/codex/goal` or `…/codex/runtime`.
    public static func codex(_ json: JSON) -> ChatGoal? {
        let goal = json["goal"]
        guard let objective = goal["objective"].text else { return nil }
        return ChatGoal(backend: .codex, text: objective,
                        status: Status(rawValue: goal["status"].string ?? "") ?? .active,
                        tokensUsed: goal["tokensUsed"].int, tokenBudget: goal["tokenBudget"].int,
                        secondsUsed: goal["timeUsedSeconds"].int,
                        timeBudgetSeconds: json["time_budget_seconds"].int,
                        timeBudgetExhausted: json["time_budget_exhausted"].bool ?? false)
    }
}

/// What the goal controls may do for a chat right now.
public struct GoalSupport: Hashable, Sendable {
    public var available: Bool
    public var reason: String?

    public init(available: Bool, reason: String? = nil) {
        self.available = available
        self.reason = reason
    }

    public static func claude(runtime json: JSON) -> GoalSupport {
        json["features"]["goals"].bool == true
            ? GoalSupport(available: true)
            : GoalSupport(available: false, reason: "这台服务器上的 Claude Code 不支持 /goal（需要 Agent SDK 连接）。")
    }

    public static func codex(runtime json: JSON) -> GoalSupport {
        let enabled = json["goals_enabled"].bool ?? json["enabled"].bool ?? false
        return enabled
            ? GoalSupport(available: true)
            : GoalSupport(available: false, reason: "服务器关闭了 Codex 持久目标。")
    }
}

/// A group's goal: a native goal set on one member's chat.
public struct GroupGoal: Hashable, Sendable {
    public var handle: String
    public var objective: String
    public var setAt: Date?

    public init(handle: String, objective: String, setAt: Date? = nil) {
        self.handle = handle
        self.objective = objective
        self.setAt = setAt
    }

    static func parse(_ json: JSON) -> GroupGoal? {
        guard let handle = json["handle"].string, let objective = json["objective"].text else { return nil }
        return GroupGoal(handle: handle, objective: objective, setAt: json["set_at"].date)
    }
}

// MARK: - Slash commands and skills

/// One command or skill the chat's agent offers, as the server discovered it.
public struct ProviderCommand: Identifiable, Hashable, Sendable {
    public enum Kind: String, Hashable, Sendable { case command, skill }

    /// Opaque, server-issued; sent back with the turn.
    public let id: String
    public let name: String
    public let label: String
    public let detail: String
    /// What the message starts with, e.g. "/review".
    public let invocation: String
    public let kind: Kind
    /// Where it was defined (user, project …), when the provider says.
    public let scope: String?

    public init(id: String, name: String, label: String, detail: String, invocation: String, kind: Kind,
                scope: String? = nil) {
        self.id = id
        self.name = name
        self.label = label
        self.detail = detail
        self.invocation = invocation
        self.kind = kind
        self.scope = scope
    }
}

/// `GET /api/sessions/{id}/provider-commands`.
public struct CommandInventory: Hashable, Sendable {
    public var revision: String
    public var available: Bool
    public var reason: String?
    public var commands: [ProviderCommand]

    public init(revision: String, available: Bool, reason: String? = nil, commands: [ProviderCommand]) {
        self.revision = revision
        self.available = available
        self.reason = reason
        self.commands = commands
    }

    public static func parse(_ json: JSON) -> CommandInventory? {
        guard let revision = json["revision"].string else { return nil }
        let commands = json["commands"].array.compactMap { item -> ProviderCommand? in
            guard let id = item["id"].string, let invocation = item["invocation"].string, invocation.hasPrefix("/")
            else { return nil }
            let name = item["name"].text ?? String(invocation.dropFirst())
            return ProviderCommand(id: id, name: name, label: item["label"].text ?? name,
                                   detail: Self.trimScopeSuffix(item["description"].text ?? ""),
                                   invocation: invocation,
                                   kind: item["kind"].string == "skill" ? .skill : .command,
                                   scope: item["scope"].string)
        }
        return CommandInventory(revision: revision, available: json["support"]["available"].bool ?? false,
                                reason: json["support"]["reason"].text, commands: commands)
    }

    /// Claude appends "(user)" or "(project)" to descriptions; the scope is shown separately.
    private static func trimScopeSuffix(_ text: String) -> String {
        guard let range = text.range(of: #"\s*\((user|project|local|plugin[^)]*)\)$"#, options: .regularExpression)
        else { return text }
        return String(text[..<range.lowerBound])
    }

    /// Commands for a partial "/name" the user is typing, best matches first.
    public func matches(_ partial: String, limit: Int = 8) -> [ProviderCommand] {
        let query = partial.lowercased()
        guard !query.isEmpty else { return Array(commands.prefix(limit)) }
        func rank(_ command: ProviderCommand) -> Int? {
            let name = String(command.invocation.dropFirst()).lowercased()
            if name.hasPrefix(query) { return 0 }
            if command.label.lowercased().hasPrefix(query) { return 1 }
            if name.contains(query) || command.label.lowercased().contains(query) { return 2 }
            return nil
        }
        return commands.compactMap { command in rank(command).map { ($0, command) } }
            .sorted { $0.0 < $1.0 }
            .prefix(limit)
            .map(\.1)
    }

    /// The command a message invokes: it starts with the invocation, then a
    /// space, a newline or nothing.
    public func command(for message: String) -> ProviderCommand? {
        guard message.hasPrefix("/") else { return nil }
        let token = message.prefix { !$0.isWhitespace }
        return commands.first { $0.invocation == token }
    }
}

/// Slash-command input in the composer.
public enum SlashParser {
    /// The partial command name while the user types the first word ("/rev" → "rev").
    public static func partialCommand(in draft: String) -> String? {
        guard draft.hasPrefix("/"), !draft.contains(where: \.isWhitespace) else { return nil }
        return String(draft.dropFirst())
    }

    /// The argument of a "/goal …" message ("" for a bare "/goal").
    public static func goalArgument(_ text: String) -> String? {
        guard text.hasPrefix("/goal"), text.dropFirst(5).first.map(\.isWhitespace) ?? true else { return nil }
        return text.dropFirst(5).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - MCP servers

/// One MCP server the chat's agent loaded.
public struct MCPServer: Identifiable, Hashable, Sendable {
    public enum Status: String, Hashable, Sendable {
        case connected, pending, failed, needsAuth = "needs-auth", disabled, unknown
    }

    public var id: String { name }
    public let name: String
    public let status: Status
    public let enabled: Bool
    public let error: String?
    public let scope: String?
    public let serverInfo: String?
    public let toolCount: Int?

    public init(name: String, status: Status, enabled: Bool = true, error: String? = nil, scope: String? = nil,
                serverInfo: String? = nil, toolCount: Int? = nil) {
        self.name = name
        self.status = status
        self.enabled = enabled
        self.error = error
        self.scope = scope
        self.serverInfo = serverInfo
        self.toolCount = toolCount
    }

    public var statusLabel: String {
        switch status {
        case .connected: "已连接"
        case .pending: "连接中"
        case .failed: "连接失败"
        case .needsAuth: "需要登录"
        case .disabled: "已停用"
        // Codex starts a chat's MCP servers with its thread; before that there is no state.
        case .unknown: error == nil ? "未启动" : "未知"
        }
    }

    public var activity: ActivityState {
        switch status {
        case .connected: .done
        case .pending: .running
        case .failed: .failed
        case .needsAuth: .needsAction
        case .disabled, .unknown: .idle
        }
    }

    public var scopeLabel: String? {
        switch scope {
        case "user": "用户"
        case "project": "项目"
        case "local": "本地"
        case "claudeai": "claude.ai"
        case "managed": "托管"
        case let other?: other
        case nil: nil
        }
    }
}

/// `GET /api/sessions/{id}/{claude|codex}/mcp`.
public struct MCPStatus: Hashable, Sendable {
    public var available: Bool
    /// Claude: the SDK connection generation control requests must name.
    public var generation: String?
    public var servers: [MCPServer]
    public var truncated: Bool
    public var reason: String?
    /// Whether servers can be reconnected, enabled or disabled from the app.
    public var controllable: Bool

    public init(available: Bool, generation: String? = nil, servers: [MCPServer], truncated: Bool = false,
                reason: String? = nil, controllable: Bool = false) {
        self.available = available
        self.generation = generation
        self.servers = servers
        self.truncated = truncated
        self.reason = reason
        self.controllable = controllable
    }

    public static func parse(_ json: JSON, backend: AgentBackend) -> MCPStatus? {
        guard json["version"].int == 1 else { return nil }
        let servers = json["servers"].array.compactMap { item -> MCPServer? in
            guard let name = item["name"].string else { return nil }
            let info = item["server_info"]
            let infoText = info["name"].text.map { name in info["version"].text.map { "\(name) \($0)" } ?? name }
            return MCPServer(name: name, status: MCPServer.Status(rawValue: item["status"].string ?? "") ?? .unknown,
                             enabled: item["enabled"].bool ?? true, error: item["error"].text,
                             scope: item["scope"].string, serverInfo: infoText, toolCount: item["tool_count"].int)
        }
        let available = json["available"].bool ?? false
        return MCPStatus(available: available, generation: json["generation"].string, servers: servers,
                         truncated: json["truncated"].bool ?? false, reason: json["reason"]["message"].text,
                         controllable: available && backend == .claude && json["generation"].string != nil)
    }
}
