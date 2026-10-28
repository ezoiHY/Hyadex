import Foundation
import HyadexAPI

// Full-text search hits, side chats and subagents, as the server reports them.

// MARK: - Search

/// One chat's newest message matching a full-text search.
public struct SearchHit: Identifiable, Hashable, Sendable {
    public var id: String { sessionID }
    public let sessionID: String
    /// The matching event; timeline items carry the same id.
    public let eventID: String
    public let seq: Int
    public let time: Date?
    public let role: String
    public let snippet: String
    /// Matching messages in this chat.
    public let matches: Int

    public init(sessionID: String, eventID: String, seq: Int, time: Date?, role: String, snippet: String, matches: Int) {
        self.sessionID = sessionID
        self.eventID = eventID
        self.seq = seq
        self.time = time
        self.role = role
        self.snippet = snippet
        self.matches = matches
    }

    public var roleLabel: String {
        switch role {
        case "user": "你"
        case "assistant": "代理"
        case "trace": "思考"
        case "file": "文件"
        case "job": "定时任务"
        case "error": "出错"
        default: "系统"
        }
    }

    public static func parse(_ json: JSON) -> [SearchHit] {
        json["results"].array.compactMap { item in
            guard let session = item["session_id"].string, let event = item["event_id"].string,
                  let seq = item["seq"].int else { return nil }
            return SearchHit(sessionID: session, eventID: event, seq: seq, time: item["ts"].date,
                             role: item["role"].string ?? "", snippet: item["snippet"].text ?? "",
                             matches: max(1, item["match_count"].int ?? 1))
        }
    }

    /// The words to highlight in a snippet: quoted phrases and single words.
    public static func terms(in query: String) -> [String] {
        var terms: [String] = []
        var rest = query
        while let open = rest.firstIndex(of: "\""),
              let close = rest[rest.index(after: open)...].firstIndex(of: "\"") {
            let phrase = rest[rest.index(after: open)..<close].trimmingCharacters(in: .whitespaces)
            if !phrase.isEmpty { terms.append(phrase) }
            rest.removeSubrange(open...close)
        }
        terms += rest.split(whereSeparator: \.isWhitespace).map(String.init)
        return terms.filter { !$0.isEmpty }
    }
}

// MARK: - Side chat

/// A side chat: quick questions about the conversation that neither
/// interrupt the agent nor enter its transcript (Claude Code's `/btw`;
/// Codex answers from a fork of the thread).
public struct SideChat: Hashable, Sendable {
    public struct Exchange: Identifiable, Hashable, Sendable {
        public enum Status: String, Hashable, Sendable { case running, completed, failed, cancelled, interrupted }

        public var id: String { requestID }
        public let requestID: String
        public let question: String
        public let answer: String?
        public let status: Status
        public let error: String?
        public let time: Date?

        public init(requestID: String, question: String, answer: String?, status: Status, error: String? = nil,
                    time: Date? = nil) {
            self.requestID = requestID
            self.question = question
            self.answer = answer
            self.status = status
            self.error = error
            self.time = time
        }
    }

    public var sideChatID: String
    public var revision: Int
    public var exchanges: [Exchange]
    public var lastRequestID: String?
    /// What the answers can see, as the provider explains it.
    public var contextNote: String?

    public init(sideChatID: String, revision: Int = 0, exchanges: [Exchange] = [], lastRequestID: String? = nil,
                contextNote: String? = nil) {
        self.sideChatID = sideChatID
        self.revision = revision
        self.exchanges = exchanges
        self.lastRequestID = lastRequestID
        self.contextNote = contextNote
    }

    public var isRunning: Bool { exchanges.contains { $0.status == .running } }

    public static func parse(_ json: JSON) -> SideChat? {
        guard let id = json["side_chat_id"].string else { return nil }
        let exchanges = json["exchanges"].array.compactMap { item -> Exchange? in
            guard let request = item["request_id"].string else { return nil }
            return Exchange(requestID: request, question: item["question"].text ?? "", answer: item["answer"].text,
                            status: Exchange.Status(rawValue: item["status"].string ?? "") ?? .failed,
                            error: item["error"].string, time: item["created_at"].date)
        }
        let note = json["exchanges"].array.reversed().compactMap { $0["context_note"].text }.first
        return SideChat(sideChatID: id, revision: json["revision"].int ?? 0, exchanges: exchanges,
                        lastRequestID: json["last_request_id"].string, contextNote: note)
    }
}

// MARK: - Subagents

/// A helper agent the chat's agent started: a Claude Task agent or a Codex collaborator.
public struct Subagent: Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let kind: String?
    public let state: ActivityState
    public let statusLabel: String
    public let activity: String?
    public let summary: String?
    public let startedAt: Date?
    public let log: [String]

    public init(id: String, name: String, kind: String? = nil, state: ActivityState, statusLabel: String,
                activity: String? = nil, summary: String? = nil, startedAt: Date? = nil, log: [String] = []) {
        self.id = id
        self.name = name
        self.kind = kind
        self.state = state
        self.statusLabel = statusLabel
        self.activity = activity
        self.summary = summary
        self.startedAt = startedAt
        self.log = log
    }

    public static func parse(_ json: JSON) -> [Subagent] {
        json["subagents"].array.compactMap { item in
            guard let id = item["id"].string else { return nil }
            let status = item["subagent_status"].string ?? ""
            let (state, label): (ActivityState, String) = switch status {
            case "starting": (.running, "启动中")
            case "running": (.running, "运行中")
            case "completed": (.done, "已完成")
            case "failed": (.failed, "失败")
            case "stopped": (.idle, "已停止")
            case "tracking_lost": (.idle, "失去跟踪")
            default: (.idle, status.isEmpty ? "未知" : status)
            }
            let name = item["subagent_title"].text ?? item["subagent_name"].text ?? item["subagent_nickname"].text
                ?? "子代理"
            return Subagent(id: id, name: name, kind: item["subagent_kind"].text, state: state, statusLabel: label,
                            activity: item["subagent_activity"].text, summary: item["subagent_summary"].text,
                            startedAt: item["subagent_started_at"].date,
                            log: item["subagent_log"].array.compactMap { $0["text"].text })
        }
    }
}
