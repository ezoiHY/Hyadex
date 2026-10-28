import Foundation
import HyadexAPI
import HyadexTerminal
import Observation

public enum ConnectionState: Hashable, Sendable {
    case disconnected
    case connecting
    case connected(version: String)
    case offline(String)

    public var label: String {
        switch self {
        case .disconnected: "未连接"
        case .connecting: "正在连接…"
        case .connected(let version): "已连接 · v\(version)"
        case .offline(let reason): "离线 · \(reason)"
        }
    }
}

public enum SheetKind: String, Identifiable, Hashable, Sendable {
    case newChat, newGroup, newAutopilot, commandPalette, feedback

    public var id: String { rawValue }
}

/// What the job sheet creates or edits.
public enum JobEditorTarget: Identifiable, Hashable, Sendable {
    case new
    case edit(String)

    public var id: String {
        switch self {
        case .new: "new"
        case .edit(let id): id
        }
    }
}

/// Where a new job posts its prompt.
public enum JobTarget: Hashable, Sendable {
    case chat(String)
    case group(String)
}

/// A chat or group being renamed.
public struct RenameTarget: Identifiable, Hashable, Sendable {
    public let item: SidebarItem
    public let title: String

    public var id: String { "\(item)" }

    public init(item: SidebarItem, title: String) {
        self.item = item
        self.title = title
    }
}

/// Whose goal the goal sheet edits.
public enum GoalEditorTarget: Identifiable, Hashable, Sendable {
    case chat(String)
    case group(String)

    public var id: String {
        switch self {
        case .chat(let id): "chat-\(id)"
        case .group(let id): "group-\(id)"
        }
    }
}

/// The app's single source of truth. Views read slices of it and call its
/// methods; nothing else mutates state.
@MainActor
@Observable
public final class AppModel {
    public let localServer = LocalServerController()
    public private(set) var managedLocalServerID: UUID?
    public var servers: [ServerEndpoint]
    public var currentServerID: UUID
    public var connection: ConnectionState = .disconnected

    public var chats: [ChatSummary]
    public var groups: [GroupSummary]
    public var runs: [AutopilotRun]
    public var jobs: [ScheduledJob]
    public var timelines: [String: [TimelineItem]]
    public var queues: [String: [QueuedMessage]] = [:]
    /// Sent messages awaiting the server's acknowledgement, by chat id.
    public var pendingSends: [String: [PendingSend]] = [:]
    /// The provider's live reasoning summary, by chat id (non-durable stream).
    public var liveReasoning: [String: String] = [:]
    /// When each chat's running turn started.
    public var turnStartedAt: [String: Date] = [:]
    /// Finished turn durations per chat, keyed by the user message id.
    public var turnSeconds: [String: [String: Int]] = [:]
    /// Native model and effort catalogs from the server, by backend.
    public var catalogs: [AgentBackend: RuntimeCatalog] = [:]
    /// Approvals group members are waiting for, by group id.
    public var groupApprovals: [String: [GroupApproval]] = [:]
    /// Group members' answers in progress, by group id.
    public var groupLive: [String: [MemberTurn]] = [:]
    /// How long each finished member turn took, keyed by its first step id, by group id.
    public var processSeconds: [String: [String: Int]] = [:]
    /// Plan usage of each provider account, as the native CLI reports it.
    public var usage: [AgentBackend: ProviderUsage] = [:]
    /// How full each chat's context window is, by chat id.
    public var contextUsage: [String: ContextUsage] = [:]
    /// Native goals by chat id (group members' chats included).
    public var goals: [String: ChatGoal] = [:]
    /// Whether each chat's provider offers goals on this server.
    public var goalSupport: [String: GoalSupport] = [:]
    /// Goal changes the server is still applying, by chat id.
    public var goalBusy: Set<String> = []
    public var goalEditor: GoalEditorTarget?
    /// Slash commands and skills each chat's agent offers, by chat id.
    public var commands: [String: CommandInventory] = [:]
    /// MCP servers each chat's agent loaded, by chat id.
    public var mcp: [String: MCPStatus] = [:]
    public var mcpBusy: Set<String> = []
    /// Files added to each chat's composer, by chat id.
    public var attachments: [String: [DraftAttachment]] = [:]
    /// Agents' unacknowledged emergency alerts, one per chat.
    public var emergencies: [EmergencyAlert] = []
    /// Chats with older history on the server, and those loading it.
    public var olderHistory: Set<String> = []
    public var loadingOlder: Set<String> = []
    public var jobEditor: JobEditorTarget?
    public var renaming: RenameTarget?
    /// The chat's advanced settings or the group's settings, open in a sheet.
    public var settingsEditor: SettingsEditorTarget?
    /// A chat or group waiting for the user to confirm its deletion.
    public var pendingDeletion: SidebarItem?
    /// Full-text hits for the sidebar search, and whether a search is running.
    public var searchHits: [SearchHit] = []
    public var searching = false
    /// The message to scroll to and highlight, by chat id (a search hit):
    /// "eventID|token", so focusing the same message again is a new request.
    public var timelineFocus: [String: String] = [:]
    /// Side chats by chat id, and the chat whose side-chat panel is open.
    public var sideChats: [String: SideChat] = [:]
    public var sideChatOpen: String?
    public var sideChatErrors: [String: String] = [:]
    /// Subagents by chat id.
    public var subagents: [String: [Subagent]] = [:]
    /// Chats the user marked unread while they were open: keep them unread until reselected.
    @ObservationIgnored var markedUnread: Set<String> = []
    public var drafts: [String: String] = [:]

    public var selection: SidebarItem?
    /// Which list the sidebar shows. Each tab remembers its last selection.
    public private(set) var sidebarMode: SidebarMode =
        UserDefaults.standard.string(forKey: "sidebarMode").flatMap(SidebarMode.init(rawValue:)) ?? .work
    @ObservationIgnored private var lastSelection: [SidebarMode: SidebarItem] = [:]
    /// The changes panel slides over the conversation on demand (a change card,
    /// a receipt, "查看 diff", or the toolbar). It is not a permanent column.
    public var showChanges = false
    public var selectedFilePath: String?
    /// Chats whose terminal panel is open, and the panel height they share.
    public var terminalOpen: Set<String> = []
    public var terminalHeight: Double = UserDefaults.standard.object(forKey: "terminalHeight") as? Double ?? 260 {
        didSet { UserDefaults.standard.set(terminalHeight, forKey: "terminalHeight") }
    }
    /// tmux windows of each chat's terminal, refreshed while its panel is open.
    public var terminalWindows: [String: [TerminalWindow]] = [:]
    @ObservationIgnored private var terminalSessions: [String: TerminalSession] = [:]
    public var sheet: SheetKind?
    public var searchText = ""

