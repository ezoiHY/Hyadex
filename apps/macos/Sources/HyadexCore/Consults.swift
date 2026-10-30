import Foundation
import HyadexAPI

/// How the asked chat answers: from what it already knows, or by sending a
/// read-only copy of itself to check its files.
public enum ConsultMode: String, Hashable, Sendable {
    case basic, deep

    public var label: String { self == .deep ? "深度" : "基础" }
}

/// Which questions other chats in the project may send a chat (its settings).
public enum ConsultInbound: String, CaseIterable, Identifiable, Hashable, Sendable {
    case off, basic, deep

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .off: "关闭"
        case .basic: "只允许基础"
        case .deep: "基础和深度"
        }
    }
}

public enum ConsultStatus: String, Hashable, Sendable {
    case awaitingApproval = "awaiting_approval"
    case queued, running, completed, failed, cancelled, denied, expired, interrupted

    public var isActive: Bool { self == .awaitingApproval || self == .queued || self == .running }

    public var label: String {
        switch self {
        case .awaitingApproval: "等你批准"
        case .queued: "排队中"
        case .running: "正在回答"
        case .completed: "已回答"
        case .failed: "没有答上来"
        case .cancelled: "已取消"
        case .denied: "已拒绝"
        case .expired: "批准已超时"
        case .interrupted: "被中断"
        }
    }
}

/// One thing a deep consult's read-only copy did: read a file, search, run a command.
public struct ConsultStep: Hashable, Sendable {
    public var kind: String
    public var title: String

    public init(kind: String, title: String) {
        self.kind = kind
        self.title = title
    }

    public var symbol: String {
        switch kind {
        case "read": "doc.text"
        case "search": "magnifyingglass"
        case "command": "terminal"
        default: "gearshape"
        }
    }
}

/// A consult as the server records it: who asked whom, how, and what came back.
public struct ConsultRecord: Identifiable, Hashable, Sendable {
    public let id: String
    /// The asking chat; nil when the user asked from the side panel.
    public var sourceID: String?
    public var sourceTitle: String?
    public var sourceBackend: AgentBackend?
    public var targetID: String
    public var targetTitle: String
    public var targetBackend: AgentBackend?
    public var mode: ConsultMode
    public var question: String
    public var status: ConsultStatus
    public var answer: String?
    /// Whether the asked chat was sure; nil when it did not say.
    public var confident: Bool?
    /// A basic answer the chat was unsure of: asking again in depth may help.
    public var suggestDeep: Bool
    public var steps: [ConsultStep]
    public var error: String?
    /// Why it ended without an answer: turn_ended, abandoned, timeout, failed…
    public var errorCode: String?
    public var createdAt: Date
    public var duration: Double?
    public var inputTokens: Int?
    public var outputTokens: Int?

    public init(id: String, sourceID: String? = nil, sourceTitle: String? = nil, sourceBackend: AgentBackend? = nil,
                targetID: String, targetTitle: String, targetBackend: AgentBackend? = nil, mode: ConsultMode,
                question: String, status: ConsultStatus, answer: String? = nil, confident: Bool? = nil,
                suggestDeep: Bool = false, steps: [ConsultStep] = [], error: String? = nil, errorCode: String? = nil,
                createdAt: Date = .now,
                duration: Double? = nil, inputTokens: Int? = nil, outputTokens: Int? = nil) {
        self.id = id
        self.sourceID = sourceID
        self.sourceTitle = sourceTitle
        self.sourceBackend = sourceBackend
        self.targetID = targetID
        self.targetTitle = targetTitle
        self.targetBackend = targetBackend
        self.mode = mode
        self.question = question
        self.status = status
        self.answer = answer
        self.confident = confident
        self.suggestDeep = suggestDeep
        self.steps = steps
        self.error = error
        self.errorCode = errorCode
        self.createdAt = createdAt
        self.duration = duration
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
    }

    /// Why there is no answer, in a line; nil when the status says it all.
    public var problem: String? {
        guard !status.isActive, status != .completed else { return nil }
        switch errorCode {
        case "turn_ended": return "提问的那一轮已经结束"
        case "abandoned": return "提问的一方不再等待"
        case "chat_deleted": return "有一方聊天已被删除"
        case "approval_expired": return "10 分钟内没有人批准"
        case "interrupted": return "服务器重启，没来得及回答"
        case "timeout": return mode == .deep ? "5 分钟内没有查完" : "2 分钟内没有回答"
        case "denied", "cancelled": return nil
        default: return error
        }
    }

