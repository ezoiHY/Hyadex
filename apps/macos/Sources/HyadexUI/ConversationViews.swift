import HyadexCore
import SwiftUI

extension AppModel {
    func draftBinding(_ key: String) -> Binding<String> {
        Binding(get: { self.drafts[key] ?? "" }, set: { self.drafts[key] = $0 })
    }
}

// MARK: - Chat

struct ChatDetailView: View {
    @Bindable var model: AppModel
    let chatID: String

    var body: some View {
        if let index = model.chats.firstIndex(where: { $0.id == chatID }) {
            let chat = model.chats[index]
            VStack(spacing: 0) {
                TimelineView(items: model.timeline(chatID),
                             showsAuthors: false,
                             older: model.olderHistory.contains(chatID)
                                 ? (loading: model.loadingOlder.contains(chatID), load: { model.loadOlder(chatID) }) : nil,
                             pending: model.pendingSends[chatID] ?? [],
                             activity: model.activity(forChat: chatID),
                             turnSeconds: model.turnSeconds[chatID] ?? [:],
                             onOpenFile: model.openFile,
                             onReply: { model.reply($0, $1, in: chatID) },
                             loadMedia: { [model] file in await model.media(chat: chatID, fileID: file.id) },
                             processSeconds: model.processSeconds[chatID] ?? [:],
                             focusID: model.timelineFocus[chatID])
                    // Files dropped anywhere on the conversation are attached to the next message.
                    .dropDestination(for: URL.self) { urls, _ in
                        let files = urls.filter(\.isFileURL)
                        model.attach(files, toChat: chatID)
                        return !files.isEmpty
                    }
                if let goal = model.goals[chatID], goal.status != .cleared {
                    GoalBar(goal: goal, busy: model.goalBusy.contains(chatID),
                            onEdit: { model.goalEditor = .chat(chatID) },
                            onPause: { model.pauseGoal(forChat: chatID) },
                            onResume: { model.resumeGoal(forChat: chatID) },
                            onClear: { model.clearGoal(forChat: chatID) })
                        .padding(.top, 6)
                }
                ComposerView(draft: model.draftBinding(chatID),
                             placeholder: "给 \(chat.backend.displayName) 发消息",
                             isRunning: chat.state == .running || chat.state == .needsAction,
                             queued: model.queues[chatID] ?? [],
                             slashSuggestions: { model.slashSuggestions(forChat: chatID, partial: $0) },
                             slashNote: model.slashNote(forChat: chatID),
                             onBeginSlash: { model.loadCommandsIfNeeded(forChat: chatID) },
                             attachments: model.attachments[chatID] ?? [],
                             onAttach: { model.attach($0, toChat: chatID) },
                             onRemoveAttachment: { model.removeAttachment($0, fromChat: chatID) },
                             onRunQueuedNow: { model.runQueuedNow($0, in: chatID) },
                             onMoveQueued: { model.moveQueued($0, up: $1, in: chatID) },
                             onResumeQueue: { model.resumeQueue(in: chatID) },
                             onSend: { model.send($0, toChat: chatID) },
                             onStop: { model.stop(chatID) },
                             onEditQueued: { model.editQueued($0, in: chatID) },
                             onWithdrawQueued: { model.withdrawQueued($0, in: chatID) },
                             menuItems: { ChatMenuItems(model: model, chat: chat) },
                             leading: { PermissionMenu(model: model, chat: chat) },
                             trailing: { ChatControls(model: model, chat: chat) },
                             header: AnyView(WorkingDirectoryMenu(model: model, path: chat.cwd, chatID: chat.id)),
                             chatMentions: model.consultTargets[chatID] ?? [],
                             onBeginChatMention: { model.loadConsultTargets(chatID) },
                             onAskChat: { target, question, deep in
                                 model.askFromChat(target, question: question, deep: deep, inChat: chatID)
                             },
                             agentName: chat.backend.displayName)
                if model.terminalOpen.contains(chatID) {
                    Divider()
                    TerminalDock(model: model, chatID: chatID)
                }
            }
            .environment(\.messageActions, model.messageActions(chat: chatID))
            .environment(\.consultActions, model.consultActions(chat: chatID))
            .navigationTitle(chat.title)
            .toolbar { ConversationMark(backends: [chat.backend], help: chat.backend.displayName) }
        }
    }
}

