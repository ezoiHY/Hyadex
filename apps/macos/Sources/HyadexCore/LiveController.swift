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
    private var socket: (key: String, socket: EventSocket)?
    private var pollTask: Task<Void, Never>?
    private var runPollTask: Task<Void, Never>?
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
        socket?.socket.stop()
        socket = nil
    }

    var connectedState: ConnectionState { .connected(version: health?.version ?? "?") }

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
        let mappedGroups = groups.compactMap { LiveMapping.group($0, previous: previousGroups[$0["id"].string ?? ""]) }
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

    static func preview(_ text: String) -> String {
        String(text.replacingOccurrences(of: "\n", with: " ").prefix(120))
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
        // The user may have moved on while the history loaded; only the
        // conversation that is still selected gets the live stream.
        guard model.selection == .chat(id) else { return }
        try? await client.markRead(id)
        guard model.selection == .chat(id) else { return }
        subscribe(key: id, path: "api/sessions/\(id)/events", after: chatProjections[id]?.lastSeq ?? 0) { controller, event in
            controller.chatProjections[id]?.apply(event)
            controller.publishChat(id)
        }
    }

    private func openGroup(_ id: String) async {
        guard let group = model.group(id) else { return }
        await loadGroupEvents(id, backends: Dictionary(group.participants.map { ($0.handle, $0.backend) },
                                                       uniquingKeysWith: { $1 }))
        publishGroup(id)
        guard model.selection == .group(id) else { return }
        subscribe(key: id, path: "api/groups/\(id)/events/stream", after: groupProjections[id]?.lastSeq ?? 0) { controller, event in
            controller.groupProjections[id]?.apply(event)
            controller.publishGroup(id)
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

    private func subscribe(key: String, path: String, after: Int,
                           apply: @escaping @MainActor (LiveController, JSON) -> Void) {
        guard socket?.key != key else { return }
        socket?.socket.stop()
        let stream = EventSocket(client: client, path: path, after: after)
        socket = (key, stream)
        stream.start { [weak self] frame in
            guard case .event(let event) = frame else { return }
            await MainActor.run {
                guard let self, self.socket?.key == key else { return }
                apply(self, event)
            }
        }
    }

    private func publishChat(_ id: String) {
        guard let projection = chatProjections[id] else { return }
        model.timelines[id] = projection.items
        model.queues[id] = projection.queued
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
        model.timelines[id] = projection.items
        guard let index = model.groups.firstIndex(where: { $0.id == id }) else { return }
        for (handle, state) in projection.memberStates {
            if let member = model.groups[index].participants.firstIndex(where: { $0.handle == handle }),
               model.groups[index].participants[member].state != state {
                model.groups[index].participants[member].state = state
            }
        }
        if case .message(_, _, let text, _, _, _, _)? = projection.items.last(where: {
            if case .message = $0 { return true } else { return false }
        }) {
            model.groups[index].preview = Self.preview(text)
        }
    }

    // MARK: Chat actions

    func send(_ text: String, toChat id: String) async {
        do {
            _ = try await client.sendTurn(id, prompt: text, requestID: "mac-" + UUID().uuidString,
                                          capabilities: Self.capabilities)
            if let index = model.chats.firstIndex(where: { $0.id == id }) {
                model.chats[index].preview = Self.preview(text)
                model.chats[index].lastActivity = .now
            }
        } catch {
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
        do {
            _ = try await client.postGroupMessage(id, text: text, mode: mode.rawValue, analysisOnly: analysisOnly)
        } catch {
            model.drafts[id] = text
            model.report(error)
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