    public static func parse(_ json: JSON) -> ConsultRecord? {
        guard let id = json["id"].string, let targetID = json["target"]["id"].string,
              let status = json["status"].string.flatMap(ConsultStatus.init(rawValue:)) else { return nil }
        let source = json["source"]
        let usage = json["usage"]
        return ConsultRecord(
            id: id,
            sourceID: source["kind"].string == "chat" ? source["id"].string : nil,
            sourceTitle: source["title"].string,
            sourceBackend: source["backend"].string.flatMap(AgentBackend.named),
            targetID: targetID,
            targetTitle: json["target"]["title"].string ?? "聊天",
            targetBackend: json["target"]["backend"].string.flatMap(AgentBackend.named),
            mode: json["mode"].string.flatMap(ConsultMode.init(rawValue:)) ?? .basic,
            question: json["question"].string ?? "",
            status: status,
            answer: json["answer"].string,
            confident: json["confident"].bool,
            suggestDeep: json["suggest_deep"].bool == true,
            steps: json["steps"].array.map { ConsultStep(kind: $0["kind"].string ?? "other", title: $0["title"].string ?? "") },
            error: json["error"]["message"].string,
            errorCode: json["error"]["code"].string,
            createdAt: json["created_at"].double.map { Date(timeIntervalSince1970: $0) } ?? .now,
            duration: json["duration_seconds"].double,
            inputTokens: usage["input_tokens"].int,
            outputTokens: usage["output_tokens"].int
        )
    }
}

/// A chat's consults: what it asked other chats, and what it was asked.
public struct ChatConsults: Hashable, Sendable {
    public var outgoing: [ConsultRecord]
    public var incoming: [ConsultRecord]

    public init(outgoing: [ConsultRecord] = [], incoming: [ConsultRecord] = []) {
        self.outgoing = outgoing
        self.incoming = incoming
    }

    public static func parse(_ json: JSON) -> ChatConsults {
        ChatConsults(outgoing: json["outgoing"].array.compactMap(ConsultRecord.parse),
                     incoming: json["incoming"].array.compactMap(ConsultRecord.parse))
    }
}

/// A consult still waiting or answering anywhere on the server (for the sidebar).
public struct ActiveConsult: Hashable, Sendable {
    public var id: String
    public var sourceID: String?
    public var targetID: String
    public var mode: ConsultMode
    public var status: ConsultStatus

    public init(id: String, sourceID: String?, targetID: String, mode: ConsultMode, status: ConsultStatus) {
        self.id = id
        self.sourceID = sourceID
        self.targetID = targetID
        self.mode = mode
        self.status = status
    }

    public static func parse(_ json: JSON) -> ActiveConsult? {
        guard let id = json["id"].string, let target = json["target_id"].string,
              let status = json["status"].string.flatMap(ConsultStatus.init(rawValue:)) else { return nil }
        return ActiveConsult(id: id, sourceID: json["source_id"].string, targetID: target,
                             mode: json["mode"].string.flatMap(ConsultMode.init(rawValue:)) ?? .basic, status: status)
    }
}

/// A question in a chat's timeline to another chat: the agent's `ask` call
/// (with the helper's receipt once it returned), or one the user asked with @.
public struct ConsultCall: Hashable, Sendable {
    public var target: String
    public var deep: Bool
    public var question: String
    public var running: Bool
    public var receipt: ConsultReceipt?
    /// Known up front when the user asked; an agent's call learns it from its receipt.
    public var consultID: String?
    /// The user asked with @: the answer is for the user, not this chat's agent.
    public var byUser: Bool

    public init(target: String, deep: Bool, question: String, running: Bool = true, receipt: ConsultReceipt? = nil,
                consultID: String? = nil, byUser: Bool = false) {
        self.target = target
        self.deep = deep
        self.question = question
        self.running = running
        self.receipt = receipt
        self.consultID = consultID
        self.byUser = byUser
    }

    /// Hyadex's own provider tool, as Claude (`mcp__<server>__run`) and Codex (`<server>/run`) name it.
    public static func isHyadexTool(_ name: String) -> Bool {
        name.contains("_hyadex_internal_provider_")
    }