    /// Last action error, shown as a dismissible banner.
    public var errorMessage: String?
    /// Whether a server connection has completed successfully.
    public private(set) var isLive = false

    @ObservationIgnored var live: LiveController?
    @ObservationIgnored private let networkSession: URLSession
    @ObservationIgnored private var connectionGeneration = UUID()
    @ObservationIgnored private var managedLocalToken: String?
    @ObservationIgnored private var defaultPlaceholder: ServerEndpoint?
    /// Events loaded when a chat opens, and per "加载更早的消息" page.
    public static var historyPageSize = 400

    public init(session: URLSession = .shared) {
        networkSession = session
        let saved = (UserDefaults.standard.data(forKey: Self.serversKey))
            .flatMap { try? JSONDecoder().decode([ServerEndpoint].self, from: $0) } ?? []
        let local = ServerEndpoint(name: "这台 Mac", baseURL: URL(string: "http://127.0.0.1:7870")!)
        defaultPlaceholder = saved.isEmpty ? local : nil
        servers = saved.isEmpty ? [local] : saved
        currentServerID = UserDefaults.standard.string(forKey: Self.currentServerKey).flatMap(UUID.init)
            .flatMap { id in saved.contains { $0.id == id } ? id : nil } ?? (saved.first ?? local).id
        chats = []; groups = []; runs = []; jobs = []; timelines = [:]
    }

    /// Explicit development fixtures for snapshots. They never enable server actions.
    public convenience init(snapshotFixturesAt now: Date) {
        self.init()
        chats = SampleData.chats(now: now)
        groups = SampleData.groups(now: now)
        runs = SampleData.runs(now: now)
        jobs = SampleData.jobs(now: now)
        timelines = SampleData.chatTimelines(now: now).merging(SampleData.groupTimelines(now: now)) { $1 }
        queues["c3"] = [QueuedMessage(text: "测试跑完后把 CHANGELOG 也检查一下。")]
        (usage, contextUsage) = SampleData.usage(chats: chats, now: now)
        (groupLive, processSeconds) = SampleData.groupLive(now: now)
        (goals, goalSupport, commands, mcp) = SampleData.capabilities()
        select(firstItem(in: sidebarMode))
    }

    public var currentServer: ServerEndpoint? { servers.first { $0.id == currentServerID } }

    // MARK: Lookup

    public func chat(_ id: String) -> ChatSummary? { chats.first { $0.id == id } }
    public func group(_ id: String) -> GroupSummary? { groups.first { $0.id == id } }
    public func run(_ id: String) -> AutopilotRun? { runs.first { $0.id == id } }
    public func timeline(_ key: String) -> [TimelineItem] { timelines[key] ?? [] }

    /// The timeline key of the current selection (chat or group id).
    public var selectedTimelineKey: String? {
        switch selection {
        case .chat(let id), .group(let id): id
        default: nil
        }
    }

    /// Files changed in whatever is selected; drives the inspector.
    public var selectedChanges: FileChangeSet {
        switch selection {
        case .chat(let id), .group(let id): timeline(id).allChanges
        case .autopilot(let id):
            FileChangeSet(files: (run(id)?.tasks ?? []).flatMap { $0.changes?.files ?? [] })
        default: FileChangeSet(files: [])
        }
    }

    public var needsActionCount: Int {
        chats.filter { $0.state == .needsAction }.count
            + groups.filter { $0.state == .needsAction }.count
            + runs.filter { $0.status.activity == .needsAction }.count
    }

    public var runningCount: Int {
        chats.filter { $0.state == .running }.count
            + groups.filter { $0.state == .running }.count
            + runs.filter { $0.status.isActive }.count
    }

    // MARK: Filtering

