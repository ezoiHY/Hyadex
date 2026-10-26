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
                             pending: model.pendingSends[chatID] ?? [],
                             activity: model.activity(forChat: chatID),
                             turnSeconds: model.turnSeconds[chatID] ?? [:],
                             onOpenFile: model.openFile,
                             onDecide: { model.decide($0, $1, in: chatID) },
                             loadMedia: { [model] file in await model.media(chat: chatID, fileID: file.id) })
                ComposerView(draft: model.draftBinding(chatID),
                             placeholder: "给 \(chat.backend.displayName) 发消息",
                             isRunning: chat.state == .running || chat.state == .needsAction,
                             queued: model.queues[chatID] ?? [],
                             onSend: { model.send($0, toChat: chatID) },
                             onStop: { model.stop(chatID) },
                             onEditQueued: { model.editQueued($0, in: chatID) },
                             onWithdrawQueued: { model.withdrawQueued($0, in: chatID) }) {
                    ChatControls(model: model, chat: chat)
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
        if let usage = model.contextUsage[chat.id] {
            ContextMeter(usage: usage)
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

/// A member card. Clicking it changes that agent's model, effort and
/// permissions; a turn already running keeps its settings.
private struct MemberMenu: View {
    let model: AppModel
    let groupID: String
    let member: Participant

    var body: some View {
        Menu {
            Section("@\(member.handle) · \(member.backend.displayName)") {
                Picker("模型", selection: Binding(get: { member.model }, set: { model.updateMember(member.handle, inGroup: groupID, model: $0) })) {
                    ForEach(catalog.modelOptions(current: member.model)) { Text($0.label).tag($0.value) }
                }
                Picker("推理强度", selection: Binding(get: { member.effort }, set: { model.updateMember(member.handle, inGroup: groupID, effort: $0) })) {
                    ForEach(catalog.effortOptions(model: member.model, current: member.effort)) { Text($0.label).tag($0.value) }
                }
                Picker("权限", selection: Binding(get: { member.permission }, set: {
                    model.updateMember(member.handle, inGroup: groupID, permission: $0)
                })) {
                    ForEach(model.permissionChoices(for: member), id: \.self) { label in
                        Text(label).tag(label)
                    }
                }
            }
            Text(member.state == .running ? "正在回答：改动从下一次回答开始生效" : "改动从下一次回答开始生效")
        } label: {
            HStack(spacing: 6) {
                AgentAvatar(backend: member.backend, size: 20)
                VStack(alignment: .leading, spacing: 0) {
                    Text("@\(member.handle)")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(Theme.color(for: member.backend))
                    Text(summary)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if member.state != .idle {
                    StatePill(state: member.state)
                }
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.07)))
            .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("更改 @\(member.handle) 的模型、推理强度和权限")
        .accessibilityLabel("@\(member.handle)，\(summary)")
    }

    private var catalog: RuntimeCatalog { model.catalog(for: member.backend) }

    private var summary: String {
        [member.role, catalog.modelName(member.model), catalog.effortName(member.effort, model: member.model)]
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
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
                MemberBar(model: model, group: group)
                Divider()
                TimelineView(items: model.timeline(groupID),
                             mentionHandles: Set(group.participants.map(\.handle)),
                             onOpenFile: model.openFile)
                ForEach(model.groupApprovals[groupID] ?? []) { approval in
                    ApprovalCard(author: .agent(approval.handle, approval.backend), title: approval.title,
                                 command: approval.detail,
                                 reason: approval.reason.isEmpty ? "@\(approval.handle) 在等你批准后才能继续。" : approval.reason,
                                 decision: .pending, allowsSession: approval.allowsSession) { decision in
                        model.decide(approval, decision, inGroup: groupID)
                    }
                    .frame(maxWidth: Theme.readableWidth)
                    .padding(.horizontal, 28)
                    .padding(.top, 6)
                }
                ComposerView(draft: model.draftBinding(groupID),
                             placeholder: "发消息，用 @ 指定成员（不指定时由 @\(group.defaultResponder) 回答）",
                             isRunning: group.state == .running,
                             queued: model.queues[groupID] ?? [],
                             mentionCandidates: group.participants,
                             onSend: { model.send($0, toGroup: groupID, mode: mode, analysisOnly: analysisOnly) },
                             onStop: { model.stop(groupID) },
                             onEditQueued: { model.editQueued($0, in: groupID) },
                             onWithdrawQueued: { model.withdrawQueued($0, in: groupID) }) {
                    Picker("回答方式", selection: $mode) {
                        ForEach(GroupMode.allCases, id: \.self) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                    .help("依次：后回答的成员能看到前面的回答。同时：各自独立回答。")
                    Toggle("只分析，不改代码", isOn: $analysisOnly)
                        .toggleStyle(.checkbox)
                }
            }
            .environment(\.messageActions, model.messageActions(chat: nil))
            .navigationTitle(group.title)
            .navigationSubtitle("讨论组 · \(group.participants.count) 位成员 · \(group.cwd)")
        }
    }
}

private struct MemberBar: View {
    let model: AppModel
    let group: GroupSummary

    var body: some View {
        HStack(spacing: 8) {
            // Members scroll sideways rather than being clipped in a narrow window.
            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    ForEach(group.participants) { member in
                        MemberMenu(model: model, groupID: group.id, member: member)
                    }
                }
            }
            .scrollIndicators(.never)
            .fixedSize(horizontal: false, vertical: true)
            Button {
            } label: {
                Image(systemName: "person.badge.plus")
            }
            .buttonStyle(.borderless)
            .help("添加成员（最多 6 位）")
            Spacer()
            Label("\(group.dispatches) 次派发 · \(group.promptChars.compact) 字符", systemImage: "chart.bar.xaxis")
                .font(.caption)
                .foregroundStyle(.secondary)
                .help("本组累计发给代理的提示词。成员只收到自己没看过的新消息。")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
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