    /// An `ask TITLE [--deep]` call of the Hyadex helper tool; nil for anything else (including `ask list`).
    public static func parse(tool: JSON) -> ConsultCall? {
        guard isHyadexTool(tool["name"].string ?? ""), tool["input"]["helper"].string == "ask" else { return nil }
        let arguments = tool["input"]["arguments"].array.compactMap(\.string)
        guard arguments.first == "ask" else { return nil }
        var target: String?
        var question: String?
        var position = 1
        while position < arguments.count {
            let argument = arguments[position]
            if argument == "--question" || argument == "--request-id" {
                if argument == "--question", position + 1 < arguments.count { question = arguments[position + 1] }
                position += 2
                continue
            }
            if argument.hasPrefix("--question=") { question = String(argument.dropFirst("--question=".count)) }
            if !argument.hasPrefix("--") {
                if target == nil { target = argument } else if question == nil { question = argument }
            }
            position += 1
        }
        guard let target else { return nil }
        let asked = question ?? tool["input"]["stdin"].string ?? ""
        return ConsultCall(target: target, deep: arguments.contains("--deep"),
                           question: asked.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}

/// A chat this one may ask: what the @ list in its composer offers.
public struct ConsultTarget: Identifiable, Hashable, Sendable {
    public let id: String
    public var title: String
    public var backend: AgentBackend?
    /// Whether it also answers in depth (its settings may allow only basic).
    public var allowsDeep: Bool
    /// Whether it is working right now (a deep copy may see files mid-change).
    public var working: Bool

    public init(id: String, title: String, backend: AgentBackend? = nil, allowsDeep: Bool = true, working: Bool = false) {
        self.id = id
        self.title = title
        self.backend = backend
        self.allowsDeep = allowsDeep
        self.working = working
    }

    public static func parse(_ json: JSON) -> ConsultTarget? {
        guard let id = json["id"].string else { return nil }
        return ConsultTarget(id: id, title: json["title"].string ?? "聊天",
                             backend: json["backend"].string.flatMap(AgentBackend.named),
                             allowsDeep: json["modes"].array.contains { $0.string == "deep" },
                             working: json["working"].bool == true)
    }
}

/// `@「title」` names another chat in a composer. Written at the very start of a
/// message, it asks that chat directly; anywhere else it is ordinary text the
/// chat's agent reads (and may ask with its `ask` helper).
public enum ChatMention {
    public static func token(_ title: String) -> String { "@「\(title)」" }

    /// The chat name being typed after the last "@" (`"问 @后端"` → `"后端"`), or nil.
    public static func partial(in text: String) -> String? {
        guard let at = text.lastIndex(of: "@") else { return nil }
        let before = at > text.startIndex ? text[text.index(before: at)] : " "
        guard before.isWhitespace else { return nil }
        var tail = String(text[text.index(after: at)...])
        guard !tail.contains("」"), !tail.contains("\n"), tail.count <= 60 else { return nil }
        if tail.hasPrefix("「") { tail.removeFirst() }
        return tail
    }

    /// The chats whose titles contain `partial`, prefix matches first.
    public static func matches(_ partial: String, in targets: [ConsultTarget]) -> [ConsultTarget] {
        let wanted = partial.trimmingCharacters(in: .whitespaces)
        guard !wanted.isEmpty else { return Array(targets.prefix(6)) }
        let found = targets.filter { $0.title.localizedCaseInsensitiveContains(wanted) }
        return Array((found.filter { $0.title.lowercased().hasPrefix(wanted.lowercased()) }
                      + found.filter { !$0.title.lowercased().hasPrefix(wanted.lowercased()) }).prefix(6))
    }

    /// A message that opens with a mention of one of `targets`: whom it asks, and the question.
    public static func direct(in text: String, targets: [ConsultTarget]) -> (target: ConsultTarget, question: String)? {
        let trimmed = text.drop { $0.isWhitespace }
        guard trimmed.hasPrefix("@「"), let close = trimmed.firstIndex(of: "」") else { return nil }
        let title = String(trimmed[trimmed.index(trimmed.startIndex, offsetBy: 2)..<close])
        guard let target = targets.first(where: { $0.title == title }) else { return nil }
        let question = trimmed[trimmed.index(after: close)...].trimmingCharacters(in: .whitespacesAndNewlines)
        return (target, question)
    }
}

/// What the `ask` helper printed: the answer, or why there is none.
public struct ConsultReceipt: Hashable, Sendable {
    public var ok: Bool
    public var consultID: String?
    public var chatID: String?
    public var chatTitle: String?
    public var status: ConsultStatus?
    public var answer: String?
    public var confident: Bool?
    public var suggestDeep: Bool
    public var error: String?

    public init(ok: Bool, consultID: String? = nil, chatID: String? = nil, chatTitle: String? = nil,
                status: ConsultStatus? = nil, answer: String? = nil, confident: Bool? = nil,
                suggestDeep: Bool = false, error: String? = nil) {
        self.ok = ok
        self.consultID = consultID
        self.chatID = chatID
        self.chatTitle = chatTitle
        self.status = status
        self.answer = answer
        self.confident = confident
        self.suggestDeep = suggestDeep
        self.error = error
    }

    /// From the tool's output: a JSON receipt, or plain text when the helper failed early.
    /// Codex hands the receipt over as an MCP result, its text inside `content`.
    public static func parse(output: String, failed: Bool) -> ConsultReceipt {
        var text = output.trimmingCharacters(in: .whitespacesAndNewlines)
        var json = text.data(using: .utf8).flatMap { try? JSONDecoder().decode(JSON.self, from: $0) }
        if let wrapper = json, wrapper["ok"].bool == nil, !wrapper["content"].array.isEmpty {
            text = wrapper["content"].array.compactMap { $0["text"].string }.joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            json = text.data(using: .utf8).flatMap { try? JSONDecoder().decode(JSON.self, from: $0) }
        }
        guard let json, json["ok"].bool != nil else {
            return ConsultReceipt(ok: false, error: text.isEmpty ? (failed ? "询问失败" : nil) : text)
        }
        return ConsultReceipt(
            ok: json["ok"].bool == true,
            consultID: json["consult_id"].string,
            chatID: json["chat_id"].string,
            chatTitle: json["chat"].string,
            status: json["status"].string.flatMap(ConsultStatus.init(rawValue:)),
            answer: json["answer"].string,
            confident: json["confident"].bool,
            suggestDeep: json["suggest_deep"].bool == true,
            error: json["error"].string
        )
    }
}