/// Who the conversation is with, left of its title: the provider's mark, or
/// the members' marks in a group. On macOS 26 it sits on the bar itself, not
/// in a glass capsule like the buttons.
struct ConversationMark: ToolbarContent {
    let backends: [AgentBackend]
    let help: String

    var body: some ToolbarContent {
        if #available(macOS 26.0, *) {
            ToolbarItem(placement: .navigation) { mark }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .navigation) { mark }
        }
    }

    private var mark: some View {
        Group {
            if backends.count == 1, let backend = backends.first {
                ProviderMark(backend: backend, size: 18)
            } else {
                AvatarStack(backends: backends, size: 18)
            }
        }
        .padding(.horizontal, 2)
        .help(help)
        .accessibilityElement()
        .accessibilityLabel(help)
    }
}

/// The agent's working directory, above the composer: reveal, copy, or open a terminal there.
struct WorkingDirectoryMenu: View {
    let model: AppModel
    let path: String
    /// The chat whose terminal opens there (nil in groups).
    var chatID: String? = nil

    var body: some View {
        Menu {
            if model.serverIsLocal {
                Button("在 Finder 中显示", systemImage: "folder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: (path as NSString).expandingTildeInPath)])
                }
            }
            Button("拷贝路径", systemImage: "doc.on.doc") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(path, forType: .string)
            }
            if let chatID {
                Button(model.terminalOpen.contains(chatID) ? "隐藏终端" : "在终端中打开", systemImage: "terminal") {
                    model.toggleTerminal(chatID)
                }
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "folder")
                Text(Self.short(path))
                    .lineLimit(1)
            }
            .font(.uiMeta)
            .foregroundStyle(.secondary)
        }
        .menuStyle(.button)
        .buttonStyle(.borderless)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("工作目录：\(path)")
        .accessibilityLabel("工作目录 \(path)")
    }

    /// A long path keeps its last folders: "…/scratchpad/live-work".
    static func short(_ path: String, limit: Int = 48) -> String {
        guard path.count > limit else { return path }
        var kept: [Substring] = []
        for part in path.split(separator: "/").reversed() {
            let candidate = ([part] + kept).joined(separator: "/")
            if !kept.isEmpty, candidate.count + 2 > limit { break }
            kept.insert(part, at: 0)
        }
        let tail = kept.joined(separator: "/")
        return "…/" + (tail.count > limit ? String(tail.suffix(limit)) : tail)
    }
}

/// The chat's model and effort, as quiet text with the provider's own names,
/// and how full its context is. Changes apply to the chat's next turn.
private struct ChatControls: View {
    let model: AppModel
    let chat: ChatSummary

    var body: some View {
        let catalog = model.catalog(for: chat.backend)
        if let usage = model.contextUsage[chat.id] {
            ContextMeter(usage: usage, compact: model.canCompact(chat.id) ? { model.compact(chat.id) } : nil)
                .onAppear { if chat.backend == .claude { model.loadCommandsIfNeeded(forChat: chat.id) } }
        }
        let modelName = catalog.modelName(chat.model)
        Menu {
            Picker("模型", selection: Binding(get: { chat.model }, set: { model.setModel($0, forChat: chat.id) })) {
                ForEach(catalog.modelOptions(current: chat.model)) { option in
                    Text(option.label).tag(option.value)
                }
            }
            .pickerStyle(.inline)
        } label: {
            Text(modelName).font(.uiSecondary)
        }
        .modifier(QuietMenu(help: "模型", label: "模型 \(modelName)"))
        // A model without effort levels has only its default: no menu.
        let efforts = catalog.effortOptions(model: chat.model, current: chat.effort)
        if efforts.count > 1 {
            let effortName = catalog.effortName(chat.effort, model: chat.model)
            Menu {
                Picker("推理强度", selection: Binding(get: { chat.effort }, set: { model.setEffort($0, forChat: chat.id) })) {
                    ForEach(efforts) { option in
                        Text(option.label).tag(option.value)
                    }
                }
                .pickerStyle(.inline)
            } label: {
                Text(effortName).font(.uiSecondary)
            }
            .modifier(QuietMenu(help: "推理强度", label: "推理强度 \(effortName)"))
        }
    }
}

/// A composer menu as quiet text with the system's own trailing chevron, as
/// Codex shows next to its model.
private struct QuietMenu: ViewModifier {
    let help: String
    let label: String

