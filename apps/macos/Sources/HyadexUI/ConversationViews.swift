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
                             onDecide: { model.decide($0, $1, in: chatID) },
                             loadMedia: { [model] file in await model.media(chat: chatID, fileID: file.id) },
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
                             placeholder: "给 \(chat.backend.displayName) 发消息，输入 / 使用命令和技能",
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
                             onWithdrawQueued: { model.withdrawQueued($0, in: chatID) }) {
                    ChatControls(model: model, chat: chat)
                }
                if model.terminalOpen.contains(chatID) {
                    Divider()
                    TerminalDock(model: model, chatID: chatID)
                }
            }
            .environment(\.messageActions, model.messageActions(chat: chatID))
            .navigationTitle(chat.title)
            .navigationSubtitle("\(chat.backend.displayName) · \(model.catalog(for: chat.backend).modelName(chat.model)) · \(chat.cwd)")
        }
    }
}

/// Per-chat run settings, shown as quiet menus in the composer. Model,
/// effort and permission modes use the provider's own names; changes apply
/// to the chat's next turn.
private struct ChatControls: View {
    let model: AppModel
    let chat: ChatSummary

    var body: some View {
        let catalog = model.catalog(for: chat.backend)
        Menu {
            Picker("模型", selection: Binding(get: { chat.model }, set: { model.setModel($0, forChat: chat.id) })) {
                ForEach(catalog.modelOptions(current: chat.model)) { option in
                    Text(option.label).tag(option.value)
                }
            }
            .pickerStyle(.inline)
            Picker("推理强度", selection: Binding(get: { chat.effort }, set: { model.setEffort($0, forChat: chat.id) })) {
                ForEach(catalog.effortOptions(model: chat.model, current: chat.effort)) { option in
                    Text(option.label).tag(option.value)
                }
            }
            .pickerStyle(.inline)
        } label: {
            Label {
                Text("\(catalog.modelName(chat.model)) · \(catalog.effortName(chat.effort, model: chat.model))")
            } icon: {
                Image(nsImage: ProviderMark.image(chat.backend, size: 14))
            }
        }
        .help("模型和推理强度")
        PermissionMenu(model: model, chat: chat)
        Button {
            model.toggleSideChat(chat.id)
        } label: {
            Label("侧问", systemImage: model.sideChatOpen == chat.id ? "text.bubble.fill" : "text.bubble")
        }
        .help("侧问：问个问题，不打断代理，也不写进对话（也可以输入 /btw）")
        let support = model.goalSupport(forChat: chat.id)
        Button {
            model.goalEditor = .chat(chat.id)
        } label: {
            Label("目标", systemImage: model.goals[chat.id]?.isOpen == true ? "scope" : "target")
        }
        .disabled(!support.available)
        .help(support.available ? (chat.backend == .claude ? "设定完成条件（Claude Code /goal）" : "设定 Codex 持久目标")
                                : support.reason ?? "目标不可用")
        if let usage = model.contextUsage[chat.id] {
            ContextMeter(usage: usage, compact: model.canCompact(chat.id) ? { model.compact(chat.id) } : nil)
                .onAppear { if chat.backend == .claude { model.loadCommandsIfNeeded(forChat: chat.id) } }
        }
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
        }
        .help(current?.detail ?? "权限：代理在什么情况下需要你批准")
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
                .frame(width: 12, height: 12)
                Text("\(Int(usage.percent.rounded()))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .help("上下文已用 \(Self.tokens(usage.tokens)) / \(Self.tokens(usage.window))")
        .accessibilityLabel("上下文已用 \(Int(usage.percent.rounded()))%")
        .popover(isPresented: $showing, arrowEdge: .top) {
            VStack(alignment: .leading, spacing: 8) {
                Text("上下文窗口").font(.headline)
                ProgressView(value: usage.percent, total: 100).tint(tint)
                Text("已用 \(Self.tokens(usage.tokens)) / \(Self.tokens(usage.window)) tokens（\(String(format: "%.1f", usage.percent))%）")
                    .font(.callout.monospacedDigit())
                Text(usage.percent >= 80 ? "快满了：代理会自动压缩较早的内容，也可以开一个新聊天。" : "包括系统提示、工具定义和整段对话。")
                    .font(.caption)
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

/// The group's composer controls: members, how they answer, and whether
/// they may change code.
private struct GroupControls: View {
    let model: AppModel
    let group: GroupSummary
    @Binding var mode: GroupMode
    @Binding var analysisOnly: Bool

    var body: some View {
        ForEach(group.participants) { member in
            MemberMenu(model: model, groupID: group.id, member: member)
        }
        Menu {
            Section("添加成员") {
                ForEach(AgentBackend.allCases, id: \.self) { backend in
                    Button {
                        model.addMember(backend, toGroup: group.id)
                    } label: {
                        Image(nsImage: ProviderMark.image(backend, size: 14))
                        Text(backend.displayName)
                    }
                }
            }
        } label: {
            Image(systemName: "plus")
        }
        .disabled(group.participants.count >= 6 || !model.isLive)
        .help("添加成员（最多 6 位）")
        Divider().frame(height: 14)
        Menu {
            Picker("回答方式", selection: $mode) {
                ForEach(GroupMode.allCases, id: \.self) { option in
                    Text(option.label).tag(option)
                }
            }
            .pickerStyle(.inline)
            Text("依次：后回答的成员能看到前面的回答。同时：各自独立回答。")
        } label: {
            Label(mode.label, systemImage: mode == .sequential ? "list.number" : "rectangle.split.2x1")
        }
        .help("回答方式")
        Button {
            model.goalEditor = .group(group.id)
        } label: {
            Label("目标", systemImage: model.goal(forGroup: group.id)?.state?.isOpen == true ? "scope" : "target")
        }
        .disabled(group.participants.isEmpty)
        .help("给一位成员设定原生目标，并告知讨论组")
        Toggle(isOn: $analysisOnly) {
            Label("只分析", systemImage: analysisOnly ? "eye.fill" : "eye")
        }
        .toggleStyle(.button)
        .help("只分析，不改代码")
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
                             onDecideMember: { model.decide($0, $1, inGroup: groupID) })
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
                             placeholder: "发消息，用 @ 指定成员（不指定时由 @\(group.defaultResponder) 回答）",
                             isRunning: group.state == .running || group.state == .needsAction,
                             queued: model.queues[groupID] ?? [],
                             mentionCandidates: group.participants,
                             onSend: { model.send($0, toGroup: groupID, mode: mode, analysisOnly: analysisOnly) },
                             onStop: { model.stop(groupID) },
                             onEditQueued: { model.editQueued($0, in: groupID) },
                             onWithdrawQueued: { model.withdrawQueued($0, in: groupID) }) {
                    GroupControls(model: model, group: group, mode: $mode, analysisOnly: $analysisOnly)
                }
            }
            .environment(\.messageActions, model.messageActions(chat: nil))
            .navigationTitle(group.title)
            .navigationSubtitle("讨论组 · \(group.participants.map { "@" + $0.handle }.joined(separator: " ")) · \(group.cwd)")
        }
    }
}

extension AppModel {
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
                    NSWorkspace.shared.open(URL(fileURLWithPath: path))
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
