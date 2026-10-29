import AppKit
import Foundation
import HyadexAPI

/// Connects `AppModel` to a Hyadex server: loads lists, keeps the open
/// conversation live over its event stream, and performs user actions.
/// Sidebar lists are refreshed by polling; the selected chat or group gets a
/// WebSocket that resumes after the last applied `seq`.
@MainActor
final class LiveController {
    unowned let model: AppModel
    let client: HyadexClient
    private(set) var health: HealthResponse?
    private var stopped = false

    /// A controller may publish only while it still owns the model's connection.
    private var isCurrent: Bool { !stopped && model.live === self && !Task.isCancelled }

    private var chatProjections: [String: SessionProjection] = [:]
    /// Raw events of each open chat, oldest first, so older pages can be prepended.
    private var chatEvents: [String: [JSON]] = [:]
    private var emergencySocket: EventSocket?
    private var sendChains: [String: Task<Void, Never>] = [:]
    private var groupProjections: [String: GroupProjection] = [:]
    private var runGroups: [String: String] = [:]  // run id → group id
    /// Member-chat records (model, effort, permissions) by session id.
    private var memberSessions: [String: JSON] = [:]
    private var socket: (key: String, socket: EventSocket)?
    /// Hidden member chats of the open group: their projections, live streams
    /// and live reasoning, keyed by session id.
    private var memberProjections: [String: SessionProjection] = [:]
    private var memberSockets: [String: EventSocket] = [:]
    private var memberReasoning: [String: String] = [:]
    /// Answers still being written, by chat (and member chat) id; not durable.
    private var chatLiveText: [String: LiveText] = [:]
    private var memberLiveText: [String: LiveText] = [:]
    private var pollTask: Task<Void, Never>?
    private var runPollTask: Task<Void, Never>?
    private var usageTask: Task<Void, Never>?
    /// Chats whose turn was running at the last publish (to notice a turn end).
    private var runningChats: Set<String> = []
    private var loadingDiffs: Set<String> = []
    /// Chats marked read after the user saw them, with when the server
    /// confirmed (`distantFuture` while the request is out). Lists fetched
    /// before then still count them unread and are overridden.
    private var seenAt: [String: Date] = [:]

    /// Events after which the chat's native goal changed.
    static let goalEventTypes: Set<String> = ["claude_goal_changed", "codex_goal_updated", "codex_goal_cleared"]

    /// Opt in to native approvals and questions for both providers.
    static let capabilities = ["claude_sdk_interactive_v1", "codex_interactive_v1"]

    init(model: AppModel, client: HyadexClient) {
        self.model = model
        self.client = client
    }

