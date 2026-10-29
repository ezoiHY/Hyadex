import HyadexCore
import SwiftUI
import UniformTypeIdentifiers

/// Message input shared by chats and groups.
/// Return sends, Shift-Return adds a line, Esc stops a running turn.
/// While an agent is busy, new messages are queued and stay editable.
struct ComposerView<MenuItems: View, Leading: View, Trailing: View>: View {
    @Binding var draft: String
    let placeholder: String
    let isRunning: Bool
    let queued: [QueuedMessage]
    var mentionCandidates: [Participant] = []
    /// The agent's commands and skills matching a partial "/name" (chats only).
    var slashSuggestions: ((String) -> [ProviderCommand])? = nil
    /// Shown under the suggestions when the list is loading or unavailable.
    var slashNote: String? = nil
    /// Called once the user starts typing a slash command.
    var onBeginSlash: () -> Void = {}
    /// Files in the composer; nil where attachments are not supported (groups).
    var attachments: [DraftAttachment]? = nil
    var onAttach: ([URL]) -> Void = { _ in }
    var onRemoveAttachment: (String) -> Void = { _ in }
    /// Queue controls; nil hides them.
    var onRunQueuedNow: ((String) -> Void)? = nil
    var onMoveQueued: ((String, Bool) -> Void)? = nil
    var onResumeQueue: (() -> Void)? = nil
    let onSend: (String) -> Void
    let onStop: () -> Void
    let onEditQueued: (String) -> Void
    let onWithdrawQueued: (String) -> Void
    /// Less frequent actions, in the "+" menu after 添加文件… (a screenshot, a group's answer mode…).
    @ViewBuilder let menuItems: () -> MenuItems
    /// Shown right of "+": the permission mode, or a group's members.
    @ViewBuilder let leading: () -> Leading
    /// Shown left of the send button: model, effort and context.
    @ViewBuilder let trailing: () -> Trailing
    /// A line above the box: where the agent works.
    var header: AnyView? = nil
    /// Chats this one may ask with @ (chats only); nil turns chat mentions off.
    var chatMentions: [ConsultTarget]? = nil
    /// Called once the user starts typing @ (loads `chatMentions`).
    var onBeginChatMention: () -> Void = {}
    /// Asks a chat directly when the message starts with its @: (chat, question, deep).
    var onAskChat: ((ConsultTarget, String, Bool) -> Void)? = nil
    /// The chat's agent, for "not sent to …".
    var agentName = "代理"

    @FocusState private var focused: Bool
    @State private var highlighted = 0
    /// 设置 › 通用: ⌘↩ sends and ↩ adds a line.
    @AppStorage("sendWithCommandReturn") private var sendWithCommandReturn = false
    @State private var picking = false
    @State private var dropTargeted = false
    /// A direct question to another chat asks a read-only copy of it to check its files.
    @State private var askDeep = false

    private var uploading: Bool { attachments?.contains { $0.state == .uploading } ?? false }
    private var hasFiles: Bool { attachments?.contains { $0.fileID != nil } ?? false }
    private var canSend: Bool {
        if let directAsk { return !directAsk.question.isEmpty }
        return (!trimmed.isEmpty || hasFiles) && !uploading
    }

    /// The chat name typed after "@", while chat mentions are on.
    private var chatMentionPartial: String? { chatMentions == nil ? nil : ChatMention.partial(in: draft) }

    private var chatSuggestions: [ConsultTarget] {
        guard let partial = chatMentionPartial, let chatMentions else { return [] }
        return ChatMention.matches(partial, in: chatMentions)
    }

    /// A message that opens with another chat's @ asks that chat, not this agent.
    private var directAsk: (target: ConsultTarget, question: String)? {
        guard let chatMentions, onAskChat != nil else { return nil }
        return ChatMention.direct(in: draft, targets: chatMentions)
    }

    private var trimmed: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var suggestions: [String] {
        guard !mentionCandidates.isEmpty, let partial = MentionParser.partialMention(at: draft) else { return [] }
        let handles = mentionCandidates.map(\.handle) + ["all"]
        return handles.filter { partial.isEmpty || $0.hasPrefix(partial) }
    }

