import Foundation

/// Who wrote a timeline entry.
public struct Author: Hashable, Sendable {
    public enum Kind: String, Hashable, Sendable { case user, agent, system, orchestrator }

    public var name: String
    public var kind: Kind
    public var backend: AgentBackend?

    public init(name: String, kind: Kind, backend: AgentBackend? = nil) {
        self.name = name
        self.kind = kind
        self.backend = backend
    }

    public static let user = Author(name: "你", kind: .user)
    public static let autopilot = Author(name: "自动接力", kind: .orchestrator)
    public static func agent(_ handle: String, _ backend: AgentBackend) -> Author {
        Author(name: handle, kind: .agent, backend: backend)
    }
}

public struct FileChange: Identifiable, Hashable, Sendable {
    public var id: String { path }
    public var path: String
    public var added: Int
    public var removed: Int
    public var diff: String

    public init(path: String, added: Int, removed: Int, diff: String) {
        self.path = path
        self.added = added
        self.removed = removed
        self.diff = diff
    }
}

public struct FileChangeSet: Hashable, Sendable {
    public var files: [FileChange]

    public init(files: [FileChange]) { self.files = files }

    public var added: Int { files.reduce(0) { $0 + $1.added } }
    public var removed: Int { files.reduce(0) { $0 + $1.removed } }
}

public enum ToolKind: String, Hashable, Sendable {
    case command, read, edit, search, mcp, web

    public var symbol: String {
        switch self {
        case .command: "terminal"
        case .read: "doc.text"
        case .edit: "pencil"
        case .search: "magnifyingglass"
        case .mcp: "puzzlepiece.extension"
        case .web: "globe"
        }
    }
}

public enum ToolStatus: Hashable, Sendable { case running, succeeded, failed }

public enum ApprovalDecision: Hashable, Sendable {
    case pending, allowed, allowedForSession, denied
    /// Answered elsewhere (another client, or before this app connected).
    case resolved
}

/// Something the timeline shows. The server's raw events are projected into
/// these items; the views never look at raw events.
public enum TimelineItem: Identifiable, Hashable, Sendable {
    case message(id: String, author: Author, text: String, time: Date, mentions: [String] = [],
                 receipt: FileChangeSet? = nil, streaming: Bool = false)
    case thinking(id: String, seconds: Int, text: String)
    case tool(id: String, kind: ToolKind, title: String, output: String, status: ToolStatus, seconds: Double)
    case changes(id: String, set: FileChangeSet)
    case approval(id: String, author: Author, title: String, command: String, reason: String,
                  decision: ApprovalDecision, allowsSession: Bool = true)
    case divider(id: String, text: String)

    public var id: String {
        switch self {
        case .message(let id, _, _, _, _, _, _), .thinking(let id, _, _), .tool(let id, _, _, _, _, _),
             .changes(let id, _), .approval(let id, _, _, _, _, _, _), .divider(let id, _):
            id
        }
    }
}

public extension Array where Element == TimelineItem {
    /// The newest unresolved approval request, if any.
    var pendingApproval: (id: String, author: Author)? {
        for item in reversed() {
            if case .approval(let id, let author, _, _, _, .pending, _) = item { return (id, author) }
        }
        return nil
    }

    /// Every file change in the timeline, merged by path (latest diff wins).
    var allChanges: FileChangeSet {
        var merged: [String: FileChange] = [:]
        var order: [String] = []
        for item in self {
            let set: FileChangeSet?
            switch item {
            case .changes(_, let changes): set = changes
            case .message(_, _, _, _, _, let receipt, _): set = receipt
            default: set = nil
            }
            for file in set?.files ?? [] {
                if merged[file.path] == nil { order.append(file.path) }
                merged[file.path] = file
            }
        }
        return FileChangeSet(files: order.compactMap { merged[$0] })
    }
}

/// Splits `@handle` mentions out of message text (outside code spans), the
/// same rule the server uses to pick group targets.
public enum MentionParser {
    public static func mentions(in text: String, handles: Set<String>) -> [String] {
        var found: [String] = []
        var inCode = false
        var index = text.startIndex
        while index < text.endIndex {
            let character = text[index]
            if character == "`" {
                inCode.toggle()
            } else if character == "@", !inCode {
                let previous = index > text.startIndex ? text[text.index(before: index)] : " "
                if !(previous.isLetter || previous.isNumber) {
                    var end = text.index(after: index)
                    while end < text.endIndex, text[end].isLetter || text[end].isNumber || text[end] == "_" || text[end] == "-" {
                        end = text.index(after: end)
                    }
                    let handle = text[text.index(after: index)..<end].lowercased()
                    if handle == "all" || handles.contains(handle), !found.contains(handle) {
                        found.append(handle)
                    }
                    index = end
                    continue
                }
            }
            index = text.index(after: index)
        }
        return found
    }

    /// The partial handle being typed at the end of `text` (`"hi @co"` → `"co"`).
    public static func partialMention(at text: String) -> String? {
        guard let at = text.lastIndex(of: "@") else { return nil }
        let before = at > text.startIndex ? text[text.index(before: at)] : " "
        guard before.isWhitespace else { return nil }
        let tail = text[text.index(after: at)...]
        guard tail.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" }) else { return nil }
        return String(tail).lowercased()
    }
}
