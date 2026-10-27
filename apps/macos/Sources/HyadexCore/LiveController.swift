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

    private var chatProjections: [String: SessionProjection] = [:]
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
    private var pollTask: Task<Void, Never>?
    private var runPollTask: Task<Void, Never>?
    private var usageTask: Task<Void, Never>?
    /// Chats whose turn was running at the last publish (to notice a turn end).
    private var runningChats: Set<String> = []
    private var loadingDiffs: Set<String> = []

    /// Opt in to native approvals and questions for both providers.
    static let capabilities = ["claude_sdk_interactive_v1", "codex_interactive_v1"]

    init(model: AppModel, client: HyadexClient) {
        self.model = model
        self.client = client
    }

    func start() async throws {
        health = try await client.health()
        try await refreshLists()
        // Probing the native CLIs can take a few seconds; do not hold up the lists.
        Task { await loadCatalogs() }
        // Plan usage changes slowly; native events also prompt a reread.
        usageTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshUsage()
                try? await Task.sleep(for: .seconds(90))
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(4))
                guard let self else { return }
                do {
                    try await self.refreshLists()
                    if case .offline = self.model.connection { self.model.connection = self.connectedState }
                } catch {
                    self.model.connection = .offline(error.localizedDescription)
                }
            }
        }
    }

    func stop() {
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
        guard let catalog = try? await client.runtimeCatalog(refresh: refresh) else { return }
        var result: [AgentBackend: RuntimeCatalog] = [:]
        for backend in AgentBackend.allCases {
            if let parsed = RuntimeCatalog.parse(catalog["backends"][backend.rawValue]) { result[backend] = parsed }
        }
        if !result.isEmpty { model.catalogs = result }
    }

    // MARK: Usage

    /// Plan usage of both provider accounts.
    func refreshUsage(refresh: Bool = false, only: AgentBackend? = nil) async {
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
                // Keep the last reading when the server cannot be reached.
                if let usage, model.usage[backend] != usage { model.usage[backend] = usage }
            }
        }
    }

    /// The chat's context-window fill, as its provider last reported it.
    func refreshContext(_ id: String) async {
        guard let chat = model.chat(id) else { return }
        await refreshContext(id, backend: chat.backend)
    }

    func refreshContext(_ id: String, backend: AgentBackend) async {
        guard let runtime = try? await client.runtime(id, backend: backend.rawValue) else { return }
        applyContext(runtime, to: id)
    }

    private func applyContext(_ json: JSON, to id: String) {
        let snapshot = json["context_usage"].isNull ? json["context_usage_snapshot"] : json["context_usage"]
        if let usage = ContextUsage.parse(snapshot) {
            if model.contextUsage[id] != usage { model.contextUsage[id] = usage }
        } else if json["context_usage_state"].string.map({ $0 != "available" }) ?? false {
            model.contextUsage[id] = nil
        }
    }

    // MARK: Lists

    func refreshLists() async throws {
        async let sessionList = client.sessions()
        async let groupList = client.groups()
        async let runList = client.runs()
        async let jobList = client.jobs()
        let (sessions, groups, runs, jobs) = try await (sessionList, groupList, runList, jobList)

        let previousChats = Dictionary(model.chats.map { ($0.id, $0) }, uniquingKeysWith: { $1 })
        var chats = sessions.compactMap { LiveMapping.chat($0, previous: previousChats[$0["id"].string ?? ""]) }
        for index in chats.indices {
            if let projection = chatProjections[chats[index].id] {
                chats[index].state = projection.needsAction ? .needsAction : projection.running ? .running : .idle
                if let text = projection.lastAssistantText { chats[index].preview = Self.preview(text) }
            }
            if model.selection == .chat(chats[index].id) { chats[index].unread = false }
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
    }

    /// The list has no message text, so fetch a short tail for chats without a preview.
    private func loadMissingPreviews() async {
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
        runPollTask?.cancel()
        let key: String? = switch item {
        case .chat(let id), .group(let id): id
        case .autopilot(let id): runGroups[id]
        default: nil
        }
        if socket?.key != key {
            socket?.socket.stop()
            socket = nil
            for stream in memberSockets.values { stream.stop() }
            memberSockets = [:]
        }
        switch item {
        case .chat(let id): Task { await openChat(id) }
        case .group(let id): Task { await openGroup(id) }
        case .autopilot(let id): Task { await openRun(id) }
        default: break
        }
    }

    private func openChat(_ id: String) async {
        guard let chat = model.chat(id) else { return }
        if chatProjections[id] == nil {
            do {
                let page = try await client.sessionTail(id, limit: 400)
                var projection = SessionProjection(backend: chat.backend, cwd: LiveMapping.expand(chat.cwd))
                projection.apply(page["events"].array)
                projection.setQueue(page["queued_turns"].array)
                chatProjections[id] = projection
            } catch {
                model.report(error)
                return
            }
        }
        publishChat(id)
        Task { await refreshContext(id) }
        // The user may have moved on while the history loaded; only the
        // conversation that is still selected gets the live stream.
        guard model.selection == .chat(id) else { return }
        try? await client.markRead(id)
        guard model.selection == .chat(id) else { return }
        // The reasoning lane carries the provider's live reasoning summary (not durable).
        subscribe(key: id, path: "api/sessions/\(id)/events", after: chatProjections[id]?.lastSeq ?? 0,
                  query: ["reasoning_stream": "true", "reasoning_text": "true"]) { controller, event in
            controller.chatProjections[id]?.apply(event)
            controller.publishChat(id)
        } control: { controller, frame in
            switch frame["type"].string {
            case "reasoning_summary_stream":
                let text = frame["items"].array.reversed().compactMap { $0["text"].text }.first
                controller.model.liveReasoning[id] = text.map { String($0.suffix(400)) }
            case "provider_runtime_changed" where frame["runtime"].string == "context_usage":
                controller.applyContext(frame, to: id)
            case "provider_usage_changed":
                let backend = frame["backend"].string.flatMap(AgentBackend.named)
                Task { await controller.refreshUsage(only: backend) }
            default:
                break
            }
        }
    }

    private func openGroup(_ id: String) async {
        guard let group = model.group(id) else { return }
        await loadMemberSessions(of: group)
        await loadGroupEvents(id, backends: Dictionary(group.participants.map { ($0.handle, $0.backend) },
                                                       uniquingKeysWith: { $1 }))
        await loadMemberProjections(of: id)
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
        guard let group = model.group(groupID) else { return }
        for member in group.participants {
            guard let sessionID = member.sessionID, memberProjections[sessionID] == nil,
                  let page = try? await client.sessionTail(sessionID, limit: 400) else { continue }
            var projection = SessionProjection(backend: member.backend, cwd: LiveMapping.expand(group.cwd))
            projection.apply(page["events"].array)
            memberProjections[sessionID] = projection
            Task { await self.refreshContext(sessionID, backend: member.backend) }
        }
    }

    private func subscribeMembers(of groupID: String) {
        guard let group = model.group(groupID) else { return }
        for member in group.participants {
            guard let sessionID = member.sessionID, memberSockets[sessionID] == nil else { continue }
            let stream = EventSocket(client: client, path: "api/sessions/\(sessionID)/events",
                                     after: memberProjections[sessionID]?.lastSeq ?? 0,
                                     query: ["reasoning_stream": "true", "reasoning_text": "true"])
            memberSockets[sessionID] = stream
            let backend = member.backend
            stream.start { [weak self] frame in
                await MainActor.run {
                    guard let self, self.memberSockets[sessionID] === stream else { return }
                    switch frame {
                    case .event(let event):
                        let wasRunning = self.memberProjections[sessionID]?.running ?? false
                        self.memberProjections[sessionID]?.apply(event)
                        if wasRunning, self.memberProjections[sessionID]?.running == false {
                            self.memberReasoning[sessionID] = nil
                            Task { await self.refreshContext(sessionID, backend: backend) }
                        }
                    case .control(let json):
                        switch json["type"].string {
                        case "reasoning_summary_stream":
                            let text = json["items"].array.reversed().compactMap { $0["text"].text }.first
                            self.memberReasoning[sessionID] = text.map { String($0.suffix(400)) }
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
        await refreshRun(id)
        guard model.selection == .autopilot(id) else { return }
        runPollTask?.cancel()
        runPollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2.5))
                guard let self, !Task.isCancelled else { return }
                await self.refreshRun(id)
            }
        }
    }

    private func refreshRun(_ id: String) async {
        guard let json = try? await client.run(id) else { return }
        if let group = json["group_id"].string {
            runGroups[id] = group
            await loadGroupEvents(group, backends: ["planner": .claude, "executor": .codex, "reviewer": .claude])
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
        var projection = groupProjections[id] ?? GroupProjection(backends: backends)
        for _ in 0..<20 {
            guard let page = try? await client.groupEvents(id, after: projection.lastSeq, limit: 500), !page.isEmpty else { break }
            projection.apply(page)
            if page.count < 500 { break }
        }
        groupProjections[id] = projection
    }

    private func subscribe(key: String, path: String, after: Int, query: [String: String] = [:],
                           apply: @escaping @MainActor (LiveController, JSON) -> Void,
                           control: @escaping @MainActor (LiveController, JSON) -> Void = { _, _ in }) {
        guard socket?.key != key else { return }
        socket?.socket.stop()
        let stream = EventSocket(client: client, path: path, after: after, query: query)
        socket = (key, stream)
        stream.start { [weak self] frame in
            await MainActor.run {
                guard let self, self.socket?.key == key else { return }
                switch frame {
                case .event(let event): apply(self, event)
                case .control(let json): control(self, json)
                }
            }
        }
    }

    private func publishChat(_ id: String) {
        guard let projection = chatProjections[id] else { return }
        model.timelines[id] = projection.items
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
                await refreshUsage()
            }
        }
        if let index = model.chats.firstIndex(where: { $0.id == id }) {
            let state: ActivityState = projection.needsAction ? .needsAction : projection.running ? .running : .idle
            if model.chats[index].state != state { model.chats[index].state = state }
            if let text = projection.lastAssistantText { model.chats[index].preview = Self.preview(text) }
            if let time = projection.lastActivity, time > model.chats[index].lastActivity {
                model.chats[index].lastActivity = time
            }
        }
        for (itemID, runID) in projection.diffsToLoad where !loadingDiffs.contains(itemID) {
            loadingDiffs.insert(itemID)
            Task {
                if let diff = try? await client.diff(id, runID: runID) {
                    chatProjections[id]?.setDiff(diff, forItem: itemID)
                    publishChat(id)
                }
                loadingDiffs.remove(itemID)
            }
        }
    }

    private func publishGroup(_ id: String) {
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
                guard case .approval(let interaction, _, let title, let detail, let reason, _, let allowsSession) = item,
                      let sessionID else { return nil }
                return GroupApproval(interactionID: interaction, handle: member.handle, backend: member.backend,
                                     sessionID: sessionID, title: title, detail: detail, reason: reason,
                                     allowsSession: allowsSession)
            }
            let since = turn?.startedAt ?? dispatch.time
            var activity: LiveActivity?
            if approvals.isEmpty {
                if turn == nil {
                    activity = LiveActivity(kind: .starting, label: "正在启动 \(member.backend.displayName)", since: since)
                } else if case .tool(_, let kind, let title, _, .running, _)? = turn?.steps.last {
                    activity = LiveActivity(kind: .tool, label: kind.activeVerb, detail: title, since: since)
                } else {
                    let reasoning = sessionID.flatMap { memberReasoning[$0] }
                    activity = LiveActivity(kind: .thinking, label: "正在思考", detail: reasoning, since: since)
                }
            }
            live.append(MemberTurn(id: dispatch.id, author: .agent(member.handle, member.backend),
                                   steps: turn?.steps ?? [], approvals: approvals, activity: activity))
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
        for (sessionID, projection) in memberProjections {
            for (itemID, runID) in projection.diffsToLoad where !loadingDiffs.contains(itemID) {
                loadingDiffs.insert(itemID)
                Task {
                    if let diff = try? await client.diff(sessionID, runID: runID) {
                        memberProjections[sessionID]?.setDiff(diff, forItem: itemID)
                        publishGroup(groupID)
                    }
                    loadingDiffs.remove(itemID)
                }
            }
        }
    }

    // MARK: Chat actions

    func send(_ text: String, toChat id: String) async {
        // Show the message and a "starting" status at once; the server's
        // turn_started (matched by client_request_id) replaces it.
        let requestID = "mac-" + UUID().uuidString
        let isBusy = model.chat(id).map { $0.state == .running || $0.state == .needsAction } ?? false
        if !isBusy { model.pendingSends[id, default: []].append(PendingSend(id: requestID, text: text)) }
        do {
            _ = try await client.sendTurn(id, prompt: text, requestID: requestID, capabilities: Self.capabilities)
            if let index = model.chats.firstIndex(where: { $0.id == id }) {
                model.chats[index].preview = Self.preview(text)
                model.chats[index].lastActivity = .now
            }
        } catch {
            model.pendingSends[id]?.removeAll { $0.id == requestID }
            model.drafts[id] = text
            model.report(error)
        }
    }

    func stop(_ id: String) async {
        do {
            if model.group(id) != nil {
                try await client.stopGroup(id)
            } else {
                try await client.stop(id)
            }
        } catch {
            model.report(error)
        }
    }

    func decide(_ interactionID: String, _ decision: ApprovalDecision, in id: String) async {
        guard let chat = model.chat(id) else { return }
        let value = switch decision {
        case .allowed: "accept"
        case .allowedForSession: "acceptForSession"
        default: "decline"
        }
        chatProjections[id]?.noteDecision(decision, for: interactionID)
        publishChat(id)
        do {
            try await client.resolveInteraction(id, backend: chat.backend.rawValue, interactionID: interactionID, decision: value)
        } catch {
            model.report(error)
        }
    }

    func withdrawQueued(_ queuedID: String, in id: String) async {
        do {
            try await client.deleteQueued(id, queuedID: queuedID)
            model.queues[id]?.removeAll { $0.id == queuedID }
        } catch {
            model.report(error)
        }
    }

    func updateChat(_ id: String, fields: JSON) async {
        do {
            let session = try await client.updateSession(id, fields)
            if let index = model.chats.firstIndex(where: { $0.id == id }),
               let mapped = LiveMapping.chat(session, previous: model.chats[index]) {
                model.chats[index] = mapped
            }
        } catch {
            model.report(error)
        }
    }

    func createChat(backend: AgentBackend, model modelName: String?, cwd: String, permission: PermissionPreset?) async {
        var body: [String: JSON] = ["backend": .string(backend.rawValue), "cwd": .string(LiveMapping.expand(cwd))]
        if let modelName { body["model"] = .string(modelName) }
        for (key, value) in permission?.fields ?? [:] { body[key] = .string(value) }
        do {
            let session = try await client.createSession(.object(body))
            if let chat = LiveMapping.chat(session, previous: nil) {
                model.chats.insert(ChatSummary(id: chat.id, title: chat.title, backend: chat.backend, model: chat.model,
                                               effort: chat.effort, permission: chat.permission, cwd: chat.cwd,
                                               preview: "还没有消息", lastActivity: .now), at: 0)
                model.select(.chat(chat.id))
            }
        } catch {
            model.report(error)
        }
    }

    // MARK: Group actions

    func send(_ text: String, toGroup id: String, mode: GroupMode, analysisOnly: Bool) async {
        let pending = PendingSend(id: "mac-" + UUID().uuidString, text: text)
        model.pendingSends[id, default: []].append(pending)
        do {
            _ = try await client.postGroupMessage(id, text: text, mode: mode.rawValue, analysisOnly: analysisOnly)
        } catch {
            model.pendingSends[id]?.removeAll { $0.id == pending.id }
            model.drafts[id] = text
            model.report(error)
        }
    }

    func addMember(_ backend: AgentBackend, toGroup id: String) async {
        do {
            let json = try await client.addParticipant(id, ["backend": .string(backend.rawValue)])
            try? await refreshLists()
            if let group = LiveMapping.group(json, previous: model.group(id), members: memberSessions),
               let index = model.groups.firstIndex(where: { $0.id == id }) {
                model.groups[index] = group
            }
            if let group = model.group(id) {
                await loadMemberSessions(of: group)
                await loadMemberProjections(of: id)
                if model.selection == .group(id) { subscribeMembers(of: id) }
            }
        } catch {
            model.report(error)
        }
    }

    func removeMember(_ handle: String, fromGroup id: String) async {
        do {
            try await client.removeParticipant(id, handle: handle)
            try? await refreshLists()
        } catch {
            model.report(error)
            try? await refreshLists()
        }
    }

    func createGroup(title: String, cwd: String, participants: [Participant]) async {
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
            if let mapped = LiveMapping.group(group, previous: nil) {
                model.groups.insert(mapped, at: 0)
                model.select(.group(mapped.id))
            }
        } catch {
            model.report(error)
        }
    }

    /// Reads each member chat's current settings (they decide how its turns run).
    private func loadMemberSessions(of group: GroupSummary) async {
        for member in group.participants {
            guard let sessionID = member.sessionID,
                  let session = try? await client.sessionTail(sessionID, limit: 1)["session"], !session.isNull else { continue }
            memberSessions[sessionID] = session
        }
        applyMemberSessions(to: group.id)
    }

    private func applyMemberSessions(to groupID: String) {
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

    func decide(_ approval: GroupApproval, _ decision: ApprovalDecision, inGroup groupID: String) async {
        let value = switch decision {
        case .allowed: "accept"
        case .allowedForSession: "acceptForSession"
        default: "decline"
        }
        memberProjections[approval.sessionID]?.noteDecision(decision, for: approval.interactionID)
        publishGroup(groupID)
        do {
            try await client.resolveInteraction(approval.sessionID, backend: approval.backend.rawValue,
                                                interactionID: approval.interactionID, decision: value)
        } catch {
            model.report(error)
        }
    }

    /// Changes one member's model, effort or permissions. Applies from its next turn.
    func updateMember(_ handle: String, inGroup groupID: String, fields: JSON) async {
        guard let sessionID = model.group(groupID)?.participants.first(where: { $0.handle == handle })?.sessionID else {
            model.errorMessage = "找不到 @\(handle) 的成员聊天"
            return
        }
        do {
            memberSessions[sessionID] = try await client.updateSession(sessionID, fields)
            applyMemberSessions(to: groupID)
        } catch {
            model.report(error)
        }
    }

    // MARK: Autopilot actions

    func runAction(_ id: String, _ action: String, feedback: String? = nil) async {
        do {
            if let feedback {
                _ = try await client.runFeedback(id, text: feedback)
            } else {
                _ = try await client.runAction(id, action)
            }
            await refreshRun(id)
        } catch {
            model.report(error)
        }
    }

    func createRun(goal: String, cwd: String, approval: ApprovalOption, isolation: IsolationOption,
                   reviewer: ReviewerOption, verifyCommand: String) async {
        var body: [String: JSON] = [
            "goal": .string(goal), "cwd": .string(LiveMapping.expand(cwd)), "approval": .string(approval.rawValue),
            "isolation": .string(isolation == .inPlace ? "in_place" : "worktree"), "reviewer": .string(reviewer.rawValue),
        ]
        let command = verifyCommand.trimmingCharacters(in: .whitespaces)
        if !command.isEmpty { body["verify_command"] = .string(command) }
        do {
            let run = try await client.createRun(.object(body))
            if let mapped = LiveMapping.run(run, previous: nil) {
                model.runs.insert(mapped, at: 0)
                model.select(.autopilot(mapped.id))
            }
        } catch {
            model.report(error)
        }
    }

    // MARK: Jobs

    func setJob(_ id: String, enabled: Bool) async {
        do {
            _ = try await client.updateJob(id, ["enabled": .bool(enabled)])
            try? await refreshLists()
        } catch {
            model.report(error)
        }
    }

    func runJob(_ id: String) async {
        do {
            try await client.runJob(id)
        } catch {
            model.report(error)
        }
    }
}