    func body(content: Content) -> some View {
        content
            .menuStyle(.borderlessButton)
            .menuIndicator(.visible)
            .fixedSize()
            .help(help)
            .accessibilityLabel(label)
    }
}

/// The chat's less frequent actions, in the composer's "+" menu.
private struct ChatMenuItems: View {
    let model: AppModel
    let chat: ChatSummary

    var body: some View {
        Button(screenshotTitle, systemImage: "camera.viewfinder") {
            Task { await ScreenCapture.capture(into: model, chat: chat.id, hideApp: true) }
        }
    }

    private var screenshotTitle: String {
        let shortcut = ScreenshotShortcut.current
        return shortcut == .off ? "截图…" : "截图…（任意 App 中 \(shortcut.label)）"
    }
}

/// The provider's permission modes, each with what it allows.
private struct PermissionMenu: View {
    let model: AppModel
    let chat: ChatSummary

    var body: some View {
        let current = model.permissionPreset(chat.permission, backend: chat.backend)
        Menu {
            Section(chat.backend == .claude ? "Claude Code 权限模式" : "Codex 权限") {
                ForEach(PermissionPreset.presets(for: chat.backend), id: \.label) { preset in
                    Button {
                        model.setPermission(preset.label, forChat: chat.id)
                    } label: {
                        Image(systemName: preset.label == chat.permission ? "checkmark" : preset.symbol)
                        Text(preset.label)
                        Text(preset.detail)
                    }
                }
            }
            if current == nil {
                Section {
                    Label(chat.permission, systemImage: "checkmark").disabled(true)
                }
            }
        } label: {
            Label(chat.permission, systemImage: current?.symbol ?? "lock.shield")
                .font(.uiSecondary)
                .foregroundStyle(current?.risky == true ? Color.orange : Color.secondary)
        }
        .help(current?.detail ?? "权限：代理在什么情况下需要你批准")
        .accessibilityLabel("权限 \(chat.permission)")
    }
}

/// How full the chat's context window is: a small ring, details on hover.
struct ContextMeter: View {
    let usage: ContextUsage
    /// Compacts the context now; nil when the chat cannot (busy, or no native command).
    var compact: (() -> Void)? = nil
    @State private var showing = false

