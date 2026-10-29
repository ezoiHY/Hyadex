import Foundation
import HyadexAPI

/// What the user sends back for one agent interaction: the `response` body
/// the server expects for that kind of request, and how the answered card reads.
public struct InteractionReply: Hashable, Sendable {
    public let response: JSON
    public let outcome: ApprovalDecision

    public init(response: JSON, outcome: ApprovalDecision) {
        self.response = response
        self.outcome = outcome
    }
}

/// One answer an approval card offers.
public struct ApprovalChoice: Identifiable, Hashable, Sendable {
    public enum Role: Hashable, Sendable {
        /// Allow this once (the main button, ⌘↩).
        case allow
        /// Allow more than this once: for the session, or as a rule for later.
        case allowMore
        /// Refuse (the first one is ⌘⌫).
        case deny
    }

    public var id: String { label }
    public let label: String
    public let role: Role
    public let reply: InteractionReply

    public init(label: String, role: Role, reply: InteractionReply) {
        self.label = label
        self.role = role
        self.reply = reply
    }

    /// Claude's and Codex's own decisions for a command, file change or tool.
    public static let standard: [ApprovalChoice] = choices(decisions: ["accept", "acceptForSession", "decline"])

    /// The answers a native approval request offers, in its provider's terms.
    public static func choices(method: String, params: JSON) -> [ApprovalChoice] {
        switch method {
        case "execCommandApproval", "applyPatchApproval":
            // Codex's legacy review decisions.
            return [
                ApprovalChoice(label: "拒绝", role: .deny,
                               reply: InteractionReply(response: ["decision": "denied"], outcome: .denied)),
                ApprovalChoice(label: "本会话内始终允许", role: .allowMore,
                               reply: InteractionReply(response: ["decision": "approved_for_session"],
                                                       outcome: .allowedForSession)),
                ApprovalChoice(label: "允许", role: .allow,
                               reply: InteractionReply(response: ["decision": "approved"], outcome: .allowed)),
            ]
        case "item/permissions/requestApproval":
            // A grant is a subset of what was asked; granting nothing refuses.
            let grant = withoutNulls(params["permissions"])
            return [
                ApprovalChoice(label: "拒绝", role: .deny,
                               reply: InteractionReply(response: ["permissions": [:], "scope": "turn"], outcome: .denied)),
                ApprovalChoice(label: "本会话内允许", role: .allowMore,
                               reply: InteractionReply(response: ["permissions": grant, "scope": "session"],
                                                       outcome: .allowedForSession)),
                ApprovalChoice(label: "允许", role: .allow,
                               reply: InteractionReply(response: ["permissions": grant, "scope": "turn"], outcome: .allowed)),
            ]
        default:
            let offered = params["availableDecisions"].array
            // Codex may omit the list; then every plain decision is available.
            return offered.isEmpty ? standard : choices(decisions: offered)
        }
    }

    private static func choices(decisions: [JSON]) -> [ApprovalChoice] {
        decisions.compactMap { decision -> ApprovalChoice? in
            let reply = { (outcome: ApprovalDecision) in
                InteractionReply(response: ["decision": decision], outcome: outcome)
            }
            switch decision.string {
            case "accept": return ApprovalChoice(label: "允许", role: .allow, reply: reply(.allowed))
            case "acceptForSession":
                return ApprovalChoice(label: "本会话内始终允许", role: .allowMore, reply: reply(.allowedForSession))
            case "decline": return ApprovalChoice(label: "拒绝", role: .deny, reply: reply(.denied))
            // Refuses and stops the turn, so the user can say what to do instead.
            // Codex often offers only this refusal.
            case "cancel": return ApprovalChoice(label: "拒绝并停止", role: .deny, reply: reply(.denied))
            case .some: return nil
            case nil: break
            }
            if let prefix = decision["acceptWithExecpolicyAmendment"]["execpolicy_amendment"].array.compactMap(\.string)
                .nonEmpty {
                var shown = SessionProjection.abbreviateHome(SessionProjection.unwrapShell(prefix.joined(separator: " ")))
                    .components(separatedBy: "\n")[0]
                if shown.count > 48 { shown = String(shown.prefix(48)) + "…" }
                return ApprovalChoice(label: "允许，以后不再询问以 \(shown) 开头的命令",
                                      role: .allowMore, reply: reply(.allowedAlways))
            }
            let network = decision["applyNetworkPolicyAmendment"]["network_policy_amendment"]
            if let host = network["host"].string {
                return network["action"].string == "deny"
                    ? ApprovalChoice(label: "始终阻止访问 \(host)", role: .deny, reply: reply(.denied))
                    : ApprovalChoice(label: "始终允许访问 \(host)", role: .allowMore, reply: reply(.allowedAlways))
            }
            return nil
        }
    }