    private var partialCommand: String? {
        slashSuggestions == nil ? nil : SlashParser.partialCommand(in: draft)
    }

    private var commandSuggestions: [ProviderCommand] {
        guard let partial = partialCommand, let slashSuggestions else { return [] }
        return slashSuggestions(partial)
    }

    var body: some View {
        VStack(spacing: 8) {
            if let header {
                HStack(spacing: 0) {
                    header
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 8)
                .padding(.bottom, -2)
            }
            if !queued.isEmpty {
                QueueStrip(queued: queued, isRunning: isRunning, onEdit: onEditQueued, onWithdraw: onWithdrawQueued,
                           onRunNow: onRunQueuedNow, onMove: onMoveQueued, onResume: onResumeQueue)
            }
            if !suggestions.isEmpty {
                MentionSuggestions(handles: suggestions, candidates: mentionCandidates, onPick: complete)
            }
            if !chatSuggestions.isEmpty {
                ChatMentionSuggestions(targets: chatSuggestions, onPick: complete)
            }
            if let directAsk {
                DirectAskBar(target: directAsk.target, deep: $askDeep, agentName: agentName)
            }
            if partialCommand != nil, !commandSuggestions.isEmpty || slashNote != nil {
                CommandSuggestions(commands: commandSuggestions, highlighted: min(highlighted, commandSuggestions.count - 1),
                                   note: slashNote, onPick: complete)
            }
            VStack(alignment: .leading, spacing: 8) {
                if let attachments, !attachments.isEmpty {
                    AttachmentStrip(attachments: attachments, onRemove: onRemoveAttachment)
                }
                TextField(placeholder, text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.uiBody)
                    // Two lines tall when empty, like Claude's and Codex's apps.
                    .lineLimit(2...12)
                    .lineSpacing(2)
                    .focused($focused)
                    .onSubmit(send)
                    .onKeyPress(.return, phases: .down) { press in
                        // Return picks the highlighted command instead of sending "/rev".
                        if let command = highlightedCommand, !press.modifiers.contains(.shift) {
                            complete(command)
                            return .handled
                        }
                        // Return picks the chat being typed after "@", like Tab.
                        if let chat = chatSuggestions.first, chatMentionPartial?.isEmpty == false,
                           !press.modifiers.contains(.shift) {
                            complete(chat)
                            return .handled
                        }
                        if sendWithCommandReturn {
                            if press.modifiers.contains(.command) {
                                send()
                            } else {
                                draft.append("\n")
                            }
                            return .handled
                        }
                        if press.modifiers.contains(.shift) {
                            draft.append("\n")
                            return .handled
                        }
                        return .ignored
                    }
                    .onKeyPress(.tab) {
                        if let command = highlightedCommand {
                            complete(command)
                            return .handled
                        }
                        if let chat = chatSuggestions.first {
                            complete(chat)
                            return .handled
                        }
                        guard let first = suggestions.first else { return .ignored }
                        complete(first)
                        return .handled
                    }
                    .onKeyPress(.upArrow) {
                        guard !commandSuggestions.isEmpty else { return .ignored }
                        highlighted = max(0, min(highlighted, commandSuggestions.count - 1) - 1)
                        return .handled
                    }
                    .onKeyPress(.downArrow) {
                        guard !commandSuggestions.isEmpty else { return .ignored }
                        highlighted = min(commandSuggestions.count - 1, highlighted + 1)
                        return .handled
                    }
                    .onChange(of: chatMentionPartial == nil) { wasNone, isNone in
                        if wasNone && !isNone { onBeginChatMention() }
                    }
                    .onChange(of: partialCommand) { old, new in
                        highlighted = 0
                        if old == nil, new != nil { onBeginSlash() }
                    }
                    .onKeyPress(.escape) {
                        if partialCommand != nil, !commandSuggestions.isEmpty {
                            draft = ""
                            return .handled
                        }
                        guard isRunning else { return .ignored }
                        onStop()
                        return .handled
                    }
                    .padding(.horizontal, 4)
                    .padding(.top, 2)
                HStack(spacing: 10) {
                    Menu {
                        if attachments != nil {
                            Button("添加文件…", systemImage: "paperclip") { picking = true }
                            Divider()
                        }
                        menuItems()
                    } label: {
                        Image(systemName: "plus")
                            .font(.system(size: 15, weight: .medium))
                            .frame(width: 26, height: 26)
                            .contentShape(Circle())
                    }
                    .menuStyle(.button)
                    .buttonStyle(.borderless)
                    .menuIndicator(.hidden)
                    .fixedSize()
                    .foregroundStyle(.secondary)
                    .help("添加文件和更多操作（也可以拖入文件或 ⌘V 粘贴）")
                    .accessibilityLabel("添加")
                    leading()
                        .menuStyle(.button)
                        .buttonStyle(.borderless)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 8)
                    trailing()
                        .buttonStyle(.borderless)
                        .fixedSize()
                        .foregroundStyle(.secondary)
                    if uploading {
                        Text("正在上传…")
                            .font(.uiMeta)
                            .foregroundStyle(.secondary)
                    }
                    sendOrStopButton
                }
            }
            .padding(.horizontal, 12)
            .padding(.top, 14)
            .padding(.bottom, 10)
            .background(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(Color(nsColor: .textBackgroundColor))
                    .shadow(color: .black.opacity(0.08), radius: 10, y: 3)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .strokeBorder(dropTargeted ? Color.accentColor
                                  : Color(nsColor: .separatorColor).opacity(focused ? 1 : 0.7),
                                  lineWidth: dropTargeted ? 1.5 : 0.5)
            )
            .fileImporter(isPresented: $picking, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
                if case .success(let urls) = result { onAttach(urls) }
            }
            .dropDestination(for: URL.self) { urls, _ in
                guard attachments != nil else { return false }
                let files = urls.filter(\.isFileURL)
                onAttach(files)
                return !files.isEmpty
            } isTargeted: { dropTargeted = attachments != nil && $0 }
            .background(PasteInterceptor(enabled: focused && attachments != nil, onFiles: onAttach))
            .onTapGesture { focused = true }
        }
        .frame(maxWidth: Theme.readableWidth)
        .padding(.horizontal, 28)
        .padding(.bottom, 16)
        .padding(.top, 6)
        .frame(maxWidth: .infinity)
        .onAppear { focused = true }
    }

    @ViewBuilder private var sendOrStopButton: some View {
        if isRunning && trimmed.isEmpty && !hasFiles {
            Button(action: onStop) {
                Image(systemName: "stop.fill")
                    .font(.system(size: 11, weight: .bold))
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(Color.primary.opacity(0.85)))
                    .foregroundStyle(Color(nsColor: .textBackgroundColor))
            }
            .buttonStyle(.plain)
            .help("停止（Esc）")
            .accessibilityLabel("停止")
        } else {
            Button(action: send) {
                Image(systemName: isRunning ? "text.line.last.and.arrowtriangle.forward" : "arrow.up")
                    .font(.system(size: 14, weight: .bold))
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(canSend ? Color.accentColor : Color.secondary.opacity(0.25)))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
            .disabled(!canSend)
            // ⌘↩ must be a key equivalent: the text view would take it before onKeyPress.
            .keyboardShortcut(sendWithCommandReturn ? KeyboardShortcut(.return, modifiers: .command) : nil)
            .help(directAsk.map { "问「\($0.target.title)」" }
                  ?? (isRunning ? "加入队列（\(sendWithCommandReturn ? "⌘↩" : "↩")）" : "发送（\(sendWithCommandReturn ? "⌘↩" : "↩")）"))
            .accessibilityLabel(directAsk.map { "问\($0.target.title)" } ?? (isRunning ? "加入队列" : "发送"))
        }
    }

    private func send() {
        guard canSend else { return }
        if let directAsk, let onAskChat {
            onAskChat(directAsk.target, directAsk.question, askDeep && directAsk.target.allowsDeep)
            draft = ""
            askDeep = false
            return
        }
        onSend(draft)
        draft = ""
    }

    private func complete(_ chat: ConsultTarget) {
        guard let at = draft.lastIndex(of: "@") else { return }
        draft = String(draft[..<at]) + ChatMention.token(chat.title) + " "
    }

    private var highlightedCommand: ProviderCommand? {
        let commands = commandSuggestions
        return commands.isEmpty ? nil : commands[min(highlighted, commands.count - 1)]
    }

    private func complete(_ command: ProviderCommand) {
        draft = command.invocation + " "
    }

    private func complete(_ handle: String) {
        guard let at = draft.lastIndex(of: "@") else { return }
        draft = String(draft[..<at]) + "@\(handle) "
    }
}

