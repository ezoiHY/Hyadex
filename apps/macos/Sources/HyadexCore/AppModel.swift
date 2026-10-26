import Foundation
import HyadexAPI
import Observation

public enum ConnectionState: Hashable, Sendable {
    case demo
    case connecting
    case connected(version: String)
    case offline(String)

    public var label: String {
        switch self {
        case .demo: "演示数据"
        case .connecting: "正在连接…"
        case .connected(let version): "已连接 · v\(version)"
        case .offline(let reason): "离线 · \(reason)"
        }
    }
}

public enum InspectorTab: String, CaseIterable, Hashable, Sendable {
    case changes, details

    public var label: String { self == .changes ? "改动" : "详情" }
}

public enum SheetKind: String, Identifiable, Hashable, Sendable {
    case newChat, newGroup, newAutopilot, commandPalette, feedback

    public var id: String { rawValue }
}

/// The app's single source of truth. Views read slices of it and call its
/// methods; nothing else mutates state.
@MainActor
@Observable
public final class AppModel {
    public var servers: [ServerEndpoint]
    public var currentServerID: UUID
    public var connection: ConnectionState = .demo

    public var chats: [ChatSummary]
    public var groups: [GroupSummary]
    public var runs: [AutopilotRun]
    public var jobs: [ScheduledJob]
    public var timelines: [String: [TimelineItem]]
    public var queues: [String: [QueuedMessage]] = [:]
    public var drafts: [String: String] = [:]

    public var selection: SidebarItem?
    /// Which list the sidebar shows. Each tab remembers its last selection.
    public private(set) var sidebarMode: SidebarMode =
        UserDefaults.standard.string(forKey: "sidebarMode").flatMap(SidebarMode.init(rawValue:)) ?? .work
    @ObservationIgnored private var lastSelection: [SidebarMode: SidebarItem] = [:]
    public var showInspector = true
    public var inspectorTab: InspectorTab = .changes
    public var selectedFilePath: String?
    public var sheet: SheetKind?
    public var searchText = ""

    /// Last action error, shown as a dismissible banner.
    public var errorMessage: String?
    /// Whether the data shown comes from a server (false: built-in demo data).
    public private(set) var isLive = false

    @ObservationIgnored private var simulations: [String: Task<Void, Never>] = [:]
    @ObservationIgnored var live: LiveController?