    /// Codex sends absent permissions as `null`; a grant leaves them out.
    static func withoutNulls(_ value: JSON) -> JSON {
        switch value {
        case .object(let object): .object(object.filter { !$0.value.isNull }.mapValues(withoutNulls))
        case .array(let array): .array(array.map(withoutNulls))
        default: value
        }
    }
}

/// Questions an agent asks (Claude's AskUserQuestion, Codex's request_user_input)
/// or an MCP server's form: fields the user fills in and sends back.
public struct InteractionForm: Hashable, Sendable {
    public enum Kind: Hashable, Sendable { case questions, elicitation }

    public let kind: Kind
    public let title: String
    /// The MCP server's explanation (elicitations only).
    public let message: String
    /// A page to open instead of filling in fields (URL-mode elicitations).
    public let url: URL?
    public let fields: [FormField]

    public init(kind: Kind, title: String, message: String = "", url: URL? = nil, fields: [FormField]) {
        self.kind = kind
        self.title = title
        self.message = message
        self.url = url
        self.fields = fields
    }

    /// The form for a requested interaction, or nil when it is an approval.
    public static func parse(method: String, params: JSON) -> InteractionForm? {
        switch method {
        case "item/tool/requestUserInput":
            let fields = params["questions"].array.compactMap(FormField.question)
            return InteractionForm(kind: .questions, title: "需要你回答", fields: fields)
        case "mcpServer/elicitation/request":
            let server = params["serverName"].text ?? "MCP 服务器"
            let message = params["message"].text ?? ""
            if params["mode"].string == "url" {
                return InteractionForm(kind: .elicitation, title: "\(server) 请你打开链接", message: message,
                                       url: params["url"].string.flatMap(URL.init(string:)), fields: [])
            }
            let schema = params["requestedSchema"]
            let required = Set(schema["required"].array.compactMap(\.string))
            // The order of an object's keys does not survive decoding; required fields come first.
            let fields = (schema["properties"].object ?? [:])
                .sorted { (required.contains($0.key) ? 0 : 1, $0.key) < (required.contains($1.key) ? 0 : 1, $1.key) }
                .compactMap { FormField.property($0.key, $0.value, required: required.contains($0.key)) }
            return InteractionForm(kind: .elicitation, title: "\(server) 需要你填写", message: message, fields: fields)
        default:
            return nil
        }
    }

    /// Whether every field that needs an answer has one.
    public func isComplete(_ answers: [String: [String]]) -> Bool {
        fields.allSatisfy { field in
            let values = (answers[field.id] ?? []).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            if values.isEmpty { return !(kind == .questions || field.required) || field.input == .toggle }
            if case .number(let integer) = field.input {
                return integer ? Int(values[0]) != nil : Double(values[0]) != nil
            }
            return true
        }
    }

    /// The reply carrying these answers.
    public func reply(_ answers: [String: [String]]) -> InteractionReply {
        let filled = answers.mapValues { $0.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty } }
            .filter { !$0.value.isEmpty }
        let summary = fields.compactMap { field -> String? in
            guard let values = filled[field.id] else { return nil }
            let name = field.header.isEmpty ? field.prompt : field.header
            let shown = field.input == .text(secret: true) ? "••••" : values.map(field.label(for:)).joined(separator: ", ")
            return name.isEmpty ? shown : "\(name)：\(shown)"
        }.joined(separator: "；")
        switch kind {
        case .questions:
            let response: JSON = ["answers": .object(filled.mapValues { values -> JSON in
                ["answers": .array(values.map(JSON.string))]
            })]
            return InteractionReply(response: response, outcome: .answered(summary))
        case .elicitation:
            var content: [String: JSON] = [:]
            for field in fields {
                guard let values = filled[field.id] else { continue }
                content[field.id] = field.value(values)
            }
            return InteractionReply(response: ["action": "accept", "content": .object(content)],
                                    outcome: url == nil ? .answered(summary) : .allowed)
        }
    }

    /// Questions are skipped; an MCP form is declined.
    public var dismissal: InteractionReply {
        switch kind {
        case .questions: InteractionReply(response: ["answers": [:]], outcome: .skipped)
        case .elicitation: InteractionReply(response: ["action": "decline"], outcome: .denied)
        }
    }
}

