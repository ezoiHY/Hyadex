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
                             onOpenFile: model.openFile,
                             onDecide: { model.decide($0, $1, in: chatID) })
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
            .navigationTitle(chat.title)
            .navigationSubtitle("\(chat.backend.displayName) · \(chat.model) · \(chat.cwd)")
        }
    }
}

/// Per-chat run settings, shown as quiet menus in the composer. Changes
/// apply to the chat's next turn.
private struct ChatControls: View {
    let model: AppModel
    let chat: ChatSummary

    var body: some View {
        Menu {
            Picker("模型", selection: Binding(get: { chat.model }, set: { model.setModel($0, forChat: chat.id) })) {
                ForEach(model.modelChoices(for: chat), id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.inline)
        } label: {
            Label(chat.model, systemImage: Theme.symbol(for: chat.backend))
        }
        .help("模型")
        Menu {
            Picker("推理强度", selection: Binding(get: { chat.effort }, set: { model.setEffort($0, forChat: chat.id) })) {
                ForEach(EffortPreset.labels.map(\.label), id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.inline)
        } label: {
            Label("推理 \(chat.effort)", systemImage: "gauge.with.dots.needle.67percent")
        }
        .help("推理强度")
        Menu {
            Picker("权限", selection: Binding(get: { chat.permission }, set: { model.setPermission($0, forChat: chat.id) })) {
                ForEach(model.permissionChoices(for: chat), id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.inline)
        } label: {
            Label(chat.permission, systemImage: "lock.shield")
        }
        .help("权限：代理在什么情况下需要你批准")
    }
}

// MARK: - Group

struct GroupDetailView: View {
    @Bindable var model: AppModel
    let groupID: String
    @State private var mode: GroupMode = .sequential
    @State private var analysisOnly = false

    var body: some View {
        if let group = model.group(groupID) {
            VStack(spacing: 0) {
                MemberBar(group: group)
                Divider()
                TimelineView(items: model.timeline(groupID),
                             mentionHandles: Set(group.participants.map(\.handle)),
                             onOpenFile: model.openFile)
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
            .navigationTitle(group.title)
            .navigationSubtitle("讨论组 · \(group.participants.count) 位成员 · \(group.cwd)")
        }
    }
}

private struct MemberBar: View {
    let group: GroupSummary

    var body: some View {
        HStack(spacing: 8) {
            ForEach(group.participants) { member in
                HStack(spacing: 6) {
                    AgentAvatar(backend: member.backend, size: 20)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("@\(member.handle)")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(Theme.color(for: member.backend))
                        Text("\(member.role) · \(member.model)")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if member.state != .idle {
                        StatePill(state: member.state)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.07)))
                .accessibilityElement(children: .combine)
            }
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