private struct QueueStrip: View {
    let queued: [QueuedMessage]
    let isRunning: Bool
    let onEdit: (String) -> Void
    let onWithdraw: (String) -> Void
    let onRunNow: ((String) -> Void)?
    let onMove: ((String, Bool) -> Void)?
    let onResume: (() -> Void)?

    private var stoppedCount: Int { queued.filter { $0.hold == .stopped }.count }

    var body: some View {
        VStack(spacing: 0) {
            if stoppedCount > 0 {
                HStack(spacing: 8) {
                    Image(systemName: "pause.circle.fill").foregroundStyle(.orange)
                    Text("停止后保留的 \(stoppedCount) 条消息不会自动发送")
                        .font(.uiMeta)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let onResume {
                        Button("继续发送", action: onResume)
                            .buttonStyle(.borderless)
                            .help(isRunning ? "当前回复结束后按顺序发送" : "按顺序发送")
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                Divider()
            }
            ForEach(queued) { message in
                row(message)
            }
        }
        .card()
    }

    private func row(_ message: QueuedMessage) -> some View {
        HStack(spacing: 8) {
            Image(systemName: symbol(message))
                .foregroundStyle(message.hold == .deliveryUncertain ? Color.orange : .secondary)
            Text(label(message))
                .font(.uiMeta.weight(.medium))
                .foregroundStyle(.secondary)
            Text(message.text)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 6)
            // Busy agent: Force Send. Idle with a held message: send just this one.
            if let onRunNow, isRunning || message.hold != nil {
                Button("立即发送") { onRunNow(message.id) }
                    .buttonStyle(.borderless)
                    .help(isRunning ? "Codex 会把它并入正在进行的回复；否则停止当前回复，马上发这条" : "现在发送这一条")
            }
            Button("编辑") { onEdit(message.id) }
                .buttonStyle(.borderless)
                .help("取回到输入框修改")
            if let onMove, queued.count > 1 {
                Menu {
                    moveButtons(message, onMove)
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.button)
                .buttonStyle(.borderless)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("调整顺序")
            }
            Button {
                onWithdraw(message.id)
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .help("撤回")
            .accessibilityLabel("撤回")
        }
        .font(.uiSecondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .help(message.hold == .deliveryUncertain ? "代理可能已经收到这条消息。先看看对话里有没有，再决定是否重新发送。" : "")
        .contextMenu {
            if let onMove, queued.count > 1 { moveButtons(message, onMove) }
            Button("编辑") { onEdit(message.id) }
            Button("撤回", role: .destructive) { onWithdraw(message.id) }
        }
    }

    @ViewBuilder
    private func moveButtons(_ message: QueuedMessage, _ onMove: @escaping (String, Bool) -> Void) -> some View {
        Button("上移一位") { onMove(message.id, true) }
            .disabled(message.id == queued.first?.id)
        Button("下移一位") { onMove(message.id, false) }
            .disabled(message.id == queued.last?.id)
    }

    private func symbol(_ message: QueuedMessage) -> String {
        switch message.hold {
        case .stopped: "pause.circle"
        case .deliveryUncertain: "exclamationmark.triangle"
        case nil: "clock"
        }
    }

    private func label(_ message: QueuedMessage) -> String {
        switch message.hold {
        case .stopped: "已暂停"
        case .deliveryUncertain: "投递未确认"
        case nil: "排队中"
        }
    }
}

private struct MentionSuggestions: View {
    let handles: [String]
    let candidates: [Participant]
    let onPick: (String) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(handles, id: \.self) { handle in
                let participant = candidates.first { $0.handle == handle }
                Button {
                    onPick(handle)
                } label: {
                    HStack(spacing: 5) {
                        if let participant {
                            AgentAvatar(backend: participant.backend, size: 16)
                        } else {
                            Image(systemName: "person.2.fill").font(.uiMeta)
                        }
                        Text("@\(handle)")
                        if let participant {
                            Text(participant.role).foregroundStyle(.secondary)
                        } else {
                            Text("所有成员").foregroundStyle(.secondary)
                        }
                    }
                    .font(.uiSecondary)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            Spacer()
            Text("⇥ 补全").font(.uiMeta).foregroundStyle(.tertiary)
        }
    }
}

/// Chats in the project matching what follows "@".
private struct ChatMentionSuggestions: View {
    let targets: [ConsultTarget]
    let onPick: (ConsultTarget) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(targets) { target in
                Button {
                    onPick(target)
                } label: {
                    HStack(spacing: 5) {
                        if let backend = target.backend {
                            ProviderMark(backend: backend, size: 13)
                        }
                        Text(target.title).lineLimit(1)
                        if target.working {
                            Text("工作中").foregroundStyle(.secondary)
                        }
                    }
                    .font(.uiSecondary)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("问「\(target.title)」：把问题写在后面，回答会显示在这个聊天里")
            }
            Spacer()
            Text("⇥ 补全").font(.uiMeta).foregroundStyle(.tertiary)
        }
    }
}

/// Shown while a message opens with another chat's @: it goes to that chat, not this agent.
private struct DirectAskBar: View {
    let target: ConsultTarget
    @Binding var deep: Bool
    let agentName: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "bubble.left.and.text.bubble.right").foregroundStyle(.secondary)
            Text("直接问「\(target.title)」").font(.uiMeta.weight(.semibold)).lineLimit(1)
            Text("回答显示在这里，不发给 \(agentName)").font(.uiMeta).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 6)
            Picker("询问方式", selection: $deep) {
                Text("基础").tag(false)
                Text("深度").tag(true)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .disabled(!target.allowsDeep)
            .help(target.allowsDeep ? "基础凭它记得的回答；深度让它的只读副本查看文件后回答，较慢"
                                    : "「\(target.title)」只接受基础提问")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.accentColor.opacity(0.08)))
    }
}

