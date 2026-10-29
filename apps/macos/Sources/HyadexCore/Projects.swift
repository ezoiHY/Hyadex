import Foundation

/// A project the user made. Chats join it when moved into it (stored on the
/// server as the chat's `folder`) or, unless moved elsewhere, by working in
/// its directory. The list itself is kept on this Mac, per server, so an
/// empty project stays until it is deleted.
public struct ChatProject: Codable, Hashable, Identifiable, Sendable {
    public var name: String
    /// "~/Code/Hyadex": new chats in the project start here.
    public var directory: String?

    public var id: String { name }

    public init(name: String, directory: String? = nil) {
        self.name = name
        self.directory = directory
    }
}

/// One group of chats in the sidebar.
public struct ProjectSection: Identifiable, Hashable, Sendable {
    public enum Kind: Hashable, Sendable {
        /// A project the user made, or a folder another client set.
        case project
        /// Chats that share a working directory.
        case directory
        /// Chats in the home folder.
        case other
    }

    public let id: String
    public var title: String
    public let kind: Kind
    public let directory: String?
    public var chats: [ChatSummary]

    /// The project's name, for moving chats into it (nil for automatic groups).
    public var projectName: String? { kind == .project ? title : nil }
}

public enum ProjectGrouping {
    /// The folder the server gives every chat that is in no project.
    public static let serverDefault = "General"

    /// Chats by project, then by working directory; the most recently active group first.
    public static func sections(chats: [ChatSummary], projects: [ChatProject]) -> [ProjectSection] {
        var sections: [String: ProjectSection] = [:]
        for project in projects {
            sections["project:" + project.name] = ProjectSection(id: "project:" + project.name, title: project.name,
                                                                 kind: .project, directory: project.directory, chats: [])
        }
        for chat in chats {
            let key: String
            if let folder = chat.folder {
                key = "project:" + folder
                if sections[key] == nil {
                    sections[key] = ProjectSection(id: key, title: folder, kind: .project, directory: nil, chats: [])
                }
            } else if let project = projects.first(where: { $0.directory.map(normalized) == normalized(chat.cwd) }) {
                key = "project:" + project.name
            } else if !isHome(chat.cwd) {
                let directory = normalized(chat.cwd)
                key = "directory:" + directory
                if sections[key] == nil {
                    sections[key] = ProjectSection(id: key, title: lastComponents(directory, 1), kind: .directory,
                                                   directory: directory, chats: [])
                }
            } else {
                key = "other"
                if sections[key] == nil {
                    sections[key] = ProjectSection(id: key, title: "其他", kind: .other, directory: nil, chats: [])
                }
            }
            sections[key]!.chats.append(chat)
        }
        var result = Array(sections.values)
        for index in result.indices {
            result[index].chats.sort { $0.lastActivity > $1.lastActivity }
        }
        // Two directories with the same name show their parent too.
        let titles = Dictionary(grouping: result.filter { $0.kind == .directory }, by: \.title)
        for index in result.indices where result[index].kind == .directory && (titles[result[index].title]?.count ?? 0) > 1 {
            result[index].title = lastComponents(result[index].directory ?? "", 2)
        }
        return result.sorted { a, b in
            if (a.kind == .other) != (b.kind == .other) { return b.kind == .other }
            let newestA = a.chats.first?.lastActivity, newestB = b.chats.first?.lastActivity
            switch (newestA, newestB) {
            case let (x?, y?) where x != y: return x > y
            case (_?, nil): return true
            case (nil, _?): return false
            default: return a.title.localizedStandardCompare(b.title) == .orderedAscending
            }
        }
    }

    /// "~/Code/Hyadex/" and "~/Code/Hyadex" are the same directory.
    public static func normalized(_ path: String) -> String {
        var path = path.trimmingCharacters(in: .whitespaces)
        while path.count > 1, path.hasSuffix("/") { path.removeLast() }
        return path
    }

    static func isHome(_ path: String) -> Bool {
        let path = normalized(path)
        return path.isEmpty || path == "~" || path == NSHomeDirectory()
    }

    static func lastComponents(_ path: String, _ count: Int) -> String {
        let parts = path.split(separator: "/").map(String.init)
        guard !parts.isEmpty else { return path }
        return parts.suffix(count).joined(separator: "/")
    }
}