    private func matches(_ values: String...) -> Bool {
        let query = searchText.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return true }
        return values.contains { $0.localizedCaseInsensitiveContains(query) }
    }

    public var visiblePinned: [ChatSummary] {
        chats.filter { $0.pinned && !$0.archived && matches($0.title, $0.preview) }
    }
    public var visibleChats: [ChatSummary] {
        chats.filter { !$0.pinned && !$0.archived && matches($0.title, $0.preview) }
            .sorted { $0.lastActivity > $1.lastActivity }
    }
    public var archivedChats: [ChatSummary] {
        chats.filter { $0.archived && matches($0.title, $0.preview) }.sorted { $0.lastActivity > $1.lastActivity }
    }
    public var visibleGroups: [GroupSummary] {
        groups.filter { !$0.archived && matches($0.title, $0.preview) }.sorted { $0.lastActivity > $1.lastActivity }
    }
    public var archivedGroups: [GroupSummary] {
        groups.filter { $0.archived && matches($0.title, $0.preview) }.sorted { $0.lastActivity > $1.lastActivity }
    }
    public var visibleRuns: [AutopilotRun] {
        runs.filter { matches($0.title, $0.goal) }.sorted { $0.lastActivity > $1.lastActivity }
    }

    // MARK: Selection

    /// The sidebar list's selection. SwiftUI may set the same selection again
    /// when a row changes (for example its unread dot); that is not a new visit.
    public func selectFromSidebar(_ item: SidebarItem?) {
        guard item != selection else { return }
        select(item)
    }

    public func select(_ item: SidebarItem?) {
        selection = item
        selectedFilePath = nil
        showChanges = false
        if let item {
            lastSelection[item.mode] = item
            if item.mode != sidebarMode { setSidebarMode(item.mode) }
        }
        live?.open(item)
        switch item {
        case .chat(let id):
            markedUnread.remove(id)
            if let index = chats.firstIndex(where: { $0.id == id }) { chats[index].unread = false }
        case .group(let id):
            if let index = groups.firstIndex(where: { $0.id == id }) { groups[index].unread = false }
        default: break
        }
    }

    /// Switches the sidebar tab and shows that tab's last (or first) item.
    public func switchSidebar(to mode: SidebarMode) {
        guard mode != sidebarMode else { return }
        setSidebarMode(mode)
        let remembered = lastSelection[mode].flatMap { exists($0) ? $0 : nil }
        select(remembered ?? firstItem(in: mode))
    }

    /// Opens the creation sheet for the current tab.
    public func newItemForCurrentMode() {
        switch sidebarMode {
        case .work: sheet = .newChat
        case .group: sheet = .newGroup
        case .autopilot: sheet = .newAutopilot
        }
    }

    /// Items in a tab that are waiting for the user.
    public func needsActionCount(in mode: SidebarMode) -> Int {
        switch mode {
        case .work: chats.filter { $0.state == .needsAction }.count
        case .group: groups.filter { $0.state == .needsAction }.count
        case .autopilot: runs.filter { $0.status.activity == .needsAction }.count
        }
    }

    private func setSidebarMode(_ mode: SidebarMode) {
        sidebarMode = mode
        UserDefaults.standard.set(mode.rawValue, forKey: "sidebarMode")
    }

    private func exists(_ item: SidebarItem) -> Bool {
        switch item {
        case .chat(let id): chat(id) != nil
        case .group(let id): group(id) != nil
        case .autopilot(let id): run(id) != nil
        case .jobs: true
        }
    }

    private func firstItem(in mode: SidebarMode) -> SidebarItem? {
        switch mode {
        case .work: (visiblePinned.first ?? visibleChats.first).map { .chat($0.id) }
        case .group: visibleGroups.first.map { .group($0.id) }
        case .autopilot: visibleRuns.first.map { .autopilot($0.id) }
        }
    }

    public func openFile(_ path: String) {
        selectedFilePath = path
        showChanges = true
    }

    // MARK: Terminal

    /// The chat's terminal attachment; available only while connected.
    public func terminalSession(for chatID: String) -> TerminalSession? {
        guard let live = requireLive() else { return nil }

        if let session = terminalSessions[chatID] { return session }
        let client = live.client
        let session = TerminalSession { columns, rows in
            client.terminalSocketRequest(chatID, columns: columns, rows: rows)
        }
        terminalSessions[chatID] = session
        return session
    }

    public func toggleTerminal(_ chatID: String) {
        guard requireLive() != nil else { return }
        if terminalOpen.contains(chatID) {
            terminalOpen.remove(chatID)
        } else {
            terminalOpen.insert(chatID)
        }
    }

    public func refreshTerminalWindows(_ chatID: String) async {
        guard let live, let json = try? await live.client.terminalWindows(chatID) else { return }
        guard self.live === live else { return }
        terminalWindows[chatID] = TerminalWindow.parse(json)
    }

    /// tmux window and pane actions: new-window, split-right, split-down,
    /// select-window, kill-window, kill-pane.
    public func terminalAction(_ chatID: String, _ action: String, target: String? = nil) {
        guard let live = requireLive() else { return }
        Task {
            do {
                let json = try await live.client.terminalAction(chatID, action: action, target: target)
                guard self.live === live else { return }
                terminalWindows[chatID] = TerminalWindow.parse(json)
            } catch {
                guard self.live === live else { return }
                errorMessage = error.localizedDescription
            }
        }
    }

    /// Ends the tmux session and everything running in it.
    public func killTerminal(_ chatID: String) {
        guard let live = requireLive() else { return }
        terminalSessions[chatID]?.disconnect()
        terminalSessions[chatID] = nil
        terminalWindows[chatID] = nil
        terminalOpen.remove(chatID)
        Task {
            do { try await live.client.killTerminal(chatID) } catch {
                guard self.live === live else { return }
                errorMessage = error.localizedDescription
            }
        }
    }

    private func dropTerminalSessions() {
        terminalSessions.values.forEach { $0.disconnect() }
        terminalSessions = [:]
        terminalWindows = [:]
    }

    // MARK: Chats

    public func send(_ rawText: String, toChat id: String) {
        guard let live = requireLive() else { return }
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        let files = attachments[id] ?? []
        guard !text.isEmpty || !files.isEmpty, chat(id) != nil,
              !files.contains(where: { $0.state == .uploading }) else { return }
        if text.hasPrefix("/btw"), text.dropFirst(4).first.map(\.isWhitespace) ?? true {
            let question = text.dropFirst(4).trimmingCharacters(in: .whitespacesAndNewlines)
            sideChatOpen = id
            if !question.isEmpty { askSide(question, inChat: id) }
            return
        }
        if let argument = SlashParser.goalArgument(text), goalSupport(forChat: id).available {
            switch argument.lowercased() {
            case "": goalEditor = .chat(id)
            case "clear", "off", "stop": clearGoal(forChat: id)
            default: setGoal(argument, forChat: id)
            }
            return
        }
        let fileIDs = files.compactMap(\.fileID)
        attachments[id] = nil
        live.enqueueSend(text, toChat: id, fileIDs: fileIDs)
    }

    public func stop(_ key: String) {
        guard let live = requireLive() else { return }
        Task { await live.stop(key) }
    }

    public func withdrawQueued(_ messageID: String, in key: String) {
        guard let live = requireLive() else { return }
        Task { await live.withdrawQueued(messageID, in: key) }
    }

    /// Moves a queued message back into the composer for editing.
    public func editQueued(_ messageID: String, in key: String) {
        guard requireLive() != nil,
              let message = queues[key]?.first(where: { $0.id == messageID }) else { return }
        withdrawQueued(messageID, in: key)
        drafts[key] = message.text
    }

    public func decide(_ approvalID: String, _ decision: ApprovalDecision, in key: String) {
        guard let live = requireLive() else { return }
        Task { await live.decide(approvalID, decision, in: key) }
    }

    public func togglePinned(_ id: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].pinned.toggle()
        let pinned = chats[index].pinned
        Task { await live.updateChat(id, fields: ["pinned": .bool(pinned)]) }
    }

    // MARK: Live activity

    /// The status line for a busy chat: starting, thinking (with the live
    /// reasoning summary), running a tool, or waiting for an approval.
    public func activity(forChat id: String) -> LiveActivity? {
        guard let chat = chat(id) else { return nil }
        let pending = pendingSends[id] ?? []
        guard chat.state == .running || chat.state == .needsAction || !pending.isEmpty else { return nil }
        let since = turnStartedAt[id] ?? pending.first?.time ?? .now
        let items = timeline(id)
        if items.pendingApproval != nil {
            return LiveActivity(kind: .waiting, label: "等待你批准", since: since)
        }
        if turnStartedAt[id] == nil, !pending.isEmpty {
            return LiveActivity(kind: .starting, label: "正在启动 \(chat.backend.displayName)", since: since)
        }
        for item in items.reversed() {
            if case .tool(_, let kind, let title, _, .running, _) = item {
                return LiveActivity(kind: .tool, label: kind.activeVerb, detail: title, since: since)
            }
            if case .message(_, let author, _, _, _, _, _) = item, author.kind == .user { break }
        }
        if let reasoning = liveReasoning[id], !reasoning.isEmpty {
            return LiveActivity(kind: .thinking, label: "正在思考", detail: reasoning, since: since)
        }
        return LiveActivity(kind: .thinking, label: "正在思考", since: since)
    }

    // MARK: Usage and media

    /// Rereads both providers' plan usage from the server.
    public func refreshUsage() {
        if let live { Task { await live.refreshUsage(refresh: true) } }
    }

    @ObservationIgnored private var mediaCache: [String: Data] = [:]

    /// Bytes of an image a chat uploaded or published (`fileID`), or of an
    /// artifact a message links to by path (`target`). Cached per session.
    public func media(chat id: String, fileID: String? = nil, target: String? = nil) async -> Data? {
        let key = "\(id)|\(fileID ?? "")|\(target ?? "")"
        if let cached = mediaCache[key] { return cached }
        guard let live, let data = try? await live.client.fileData(id, fileID: fileID, target: target) else { return nil }
        guard self.live === live else { return nil }
        mediaCache[key] = data
        return data
    }

    /// The status line for a group whose message is on its way to the members.
    public func activity(forGroup id: String) -> LiveActivity? {
        guard let pending = pendingSends[id]?.first, (groupLive[id] ?? []).isEmpty else { return nil }
        return LiveActivity(kind: .starting, label: "正在派发", since: pending.time)
    }

    /// Adds a member to a group; the server picks a free handle.
    public func addMember(_ backend: AgentBackend, toGroup id: String) {
        guard let live = requireLive() else { return }
        Task { await live.addMember(backend, toGroup: id) }
    }

    public func removeMember(_ handle: String, fromGroup id: String) {
        guard let live = requireLive() else { return }
        guard let index = groups.firstIndex(where: { $0.id == id }) else { return }
        groups[index].participants.removeAll { $0.handle == handle }
        Task { await live.removeMember(handle, fromGroup: id) }
    }

    // MARK: Models and reasoning effort
    //
    // Chats and members store the server's raw values ("" = server default);
    // names and choices come from the native catalog the server discovers.

    public func catalog(for backend: AgentBackend) -> RuntimeCatalog {
        catalogs[backend] ?? .fallback(for: backend)
    }

    public func permissionChoices(for chat: ChatSummary) -> [String] {
        let labels = PermissionPreset.presets(for: chat.backend).map(\.label)
        return labels.contains(chat.permission) ? labels : labels + [chat.permission]
    }

    public func permissionPreset(_ label: String, backend: AgentBackend) -> PermissionPreset? {
        PermissionPreset.preset(label, for: backend)
    }

    public func setModel(_ value: String, forChat id: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].model = value
        var fields: [String: JSON] = ["model": value.isEmpty ? .null : .string(value)]
        // A Codex model only accepts its own efforts; fall back to the default.
        if !catalog(for: chats[index].backend).supports(effort: chats[index].effort, model: value) {
            chats[index].effort = ""
            fields["effort"] = .null
        }
        Task { await live.updateChat(id, fields: .object(fields)) }
    }

    public func setEffort(_ value: String, forChat id: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].effort = value
        Task { await live.updateChat(id, fields: ["effort": value.isEmpty ? .null : .string(value)]) }
    }

    public func setPermission(_ label: String, forChat id: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].permission = label
        guard let preset = PermissionPreset.preset(label, for: chats[index].backend) else { return }
        Task { await live.updateChat(id, fields: .object(preset.fields.mapValues(JSON.string))) }
    }

    // MARK: Goals
    //
    // Claude Code keeps working until its `/goal` condition holds; Codex
    // pursues a persistent thread objective, optionally within a token or
    // time budget. Both run on the provider; the app sets, pauses and clears.

    /// The server advertises native goal support per chat.
    public func goalSupport(forChat id: String) -> GoalSupport {
        goalSupport[id] ?? GoalSupport(available: false)
    }

    /// Sets or replaces a chat's goal. Budgets apply to Codex only; nil removes one.
    public func setGoal(_ text: String, tokenBudget: Int? = nil, timeLimitMinutes: Int? = nil, forChat id: String) {
        guard let live = requireLive() else { return }
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let chat = chat(id) ?? memberChat(id) else { return }
        Task { await live.setGoal(text, tokenBudget: tokenBudget, timeLimitMinutes: timeLimitMinutes,
                                  forChat: id, backend: chat.backend) }
    }

    public func pauseGoal(forChat id: String) { setGoalStatus("paused", forChat: id) }
    public func resumeGoal(forChat id: String) { setGoalStatus("active", forChat: id) }

    private func setGoalStatus(_ status: String, forChat id: String) {
        guard let live = requireLive(), goals[id]?.backend == .codex else { return }
        Task { await live.setCodexGoalStatus(status, forChat: id) }
    }

    public func clearGoal(forChat id: String) {
        guard let live = requireLive(), let goal = goals[id] else { return }
        Task { await live.clearGoal(forChat: id, backend: goal.backend) }
    }

    /// The member chat behind a group participant, as a chat summary.
    private func memberChat(_ sessionID: String) -> ChatSummary? {
        for group in groups {
            if let member = group.participants.first(where: { $0.sessionID == sessionID }) {
                return ChatSummary(id: sessionID, title: "@\(member.handle)", backend: member.backend,
                                   model: member.model, permission: member.permission, cwd: group.cwd,
                                   preview: "", lastActivity: group.lastActivity)
            }
        }
        return nil
    }

    /// The group's goal and its member's live goal state.
    public func goal(forGroup id: String) -> (goal: GroupGoal, member: Participant?, state: ChatGoal?)? {
        guard let group = group(id), let goal = group.goal else { return nil }
        let member = group.participants.first { $0.handle == goal.handle }
        return (goal, member, member?.sessionID.flatMap { goals[$0] })
    }

    /// Sets a native goal on one member and announces it to the group.
    public func setGroupGoal(_ objective: String, handle: String, inGroup id: String) {
        guard let live = requireLive() else { return }
        let objective = objective.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !objective.isEmpty, group(id)?.participants.contains(where: { $0.handle == handle }) == true else { return }
        Task { await live.setGroupGoal(objective, handle: handle, inGroup: id) }
    }

    /// Clears the goal on the member that holds it.
    public func clearGroupGoal(inGroup id: String) {
        guard let live = requireLive(), let current = goal(forGroup: id),
              let session = current.member?.sessionID, let backend = current.member?.backend else { return }
        Task { await live.clearGoal(forChat: session, backend: backend) }
    }

    // MARK: Slash commands and skills

    /// What the composer offers for a partial "/name": the agent's own
    /// commands and skills, plus `/goal` where the provider supports goals.
    public func slashSuggestions(forChat id: String, partial: String) -> [ProviderCommand] {
        guard let chat = chat(id) else { return [] }
        var inventory = commands[id] ?? CommandInventory(revision: "", available: false, commands: [])
        if goalSupport(forChat: id).available, !inventory.commands.contains(where: { $0.invocation == "/goal" }) {
            inventory.commands.insert(ProviderCommand(
                id: "goal", name: "goal", label: "goal",
                detail: chat.backend == .codex ? "设定 Codex 持久目标" : "设定完成条件，Claude 会一直工作到满足为止",
                invocation: "/goal", kind: .command), at: 0)
        }
        if !inventory.commands.contains(where: { $0.invocation == "/btw" }) {
            inventory.commands.append(ProviderCommand(
                id: "btw", name: "btw", label: "btw", detail: "侧问：问个问题，不打断代理，也不写进对话",
                invocation: "/btw", kind: .command))
        }
        return inventory.matches(partial)
    }

    /// Why the suggestion list is short: still loading, or the agent offers none here.
    public func slashNote(forChat id: String) -> String? {
        guard isLive else { return nil }
        guard let inventory = commands[id] else { return "正在读取代理的命令和技能…" }
        if !inventory.available { return inventory.reason ?? "这个聊天的代理没有提供命令列表。" }
        return inventory.reason
    }

    /// Loads the chat's commands the first time the user types "/".
    public func loadCommandsIfNeeded(forChat id: String) {
        // A list that failed to load (for example while the agent was busy) is retried.
        guard commands[id]?.available != true, let live else { return }
        Task { await live.loadCommands(id) }
    }

    public func reloadCommands(forChat id: String) {
        guard let live else { return }
        Task { await live.loadCommands(id, refresh: true) }
    }

    // MARK: MCP servers

    public func refreshMCP(forChat id: String) {
        guard let live, let chat = chat(id) else { return }
        mcpBusy.insert(id)
        Task { await live.refreshMCP(id, backend: chat.backend) }
    }

    /// `action` is reconnect, enable or disable (Claude chats).
    public func controlMCP(_ action: String, server: String?, forChat id: String) {
        guard let live = requireLive() else { return }
        mcpBusy.insert(id)
        Task { await live.controlMCP(action, server: server, forChat: id) }
    }

    /// Retries each server that failed to connect, one at a time. Servers
    /// that need a sign-in are left alone: Claude Code's `/mcp` signs in.
    public func reconnectFailedMCP(forChat id: String) {
        guard let live = requireLive(), mcp[id]?.servers.contains(where: { $0.status == .failed }) == true else { return }
        mcpBusy.insert(id)
        Task { await live.controlMCP("reconnect_all", server: nil, forChat: id) }
    }

    // MARK: Attachments

    /// Adds files to a chat's composer and uploads them to the chat at once.
    public func attach(_ urls: [URL], toChat id: String) {
        guard let live = requireLive() else { return }
        for url in urls where !url.hasDirectoryPath {
            let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
            let attachment = DraftAttachment(name: url.lastPathComponent, size: size)
            attachments[id, default: []].append(attachment)
            Task { await live.upload(url, attachment: attachment.id, toChat: id) }
        }
    }

    public func removeAttachment(_ attachmentID: String, fromChat id: String) {
        attachments[id]?.removeAll { $0.id == attachmentID }
    }

    func setAttachment(_ attachmentID: String, in id: String, _ state: DraftAttachment.State) {
        guard let index = attachments[id]?.firstIndex(where: { $0.id == attachmentID }) else { return }
        attachments[id]?[index].state = state
    }

    // MARK: Rename, archive, delete

    public func rename(_ item: SidebarItem, to rawTitle: String) {
        guard let live = requireLive() else { return }
        let title = rawTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return }
        switch item {
        case .chat(let id):
            guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
            chats[index].title = title
            Task { await live.updateChat(id, fields: ["title": .string(title)]) }
        case .group(let id):
            guard let index = groups.firstIndex(where: { $0.id == id }) else { return }
            groups[index].title = String(title.prefix(200))
            Task { await live.updateGroup(id, ["title": .string(String(title.prefix(200)))]) }
        default:
            break
        }
    }

    // MARK: Chat and group settings

    /// The chat's advanced settings from its full server record.
    public func chatSettings(_ id: String) async throws -> ChatSettings {
        guard let live = requireLive() else { throw HyadexClientError.unreachable(errorMessage ?? "") }
        return try await live.chatSettings(id)
    }

    /// Saves changed advanced settings and returns what the server stored.
    public func saveChatSettings(_ id: String, from old: ChatSettings, to new: ChatSettings) async throws -> ChatSettings {
        let fields = old.changes(to: new)
        guard !fields.isEmpty else { return old }
        guard let live = requireLive() else { throw HyadexClientError.unreachable(errorMessage ?? "") }
        return try await live.saveChatSettings(id, fields: .object(fields))
    }

    /// Saves a group's changed responder, hand-off limit or brief.
    public func saveGroupSettings(_ id: String, to new: GroupSettings) async throws {
        guard let old = group(id)?.settings else { return }
        let fields = old.changes(to: new)
        guard !fields.isEmpty else { return }
        guard let live = requireLive() else { throw HyadexClientError.unreachable(errorMessage ?? "") }
        try await live.saveGroupSettings(id, fields: .object(fields))
    }

    /// Archiving pauses a chat's jobs and closes its terminal; a group stops its rounds.
    public func setArchived(_ item: SidebarItem, _ archived: Bool) {
        guard let live = requireLive() else { return }
        switch item {
        case .chat(let id):
            guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
            chats[index].archived = archived
            if archived { chats[index].pinned = false }
            Task { await live.updateChat(id, fields: ["archived": .bool(archived)]) }
        case .group(let id):
            guard let index = groups.firstIndex(where: { $0.id == id }) else { return }
            groups[index].archived = archived
            Task { await live.updateGroup(id, ["archived": .bool(archived)]) }
        default:
            return
        }
        if archived, selection == item { select(firstItem(in: sidebarMode)) }
    }

    /// Deletes a chat (its turn stops; its jobs go) or a group (its member chats are archived).
    public func delete(_ item: SidebarItem) {
        guard let live = requireLive() else { return }
        Task { await live.delete(item) }
    }

    func removeLocally(_ item: SidebarItem) {
        switch item {
        case .chat(let id):
            chats.removeAll { $0.id == id }
            jobs.removeAll { $0.sessionID == id && $0.groupID == nil }
        case .group(let id):
            groups.removeAll { $0.id == id }
            jobs.removeAll { $0.groupID == id }
        default:
            return
        }
        if selection == item { select(firstItem(in: sidebarMode)) }
    }

    /// Rename, archive and delete act on the selected chat or group.
    public var selectedManageable: SidebarItem? {
        switch selection {
        case .chat(let id) where chat(id) != nil: selection
        case .group(let id) where group(id) != nil: selection
        default: nil
        }
    }

    public func title(of item: SidebarItem) -> String {
        switch item {
        case .chat(let id): chat(id)?.title ?? ""
        case .group(let id): group(id)?.title ?? ""
        default: ""
        }
    }

    public func isArchived(_ item: SidebarItem) -> Bool {
        switch item {
        case .chat(let id): chat(id)?.archived ?? false
        case .group(let id): group(id)?.archived ?? false
        default: false
        }
    }

    // MARK: Jobs

    public func job(_ id: String) -> ScheduledJob? { jobs.first { $0.id == id } }

    public func createJob(title: String, prompt: String, target: JobTarget, schedule: JobSchedule, enabled: Bool) {
        guard let live = requireLive() else { return }
        var body = schedule.fields.filter { $0.value != .null }
        body["title"] = .string(title)
        body["prompt"] = .string(prompt)
        body["enabled"] = .bool(enabled)
        Task { await live.createJob(.object(body), target: target) }
    }

    public func updateJob(_ id: String, title: String, prompt: String, schedule: JobSchedule, enabled: Bool) {
        guard let live = requireLive() else { return }
        guard let index = jobs.firstIndex(where: { $0.id == id }) else { return }
        jobs[index].title = title
        jobs[index].prompt = prompt
        jobs[index].enabled = enabled
        jobs[index].spec = schedule
        var body = schedule.fields
        body["title"] = .string(title)
        body["prompt"] = .string(prompt)
        body["enabled"] = .bool(enabled)
        // A changed schedule starts over from its own next occurrence.
        body["next_run_at"] = .null
        body.removeValue(forKey: "first_run_at")
        if case .interval(_, _, let firstRun?) = schedule {
            body["next_run_at"] = .string(ISO8601DateFormatter().string(from: firstRun))
        }
        Task { await live.updateJob(id, fields: .object(body)) }
    }

    public func deleteJob(_ id: String) {
        guard let live = requireLive() else { return }
        guard let job = job(id) else { return }
        jobs.removeAll { $0.id == id }
        Task { await live.deleteJob(id, groupID: job.groupID) }
    }

    // MARK: Server folders

    /// Folders on the connected server under (or starting with) `path`.
    public func directorySuggestions(_ path: String) async -> (folders: [DirectorySuggestion], message: String?) {
        guard let live = requireLive() else { return ([], errorMessage) }
        return await live.completeDirectory(path)
    }

    /// Where a changed file of the current selection lives on the server, and
    /// the chat whose folder contains it (nil for Autopilot worktrees).
    public func location(of file: FileChange) -> (path: String, chatID: String?) {
        switch selection {
        case .chat(let id): (file.absolutePath(fallbackRoot: chat(id)?.cwd), id)
        case .group(let id): (file.absolutePath(fallbackRoot: group(id)?.cwd), group(id)?.participants.first?.sessionID)
        case .autopilot(let id): (file.absolutePath(fallbackRoot: run(id)?.cwd), nil)
        default: (file.path, nil)
        }
    }

    /// The server's copy of a changed file, for opening it from a remote server.
    public func serverCopy(of file: FileChange) async throws -> Data {
        let (path, chatID) = location(of: file)
        guard let chatID else { throw HyadexClientError.http(404, "Autopilot 工作区的文件只能在服务器本机打开") }
        guard let live = requireLive() else { throw HyadexClientError.unreachable(errorMessage ?? "") }
        return try await live.client.workspaceFile(chatID, path: path)
    }

    /// Whether the connected server runs on this Mac, so a local folder picker applies.
    public var serverIsLocal: Bool {
        guard isLive else { return false }
        let host = currentServer?.baseURL.host()?.lowercased() ?? ""
        return ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host)
    }

    // MARK: Queue

    /// Force Send: Codex takes it into the running turn when it can; otherwise
    /// the running turn stops and this message runs next.
    public func runQueuedNow(_ messageID: String, in key: String) {
        guard let live = requireLive() else { return }
        Task { await live.runQueuedNow(messageID, in: key) }
    }

    /// Continues a queue that Stop held: the held messages are sent in order.
    public func resumeQueue(in key: String) {
        guard let live = requireLive() else { return }
        Task { await live.resumeQueue(key) }
    }

    /// Moves a queued message one place up or down.
    public func moveQueued(_ messageID: String, up: Bool, in key: String) {
        guard let live = requireLive() else { return }
        guard var queue = queues[key], let index = queue.firstIndex(where: { $0.id == messageID }) else { return }
        let target = up ? index - 1 : index + 1
        guard queue.indices.contains(target) else { return }
        let adjacent = queue[target].id
        queue.swapAt(index, target)
        queues[key] = queue
        Task { await live.moveQueued(messageID, up: up, expectedAdjacent: adjacent, in: key) }
    }

    // MARK: Emergency alerts

    public func acknowledge(_ alert: EmergencyAlert) {
        guard let live = requireLive() else { return }
        emergencies.removeAll { $0.sessionID == alert.sessionID }
        Task { await live.acknowledge(alert) }
    }

    // MARK: History and context

    public func loadOlder(_ chatID: String) {
        guard let live, olderHistory.contains(chatID), !loadingOlder.contains(chatID) else { return }
        // Marked before the request so a second click cannot load the same page twice.
        loadingOlder.insert(chatID)
        Task { await live.loadOlder(chatID) }
    }

    /// Compacts the chat's context: Codex natively, Claude with its `/compact` command.
    public func compact(_ chatID: String) {
        guard let live = requireLive() else { return }
        guard let chat = chat(chatID) else { return }
        switch chat.backend {
        case .codex:
            Task { await live.compactCodex(chatID) }
        case .claude:
            send("/compact", toChat: chatID)
        }
    }

    public func canCompact(_ chatID: String) -> Bool {
        guard isLive, case .connected = connection, let chat = chat(chatID), chat.state == .idle else { return false }
        if chat.backend == .codex { return true }
        return commands[chatID]?.commands.contains { $0.invocation == "/compact" } == true
    }

    // MARK: Search

    /// Searches every chat's messages; titles are matched locally as the user types.
    public func runSearch(_ text: String) async {
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= 2, sidebarMode == .work else {
            searchHits = []
            searching = false
            return
        }
        guard let live else {
            searchHits = []
            searching = false
            return
        }
        searching = true
        let hits = await live.search(query)
        // A newer search may have started meanwhile.
        guard self.live === live, searchText.trimmingCharacters(in: .whitespacesAndNewlines) == query else { return }
        searchHits = hits.filter { chat($0.sessionID) != nil }
        searching = false
    }

    /// Opens a chat at a search hit: loads history back to it, then scrolls there.
    public func open(_ hit: SearchHit) {
        select(.chat(hit.sessionID))
        if let live {
            Task { await live.reveal(hit) }
        } else {
            timelineFocus[hit.sessionID] = hit.eventID + "|" + UUID().uuidString
        }
    }

    // MARK: Fork, unread

    /// Copies a chat with its history (up to its last finished turn) and opens the copy.
    public func fork(_ chatID: String) {
        guard let live = requireLive(), chat(chatID) != nil else { return }
        Task { await live.fork(chatID) }
    }

    public func markUnread(_ chatID: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == chatID }) else { return }
        chats[index].unread = true
        markedUnread.insert(chatID)
        Task { await live.markUnread(chatID) }
    }

    public func markRead(_ chatID: String) {
        guard let live = requireLive() else { return }
        guard let index = chats.firstIndex(where: { $0.id == chatID }) else { return }
        chats[index].unread = false
        markedUnread.remove(chatID)
        Task { await live.markRead(chatID) }
    }

    // MARK: Side chat

    public func toggleSideChat(_ chatID: String) {
        sideChatOpen = sideChatOpen == chatID ? nil : chatID
        if sideChatOpen == chatID { refreshSide(chatID) }
    }

    public func refreshSide(_ chatID: String) {
        guard let live else { return }
        Task { await live.refreshSide(chatID) }
    }

    /// Asks a quick question about the conversation without interrupting the agent.
    public func askSide(_ rawQuestion: String, inChat chatID: String) {
        guard let live = requireLive() else { return }
        let question = rawQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { return }
        sideChatErrors[chatID] = nil
        Task { await live.askSide(question, inChat: chatID) }
    }

    public func cancelSide(_ requestID: String, inChat chatID: String) {
        guard let live = requireLive() else { return }
        Task { await live.cancelSide(requestID, inChat: chatID) }
    }

    /// Starts a fresh side chat (Codex: with the conversation as it is now).
    public func clearSide(_ chatID: String) {
        guard let live = requireLive(), let side = sideChats[chatID] else { return }
        Task { await live.clearSide(side.sideChatID, inChat: chatID) }
    }

    // MARK: Subagents

    public func refreshSubagents(_ chatID: String) {
        guard let live else { return }
        Task { await live.refreshSubagents(chatID) }
    }

    // MARK: Group members

    public func decide(_ approval: GroupApproval, _ decision: ApprovalDecision, inGroup groupID: String) {
        guard let live = requireLive() else { return }
        groupApprovals[groupID]?.removeAll { $0.id == approval.id }
        Task { await live.decide(approval, decision, inGroup: groupID) }
    }

    public func permissionChoices(for member: Participant) -> [String] {
        let labels = PermissionPreset.presets(for: member.backend).map(\.label)
        return labels.contains(member.permission) ? labels : labels + [member.permission]
    }

    /// Changes a group member's model, effort or permissions. A running turn
    /// keeps its settings; the change applies from the member's next turn.
    public func updateMember(_ handle: String, inGroup groupID: String,
                             model newModel: String? = nil, effort: String? = nil, permission: String? = nil) {
        guard let live = requireLive() else { return }
        guard let index = groups.firstIndex(where: { $0.id == groupID }),
              let member = groups[index].participants.firstIndex(where: { $0.handle == handle }) else { return }
        let backend = groups[index].participants[member].backend
        var fields: [String: JSON] = [:]
        if let newModel {
            groups[index].participants[member].model = newModel
            fields["model"] = newModel.isEmpty ? .null : .string(newModel)
            let current = groups[index].participants[member].effort
            if effort == nil, !catalog(for: backend).supports(effort: current, model: newModel) {
                groups[index].participants[member].effort = ""
                fields["effort"] = .null
            }
        }
        if let effort {
            groups[index].participants[member].effort = effort
            fields["effort"] = effort.isEmpty ? .null : .string(effort)
        }
        if let permission {
            groups[index].participants[member].permission = permission
            for (key, value) in PermissionPreset.presets(for: backend).first(where: { $0.label == permission })?.fields ?? [:] {
                fields[key] = .string(value)
            }
        }
        if !fields.isEmpty {
            Task { await live.updateMember(handle, inGroup: groupID, fields: .object(fields)) }
        }
    }

    @discardableResult
    public func createChat(backend: AgentBackend, model: String, cwd: String, permission: String) -> String {
        guard let live = requireLive() else { return "" }
        let preset = PermissionPreset.presets(for: backend).first { $0.label == permission }
        Task { await live.createChat(backend: backend, model: model.isEmpty ? nil : model, cwd: cwd, permission: preset) }
        return ""
    }

    // MARK: Groups

    public func send(_ rawText: String, toGroup id: String, mode: GroupMode, analysisOnly: Bool = false) {
        guard let live = requireLive() else { return }
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, group(id) != nil else { return }
        Task { await live.send(text, toGroup: id, mode: mode, analysisOnly: analysisOnly) }
    }

    public func createGroup(title: String, cwd: String, participants: [Participant]) {
        guard let live = requireLive() else { return }
        Task { await live.createGroup(title: title, cwd: cwd, participants: participants) }
    }



    // MARK: Autopilot

    public func approve(run id: String) {
        guard let live = requireLive() else { return }
        Task { await live.runAction(id, "approve") }
    }

    public func feedback(run id: String, text: String) {
        guard let live = requireLive() else { return }
        Task { await live.runAction(id, "feedback", feedback: text) }
    }

    public func pause(run id: String) {
        guard let live = requireLive() else { return }
        Task { await live.runAction(id, "pause") }
    }

    public func resume(run id: String) {
        guard let live = requireLive() else { return }
        Task { await live.runAction(id, "resume") }
    }

    public func abort(run id: String) {
        guard let live = requireLive() else { return }
        Task { await live.runAction(id, "abort") }
    }

    public func createRun(goal: String, cwd: String, approval: ApprovalOption, isolation: IsolationOption,
                          reviewer: ReviewerOption, verifyCommand: String) {
        guard let live = requireLive() else { return }
        Task {
            await live.createRun(goal: goal, cwd: cwd, approval: approval, isolation: isolation,
                                 reviewer: reviewer, verifyCommand: verifyCommand)
        }
    }

    // MARK: Jobs

    public func setJob(_ id: String, enabled: Bool) {
        guard let live = requireLive() else { return }
        guard let index = jobs.firstIndex(where: { $0.id == id }) else { return }
        jobs[index].enabled = enabled
        Task { await live.setJob(id, enabled: enabled) }
    }

    public func runJobNow(_ id: String) {
        guard let live = requireLive() else { return }
        Task { await live.runJob(id) }
    }

    // MARK: Server

    static let serversKey = "servers"
    static let currentServerKey = "currentServer"

    public var defaultCwd: String {
        live?.health?.defaultCwd.map(LiveMapping.abbreviate) ?? "~"
    }

    public func saveServers() {
        let persistent = servers.filter { $0.id != managedLocalServerID }
        if let data = try? JSONEncoder().encode(persistent) { UserDefaults.standard.set(data, forKey: Self.serversKey) }
        if currentServerID != managedLocalServerID {
            UserDefaults.standard.set(currentServerID.uuidString, forKey: Self.currentServerKey)
        }
    }

    func report(_ error: Error) {
        errorMessage = error.localizedDescription
    }

    /// Starts the app-owned backend and connects unless the user chose another server meanwhile.
    public func startLocalServer() async {
        let attempt = UUID()
        connectionGeneration = attempt
        errorMessage = nil
        do {
            let local = try await localServer.start()
            guard connectionGeneration == attempt, !Task.isCancelled else { return }
            managedLocalServerID = local.endpoint.id
            managedLocalToken = local.token
            if let defaultPlaceholder {
                servers.removeAll { $0 == defaultPlaceholder }
            }
            await connect(to: local.endpoint, token: local.token, remember: false)
        } catch {
            guard connectionGeneration == attempt, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }

    /// Reuses the app-owned local credentials without writing them to the keychain.
    public func reconnect(to server: ServerEndpoint) async {
        if server.id == managedLocalServerID {
            await startLocalServer()
        } else {
            await connect(to: server, token: TokenStore.token(for: server.id), remember: false)
        }
    }

    /// Connects on launch. `HYADEX_SERVER_URL` and `HYADEX_TOKEN` override the
    /// saved server (for development); otherwise a saved token is required.
    public func autoConnect() async {
        let environment = ProcessInfo.processInfo.environment
        // Development aid: a small first page, to exercise "加载更早的消息" on short chats.
        if let size = environment["HYADEX_HISTORY_PAGE"].flatMap(Int.init), size > 0 { Self.historyPageSize = size }
        if let url = environment["HYADEX_SERVER_URL"].flatMap(URL.init(string:)) {
            let server = ServerEndpoint(name: url.host() ?? "开发服务器", baseURL: url)
            await connect(to: server, token: environment["HYADEX_TOKEN"], remember: false)
            return
        }
        guard let server = currentServer, let token = TokenStore.token(for: server.id) else { return }
        await connect(to: server, token: token, remember: false)
    }

    /// Connects to one server; late results from replaced attempts are ignored.
    public func connect(to server: ServerEndpoint, token: String?, remember: Bool = true) async {
        connectionGeneration = UUID()
        let previousSelection = currentServerID == server.id ? selection : nil
        live?.stop()
        live = nil
        resetServerState()
        errorMessage = nil
        connection = .connecting
        let controller = LiveController(model: self, client: HyadexClient(endpoint: server, token: token, session: networkSession))
        live = controller
        do {
            try await controller.start()
            try Task.checkCancellation()
            guard live === controller else {
                controller.stop()
                return
            }
            isLive = true
            connection = controller.connectedState
            if let index = servers.firstIndex(where: { $0.id == server.id }) {
                servers[index] = server
            } else {
                servers.append(server)
            }
            currentServerID = server.id
            if remember, server.id != managedLocalServerID {
                if defaultPlaceholder?.id == server.id { defaultPlaceholder = nil }
                TokenStore.setToken(token, for: server.id)
                saveServers()
            }
            if let previousSelection, exists(previousSelection) {
                select(previousSelection)
            } else {
                select(firstItem(in: sidebarMode))
            }
        } catch {
            controller.stop()
            guard live === controller else { return }
            live = nil
            resetServerState()
            connection = .offline(error.localizedDescription)
            errorMessage = "无法连接到 \(server.name)：\(error.localizedDescription)"
        }
    }

    public func disconnect() {
        connectionGeneration = UUID()
        live?.stop()
        live = nil
        resetServerState()
        connection = .disconnected
        errorMessage = nil
    }

    /// Requiring a completed connection also prevents actions during startup.
    private func requireLive() -> LiveController? {
        guard isLive, case .connected = connection, let live else {
            errorMessage = "尚未连接服务器。请先启动本机后端或连接服务器。"
            return nil
        }
        return live
    }

    private func resetServerState() {
        isLive = false
        dropTerminalSessions()
        chats = []; groups = []; runs = []; jobs = []; timelines = [:]; queues = [:]
        pendingSends = [:]; liveReasoning = [:]; turnStartedAt = [:]; turnSeconds = [:]
        catalogs = [:]; groupApprovals = [:]; groupLive = [:]; processSeconds = [:]
        usage = [:]; contextUsage = [:]; goals = [:]; goalSupport = [:]; goalBusy = []
        commands = [:]; mcp = [:]; mcpBusy = []; attachments = [:]; emergencies = []
        olderHistory = []; loadingOlder = []; searchHits = []; searching = false
        timelineFocus = [:]; sideChats = [:]; sideChatErrors = [:]; subagents = [:]
        markedUnread = []; drafts = [:]; terminalOpen = []; mediaCache = [:]
        selection = nil; lastSelection = [:]; selectedFilePath = nil; showChanges = false
        goalEditor = nil; jobEditor = nil; renaming = nil; settingsEditor = nil; pendingDeletion = nil
        sideChatOpen = nil; sheet = nil
    }

    public func testConnection(to endpoint: ServerEndpoint, token: String?) async -> Result<HealthResponse, Error> {
        let token = endpoint.id == managedLocalServerID && servers.contains(endpoint) ? managedLocalToken : token
        do {
            return .success(try await HyadexClient(endpoint: endpoint, token: token, session: networkSession).health())
        } catch {
            return .failure(error)
        }
    }

}