    public init(now: Date = .now) {
        let saved = (UserDefaults.standard.data(forKey: Self.serversKey))
            .flatMap { try? JSONDecoder().decode([ServerEndpoint].self, from: $0) } ?? []
        let local = ServerEndpoint(name: "这台 Mac", baseURL: URL(string: "http://127.0.0.1:7870")!)
        servers = saved.isEmpty ? [local] : saved
        currentServerID = UserDefaults.standard.string(forKey: Self.currentServerKey).flatMap(UUID.init)
            .flatMap { id in saved.contains { $0.id == id } ? id : nil } ?? (saved.first ?? local).id
        chats = SampleData.chats(now: now)
        groups = SampleData.groups(now: now)
        runs = SampleData.runs(now: now)
        jobs = SampleData.jobs(now: now)
        timelines = SampleData.chatTimelines(now: now).merging(SampleData.groupTimelines(now: now)) { $1 }
        queues["c3"] = [QueuedMessage(text: "测试跑完后把 CHANGELOG 也检查一下。")]
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

    public var visiblePinned: [ChatSummary] { chats.filter { $0.pinned && matches($0.title, $0.preview) } }
    public var visibleChats: [ChatSummary] {
        chats.filter { !$0.pinned && matches($0.title, $0.preview) }.sorted { $0.lastActivity > $1.lastActivity }
    }
    public var visibleGroups: [GroupSummary] {
        groups.filter { matches($0.title, $0.preview) }.sorted { $0.lastActivity > $1.lastActivity }
    }
    public var visibleRuns: [AutopilotRun] {
        runs.filter { matches($0.title, $0.goal) }.sorted { $0.lastActivity > $1.lastActivity }
    }

    // MARK: Selection

    public func select(_ item: SidebarItem?) {
        selection = item
        selectedFilePath = nil
        if let item {
            lastSelection[item.mode] = item
            if item.mode != sidebarMode { setSidebarMode(item.mode) }
        }
        live?.open(item)
        switch item {
        case .chat(let id):
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
        inspectorTab = .changes
        showInspector = true
    }

    // MARK: Chats

    public func send(_ rawText: String, toChat id: String) {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let index = chats.firstIndex(where: { $0.id == id }) else { return }
        if let live {
            Task { await live.send(text, toChat: id) }
            return
        }
        if chats[index].state == .running || chats[index].state == .needsAction {
            queues[id, default: []].append(QueuedMessage(text: text))
            return
        }
        append(.message(id: UUID().uuidString, author: .user, text: text, time: .now), to: id)
        chats[index].preview = text
        chats[index].lastActivity = .now
        simulateReply(in: id, author: .agent(chats[index].backend.rawValue, chats[index].backend))
    }

    public func stop(_ key: String) {
        if let live {
            Task { await live.stop(key) }
            return
        }
        simulations[key]?.cancel()
        simulations[key] = nil
        if let index = chats.firstIndex(where: { $0.id == key }) { chats[index].state = .idle }
        if let index = groups.firstIndex(where: { $0.id == key }) {
            for member in groups[index].participants.indices { groups[index].participants[member].state = .idle }
        }
        finishStreaming(in: key)
        markRunningToolsStopped(in: key)
        append(.divider(id: UUID().uuidString, text: "已停止"), to: key)
    }

    public func withdrawQueued(_ messageID: String, in key: String) {
        if let live {
            Task { await live.withdrawQueued(messageID, in: key) }
            return
        }
        queues[key]?.removeAll { $0.id == messageID }
    }

    /// Moves a queued message back into the composer for editing.
    public func editQueued(_ messageID: String, in key: String) {
        guard let message = queues[key]?.first(where: { $0.id == messageID }) else { return }
        withdrawQueued(messageID, in: key)
        drafts[key] = message.text
    }

    public func decide(_ approvalID: String, _ decision: ApprovalDecision, in key: String) {
        if let live {
            Task { await live.decide(approvalID, decision, in: key) }
            return
        }
        guard var items = timelines[key],
              let index = items.firstIndex(where: { $0.id == approvalID }),
              case .approval(let id, let author, let title, let command, let reason, _, let allowsSession) = items[index]
        else { return }
        items[index] = .approval(id: id, author: author, title: title, command: command, reason: reason,
                                 decision: decision, allowsSession: allowsSession)
        timelines[key] = items
        guard let chatIndex = chats.firstIndex(where: { $0.id == key }) else { return }
        if decision == .denied {
            chats[chatIndex].state = .idle
            append(.message(id: UUID().uuidString, author: author, text: "好的，不运行这个命令。要换一种方式确认吗？", time: .now), to: key)
        } else {
            append(.tool(id: UUID().uuidString, kind: .command, title: command, output: "", status: .running, seconds: 0), to: key)
            simulateReply(in: key, author: author)
        }
    }

    public func togglePinned(_ id: String) {
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].pinned.toggle()
        if let live {
            let pinned = chats[index].pinned
            Task { await live.updateChat(id, fields: ["pinned": .bool(pinned)]) }
        }
    }

    /// Model choices offered for a chat. Live servers accept provider aliases.
    public func modelChoices(for chat: ChatSummary) -> [String] {
        guard isLive else { return chat.backend.defaultModels }
        let base = chat.backend == .claude ? ["默认模型", "opus", "sonnet", "haiku"] : ["默认模型"]
        return base.contains(chat.model) ? base : base + [chat.model]
    }

    public func permissionChoices(for chat: ChatSummary) -> [String] {
        let labels = PermissionPreset.presets(for: chat.backend).map(\.label)
        return labels.contains(chat.permission) ? labels : labels + [chat.permission]
    }

    public func setModel(_ value: String, forChat id: String) {
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].model = value
        if let live { Task { await live.updateChat(id, fields: ["model": value == "默认模型" ? .null : .string(value)]) } }
    }