/// The chat agent's slash commands and skills matching what the user typed.
private struct CommandSuggestions: View {
    let commands: [ProviderCommand]
    let highlighted: Int
    let note: String?
    let onPick: (ProviderCommand) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(commands.enumerated()), id: \.element.id) { index, command in
                Button {
                    onPick(command)
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: command.kind == .skill ? "sparkles" : command.invocation == "/goal"
                              ? "scope" : "command")
                            .foregroundStyle(.secondary)
                            .frame(width: 16)
                        Text(command.invocation)
                            .font(.uiSecondary.monospaced())
                        if command.label != command.name {
                            Text(command.label).font(.uiSecondary).foregroundStyle(.secondary)
                        }
                        Text(command.detail)
                            .font(.uiMeta)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                        Spacer(minLength: 6)
                        if let scope = command.scope {
                            Tag(text: scope)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .background(
                        RoundedRectangle(cornerRadius: Theme.smallRadius, style: .continuous)
                            .fill(index == highlighted ? Color.accentColor.opacity(0.14) : .clear)
                    )
                }
                .buttonStyle(.plain)
                .help(command.detail)
            }
            HStack {
                if let note {
                    Text(note)
                }
                Spacer()
            }
            .font(.uiMeta)
            .foregroundStyle(.tertiary)
            .padding(.horizontal, 10)
            .padding(.top, commands.isEmpty ? 6 : 4)
            .padding(.bottom, 6)
        }
        .padding(4)
        .card()
    }
}
