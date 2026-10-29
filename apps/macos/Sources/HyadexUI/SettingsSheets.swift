import HyadexCore
import SwiftUI

/// Opens the chat's advanced settings or the group's settings.
struct SettingsSheet: View {
    @Bindable var model: AppModel
    let target: SettingsEditorTarget

    var body: some View {
        switch target {
        case .chat(let id): ChatSettingsSheet(model: model, chatID: id)
        case .group(let id): GroupSettingsSheet(model: model, groupID: id)
        }
    }
}

/// A chat's system prompt, subagent limit, automatic title and the agent's
/// access to scheduled jobs. Loaded from the chat's full server record.
private struct ChatSettingsSheet: View {
    @Bindable var model: AppModel
    let chatID: String
    @Environment(\.dismiss) private var dismiss
    @State private var loaded: ChatSettings?
    @State private var edited = ChatSettings()
    @State private var loadError: String?
    @State private var saveError: String?
    @State private var saving = false

    private var chat: ChatSummary? { model.chat(chatID) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("高级设置").font(.uiHeadline)
                .padding([.horizontal, .top], 18)
            if let chat { Text(chat.title).font(.uiSecondary).foregroundStyle(.secondary).padding(.horizontal, 18) }
            if loaded != nil {
                form
            } else if let loadError {
                Text(loadError).foregroundStyle(.red).padding(18)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 160)
            }
            footer
        }
        .frame(width: 460)
        .task { await load() }
    }

    private var form: some View {
        Form {
            Section("系统提示词") {
                TextEditor(text: $edited.systemPrompt)
                    .frame(height: 120)
                    .scrollContentBackground(.hidden)
                    .accessibilityLabel("系统提示词")
                    .help("每一轮都会附加给代理，留空表示不附加")
                LimitCount(count: edited.systemPrompt.count, limit: ChatSettings.maxSystemPromptCharacters)
            }
            Section {
                Picker("子代理上限", selection: $edited.subagentLimit) {
                    Text("代理默认").tag(Int?.none)
                    ForEach(subagentChoices, id: \.self) { Text("\($0) 个").tag(Optional($0)) }
                }
                .disabled(!edited.subagentLimitSupported)
                .accessibilityLabel("子代理上限")
                .help(subagentNote)
                Toggle("自动起标题", isOn: $edited.autoTitle)
                    .accessibilityLabel("自动起标题")
                Picker("代理管理定时任务", selection: $edited.jobsAccess) {
                    ForEach(JobsAccess.allCases) { Text($0.label).tag($0) }
                }
                .accessibilityLabel("代理管理定时任务")
                .help("代理能否在对话里自己创建、修改定时任务")
            }
            Section {
                Picker("其他聊天向它提问", selection: $edited.consultInbound) {
                    ForEach(ConsultInbound.allCases) { Text($0.label).tag($0) }
                }
                .accessibilityLabel("其他聊天向它提问")
                .help("同一项目里的其他聊天能否问它：基础凭它已经知道的回答，深度让它的只读副本查看文件后回答")
                Toggle("它深度询问别的聊天前要我批准", isOn: $edited.consultDeepApproval)
                    .accessibilityLabel("深度询问前要我批准")
                if !edited.consultDeepAllowed.isEmpty {
                    LabeledContent("总是允许深度询问") {
                        HStack(spacing: 8) {
                            Text("\(edited.consultDeepAllowed.count) 个聊天").foregroundStyle(.secondary)
                            Button("清除") { edited.consultDeepAllowed = [] }
                        }
                    }
                }
            } header: {
                Text("聊天互问")
            } footer: {
                Text("只在同一项目（或同一工作目录）里的聊天之间。回答不进入被问聊天的对话，也不打断它。")
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .disabled(saving)
    }

    private var subagentChoices: [Int] {
        Array(Set([1, 2, 3, 4, 6, 8, 12, 16] + [loaded?.subagentLimit].compactMap { $0 })).sorted()
    }

    private var subagentNote: String {
        guard edited.subagentLimitSupported else {
            return edited.subagentLimitNote.isEmpty ? "这个代理不支持按聊天设置" : edited.subagentLimitNote
        }
        switch edited.subagentLimitAppliesTo {
        case "next_idle_provider_start": return "下次 Claude 进程启动时生效，不打断当前工作"
        case "next_provider_process_start": return "Codex 进程重启后生效，不打断当前工作"
        default: return "对新的或重新加载的 Codex 线程生效，不打断当前工作"
        }
    }

    private var footer: some View {
        HStack {
            if let saveError {
                Text(saveError).font(.uiSecondary).foregroundStyle(.red).lineLimit(3)
            }
            Spacer()
            if saving { ProgressView().controlSize(.small) }
            Button("取消", role: .cancel) { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("保存", action: save)
                .keyboardShortcut(.defaultAction)
                .disabled(loaded == nil || saving || loaded.map { $0.changes(to: edited).isEmpty } ?? true
                          || edited.systemPrompt.count > ChatSettings.maxSystemPromptCharacters)
        }
        .padding(18)
    }

    private func load() async {
        do {
            let settings = try await model.chatSettings(chatID)
            loaded = settings
            edited = settings
        } catch {
            loadError = error.localizedDescription
        }
    }

    private func save() {
        guard let loaded else { return }
        saving = true
        saveError = nil
        Task {
            defer { saving = false }
            do {
                _ = try await model.saveChatSettings(chatID, from: loaded, to: edited)
                dismiss()
            } catch {
                saveError = error.localizedDescription
            }
        }
    }
}

/// Who answers unaddressed messages, how many agent hand-offs a round allows,
/// and the brief every member receives.
private struct GroupSettingsSheet: View {
    @Bindable var model: AppModel
    let groupID: String
    @Environment(\.dismiss) private var dismiss
    @State private var edited = GroupSettings()
    @State private var saveError: String?
    @State private var saving = false

    private var group: GroupSummary? { model.group(groupID) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("讨论组设置").font(.uiHeadline)
                .padding([.horizontal, .top], 18)
            if let group {
                Text(group.archived ? "\(group.title) · 已归档，不能修改" : group.title)
                    .font(.uiSecondary).foregroundStyle(.secondary).padding(.horizontal, 18)
                form(group)
            }
            HStack {
                if let saveError {
                    Text(saveError).font(.uiSecondary).foregroundStyle(.red).lineLimit(3)
                }
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("保存", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || group?.archived != false
                              || group.map { $0.settings.changes(to: edited).isEmpty } ?? true
                              || edited.brief.count > GroupSettings.maxBriefCharacters)
            }
            .padding(18)
        }
        .frame(width: 460)
        .onAppear { if let group { edited = group.settings } }
    }

    private func form(_ group: GroupSummary) -> some View {
        Form {
            Section {
                Picker("默认回答者", selection: $edited.defaultResponder) {
                    Text("@\(group.participants.first?.handle ?? "—")（第一位）").tag(String?.none)
                    ForEach(group.participants) { member in
                        Text("@\(member.handle)").tag(Optional(member.handle))
                    }
                }
                .accessibilityLabel("默认回答者")
                .help("消息里没有 @ 任何人时由它回答")
                Stepper(value: $edited.maxAgentHops, in: GroupSettings.maxAgentHopsRange) {
                    LabeledContent("接力上限", value: edited.maxAgentHops == 0 ? "不接力" : "\(edited.maxAgentHops) 次")
                }
                .accessibilityLabel("接力上限")
                .accessibilityValue("\(edited.maxAgentHops) 次")
                .help("每轮里成员之间互相 @ 交出发言的次数")
            }
            Section("共享摘要") {
                TextEditor(text: $edited.brief)
                    .frame(height: 140)
                    .scrollContentBackground(.hidden)
                    .accessibilityLabel("共享摘要")
                    .help("背景、约束和约定。每个成员每个版本只收到一次")
                LimitCount(count: edited.brief.count, limit: GroupSettings.maxBriefCharacters)
            }
        }
        .formStyle(.grouped)
        .disabled(saving || group.archived)
    }

    private func save() {
        saving = true
        saveError = nil
        let settings = edited
        Task {
            defer { saving = false }
            do {
                try await model.saveGroupSettings(groupID, to: settings)
                dismiss()
            } catch {
                saveError = error.localizedDescription
            }
        }
    }
}

/// A character count, shown only once the text nears its limit.
private struct LimitCount: View {
    let count: Int
    let limit: Int

    var body: some View {
        if count > limit * 9 / 10 {
            Text("\(count.formatted()) / \(limit.formatted())")
                .font(.uiMeta)
                .foregroundStyle(count > limit ? .red : .secondary)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}