    public func setEffort(_ label: String, forChat id: String) {
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].effort = label
        let value = EffortPreset.labels.first { $0.label == label }?.value
        if let live { Task { await live.updateChat(id, fields: ["effort": value.map(JSON.string) ?? .null]) } }
    }

    public func setPermission(_ label: String, forChat id: String) {
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].permission = label
        guard let live, let preset = PermissionPreset.presets(for: chats[index].backend).first(where: { $0.label == label })
        else { return }
        Task { await live.updateChat(id, fields: .object(preset.fields.mapValues(JSON.string))) }
    }

    @discardableResult
    public func createChat(backend: AgentBackend, model: String, cwd: String, permission: String) -> String {
        if let live {
            let preset = PermissionPreset.presets(for: backend).first { $0.label == permission }
            Task { await live.createChat(backend: backend, model: model == "默认模型" ? nil : model, cwd: cwd, permission: preset) }
            return ""
        }
        let id = "c" + UUID().uuidString.prefix(6)
        chats.insert(ChatSummary(id: id, title: "新聊天", backend: backend, model: model, permission: permission,
                                 cwd: cwd, preview: "还没有消息", lastActivity: .now), at: 0)
        select(.chat(id))
        return id
    }

    // MARK: Groups

    public func send(_ rawText: String, toGroup id: String, mode: GroupMode, analysisOnly: Bool = false) {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let index = groups.firstIndex(where: { $0.id == id }) else { return }
        if let live {
            Task { await live.send(text, toGroup: id, mode: mode, analysisOnly: analysisOnly) }
            return
        }
        let handles = Set(groups[index].participants.map(\.handle))
        var targets = MentionParser.mentions(in: text, handles: handles)
        if targets.contains("all") { targets = groups[index].participants.map(\.handle) }
        if targets.isEmpty { targets = [groups[index].defaultResponder] }
        append(.message(id: UUID().uuidString, author: .user, text: text, time: .now, mentions: targets), to: id)
        groups[index].preview = text
        groups[index].lastActivity = .now
        for member in groups[index].participants.indices where targets.contains(groups[index].participants[member].handle) {
            groups[index].participants[member].state = .running
        }
        let authors = targets.compactMap { handle in
            groups[index].participants.first { $0.handle == handle }.map { Author.agent($0.handle, $0.backend) }
        }
        simulations[id]?.cancel()
        simulations[id] = Task { [weak self] in
            for author in authors {
                guard let self, !Task.isCancelled else { return }
                await self.stream(reply: Self.demoReply(for: author), as: author, in: id)
                self.setMember(author.name, .idle, in: id)
            }
            guard let self, !Task.isCancelled else { return }
            self.append(.divider(id: UUID().uuidString, text: "本轮结束 · \(authors.count) 次派发"), to: id)
        }
    }

    public func createGroup(title: String, cwd: String, participants: [Participant]) {
        if let live {
            Task { await live.createGroup(title: title, cwd: cwd, participants: participants) }
            return
        }
        let id = "g" + UUID().uuidString.prefix(6)
        groups.insert(GroupSummary(id: id, title: title.isEmpty ? "新讨论组" : title, cwd: cwd, participants: participants,
                                   defaultResponder: participants.first?.handle ?? "claude",
                                   preview: "还没有消息", lastActivity: .now), at: 0)
        timelines[id] = [.divider(id: "\(id)-0", text: "讨论组已创建 · " + participants.map { "@\($0.handle)" }.joined(separator: " "))]
        select(.group(id))
    }

    private func setMember(_ handle: String, _ state: ActivityState, in groupID: String) {
        guard let index = groups.firstIndex(where: { $0.id == groupID }),
              let member = groups[index].participants.firstIndex(where: { $0.handle == handle }) else { return }
        groups[index].participants[member].state = state
    }

    // MARK: Autopilot

    public func approve(run id: String) {
        if let live {
            Task { await live.runAction(id, "approve") }
            return
        }
        guard let index = runs.firstIndex(where: { $0.id == id }) else { return }
        let wasPlan = runs[index].completedTasks == 0
        runs[index].status = .executing
        if let next = runs[index].tasks.firstIndex(where: { $0.state == .pending }) {
            runs[index].tasks[next].state = .executing
        }
        runs[index].notes.append(.message(id: UUID().uuidString, author: .autopilot,
                                          text: wasPlan ? "计划已批准，开始执行任务 1。" : "已批准，继续下一步。", time: .now))
        runs[index].lastActivity = .now
    }

    public func feedback(run id: String, text: String) {
        if let live {
            Task { await live.runAction(id, "feedback", feedback: text) }
            return
        }
        guard let index = runs.firstIndex(where: { $0.id == id }), !text.isEmpty else { return }
        runs[index].status = runs[index].completedTasks == 0 ? .planning : .executing
        runs[index].notes.append(.message(id: UUID().uuidString, author: .user, text: text, time: .now))
        runs[index].lastActivity = .now
    }

    public func pause(run id: String) {
        if let live {
            Task { await live.runAction(id, "pause") }
            return
        }
        guard let index = runs.firstIndex(where: { $0.id == id }) else { return }
        runs[index].status = .paused
        runs[index].notes.append(.message(id: UUID().uuidString, author: .autopilot, text: "将在当前步骤完成后暂停。", time: .now))
    }

    public func resume(run id: String) {
        if let live {
            Task { await live.runAction(id, "resume") }
            return
        }
        guard let index = runs.firstIndex(where: { $0.id == id }) else { return }
        runs[index].status = .executing
        runs[index].notes.append(.message(id: UUID().uuidString, author: .autopilot, text: "已继续。", time: .now))
    }

    public func abort(run id: String) {
        if let live {
            Task { await live.runAction(id, "abort") }
            return
        }
        guard let index = runs.firstIndex(where: { $0.id == id }) else { return }
        runs[index].status = .aborted
        runs[index].notes.append(.message(id: UUID().uuidString, author: .autopilot,
                                          text: "已中止。worktree 和分支已保留。", time: .now))
    }

    public func createRun(goal: String, cwd: String, approval: ApprovalOption, isolation: IsolationOption,
                          reviewer: ReviewerOption, verifyCommand: String) {
        if let live {
            Task {
                await live.createRun(goal: goal, cwd: cwd, approval: approval, isolation: isolation,
                                     reviewer: reviewer, verifyCommand: verifyCommand)
            }
            return
        }
        let id = "r" + UUID().uuidString.prefix(6)
        let title = String(goal.prefix(18))
        runs.insert(AutopilotRun(id: id, title: title.isEmpty ? "新的自动接力" : title, goal: goal, cwd: cwd,
                                 branch: isolation == .worktree ? "hyadex/run-\(id.dropFirst())" : nil,
                                 status: .planning, approval: approval, isolation: isolation, reviewer: reviewer,
                                 verifyCommand: verifyCommand, planSummary: "", tasks: [],
                                 notes: [.message(id: UUID().uuidString, author: .autopilot, text: "Claude 正在规划…", time: .now)],
                                 lastActivity: .now), at: 0)
        select(.autopilot(id))
    }

    // MARK: Jobs

    public func setJob(_ id: String, enabled: Bool) {
        guard let index = jobs.firstIndex(where: { $0.id == id }) else { return }
        jobs[index].enabled = enabled
        if let live { Task { await live.setJob(id, enabled: enabled) } }
    }

    public func runJobNow(_ id: String) {
        if let live { Task { await live.runJob(id) } }
    }

    // MARK: Server

    static let serversKey = "servers"
    static let currentServerKey = "currentServer"

    public var defaultCwd: String {
        live?.health?.defaultCwd.map(LiveMapping.abbreviate) ?? "~"
    }

    public func saveServers() {
        if let data = try? JSONEncoder().encode(servers) { UserDefaults.standard.set(data, forKey: Self.serversKey) }
        UserDefaults.standard.set(currentServerID.uuidString, forKey: Self.currentServerKey)
    }

    func report(_ error: Error) {
        errorMessage = error.localizedDescription
    }

    /// Connects on launch. `HYADEX_SERVER_URL` and `HYADEX_TOKEN` override the
    /// saved server (for development); otherwise a saved token is required.
    public func autoConnect() async {
        let environment = ProcessInfo.processInfo.environment
        if let url = environment["HYADEX_SERVER_URL"].flatMap(URL.init(string:)) {
            let server = ServerEndpoint(name: url.host() ?? "开发服务器", baseURL: url)
            await connect(to: server, token: environment["HYADEX_TOKEN"], remember: false)
            return
        }
        guard let server = currentServer, let token = TokenStore.token(for: server.id) else { return }
        await connect(to: server, token: token, remember: false)
    }

    /// Replaces the demo data with the server's data, or reports why not.
    public func connect(to server: ServerEndpoint, token: String?, remember: Bool = true) async {
        live?.stop()
        live = nil
        connection = .connecting
        let controller = LiveController(model: self, client: HyadexClient(endpoint: server, token: token))
        let previousSelection = selection
        do {
            if !isLive {
                chats = []; groups = []; runs = []; jobs = []; timelines = [:]; queues = [:]; drafts = [:]
            }
            live = controller
            isLive = true
            try await controller.start()
            connection = controller.connectedState
            if remember {
                if !servers.contains(where: { $0.id == server.id }) { servers.append(server) }
                currentServerID = server.id
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
            live = nil
            connection = .offline(error.localizedDescription)
            errorMessage = "无法连接到 \(server.name)：\(error.localizedDescription)"
            loadDemo()
        }
    }

    public func disconnect() {
        live?.stop()
        live = nil
        connection = .demo
        loadDemo()
    }

    private func loadDemo() {
        let now = Date.now
        isLive = false
        chats = SampleData.chats(now: now)
        groups = SampleData.groups(now: now)
        runs = SampleData.runs(now: now)
        jobs = SampleData.jobs(now: now)
        timelines = SampleData.chatTimelines(now: now).merging(SampleData.groupTimelines(now: now)) { $1 }
        queues = ["c3": [QueuedMessage(text: "测试跑完后把 CHANGELOG 也检查一下。")]]
        select(firstItem(in: sidebarMode))
    }

    public func testConnection(to endpoint: ServerEndpoint, token: String?) async -> Result<HealthResponse, Error> {
        do {
            return .success(try await HyadexClient(endpoint: endpoint, token: token).health())
        } catch {
            return .failure(error)
        }
    }

    // MARK: Demo simulation

    private func append(_ item: TimelineItem, to key: String) {
        timelines[key, default: []].append(item)
    }

    private func simulateReply(in id: String, author: Author) {
        guard let index = chats.firstIndex(where: { $0.id == id }) else { return }
        chats[index].state = .running
        simulations[id]?.cancel()
        simulations[id] = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .milliseconds(700))
            guard !Task.isCancelled else { return }
            self.markRunningToolsFinished(in: id)
            self.append(.thinking(id: UUID().uuidString, seconds: 2, text: "先确认上下文，再给出简短的回答。"), to: id)
            await self.stream(reply: Self.demoReply(for: author), as: author, in: id)
            guard !Task.isCancelled, let index = self.chats.firstIndex(where: { $0.id == id }) else { return }
            self.chats[index].state = .idle
            self.chats[index].lastActivity = .now
            self.drainQueue(of: id)
        }
    }

    private func drainQueue(of id: String) {
        guard let next = queues[id]?.first else { return }
        queues[id]?.removeFirst()
        send(next.text, toChat: id)
    }

    private func stream(reply: String, as author: Author, in key: String) async {
        let messageID = UUID().uuidString
        append(.message(id: messageID, author: author, text: "", time: .now, streaming: true), to: key)
        var shown = ""
        for character in reply {
            if Task.isCancelled { return }
            shown.append(character)
            if shown.count % 3 == 0 || character == "\n" {
                replaceMessage(messageID, in: key, text: shown, streaming: true)
                try? await Task.sleep(for: .milliseconds(18))
            }
        }
        replaceMessage(messageID, in: key, text: reply, streaming: false)
    }

    private func replaceMessage(_ messageID: String, in key: String, text: String, streaming: Bool) {
        guard var items = timelines[key], let index = items.lastIndex(where: { $0.id == messageID }),
              case .message(let id, let author, _, let time, let mentions, let receipt, _) = items[index] else { return }
        items[index] = .message(id: id, author: author, text: text, time: time, mentions: mentions,
                                receipt: receipt, streaming: streaming)
        timelines[key] = items
    }

    private func finishStreaming(in key: String) {
        guard var items = timelines[key] else { return }
        for index in items.indices {
            if case .message(let id, let author, let text, let time, let mentions, let receipt, true) = items[index] {
                items[index] = .message(id: id, author: author, text: text, time: time, mentions: mentions,
                                        receipt: receipt, streaming: false)
            }
        }
        timelines[key] = items
    }

    private func markRunningToolsFinished(in key: String) { settleRunningTools(in: key, as: .succeeded) }
    private func markRunningToolsStopped(in key: String) { settleRunningTools(in: key, as: .failed) }

    private func settleRunningTools(in key: String, as status: ToolStatus) {
        guard var items = timelines[key] else { return }
        for index in items.indices {
            if case .tool(let id, let kind, let title, let output, .running, let seconds) = items[index] {
                items[index] = .tool(id: id, kind: kind, title: title, output: output, status: status, seconds: seconds)
            }
        }
        timelines[key] = items
    }

    nonisolated static func demoReply(for author: Author) -> String {
        switch author.backend {
        case .codex:
            "收到。这是演示模式下的模拟回复：连接真实服务器后，这里会实时流式显示 Codex 的回答、命令输出和改动的文件。"
        default:
            "好的。这是演示模式下的模拟回复——连接到你的 Hyadex 服务器后，这里会实时显示 Claude 的思考过程、工具调用和改动。"
        }
    }
}
