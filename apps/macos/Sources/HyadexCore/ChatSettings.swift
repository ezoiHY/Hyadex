import Foundation
import HyadexAPI

/// What the agent may do with scheduled jobs from inside a turn.
public enum JobsAccess: String, CaseIterable, Identifiable, Hashable, Sendable {
    case full
    case readOnly = "read_only"
    case blocked

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .full: "允许"
        case .readOnly: "只读"
        case .blocked: "禁止"
        }
    }
}

/// A chat's advanced settings, read from the server's full chat record.
public struct ChatSettings: Hashable, Sendable {
    /// Extra instructions sent with every turn ("" = none).
    public var systemPrompt: String
    /// Most subagents running at once; nil inherits the agent's own setting.
    public var subagentLimit: Int?
    /// Whether this chat's agent supports a per-chat limit, and when it applies.
    public var subagentLimitSupported: Bool
    public var subagentLimitNote: String
    /// When a saved limit takes effect: "next_idle_provider_start" (Claude),
    /// "new_or_reloaded_threads" (Codex) or "next_provider_process_start".
    public var subagentLimitAppliesTo: String
    /// Whether the server names the chat from its first turns.
    public var autoTitle: Bool
    public var jobsAccess: JobsAccess

    public init(systemPrompt: String = "", subagentLimit: Int? = nil, subagentLimitSupported: Bool = true,
                subagentLimitNote: String = "", subagentLimitAppliesTo: String = "", autoTitle: Bool = true, jobsAccess: JobsAccess = .full) {
        self.systemPrompt = systemPrompt
        self.subagentLimit = subagentLimit
        self.subagentLimitSupported = subagentLimitSupported
        self.subagentLimitNote = subagentLimitNote
        self.subagentLimitAppliesTo = subagentLimitAppliesTo
        self.autoTitle = autoTitle
        self.jobsAccess = jobsAccess
    }

    /// Longest system prompt the server accepts by default.
    public static let maxSystemPromptCharacters = 24_000

    public static func parse(_ session: JSON) -> ChatSettings {
        let control = session["subagent_limit_control"]
        return ChatSettings(
            systemPrompt: session["system_prompt"].string ?? "",
            subagentLimit: session["subagent_limit"].int,
            subagentLimitSupported: control["supported"].bool ?? true,
            subagentLimitNote: control["message"].string ?? "",
            subagentLimitAppliesTo: control["applies_to"].string ?? "",
            autoTitle: session["auto_title_enabled"].bool ?? true,
            jobsAccess: session["provider_jobs_access"].string.flatMap(JobsAccess.init(rawValue:)) ?? .full)
    }

    /// The PATCH fields that turn `self` into `updated`; unchanged fields are left out.
    public func changes(to updated: ChatSettings) -> [String: JSON] {
        var fields: [String: JSON] = [:]
        let prompt = updated.systemPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if prompt != systemPrompt.trimmingCharacters(in: .whitespacesAndNewlines) {
            fields["system_prompt"] = prompt.isEmpty ? .null : .string(prompt)
        }
        if updated.subagentLimit != subagentLimit {
            fields["subagent_limit"] = updated.subagentLimit.map { .number(Double($0)) } ?? .null
        }
        if updated.autoTitle != autoTitle { fields["auto_title_enabled"] = .bool(updated.autoTitle) }
        if updated.jobsAccess != jobsAccess { fields["provider_jobs_access"] = .string(updated.jobsAccess.rawValue) }
        return fields
    }
}

/// A group's editable settings.
public struct GroupSettings: Hashable, Sendable {
    /// Who answers a message that mentions nobody; nil = the first member.
    public var defaultResponder: String?
    /// Agent-to-agent hand-offs allowed in one round.
    public var maxAgentHops: Int
    /// Shared context every member receives once per version.
    public var brief: String

    public static let defaultMaxAgentHops = 4
    public static let maxAgentHopsRange = 0...12
    public static let maxBriefCharacters = 8_000

    public init(defaultResponder: String? = nil, maxAgentHops: Int = defaultMaxAgentHops, brief: String = "") {
        self.defaultResponder = defaultResponder
        self.maxAgentHops = maxAgentHops
        self.brief = brief
    }

    public static func parse(_ group: JSON) -> GroupSettings {
        GroupSettings(defaultResponder: group["settings"]["default_responder"].string,
                      maxAgentHops: group["settings"]["max_agent_hops"].int ?? defaultMaxAgentHops,
                      brief: group["brief"]["text"].string ?? "")
    }

    /// The PATCH body that turns `self` into `updated`; empty when nothing changed.
    public func changes(to updated: GroupSettings) -> [String: JSON] {
        var fields: [String: JSON] = [:]
        var settings: [String: JSON] = [:]
        if updated.defaultResponder != defaultResponder {
            settings["default_responder"] = updated.defaultResponder.map(JSON.string) ?? .null
        }
        if updated.maxAgentHops != maxAgentHops { settings["max_agent_hops"] = .number(Double(updated.maxAgentHops)) }
        if !settings.isEmpty { fields["settings"] = .object(settings) }
        let brief = updated.brief.trimmingCharacters(in: .whitespacesAndNewlines)
        if brief != self.brief.trimmingCharacters(in: .whitespacesAndNewlines) { fields["brief"] = .string(brief) }
        return fields
    }
}

/// Whose settings the settings sheet edits.
public enum SettingsEditorTarget: Identifiable, Hashable, Sendable {
    case chat(String)
    case group(String)

    public var id: String {
        switch self {
        case .chat(let id): "chat-\(id)"
        case .group(let id): "group-\(id)"
        }
    }
}