    var body: some View {
        Button {
            showing.toggle()
        } label: {
            HStack(spacing: 4) {
                ZStack {
                    Circle().stroke(Color.secondary.opacity(0.25), lineWidth: 2)
                    Circle()
                        .trim(from: 0, to: max(0.02, usage.percent / 100))
                        .stroke(tint, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 14, height: 14)
                // The number appears only once the window is getting full.
                if usage.percent >= 70 {
                    Text("\(Int(usage.percent.rounded()))%")
                        .font(.uiMeta.monospacedDigit())
                        .foregroundStyle(tint)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .help("上下文已用 \(Self.tokens(usage.tokens)) / \(Self.tokens(usage.window))")
        .accessibilityLabel("上下文已用 \(Int(usage.percent.rounded()))%")
        .popover(isPresented: $showing, arrowEdge: .top) {
            VStack(alignment: .leading, spacing: 8) {
                Text("上下文窗口").font(.uiHeadline)
                ProgressView(value: usage.percent, total: 100).tint(tint)
                Text("已用 \(Self.tokens(usage.tokens)) / \(Self.tokens(usage.window)) tokens（\(String(format: "%.1f", usage.percent))%）")
                    .font(.uiSecondary.monospacedDigit())
                Text(usage.percent >= 80 ? "快满了：代理会自动压缩较早的内容，也可以开一个新聊天。" : "包括系统提示、工具定义和整段对话。")
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                if let compact {
                    Button("立即压缩上下文") {
                        showing = false
                        compact()
                    }
                    .help("让代理把较早的对话总结成摘要，腾出上下文空间")
                }
            }
            .padding(14)
            .frame(width: 260)
        }
    }

    private var tint: Color {
        usage.percent >= 90 ? .red : usage.percent >= 70 ? .orange : .accentColor
    }

    static func tokens(_ count: Int) -> String {
        count >= 1_000_000 ? String(format: "%.1fM", Double(count) / 1_000_000)
            : count >= 1000 ? String(format: "%.1fk", Double(count) / 1000) : String(count)
    }
}

// MARK: - Group

/// A member in the group composer, like a chat's model menu: its model,
/// effort and permission mode, with the provider's own names, and how full
/// its context is. Changes apply from the member's next answer.
private struct MemberMenu: View {
    let model: AppModel
    let groupID: String
    let member: Participant

    var body: some View {
        let catalog = model.catalog(for: member.backend)
        Menu {
            Section("@\(member.handle) · \(member.backend.displayName)\(member.role.isEmpty ? "" : " · " + member.role)") {
                Picker("模型", selection: Binding(get: { member.model }, set: { model.updateMember(member.handle, inGroup: groupID, model: $0) })) {
                    ForEach(catalog.modelOptions(current: member.model)) { Text($0.label).tag($0.value) }
                }
                Picker("推理强度", selection: Binding(get: { member.effort }, set: { model.updateMember(member.handle, inGroup: groupID, effort: $0) })) {
                    ForEach(catalog.effortOptions(model: member.model, current: member.effort)) { Text($0.label).tag($0.value) }
                }
                Menu("权限：\(member.permission)") {
                    ForEach(PermissionPreset.presets(for: member.backend), id: \.label) { preset in
                        Button {
                            model.updateMember(member.handle, inGroup: groupID, permission: preset.label)
                        } label: {
                            Image(systemName: preset.label == member.permission ? "checkmark" : preset.symbol)
                            Text(preset.label)
                            Text(preset.detail)
                        }
                    }
                }
            }
            Section {
                if let context = member.sessionID.flatMap({ model.contextUsage[$0] }) {
                    Text("上下文 \(Int(context.percent.rounded()))% · \(ContextMeter.tokens(context.tokens)) / \(ContextMeter.tokens(context.window))")
                }
                Text(member.state == .running ? "正在回答：改动从下一次回答开始生效" : "改动从下一次回答开始生效")
            }
            Section {
                Button("从讨论组移除 @\(member.handle)", role: .destructive) {
                    model.removeMember(member.handle, fromGroup: groupID)
                }
                .disabled(member.state == .running)
            }
        } label: {
            Label {
                Text(label)
            } icon: {
                Image(nsImage: ProviderMark.image(member.backend, size: 14))
            }
        }
        .help("@\(member.handle)：\(catalog.modelName(member.model)) · \(catalog.effortName(member.effort, model: member.model)) · \(member.permission)")
        .accessibilityLabel("@\(member.handle)，\(member.state.label)")
    }

    private var label: String {
        member.state == .needsAction ? "@\(member.handle) · 待批准" : "@\(member.handle)"
    }
}

/// The group's members in the composer, and its answer mode and
/// analysis-only switch while they differ from the default.
private struct GroupControls: View {
    let model: AppModel
    let group: GroupSummary
    let mode: GroupMode
    @Binding var analysisOnly: Bool

    var body: some View {
        ForEach(group.participants) { member in
            MemberMenu(model: model, groupID: group.id, member: member)
        }
        if mode != .sequential {
            Label(mode.label, systemImage: "rectangle.split.2x1").font(.uiSecondary)
                .help("同时：各自独立回答（在 + 里切换）")
        }
        if analysisOnly {
            Button {
                analysisOnly = false
            } label: {
                Label("只分析", systemImage: "eye").font(.uiSecondary).foregroundStyle(Color.accentColor)
            }
            .help("只分析，不改代码（点一下关闭）")
        }
    }
}

/// The group's less frequent actions, in the composer's "+" menu.
private struct GroupMenuItems: View {
    let model: AppModel
    let group: GroupSummary
    @Binding var mode: GroupMode
    @Binding var analysisOnly: Bool

    var body: some View {
        Picker("回答方式", selection: $mode) {
            ForEach(GroupMode.allCases, id: \.self) { Text($0.label).tag($0) }
        }
        .pickerStyle(.inline)
        Toggle("只分析，不改代码", isOn: $analysisOnly)
        Divider()
        Menu("添加成员", systemImage: "person.badge.plus") {
            ForEach(AgentBackend.allCases, id: \.self) { backend in
                Button(backend.displayName) { model.addMember(backend, toGroup: group.id) }
            }
        }
        .disabled(group.participants.count >= 6 || !model.isLive)
    }
}

struct GroupDetailView: View {
    @Bindable var model: AppModel
    let groupID: String
    @State private var mode: GroupMode = .sequential
    @State private var analysisOnly = false

    var body: some View {
        if let group = model.group(groupID) {
            VStack(spacing: 0) {
                TimelineView(items: model.timeline(groupID),
                             mentionHandles: Set(group.participants.map(\.handle)),
                             pending: model.pendingSends[groupID] ?? [],
                             activity: model.activity(forGroup: groupID),
                             onOpenFile: model.openFile,
                             liveTurns: model.groupLive[groupID] ?? [],
                             processSeconds: model.processSeconds[groupID] ?? [:],
                             onReplyMember: { model.reply($0, $1, inGroup: groupID) })
                if let current = model.goal(forGroup: groupID), current.state?.status != .cleared {
                    let goal = current.state ?? ChatGoal(backend: current.member?.backend ?? .claude,
                                                         text: current.goal.objective, status: .starting)
                    let session = current.member?.sessionID ?? ""
                    GoalBar(goal: goal, owner: "@\(current.goal.handle)", busy: model.goalBusy.contains(session),
                            onEdit: { model.goalEditor = .group(groupID) },
                            onPause: { model.pauseGoal(forChat: session) },
                            onResume: { model.resumeGoal(forChat: session) },
                            onClear: { model.clearGroupGoal(inGroup: groupID) })
                        .padding(.top, 6)
                }
                ComposerView(draft: model.draftBinding(groupID),
                             placeholder: "发消息，@ 指定成员",
                             isRunning: group.state == .running || group.state == .needsAction,
                             queued: model.queues[groupID] ?? [],
                             mentionCandidates: group.participants,
                             onSend: { model.send($0, toGroup: groupID, mode: mode, analysisOnly: analysisOnly) },
                             onStop: { model.stop(groupID) },
                             onEditQueued: { model.editQueued($0, in: groupID) },
                             onWithdrawQueued: { model.withdrawQueued($0, in: groupID) },
                             menuItems: { GroupMenuItems(model: model, group: group, mode: $mode, analysisOnly: $analysisOnly) },
                             leading: { GroupControls(model: model, group: group, mode: mode, analysisOnly: $analysisOnly) },
                             trailing: { EmptyView() },
                             header: AnyView(WorkingDirectoryMenu(model: model, path: group.cwd)))
            }
            .environment(\.messageActions, model.messageActions(chat: nil))
            .navigationTitle(group.title)
            .toolbar {
                ConversationMark(backends: group.participants.map(\.backend),
                                 help: "讨论组 · " + group.participants.map { "@" + $0.handle }.joined(separator: " "))
            }
        }
    }
}

extension AppModel {
    /// Consult cards read the chat's live consults and answer approvals.
    func consultActions(chat id: String) -> ConsultActions {
        ConsultActions(
            record: { [weak self] call in self?.consultRecord(for: call, inChat: id) },
            decide: { [weak self] consultID, decision in self?.decideConsult(consultID, decision: decision, inChat: id) },
            cancel: { [weak self] consultID in self?.cancelConsult(consultID, inChat: id) },
            forward: { [weak self] answer, title in self?.forwardConsultAnswer(answer, from: title, inChat: id) },
            agentName: chat(id)?.backend.displayName ?? "代理"
        )
    }

    /// Images resolve through the server (with authentication); a file link
    /// opens that file's diff when the conversation changed it, the file
    /// itself when it is on this Mac, or the server's copy of an artifact.
    func messageActions(chat id: String?) -> MessageActions {
        MessageActions(
            loadImage: { [weak self] source in
                guard let id else { return nil }
                guard let data = await self?.media(chat: id, target: source) else { return nil }
                return NSImage(data: data)
            },
            openLink: { [weak self] url in
                guard let self else { return }
                guard url.isFileURL else { NSWorkspace.shared.open(url); return }
                var path = url.path
                // "file.swift:12" or "file.swift#L12" point into a file.
                if let range = path.range(of: #"(:\d+(:\d+)?|#L\d+(-L?\d+)?)$"#, options: .regularExpression) {
                    path.removeSubrange(range)
                }
                let changes = self.selectedChanges.files
                if let changed = changes.first(where: { path.hasSuffix($0.path) || $0.path.hasSuffix(path) }) {
                    self.openFile(changed.path)
                } else if FileManager.default.fileExists(atPath: path) {
                    MediaPreview.open(URL(fileURLWithPath: path))
                } else if let id {
                    let target = path
                    let name = (target as NSString).lastPathComponent
                    Task {
                        await MediaOpener.open(MediaFile(id: name, name: name, contentType: "")) { [weak self] _ in
                            await self?.media(chat: id, target: target)
                        }
                    }
                }
            })
    }
}