/// One question or form field.
public struct FormField: Identifiable, Hashable, Sendable {
    public enum Input: Hashable, Sendable {
        /// Options to pick; `allowsOther` adds a free-text answer.
        case choice(options: [FormOption], multiple: Bool, allowsOther: Bool)
        case text(secret: Bool)
        case number(integer: Bool)
        case toggle
    }

    public let id: String
    /// A short tag above the question (Claude's `header`).
    public let header: String
    public let prompt: String
    public let detail: String
    public let input: Input
    public let required: Bool
    /// Values already chosen when the form opens.
    public let initial: [String]

    public init(id: String, header: String = "", prompt: String, detail: String = "", input: Input,
                required: Bool = true, initial: [String] = []) {
        self.id = id
        self.header = header
        self.prompt = prompt
        self.detail = detail
        self.input = input
        self.required = required
        self.initial = initial
    }

    /// A Claude or Codex question.
    static func question(_ json: JSON) -> FormField? {
        guard let id = json["id"].string else { return nil }
        let options = json["options"].array.compactMap { option -> FormOption? in
            guard let label = option["label"].text else { return nil }
            return FormOption(value: label, label: label, detail: option["description"].text ?? "")
        }
        let input: Input
        if json["kind"].string == "number" {
            input = .number(integer: false)
        } else if options.isEmpty {
            input = .text(secret: json["isSecret"].bool == true)
        } else {
            input = .choice(options: options, multiple: json["multiSelect"].bool == true,
                            allowsOther: json["isOther"].bool == true)
        }
        return FormField(id: id, header: json["header"].text ?? "", prompt: json["question"].text ?? "",
                         detail: json["description"].text ?? "", input: input)
    }

    /// One property of an MCP form's schema.
    static func property(_ key: String, _ schema: JSON, required: Bool) -> FormField? {
        let title = schema["title"].text ?? key
        let detail = schema["description"].text ?? ""
        func field(_ input: Input, initial: [String]) -> FormField {
            FormField(id: key, prompt: title, detail: detail, input: input, required: required, initial: initial)
        }
        let initial: [String] = switch schema["default"] {
        case .array(let values): values.compactMap(\.string)
        case .bool(let value): [value ? "true" : "false"]
        case .null: []
        case let value: value.string.map { [$0] } ?? []
        }
        let names = schema["enumNames"].array.compactMap(\.string)
        let single = schema["oneOf"].array.compactMap(FormOption.constant)
            .nonEmpty ?? schema["enum"].array.enumerated().compactMap { index, value in
                value.string.map { FormOption(value: $0, label: names.indices.contains(index) ? names[index] : $0) }
            }.nonEmpty
        switch schema["type"].string {
        case "array":
            let items = schema["items"]
            let options = items["anyOf"].array.compactMap(FormOption.constant).nonEmpty
                ?? items["enum"].array.compactMap(\.string).map { FormOption(value: $0, label: $0) }
            return field(.choice(options: options, multiple: true, allowsOther: false), initial: initial)
        case "boolean":
            return field(.toggle, initial: initial.isEmpty ? ["false"] : initial)
        case "number", "integer":
            return field(.number(integer: schema["type"].string == "integer"), initial: initial)
        default:
            if let single { return field(.choice(options: single, multiple: false, allowsOther: false), initial: initial) }
            return field(.text(secret: false), initial: initial)
        }
    }

    /// The label shown for a chosen value.
    func label(for value: String) -> String {
        if case .choice(let options, _, _) = input, let option = options.first(where: { $0.value == value }) {
            return option.label
        }
        return value
    }

    /// The JSON an MCP form expects for this field.
    func value(_ values: [String]) -> JSON {
        switch input {
        case .choice(_, true, _): .array(values.map(JSON.string))
        case .toggle: .bool(values[0] == "true")
        case .number(true): Int(values[0]).map { .number(Double($0)) } ?? .string(values[0])
        case .number(false): Double(values[0]).map(JSON.number) ?? .string(values[0])
        default: .string(values[0])
        }
    }
}

public struct FormOption: Identifiable, Hashable, Sendable {
    public var id: String { value }
    public let value: String
    public let label: String
    public let detail: String

    public init(value: String, label: String, detail: String = "") {
        self.value = value
        self.label = label
        self.detail = detail
    }

    static func constant(_ json: JSON) -> FormOption? {
        guard let value = json["const"].string else { return nil }
        return FormOption(value: value, label: json["title"].text ?? value)
    }
}

extension Array {
    var nonEmpty: Self? { isEmpty ? nil : self }
}