    func start() async throws {
        guard isCurrent else { throw CancellationError() }
        health = try await client.health()
        guard isCurrent else { throw CancellationError() }
        try await refreshLists()
        guard isCurrent else { throw CancellationError() }
        startEmergencyStream()
        // Probing the native CLIs can take a few seconds; do not hold up the lists.
        Task { await loadCatalogs() }
        // Plan usage changes slowly; native events also prompt a reread.
        usageTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.isCurrent else { return }
                await self.refreshUsage()
                try? await Task.sleep(for: .seconds(90))
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(4))
                guard let self, self.isCurrent else { return }
                do {
                    try await self.refreshLists()
                    guard self.isCurrent else { return }
                    if case .offline = self.model.connection { self.model.connection = self.connectedState }
                } catch {
                    guard self.isCurrent else { return }
                    self.model.connection = .offline(error.localizedDescription)
                }
            }
        }
    }

    func stop() {
        stopped = true
        for task in sendChains.values { task.cancel() }
        sendChains = [:]
        emergencySocket?.stop()
        emergencySocket = nil
        pollTask?.cancel()
        runPollTask?.cancel()
        usageTask?.cancel()
        socket?.socket.stop()
        socket = nil
        for stream in memberSockets.values { stream.stop() }
        memberSockets = [:]
    }

    var connectedState: ConnectionState { .connected(version: health?.version ?? "?") }

    /// Native models and efforts per backend, as the server discovered them.
    func loadCatalogs(refresh: Bool = false) async {
        guard isCurrent else { return }
        guard let catalog = try? await client.runtimeCatalog(refresh: refresh) else { return }
        guard isCurrent else { return }
        var result: [AgentBackend: RuntimeCatalog] = [:]
        for backend in AgentBackend.allCases {
            if let parsed = RuntimeCatalog.parse(catalog["backends"][backend.rawValue]) { result[backend] = parsed }
        }
        if !result.isEmpty { model.catalogs = result }
    }

    // MARK: Usage

    /// Plan usage of both provider accounts.
    func refreshUsage(refresh: Bool = false, only: AgentBackend? = nil) async {
        guard isCurrent else { return }
        await withTaskGroup(of: (AgentBackend, ProviderUsage?).self) { group in
            for backend in AgentBackend.allCases where only == nil || only == backend {
                let client = self.client
                group.addTask {
                    guard let json = try? await client.providerUsage(backend: backend.rawValue, refresh: refresh) else {
                        return (backend, nil)
                    }
                    return (backend, ProviderUsage.parse(json, backend: backend))
                }
            }
            for await (backend, usage) in group {
                guard isCurrent else { return }
                // Keep the last reading when the server cannot be reached.
                if let usage, model.usage[backend] != usage { model.usage[backend] = usage }
            }
        }
    }

    /// The chat's context-window fill, as its provider last reported it.
    func refreshContext(_ id: String) async {
        guard isCurrent else { return }
        guard let chat = model.chat(id) else { return }
        await refreshContext(id, backend: chat.backend)
    }

    /// Rereads the chat's provider runtime: context fill and native goal.
    func refreshContext(_ id: String, backend: AgentBackend) async {
        guard isCurrent else { return }
        guard let runtime = try? await client.runtime(id, backend: backend.rawValue) else { return }
        guard isCurrent else { return }
        applyContext(runtime, to: id)
        applyGoal(runtime, to: id, backend: backend)
    }

    private func applyGoal(_ runtime: JSON, to id: String, backend: AgentBackend) {
        guard isCurrent else { return }
        let goal = backend == .claude ? ChatGoal.claude(runtime: runtime) : ChatGoal.codex(runtime)
        let support = backend == .claude ? GoalSupport.claude(runtime: runtime) : GoalSupport.codex(runtime: runtime)
        if model.goals[id] != goal { model.goals[id] = goal }
        if model.goalSupport[id] != support { model.goalSupport[id] = support }
    }

    private func applyContext(_ json: JSON, to id: String) {
        guard isCurrent else { return }
        let snapshot = json["context_usage"].isNull ? json["context_usage_snapshot"] : json["context_usage"]
        if let usage = ContextUsage.parse(snapshot) {
            if model.contextUsage[id] != usage { model.contextUsage[id] = usage }
        } else if json["context_usage_state"].string.map({ $0 != "available" }) ?? false {
            model.contextUsage[id] = nil
        }
    }

    // MARK: Lists

    func refreshLists() async throws {
        guard isCurrent else { throw CancellationError() }
        let fetchedAt = Date()
        async let sessionList = client.sessions()
        async let groupList = client.groups(includeArchived: true)
        async let runList = client.runs()
        async let jobList = client.jobs()
        let (sessions, groups, runs, jobs) = try await (sessionList, groupList, runList, jobList)
        guard isCurrent else { throw CancellationError() }
        // Older servers have no consults; a failure here never fails the poll.
        let activeConsults = ((try? await client.activeConsults())?["consults"].array ?? []).compactMap(ActiveConsult.parse)
        guard isCurrent else { throw CancellationError() }
        if activeConsults != model.activeConsults { model.activeConsults = activeConsults }
        let awaitingApproval = Set(activeConsults.filter { $0.status == .awaitingApproval }.compactMap(\.sourceID))
        if case .chat(let open)? = model.selection,
           activeConsults.contains(where: { $0.sourceID == open || $0.targetID == open }) {
            Task { await refreshConsults(open) }
        }

        let previousChats = Dictionary(model.chats.map { ($0.id, $0) }, uniquingKeysWith: { $1 })
        var chats = sessions.compactMap { LiveMapping.chat($0, previous: previousChats[$0["id"].string ?? ""]) }
        for index in chats.indices {
            if let projection = chatProjections[chats[index].id] {
                chats[index].state = projection.needsAction ? .needsAction : projection.running ? .running : .idle
                if let text = projection.lastAssistantText { chats[index].preview = Self.preview(text) }
            }
            // A deep consult waiting for approval needs the user as much as a tool approval does.
            if awaitingApproval.contains(chats[index].id), chats[index].state == .running {
                chats[index].state = .needsAction
            }
            let id = chats[index].id
            if model.selection == .chat(id), !model.markedUnread.contains(id) {
                chats[index].unread = false
            } else if let seen = seenAt[id] {
                if fetchedAt > seen { seenAt[id] = nil } else { chats[index].unread = false }
            }
        }
        // Chats that are not open change state here (the open one, which has a
        // projection, in publishChat).
        for chat in chats where chatProjections[chat.id] == nil {
            if let old = previousChats[chat.id]?.state { SystemNotifier.chatChanged(chat, from: old, to: chat.state) }
        }
        if chats != model.chats { model.chats = chats }

        let previousGroups = Dictionary(model.groups.map { ($0.id, $0) }, uniquingKeysWith: { $1 })
        let mappedGroups = groups.compactMap {
            LiveMapping.group($0, previous: previousGroups[$0["id"].string ?? ""], members: memberSessions)
        }
        if mappedGroups != model.groups { model.groups = mappedGroups }

        for run in runs {
            if let id = run["id"].string, let group = run["group_id"].string { runGroups[id] = group }
        }
        let previousRuns = Dictionary(model.runs.map { ($0.id, $0) }, uniquingKeysWith: { $1 })
        let mappedRuns = runs.compactMap { LiveMapping.run($0, previous: previousRuns[$0["id"].string ?? ""]) }
        if mappedRuns != model.runs { model.runs = mappedRuns }

        let titles = Dictionary(chats.map { ($0.id, $0.title) } + mappedGroups.map { ($0.id, $0.title) },
                                uniquingKeysWith: { $1 })
        let mappedJobs = jobs.compactMap { LiveMapping.job($0, chatTitles: titles) }
        if mappedJobs != model.jobs { model.jobs = mappedJobs }

        await loadMissingPreviews()
        guard isCurrent else { throw CancellationError() }
    }

    /// The list has no message text, so fetch a short tail for chats without a preview.
    private func loadMissingPreviews() async {
        guard isCurrent else { return }
        let missing = model.chats.filter { $0.preview.isEmpty && chatProjections[$0.id] == nil }.prefix(12)
        guard !missing.isEmpty else { return }
        await withTaskGroup(of: (String, String?).self) { group in
            for chat in missing {
                let client = self.client
                group.addTask {
                    let tail = try? await client.sessionTail(chat.id, limit: 40)
                    let text = tail?["events"].array.reversed().first {
                        ["assistant_text", "turn_started"].contains($0["type"].string ?? "")
                    }
                    return (chat.id, text.flatMap { $0["text"].text ?? $0["prompt"].text })
                }
            }
            for await (id, text) in group {
                guard isCurrent else { return }
                guard let index = model.chats.firstIndex(where: { $0.id == id }) else { continue }
                model.chats[index].preview = text.map(Self.preview) ?? "还没有消息"
            }
        }
    }

    /// One plain line for the sidebar: Markdown links, code marks, emphasis and
    /// math delimiters removed.
    nonisolated static func preview(_ text: String) -> String {
        var plain = text.replacingOccurrences(of: #"\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
        for mark in ["**", "__", "`", "$$", "$", "#"] { plain = plain.replacingOccurrences(of: mark, with: "") }
        plain = plain.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        return String(plain.trimmingCharacters(in: .whitespaces).prefix(120))
    }

    // MARK: Selection

    func open(_ item: SidebarItem?) {
        guard isCurrent else { return }
        runPollTask?.cancel()
        let key: String? = switch item {
        case .chat(let id), .group(let id): id
        case .autopilot(let id): runGroups[id]
        default: nil
        }
        if socket?.key != key {
            let previous = socket?.key
            socket?.socket.stop()
            socket = nil
            for stream in memberSockets.values { stream.stop() }
            memberSockets = [:]
            // Live answers stop updating with their socket; their messages arrive with the history.
            if let previous {
                // The server marked the chat read when it was opened; what its
                // stream showed since has been seen too.
                if model.chat(previous) != nil, !model.markedUnread.contains(previous) { markSeen(previous) }
                if chatLiveText.removeValue(forKey: previous) != nil { publishChat(previous) }
                if !memberLiveText.isEmpty {
                    memberLiveText = [:]
                    publishGroup(previous)
                }
                // Without its stream a chat's projection stops at the moment it was
                // left. Forget it, so the polled list drives its sidebar state and
                // notifications, and reopening reloads it from the server.
                chatProjections[previous] = nil
                chatEvents[previous] = nil
            }
        }
        switch item {
        case .chat(let id): Task { await openChat(id) }
        case .group(let id): Task { await openGroup(id) }
        case .autopilot(let id): Task { await openRun(id) }
        default: break
        }
    }

    private func openChat(_ id: String) async {
        guard isCurrent else { return }
        guard let chat = model.chat(id) else { return }
        if chatProjections[id] == nil {
            do {
                let page = try await client.sessionTail(id, limit: AppModel.historyPageSize)
                guard isCurrent else { return }
                var projection = SessionProjection(backend: chat.backend, cwd: LiveMapping.expand(chat.cwd))
                projection.apply(page["events"].array)
                projection.setQueue(page["queued_turns"].array)
                chatProjections[id] = projection
                chatEvents[id] = page["events"].array
                if (page["events_omitted_before"].int ?? 0) > 0 { model.olderHistory.insert(id) }
            } catch {
                guard isCurrent else { return }
                model.report(error)
                return
            }
        }
        publishChat(id)
        Task { await refreshContext(id) }
        Task { await refreshConsults(id) }
        // The user may have moved on while the history loaded; only the
        // conversation that is still selected gets the live stream.
        guard model.selection == .chat(id) else { return }
        try? await client.markRead(id)
        guard isCurrent else { return }
        guard model.selection == .chat(id) else { return }
        // The reasoning and text lanes carry the provider's live reasoning
        // summary and the answer being written (neither is durable).
        subscribe(key: id, path: "api/sessions/\(id)/events", after: chatProjections[id]?.lastSeq ?? 0,
                  query: ["reasoning_stream": "true", "reasoning_text": "true", "text_stream": "true"]) { controller, event in
            controller.chatProjections[id]?.apply(event)
            controller.chatEvents[id, default: []].append(event)
            controller.chatLiveText[id]?.settle(with: event)
            controller.publishChat(id)
            if event["type"].string == "subagent_state" || event["type"].string == "turn_finished" {
                Task { await controller.refreshSubagents(id) }
            }
            // A reply finished in front of the user is read (here and in other clients).
            if event["type"].string == "turn_finished", NSApp?.isActive == true,
               !controller.model.markedUnread.contains(id) {
                controller.markSeen(id)
            }
            if Self.goalEventTypes.contains(event["type"].string ?? "") {
                Task { await controller.refreshContext(id) }
            }
        } control: { controller, frame in
            switch frame["type"].string {
            case "reasoning_summary_stream":
                let text = frame["items"].array.reversed().compactMap { $0["text"].text }.first
                controller.model.liveReasoning[id] = text.map { String($0.suffix(400)) }
            case "assistant_text_stream":
                controller.chatLiveText[id, default: LiveText()].apply(snapshot: frame)
                controller.publishChat(id)
            case "provider_runtime_changed" where frame["runtime"].string == "context_usage":
                controller.applyContext(frame, to: id)
            case "provider_usage_changed":
                let backend = frame["backend"].string.flatMap(AgentBackend.named)
                Task { await controller.refreshUsage(only: backend) }
            case "side_chat_updated":
                Task { await controller.refreshSide(id) }
            case "chat_consult_updated":
                Task { await controller.refreshConsults(id) }
            default:
                break
            }
        }
    }

    private func openGroup(_ id: String) async {
        guard isCurrent else { return }
        guard let group = model.group(id) else { return }
        Task { await refreshMemberGoal(of: id) }
        await loadMemberSessions(of: group)
        guard isCurrent else { return }
        await loadGroupEvents(id, backends: Dictionary(group.participants.map { ($0.handle, $0.backend) },
                                                       uniquingKeysWith: { $1 }))
        guard isCurrent else { return }
        await loadMemberProjections(of: id)
        guard isCurrent else { return }
        publishGroup(id)
        guard model.selection == .group(id) else { return }
        subscribe(key: id, path: "api/groups/\(id)/events/stream", after: groupProjections[id]?.lastSeq ?? 0) { controller, event in
            controller.groupProjections[id]?.apply(event)
            controller.publishGroup(id)
        }
        subscribeMembers(of: id)
    }

    /// Each member's own chat holds the work behind its answers (thinking,
    /// tools, approvals, diffs); its turns are matched to the group by the
    /// dispatch id the server uses as the turn's request id.
    private func loadMemberProjections(of groupID: String) async {
        guard isCurrent else { return }
        guard let group = model.group(groupID) else { return }
        for member in group.participants {
            guard let sessionID = member.sessionID, memberProjections[sessionID] == nil,
                  let page = try? await client.sessionTail(sessionID, limit: 400) else { continue }
                  guard isCurrent else { return }
            var projection = SessionProjection(backend: member.backend, cwd: LiveMapping.expand(group.cwd))
            projection.apply(page["events"].array)
            memberProjections[sessionID] = projection
            Task { await self.refreshContext(sessionID, backend: member.backend) }
        }
    }

    private func subscribeMembers(of groupID: String) {
        guard isCurrent else { return }
        guard let group = model.group(groupID) else { return }
        for member in group.participants {
            guard let sessionID = member.sessionID, memberSockets[sessionID] == nil else { continue }
            let stream = EventSocket(client: client, path: "api/sessions/\(sessionID)/events",
                                     after: memberProjections[sessionID]?.lastSeq ?? 0,
                                     query: ["reasoning_stream": "true", "reasoning_text": "true", "text_stream": "true"])
            memberSockets[sessionID] = stream
            let backend = member.backend
            stream.start { [weak self] frame in
                await MainActor.run {
                    guard let self, self.isCurrent, self.memberSockets[sessionID] === stream else { return }
                    switch frame {
                    case .event(let event):
                        let wasRunning = self.memberProjections[sessionID]?.running ?? false
                        self.memberProjections[sessionID]?.apply(event)
                        self.memberLiveText[sessionID]?.settle(with: event)
                        if wasRunning, self.memberProjections[sessionID]?.running == false {
                            self.memberReasoning[sessionID] = nil
                            self.memberLiveText[sessionID] = nil
                            Task { await self.refreshContext(sessionID, backend: backend) }
                        } else if Self.goalEventTypes.contains(event["type"].string ?? "") {
                            Task { await self.refreshContext(sessionID, backend: backend) }
                        }
                    case .control(let json):
                        switch json["type"].string {
                        case "reasoning_summary_stream":
                            let text = json["items"].array.reversed().compactMap { $0["text"].text }.first
                            self.memberReasoning[sessionID] = text.map { String($0.suffix(400)) }
                        case "assistant_text_stream":
                            self.memberLiveText[sessionID, default: LiveText()].apply(snapshot: json)
                        case "provider_runtime_changed" where json["runtime"].string == "context_usage":
                            self.applyContext(json, to: sessionID)
                            return
                        default:
                            return
                        }
                    }
                    self.publishGroup(groupID)
                }
            }
        }
    }

    private func openRun(_ id: String) async {
        guard isCurrent else { return }
        await refreshRun(id)
        guard isCurrent else { return }
        guard model.selection == .autopilot(id) else { return }
        runPollTask?.cancel()
        runPollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2.5))
                guard let self, self.isCurrent else { return }
                await self.refreshRun(id)
            }
        }
    }

    private func refreshRun(_ id: String) async {
        guard isCurrent else { return }
        guard let json = try? await client.run(id) else { return }
        guard isCurrent else { return }
        if let group = json["group_id"].string {
            runGroups[id] = group
            await loadGroupEvents(group, backends: ["planner": .claude, "executor": .codex, "reviewer": .claude])
            guard isCurrent else { return }
        }
        let previous = model.run(id)
        guard var run = LiveMapping.run(json, previous: previous) else { return }
        if let group = runGroups[id], let projection = groupProjections[group] {
            run.notes = projection.notes
        }
        if let index = model.runs.firstIndex(where: { $0.id == id }) {
            if model.runs[index] != run { model.runs[index] = run }
        } else {
            model.runs.insert(run, at: 0)
        }
    }

    /// Loads a group's timeline from where the projection left off.
    private func loadGroupEvents(_ id: String, backends: [String: AgentBackend]) async {
        guard isCurrent else { return }
        var projection = groupProjections[id] ?? GroupProjection(backends: backends)
        for _ in 0..<20 {
            guard let page = try? await client.groupEvents(id, after: projection.lastSeq, limit: 500), !page.isEmpty else { break }
            guard isCurrent else { return }
            projection.apply(page)
            if page.count < 500 { break }
        }
        groupProjections[id] = projection
    }

    private func subscribe(key: String, path: String, after: Int, query: [String: String] = [:],
                           apply: @escaping @MainActor (LiveController, JSON) -> Void,
                           control: @escaping @MainActor (LiveController, JSON) -> Void = { _, _ in }) {
        guard isCurrent else { return }
        guard socket?.key != key else { return }
        socket?.socket.stop()
        let stream = EventSocket(client: client, path: path, after: after, query: query)
        socket = (key, stream)
        stream.start { [weak self] frame in
            await MainActor.run {
                guard let self, self.isCurrent, self.socket?.socket === stream else { return }
                switch frame {
                case .event(let event): apply(self, event)
                case .control(let json): control(self, json)
                }
            }
        }
    }

    private func publishChat(_ id: String) {
        guard isCurrent else { return }
        guard let projection = chatProjections[id] else { return }
        if !projection.running { chatLiveText[id] = nil }
        model.timelines[id] = projection.items + (chatLiveText[id]?.messages(author: projection.agent) ?? [])
        model.queues[id] = projection.queued
        if let pending = model.pendingSends[id], pending.contains(where: { projection.acceptedRequests.contains($0.id) }) {
            model.pendingSends[id] = pending.filter { !projection.acceptedRequests.contains($0.id) }
        }
        if model.turnStartedAt[id] != projection.turnStartedAt { model.turnStartedAt[id] = projection.turnStartedAt }
        if model.turnSeconds[id] != projection.turnSeconds { model.turnSeconds[id] = projection.turnSeconds }
        if !projection.running, model.liveReasoning[id] != nil { model.liveReasoning[id] = nil }
        if projection.running {
            runningChats.insert(id)
        } else if runningChats.remove(id) != nil {
            // A finished turn moved the context fill and the plan usage.
            Task {
                await refreshContext(id)
                guard isCurrent else { return }
                await refreshUsage()
                guard isCurrent else { return }
            }
        }
        if let index = model.chats.firstIndex(where: { $0.id == id }) {
            let state: ActivityState = projection.needsAction ? .needsAction : projection.running ? .running : .idle
            if model.chats[index].state != state {
                SystemNotifier.chatChanged(model.chats[index], from: model.chats[index].state, to: state)
                model.chats[index].state = state
            }
            if let text = projection.lastAssistantText { model.chats[index].preview = Self.preview(text) }
            if let time = projection.lastActivity, time > model.chats[index].lastActivity {
                model.chats[index].lastActivity = time
            }
        }
        for (itemID, runID) in projection.diffsToLoad where !loadingDiffs.contains(itemID) {
            loadingDiffs.insert(itemID)
            Task {
                if let diff = try? await client.diff(id, runID: runID) {
                    guard isCurrent else { return }
                    chatProjections[id]?.setDiff(diff, forItem: itemID)
                    publishChat(id)
                }
                loadingDiffs.remove(itemID)
            }
        }
    }

    private func publishGroup(_ id: String) {
        guard isCurrent else { return }
        guard let projection = groupProjections[id] else { return }
        let group = model.group(id)
        func session(of handle: String) -> String? {
            group?.participants.first { $0.handle == handle }?.sessionID
        }
        // Each answer gets the work behind it, and the diffs of what it changed.
        var items: [TimelineItem] = []
        var seconds: [String: Int] = [:]
        for item in projection.items {
            guard case .message(let messageID, let author, let text, let time, let mentions, let receipt, let streaming) = item,
                  author.kind == .agent, let dispatch = projection.dispatchOfMessage[messageID],
                  let sessionID = session(of: author.name),
                  let turn = memberProjections[sessionID]?.turn(forRequest: dispatch) else {
                items.append(item)
                continue
            }
            items += turn.steps
            if let first = turn.steps.first, let duration = turn.seconds { seconds[first.id] = duration }
            items.append(.message(id: messageID, author: author, text: text, time: time, mentions: mentions,
                                  receipt: turn.changes ?? receipt, streaming: streaming))
        }
        // Answers still being worked on.
        var live: [MemberTurn] = []
        for dispatch in projection.activeDispatches {
            guard let member = group?.participants.first(where: { $0.handle == dispatch.handle }) else { continue }
            let sessionID = dispatch.sessionID ?? member.sessionID
            let turn = sessionID.flatMap { memberProjections[$0]?.turn(forRequest: dispatch.id) }
            let approvals: [GroupApproval] = (turn?.pendingApprovals ?? []).compactMap { item in
                guard let sessionID else { return nil }
                return GroupApproval(interactionID: item.id, handle: member.handle, backend: member.backend,
                                     sessionID: sessionID, request: item)
            }
            let since = turn?.startedAt ?? dispatch.time
            var activity: LiveActivity?
            if approvals.isEmpty {
                if turn == nil {
                    activity = LiveActivity(kind: .starting, label: "正在启动 \(member.backend.displayName)", since: since)
                } else if case .tool(_, let kind, let title, _, .running, _)? = turn?.steps.last {
                    activity = LiveActivity(kind: .tool, label: kind.activeVerb, detail: title, since: since)
                } else {
                    let reasoning = sessionID.flatMap { memberReasoning[$0] }.flatMap(ThoughtSummary.line)
                    activity = LiveActivity(kind: .thinking, label: "正在思考",
                                            detail: reasoning ?? turn.flatMap { ThoughtSummary.latest(in: $0.steps) }
                                                .flatMap(ThoughtSummary.line),
                                            since: since)
                }
            }
            live.append(MemberTurn(id: dispatch.id, author: .agent(member.handle, member.backend),
                                   steps: turn?.steps ?? [], approvals: approvals, activity: activity,
                                   liveText: sessionID.flatMap { memberLiveText[$0]?.items.last?.text }))
        }
        if model.timelines[id] != items { model.timelines[id] = items }
        if model.processSeconds[id] != seconds { model.processSeconds[id] = seconds }
        if model.groupLive[id] != live { model.groupLive[id] = live }
        let approvals = live.flatMap(\.approvals)
        if model.groupApprovals[id] != approvals { model.groupApprovals[id] = approvals }
        // A sent message is confirmed once the group shows it.
        if let pending = model.pendingSends[id], !pending.isEmpty {
            let posted = Set(projection.items.compactMap { item -> String? in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .user { return text }
                return nil
            })
            let remaining = pending.filter { !posted.contains($0.text) }
            if remaining != pending { model.pendingSends[id] = remaining }
        }
        loadMemberDiffs(groupID: id)
        guard let index = model.groups.firstIndex(where: { $0.id == id }) else { return }
        for (handle, state) in projection.memberStates {
            if let member = model.groups[index].participants.firstIndex(where: { $0.handle == handle }) {
                // A member waiting for an approval needs the user, whatever the group says.
                let effective: ActivityState = approvals.contains { $0.handle == handle } ? .needsAction : state
                if model.groups[index].participants[member].state != effective {
                    model.groups[index].participants[member].state = effective
                }
            }
        }
        if case .message(_, _, let text, _, _, _, _)? = projection.items.last(where: {
            if case .message = $0 { return true } else { return false }
        }) {
            model.groups[index].preview = Self.preview(text)
        }
    }

    /// Fetches the diffs member turns produced, so receipts open real diffs.
    private func loadMemberDiffs(groupID: String) {
        guard isCurrent else { return }
        for (sessionID, projection) in memberProjections {
            for (itemID, runID) in projection.diffsToLoad where !loadingDiffs.contains(itemID) {
                loadingDiffs.insert(itemID)
                Task {
                    if let diff = try? await client.diff(sessionID, runID: runID) {
                        guard isCurrent else { return }
                        memberProjections[sessionID]?.setDiff(diff, forItem: itemID)
                        publishGroup(groupID)
                    }
                    loadingDiffs.remove(itemID)
                }
            }
        }
    }

    // MARK: Goals

    func setGoal(_ text: String, tokenBudget: Int?, timeLimitMinutes: Int?, forChat id: String,
                 backend: AgentBackend) async {
        guard isCurrent else { return }
        model.goalBusy.insert(id)
        defer { if isCurrent { model.goalBusy.remove(id) } }
        do {
            switch backend {
            case .claude:
                // Claude Code runs `/goal <condition>` as a turn and keeps going until it holds.
                let runtime = try await client.setClaudeGoal(id, condition: text)
                guard isCurrent else { return }
                applyGoal(runtime, to: id, backend: .claude)
            case .codex:
                let result = try await client.setCodexGoal(id, [
                    "objective": .string(text),
                    "status": "active",
                    "token_budget": tokenBudget.map { .number(Double($0)) } ?? .null,
                    "time_budget_seconds": timeLimitMinutes.map { .number(Double($0 * 60)) } ?? .null,
                ])
                guard isCurrent else { return }
                model.goals[id] = ChatGoal.codex(result)
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshContext(id, backend: backend)
    }

    /// Pauses ("paused") or resumes ("active") Codex's goal.
    func setCodexGoalStatus(_ status: String, forChat id: String) async {
        guard isCurrent else { return }
        model.goalBusy.insert(id)
        defer { if isCurrent { model.goalBusy.remove(id) } }
        do {
            let goal = try await client.setCodexGoal(id, ["status": .string(status)])
            guard isCurrent else { return }
            model.goals[id] = ChatGoal.codex(goal)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshContext(id, backend: .codex)
    }

    func clearGoal(forChat id: String, backend: AgentBackend) async {
        guard isCurrent else { return }
        model.goalBusy.insert(id)
        defer { if isCurrent { model.goalBusy.remove(id) } }
        do {
            switch backend {
            case .claude: applyGoal(try await client.clearClaudeGoal(id), to: id, backend: .claude)
            case .codex:
                let goal = try await client.clearCodexGoal(id)
                guard isCurrent else { return }
                model.goals[id] = ChatGoal.codex(goal)
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshContext(id, backend: backend)
    }

    func setGroupGoal(_ objective: String, handle: String, inGroup id: String) async {
        guard isCurrent else { return }
        let member = model.group(id)?.participants.first { $0.handle == handle }
        let session = member?.sessionID
        if let session { model.goalBusy.insert(session) }
        defer { if isCurrent, let session { model.goalBusy.remove(session) } }
        do {
            let json = try await client.setGroupGoal(id, handle: handle, objective: objective)
            guard isCurrent else { return }
            if let group = LiveMapping.group(json["group"], previous: model.group(id), members: memberSessions),
               let index = model.groups.firstIndex(where: { $0.id == id }) {
                model.groups[index] = group
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshMemberGoal(of: id)
    }

    /// The live goal state of the group member holding the group's goal.
    func refreshMemberGoal(of groupID: String) async {
        guard isCurrent else { return }
        guard let goal = model.group(groupID)?.goal,
              let member = model.group(groupID)?.participants.first(where: { $0.handle == goal.handle }),
              let session = member.sessionID else { return }
        await refreshContext(session, backend: member.backend)
    }

    // MARK: Slash commands and skills

    func loadCommands(_ id: String, refresh: Bool = false) async {
        guard isCurrent else { return }
        do {
            if let inventory = CommandInventory.parse(try await client.providerCommands(id, refresh: refresh)) {
                guard isCurrent else { return }
                model.commands[id] = inventory
            }
        } catch {
            guard isCurrent else { return }
            model.commands[id] = CommandInventory(revision: "", available: false,
                                                  reason: error.localizedDescription, commands: [])
        }
    }

    /// The opaque selection for a message that starts with one of the chat's commands.
    func commandSelection(for text: String, in id: String) -> (id: String, revision: String)? {
        guard isCurrent else { return nil }
        guard let inventory = model.commands[id], inventory.available,
              let command = inventory.command(for: text) else { return nil }
        return (command.id, inventory.revision)
    }

    // MARK: MCP servers

    func refreshMCP(_ id: String, backend: AgentBackend) async {
        guard isCurrent else { return }
        model.mcpBusy.insert(id)
        defer { if isCurrent { model.mcpBusy.remove(id) } }
        do {
            let json = try await client.mcpStatus(id, backend: backend.rawValue)
            guard isCurrent else { return }
            model.mcp[id] = MCPStatus.parse(json, backend: backend)
                ?? MCPStatus(available: false, servers: [], reason: "服务器返回了无法识别的 MCP 状态。")
        } catch HyadexClientError.http(404, _) where isCurrent && model.chat(id) != nil {
            guard isCurrent else { return }
            model.mcp[id] = MCPStatus(available: false, servers: [], reason: "这台服务器不提供 MCP 状态（版本过旧）。")
        } catch {
            guard isCurrent else { return }
            model.mcp[id] = MCPStatus(available: false, servers: [], reason: error.localizedDescription)
        }
    }

    func controlMCP(_ action: String, server: String?, forChat id: String) async {
        guard isCurrent else { return }
        guard let generation = model.mcp[id]?.generation else { return }
        model.mcpBusy.insert(id)
        do {
            let json = try await client.controlClaudeMCP(id, action: action, server: server, generation: generation)
            guard isCurrent else { return }
            if let status = MCPStatus.parse(json, backend: .claude) { model.mcp[id] = status }
            model.mcpBusy.remove(id)
        } catch {
            guard isCurrent else { return }
            model.mcpBusy.remove(id)
            model.report(error)
            // A stale generation or a vanished server: show the current state.
            await refreshMCP(id, backend: .claude)
            guard isCurrent else { return }
        }
    }

    // MARK: Chat actions

    /// Sends in the order the user typed: each message waits for the previous
    /// one's request, so the server queues them in that order.
    func enqueueSend(_ text: String, toChat id: String, fileIDs: [String] = []) {
        guard isCurrent else { return }
        let previous = sendChains[id]
        sendChains[id] = Task { [weak self] in
            await previous?.value
            await self?.send(text, toChat: id, fileIDs: fileIDs)
        }
    }

    func send(_ text: String, toChat id: String, fileIDs: [String] = []) async {
        guard isCurrent else { return }
        // A command typed before the list arrived must still run as that command, not as text.
        if text.hasPrefix("/"), model.commands[id]?.available != true { await loadCommands(id) }
        guard isCurrent else { return }
        // Show the message and a "starting" status at once; the server's
        // turn_started (matched by client_request_id) replaces it.
        let requestID = "mac-" + UUID().uuidString
        let isBusy = model.chat(id).map { $0.state == .running || $0.state == .needsAction } ?? false
        if !isBusy { model.pendingSends[id, default: []].append(PendingSend(id: requestID, text: text)) }
        do {
            do {
                _ = try await client.sendTurn(id, prompt: text, requestID: requestID, capabilities: Self.capabilities,
                                              command: commandSelection(for: text, in: id), fileIDs: fileIDs)
                guard isCurrent else { return }
            } catch HyadexClientError.http(409, let detail?) where detail.contains("provider command") {
                guard isCurrent else { return }
                // The agent's command list changed since it was loaded: reload and resend once.
                await loadCommands(id)
                guard isCurrent else { return }
                _ = try await client.sendTurn(id, prompt: text, requestID: requestID, capabilities: Self.capabilities,
                                              command: commandSelection(for: text, in: id), fileIDs: fileIDs)
                guard isCurrent else { return }
            }
            if let index = model.chats.firstIndex(where: { $0.id == id }) {
                model.chats[index].preview = Self.preview(text)
                model.chats[index].lastActivity = .now
            }
        } catch {
            guard isCurrent else { return }
            model.pendingSends[id]?.removeAll { $0.id == requestID }
            model.drafts[id] = text
            model.report(error)
        }
    }

    func stop(_ id: String) async {
        guard isCurrent else { return }
        do {
            if model.group(id) != nil {
                try await client.stopGroup(id)
                guard isCurrent else { return }
            } else {
                try await client.stop(id)
                guard isCurrent else { return }
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func reply(_ interactionID: String, _ reply: InteractionReply, in id: String) async {
        guard isCurrent else { return }
        guard let chat = model.chat(id) else { return }
        chatProjections[id]?.noteDecision(reply.outcome, for: interactionID)
        publishChat(id)
        do {
            try await client.resolveInteraction(id, backend: chat.backend.rawValue, interactionID: interactionID,
                                                response: reply.response)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func withdrawQueued(_ queuedID: String, in id: String) async {
        guard isCurrent else { return }
        do {
            try await client.deleteQueued(id, queuedID: queuedID)
            guard isCurrent else { return }
            model.queues[id]?.removeAll { $0.id == queuedID }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func updateChat(_ id: String, fields: JSON) async {
        guard isCurrent else { return }
        do {
            let session = try await client.updateSession(id, fields)
            guard isCurrent else { return }
            if let index = model.chats.firstIndex(where: { $0.id == id }),
               let mapped = LiveMapping.chat(session, previous: model.chats[index]) {
                model.chats[index] = mapped
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func chatSettings(_ id: String) async throws -> ChatSettings {
        let json = try await client.sessionRecord(id)
        try checkCurrent()
        return ChatSettings.parse(json)
    }

    func saveChatSettings(_ id: String, fields: JSON) async throws -> ChatSettings {
        let session = try await client.updateSession(id, fields)
        try checkCurrent()
        if let index = model.chats.firstIndex(where: { $0.id == id }),
           let mapped = LiveMapping.chat(session, previous: model.chats[index]) {
            model.chats[index] = mapped
        }
        return ChatSettings.parse(session)
    }

    func saveGroupSettings(_ id: String, fields: JSON) async throws {
        let json = try await client.updateGroup(id, fields)
        try checkCurrent()
        if let group = LiveMapping.group(json, previous: model.group(id), members: memberSessions),
           let index = model.groups.firstIndex(where: { $0.id == id }) {
            model.groups[index] = group
        }
    }

    private func checkCurrent() throws {
        guard isCurrent else { throw CancellationError() }
    }

    func createChat(backend: AgentBackend, model modelName: String?, cwd: String, permission: PermissionPreset?,
                    folder: String? = nil) async {
        guard isCurrent else { return }
        var body: [String: JSON] = ["backend": .string(backend.rawValue), "cwd": .string(LiveMapping.expand(cwd))]
        if let modelName { body["model"] = .string(modelName) }
        if let folder { body["folder"] = .string(folder) }
        for (key, value) in permission?.fields ?? [:] { body[key] = .string(value) }
        do {
            let session = try await client.createSession(.object(body))
            guard isCurrent else { return }
            if let chat = LiveMapping.chat(session, previous: nil) {
                model.chats.insert(ChatSummary(id: chat.id, title: chat.title, backend: chat.backend, model: chat.model,
                                               effort: chat.effort, permission: chat.permission, cwd: chat.cwd,
                                               preview: "还没有消息", lastActivity: .now, folder: chat.folder), at: 0)
                model.select(.chat(chat.id))
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: Group actions

    func send(_ text: String, toGroup id: String, mode: GroupMode, analysisOnly: Bool) async {
        guard isCurrent else { return }
        let pending = PendingSend(id: "mac-" + UUID().uuidString, text: text)
        model.pendingSends[id, default: []].append(pending)
        do {
            _ = try await client.postGroupMessage(id, text: text, mode: mode.rawValue, analysisOnly: analysisOnly)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.pendingSends[id]?.removeAll { $0.id == pending.id }
            model.drafts[id] = text
            model.report(error)
        }
    }

    func addMember(_ backend: AgentBackend, toGroup id: String) async {
        guard isCurrent else { return }
        do {
            let json = try await client.addParticipant(id, ["backend": .string(backend.rawValue)])
            guard isCurrent else { return }
            try? await refreshLists()
            guard isCurrent else { return }
            if let group = LiveMapping.group(json, previous: model.group(id), members: memberSessions),
               let index = model.groups.firstIndex(where: { $0.id == id }) {
                model.groups[index] = group
            }
            if let group = model.group(id) {
                await loadMemberSessions(of: group)
                guard isCurrent else { return }
                await loadMemberProjections(of: id)
                guard isCurrent else { return }
                if model.selection == .group(id) { subscribeMembers(of: id) }
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func removeMember(_ handle: String, fromGroup id: String) async {
        guard isCurrent else { return }
        do {
            try await client.removeParticipant(id, handle: handle)
            guard isCurrent else { return }
            try? await refreshLists()
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
            try? await refreshLists()
            guard isCurrent else { return }
        }
    }

    func createGroup(title: String, cwd: String, participants: [Participant]) async {
        guard isCurrent else { return }
        let specs: [JSON] = participants.map { member in
            var spec: [String: JSON] = ["backend": .string(member.backend.rawValue), "handle": .string(member.handle),
                                        "role": .string(member.role)]
            if member.backend == .codex {
                spec["codex_sandbox_mode"] = "workspace-write"
                spec["codex_approval_policy"] = "never"
            }
            return .object(spec)
        }
        var body: [String: JSON] = ["cwd": .string(LiveMapping.expand(cwd)), "participants": .array(specs),
                                    "settings": ["default_responder": .string(participants.first?.handle ?? "claude")]]
        if !title.isEmpty { body["title"] = .string(title) }
        do {
            let group = try await client.createGroup(.object(body))
            guard isCurrent else { return }
            if let mapped = LiveMapping.group(group, previous: nil) {
                model.groups.insert(mapped, at: 0)
                model.select(.group(mapped.id))
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    /// Reads each member chat's current settings (they decide how its turns run).
    private func loadMemberSessions(of group: GroupSummary) async {
        guard isCurrent else { return }
        for member in group.participants {
            guard let sessionID = member.sessionID,
                  let session = try? await client.sessionTail(sessionID, limit: 1)["session"], !session.isNull else { continue }
                  guard isCurrent else { return }
            memberSessions[sessionID] = session
        }
        applyMemberSessions(to: group.id)
    }

    private func applyMemberSessions(to groupID: String) {
        guard isCurrent else { return }
        guard let index = model.groups.firstIndex(where: { $0.id == groupID }) else { return }
        for member in model.groups[index].participants.indices {
            let participant = model.groups[index].participants[member]
            guard let session = participant.sessionID.flatMap({ memberSessions[$0] }) else { continue }
            var updated = participant
            updated.model = session["model"].text ?? ""
            updated.effort = session["effort"].text ?? ""
            updated.permission = PermissionPreset.label(for: session, backend: participant.backend)
            if updated != participant { model.groups[index].participants[member] = updated }
        }
    }

    func reply(_ approval: GroupApproval, _ reply: InteractionReply, inGroup groupID: String) async {
        guard isCurrent else { return }
        memberProjections[approval.sessionID]?.noteDecision(reply.outcome, for: approval.interactionID)
        publishGroup(groupID)
        do {
            try await client.resolveInteraction(approval.sessionID, backend: approval.backend.rawValue,
                                                interactionID: approval.interactionID, response: reply.response)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    /// Changes one member's model, effort or permissions. Applies from its next turn.
    func updateMember(_ handle: String, inGroup groupID: String, fields: JSON) async {
        guard isCurrent else { return }
        guard let sessionID = model.group(groupID)?.participants.first(where: { $0.handle == handle })?.sessionID else {
            model.errorMessage = "找不到 @\(handle) 的成员聊天"
            return
        }
        do {
            memberSessions[sessionID] = try await client.updateSession(sessionID, fields)
            guard isCurrent else { return }
            applyMemberSessions(to: groupID)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: Autopilot actions

    func runAction(_ id: String, _ action: String, feedback: String? = nil) async {
        guard isCurrent else { return }
        do {
            if let feedback {
                _ = try await client.runFeedback(id, text: feedback)
                guard isCurrent else { return }
            } else {
                _ = try await client.runAction(id, action)
                guard isCurrent else { return }
            }
            await refreshRun(id)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func createRun(goal: String, cwd: String, approval: ApprovalOption, isolation: IsolationOption,
                   reviewer: ReviewerOption, verifyCommand: String) async {
        guard isCurrent else { return }
        var body: [String: JSON] = [
            "goal": .string(goal), "cwd": .string(LiveMapping.expand(cwd)), "approval": .string(approval.rawValue),
            "isolation": .string(isolation == .inPlace ? "in_place" : "worktree"), "reviewer": .string(reviewer.rawValue),
        ]
        let command = verifyCommand.trimmingCharacters(in: .whitespaces)
        if !command.isEmpty { body["verify_command"] = .string(command) }
        do {
            let run = try await client.createRun(.object(body))
            guard isCurrent else { return }
            if let mapped = LiveMapping.run(run, previous: nil) {
                model.runs.insert(mapped, at: 0)
                model.select(.autopilot(mapped.id))
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: Jobs

    // MARK: Attachments

    func upload(_ url: URL, attachment: String, toChat id: String) async {
        guard isCurrent else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let file = try await client.uploadFile(id, fileURL: url)
            guard isCurrent else { return }
            guard let fileID = file["id"].string else { throw HyadexClientError.invalidResponse }
            model.setAttachment(attachment, in: id, .ready(fileID: fileID))
        } catch {
            guard isCurrent else { return }
            model.setAttachment(attachment, in: id, .failed(error.localizedDescription))
        }
    }

    // MARK: Rename, archive, delete

    func updateGroup(_ id: String, _ fields: JSON) async {
        guard isCurrent else { return }
        do {
            let json = try await client.updateGroup(id, fields)
            guard isCurrent else { return }
            if let group = LiveMapping.group(json, previous: model.group(id), members: memberSessions),
               let index = model.groups.firstIndex(where: { $0.id == id }) {
                model.groups[index] = group
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
            try? await refreshLists()
            guard isCurrent else { return }
        }
    }

    func delete(_ item: SidebarItem) async {
        guard isCurrent else { return }
        do {
            switch item {
            case .chat(let id):
                try await client.deleteSession(id)
                guard isCurrent else { return }
                chatProjections[id] = nil
                chatEvents[id] = nil
            case .group(let id):
                try await client.deleteGroup(id)
                guard isCurrent else { return }
                groupProjections[id] = nil
            default:
                return
            }
            model.removeLocally(item)
            try? await refreshLists()
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: Jobs

    func createJob(_ body: JSON, target: JobTarget) async {
        guard isCurrent else { return }
        var fields = body.object ?? [:]
        do {
            switch target {
            case .chat(let id):
                fields["session_id"] = .string(id)
                _ = try await client.createJob(.object(fields))
                guard isCurrent else { return }
            case .group(let id):
                _ = try await client.createJob(.object(fields), groupID: id)
                guard isCurrent else { return }
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        try? await refreshLists()
    }

    func updateJob(_ id: String, fields: JSON) async {
        guard isCurrent else { return }
        do {
            _ = try await client.updateJob(id, fields)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        try? await refreshLists()
    }

    func deleteJob(_ id: String, groupID: String?) async {
        guard isCurrent else { return }
        do {
            try await client.deleteJob(id, groupID: groupID)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        try? await refreshLists()
    }

    // MARK: Server folders

    func completeDirectory(_ path: String) async -> (folders: [DirectorySuggestion], message: String?) {
        guard isCurrent else { return ([], "服务器连接已改变。") }
        do {
            let json = try await client.completeDirectory(path)
            guard isCurrent else { return ([], "服务器连接已改变。") }
            let folders = json["suggestions"].array.compactMap { item -> DirectorySuggestion? in
                guard let path = item["path"].string else { return nil }
                return DirectorySuggestion(name: item["name"].text ?? path, path: path)
            }
            return (folders, json["message"].text)
        } catch {
            guard isCurrent else { return ([], "服务器连接已改变。") }
            return ([], error.localizedDescription)
        }
    }

    // MARK: Queue

    func runQueuedNow(_ queuedID: String, in id: String) async {
        guard isCurrent else { return }
        do {
            let result = try await client.runQueuedNow(id, queuedID: queuedID)
            guard isCurrent else { return }
            if result["deferred"].bool == true {
                model.errorMessage = result["message"].text ?? "代理还在启动或停止中，稍后再试。"
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshQueue(id)
    }

    func resumeQueue(_ id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.resumeQueue(id)
            guard isCurrent else { return }
        } catch HyadexClientError.http(404, _) where isCurrent && model.chat(id) != nil {
            guard isCurrent else { return }
            // An older server: Force Send the first held message instead.
            if let first = chatProjections[id]?.queued.first(where: { $0.hold == .stopped }) {
                await runQueuedNow(first.id, in: id)
                guard isCurrent else { return }
            }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshQueue(id)
    }

    func moveQueued(_ queuedID: String, up: Bool, expectedAdjacent: String, in id: String) async {
        guard isCurrent else { return }
        do {
            try await client.moveQueued(id, queuedID: queuedID, direction: up ? "up" : "down",
                                        expectedAdjacent: expectedAdjacent)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshQueue(id)
    }

    /// The server's queue order is authoritative after a queue action.
    private func refreshQueue(_ id: String) async {
        guard isCurrent else { return }
        guard let page = try? await client.session(id, after: chatProjections[id]?.lastSeq ?? 0, limit: 1) else { return }
        guard isCurrent else { return }
        chatProjections[id]?.setQueue(page["queued_turns"].array)
        publishChat(id)
    }

    // MARK: Emergency alerts

    private func startEmergencyStream() {
        guard isCurrent else { return }
        emergencySocket?.stop()
        let stream = EventSocket(client: client, path: "api/emergency-alerts/events", after: 0,
                                 protocolName: "hyadex-emergency-v1")
        emergencySocket = stream
        stream.start { [weak self] frame in
            guard case .control(let json) = frame else { return }
            await MainActor.run { self?.applyEmergency(json) }
        }
    }

    private func applyEmergency(_ frame: JSON) {
        guard isCurrent else { return }
        var alerts = model.emergencies
        switch frame["type"].string {
        case "emergency_snapshot":
            alerts = frame["sessions"].array.compactMap(EmergencyAlert.parse)
        case "emergency_changed":
            let session = frame["session"]
            alerts.removeAll { $0.sessionID == session["id"].string }
            if let alert = EmergencyAlert.parse(session) { alerts.append(alert) }
        case "emergency_removed":
            alerts.removeAll { $0.sessionID == frame["session_id"].string }
        default:
            return
        }
        let known = Set(model.emergencies.map(\.alertID))
        for alert in alerts where !known.contains(alert.alertID) { SystemNotifier.post(alert) }
        alerts.sort { ($0.raisedAt ?? .distantPast) > ($1.raisedAt ?? .distantPast) }
        if alerts != model.emergencies { model.emergencies = alerts }
    }

    func acknowledge(_ alert: EmergencyAlert) async {
        guard isCurrent else { return }
        do {
            try await client.acknowledgeEmergency(alert.sessionID, throughAlertID: alert.alertID)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: History and context

    /// Prepends the page before the oldest loaded event and rebuilds the timeline.
    func loadOlder(_ id: String) async {
        guard isCurrent else { return }
        defer { if isCurrent { model.loadingOlder.remove(id) } }
        guard let chat = model.chat(id), let oldest = chatEvents[id]?.first?["seq"].int else { return }
        do {
            let page = try await client.sessionPage(id, before: oldest, limit: AppModel.historyPageSize)
            guard isCurrent else { return }
            let older = page["events"].array.filter { ($0["seq"].int ?? .max) < oldest }
            let events = older + (chatEvents[id] ?? [])
            var projection = SessionProjection(backend: chat.backend, cwd: LiveMapping.expand(chat.cwd))
            projection.apply(events)
            if let current = chatProjections[id] { projection.replaceQueue(current.queued) }
            chatProjections[id] = projection
            chatEvents[id] = events
            if (page["events_omitted_before"].int ?? 0) == 0 || older.isEmpty { model.olderHistory.remove(id) }
            publishChat(id)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func compactCodex(_ id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.compactCodex(id)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    // MARK: Search

    func search(_ query: String) async -> [SearchHit] {
        guard isCurrent else { return [] }
        guard let json = try? await client.search(query) else { return [] }
        guard isCurrent else { return [] }
        return SearchHit.parse(json)
    }

    /// Opens the chat, loads older pages until the hit is in the timeline, then focuses it.
    func reveal(_ hit: SearchHit) async {
        guard isCurrent else { return }
        let id = hit.sessionID
        for _ in 0..<40 where chatEvents[id] == nil {
            try? await Task.sleep(for: .milliseconds(250))
            guard isCurrent else { return }
        }
        var pages = 0
        while let oldest = chatEvents[id]?.first?["seq"].int, oldest > hit.seq, model.olderHistory.contains(id),
              pages < 30, model.selection == .chat(id) {
            model.loadingOlder.insert(id)
            await loadOlder(id)
            guard isCurrent else { return }
            pages += 1
        }
        model.timelineFocus[id] = visibleItem(for: hit, in: id) + "|" + UUID().uuidString
    }

    /// The timeline item showing a hit. Some indexed events (a turn's final
    /// result) have no row of their own; use the nearest shown event before it.
    private func visibleItem(for hit: SearchHit, in id: String) -> String {
        guard isCurrent else { return hit.eventID }
        let shown = Set(model.timeline(id).map(\.id))
        if shown.contains(hit.eventID) { return hit.eventID }
        let earlier = (chatEvents[id] ?? []).filter { ($0["seq"].int ?? .max) <= hit.seq }
        for event in earlier.reversed() {
            if let eventID = event["id"].string, shown.contains(eventID) { return eventID }
        }
        return hit.eventID
    }

    // MARK: Fork, unread

    func fork(_ id: String) async {
        guard isCurrent else { return }
        do {
            let json = try await client.fork(id)
            guard isCurrent else { return }
            if let chat = LiveMapping.chat(json, previous: nil) {
                if !model.chats.contains(where: { $0.id == chat.id }) { model.chats.insert(chat, at: 0) }
                model.select(.chat(chat.id))
            }
            try? await refreshLists()
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func markUnread(_ id: String) async {
        guard isCurrent else { return }
        seenAt[id] = nil
        do { try await client.markUnread(id) } catch { if isCurrent { model.report(error) } }
    }

    /// Marks a chat read after the user saw it, keeping its dot off until the
    /// server's lists agree.
    private func markSeen(_ id: String) {
        seenAt[id] = .distantFuture
        if let index = model.chats.firstIndex(where: { $0.id == id }), model.chats[index].unread {
            model.chats[index].unread = false
        }
        Task {
            try? await client.markRead(id)
            guard isCurrent, seenAt[id] != nil else { return }
            seenAt[id] = Date()
        }
    }

    func markRead(_ id: String) async {
        guard isCurrent else { return }
        do { try await client.markRead(id) } catch { if isCurrent { model.report(error) } }
    }

    // MARK: Chat consults

    func refreshConsults(_ id: String) async {
        guard isCurrent else { return }
        guard let json = try? await client.consults(id) else { return }  // older servers have none
        guard isCurrent else { return }
        let consults = ChatConsults.parse(json)
        if model.consults[id] != consults { model.consults[id] = consults }
    }

    func decideConsult(_ consultID: String, decision: String, inChat id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.decideConsult(id, consultID: consultID, decision: decision)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshConsults(id)
    }

    func cancelConsult(_ consultID: String, inChat id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.cancelConsult(id, consultID: consultID)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshConsults(id)
    }

    func loadConsultTargets(_ id: String) async {
        guard isCurrent else { return }
        guard let json = try? await client.consultTargets(id) else { return }  // older servers have none
        guard isCurrent else { return }
        let targets = json["chats"].array.compactMap(ConsultTarget.parse)
        if model.consultTargets[id] != targets { model.consultTargets[id] = targets }
    }

    func askFromChat(_ target: ConsultTarget, question: String, deep: Bool, inChat id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.askFromChat(id, chat: target.id, question: question, deep: deep)
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
        await refreshConsults(id)
    }

    func askChat(_ question: String, deep: Bool, inChat id: String) async {
        guard isCurrent else { return }
        do {
            _ = try await client.askChat(id, question: question, deep: deep)
        } catch {
            guard isCurrent else { return }
            model.sideChatErrors[id] = Self.sideMessage(error)
        }
        await refreshConsults(id)
    }

    // MARK: Side chat

    func refreshSide(_ id: String) async {
        guard isCurrent else { return }
        do {
            let json = try await client.sideChat(id)
            guard isCurrent else { return }
            if let side = SideChat.parse(json), model.sideChats[id] != side { model.sideChats[id] = side }
            model.sideChatErrors[id] = nil
        } catch {
            guard isCurrent else { return }
            model.sideChatErrors[id] = Self.sideMessage(error)
        }
        // Frames can be missed across reconnects: keep checking while an answer is pending.
        if model.sideChats[id]?.isRunning == true, model.sideChatOpen == id {
            try? await Task.sleep(for: .seconds(2))
            guard isCurrent else { return }
            if model.sideChats[id]?.isRunning == true { await refreshSide(id) }
            guard isCurrent else { return }
        }
    }

    func askSide(_ question: String, inChat id: String) async {
        guard isCurrent else { return }
        if model.sideChats[id] == nil { await refreshSide(id) }
        guard isCurrent else { return }
        guard let side = model.sideChats[id] else { return }
        do {
            let json = try await client.askSideChat(id, requestID: "mac" + UUID().uuidString.replacingOccurrences(of: "-", with: ""),
                                                    question: question, sideChatID: side.sideChatID,
                                                    after: side.lastRequestID)
            guard isCurrent else { return }
            if let updated = SideChat.parse(json) { model.sideChats[id] = updated }
        } catch HyadexClientError.http(409, let detail?) where detail.contains("refresh") {
            guard isCurrent else { return }
            // Another device asked meanwhile: catch up and let the user ask again.
            await refreshSide(id)
            guard isCurrent else { return }
            model.sideChatErrors[id] = "侧问刚被更新，请再发一次。"
        } catch {
            guard isCurrent else { return }
            model.sideChatErrors[id] = Self.sideMessage(error)
        }
        await refreshSide(id)
    }

    func cancelSide(_ requestID: String, inChat id: String) async {
        guard isCurrent else { return }
        do {
            if let side = SideChat.parse(try await client.cancelSideQuestion(id, requestID: requestID)) {
                guard isCurrent else { return }
                model.sideChats[id] = side
            }
        } catch {
            guard isCurrent else { return }
            model.sideChatErrors[id] = Self.sideMessage(error)
        }
    }

    func clearSide(_ sideChatID: String, inChat id: String) async {
        guard isCurrent else { return }
        do {
            if let side = SideChat.parse(try await client.clearSideChat(id, sideChatID: sideChatID)) {
                guard isCurrent else { return }
                model.sideChats[id] = side
            }
        } catch {
            guard isCurrent else { return }
            model.sideChatErrors[id] = Self.sideMessage(error)
        }
    }

    private static func sideMessage(_ error: Error) -> String {
        if case HyadexClientError.http(409, let detail?) = error, detail.contains("not started") {
            return "先和代理说一句话，侧问才有对话内容可以参考。"
        }
        if case HyadexClientError.http(410, _) = error {
            return "主对话已经变了：点“清空”开始新的侧问。"
        }
        return error.localizedDescription
    }

    // MARK: Subagents

    func refreshSubagents(_ id: String) async {
        guard isCurrent else { return }
        guard let json = try? await client.subagents(id) else { return }
        guard isCurrent else { return }
        let agents = Subagent.parse(json)
        if model.subagents[id] != agents { model.subagents[id] = agents }
    }

    func setJob(_ id: String, enabled: Bool) async {
        guard isCurrent else { return }
        do {
            _ = try await client.updateJob(id, ["enabled": .bool(enabled)])
            guard isCurrent else { return }
            try? await refreshLists()
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }

    func runJob(_ id: String) async {
        guard isCurrent else { return }
        do {
            try await client.runJob(id)
            guard isCurrent else { return }
        } catch {
            guard isCurrent else { return }
            model.report(error)
        }
    }
}
