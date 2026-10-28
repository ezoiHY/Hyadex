import HyadexCore
import SwiftUI
import UniformTypeIdentifiers

/// Message input shared by chats and groups.
/// Return sends, Shift-Return adds a line, Esc stops a running turn.
/// While an agent is busy, new messages are queued and stay editable.
struct ComposerView<Accessory: View>: View {
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
    @ViewBuilder let accessory: () -> Accessory

    @FocusState private var focused: Bool
    @State private var highlighted = 0
    /// 设置 › 通用: ⌘↩ sends and ↩ adds a line.
    @AppStorage("sendWithCommandReturn") private var sendWithCommandReturn = false
    @State private var picking = false
    @State private var dropTargeted = false

    private var uploading: Bool { attachments?.contains { $0.state == .uploading } ?? false }
    private var hasFiles: Bool { attachments?.contains { $0.fileID != nil } ?? false }
    private var canSend: Bool { (!trimmed.isEmpty || hasFiles) && !uploading }

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
            if !queued.isEmpty {
                QueueStrip(queued: queued, isRunning: isRunning, onEdit: onEditQueued, onWithdraw: onWithdrawQueued,
                           onRunNow: onRunQueuedNow, onMove: onMoveQueued, onResume: onResumeQueue)
            }
            if !suggestions.isEmpty {
                MentionSuggestions(handles: suggestions, candidates: mentionCandidates, onPick: complete)
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
                    .font(.body)
                    .lineLimit(1...10)
                    .focused($focused)
                    .onSubmit(send)
                    .onKeyPress(.return, phases: .down) { press in
                        // Return picks the highlighted command instead of sending "/rev".
                        if let command = highlightedCommand, !press.modifiers.contains(.shift) {
                            complete(command)
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
                    .padding(.top, 2)
                HStack(spacing: 6) {
                    if attachments != nil {
                        Button {
                            picking = true
                        } label: {
                            Image(systemName: "paperclip")
                        }
                        .buttonStyle(.borderless)
                        .help("添加附件（也可以直接拖入文件）")
                        .fileImporter(isPresented: $picking, allowedContentTypes: [.item],
                                      allowsMultipleSelection: true) { result in
                            if case .success(let urls) = result { onAttach(urls) }
                        }
                    }
                    accessory()
                        .menuStyle(.button)
                        .buttonStyle(.borderless)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .foregroundStyle(.secondary)
                    Spacer()
                    if uploading {
                        Text("正在上传…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else if isRunning && !trimmed.isEmpty {
                        Text("将在当前回复后发送")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    sendOrStopButton
                }
                .controlSize(.small)
            }
            .padding(.horizontal, 12)
            .padding(.top, 10)
            .padding(.bottom, 8)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color(nsColor: .textBackgroundColor))
                    .shadow(color: .black.opacity(0.06), radius: 6, y: 2)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(dropTargeted ? Color.accentColor
                                  : focused ? Color.accentColor.opacity(0.5) : Color(nsColor: .separatorColor),
                                  lineWidth: focused || dropTargeted ? 1.5 : 0.5)
            )
            .dropDestination(for: URL.self) { urls, _ in
                guard attachments != nil else { return false }
                let files = urls.filter(\.isFileURL)
                onAttach(files)
                return !files.isEmpty
            } isTargeted: { dropTargeted = attachments != nil && $0 }
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
                    .font(.system(size: 10, weight: .bold))
                    .frame(width: 24, height: 24)
                    .background(Circle().fill(Color.primary.opacity(0.85)))
                    .foregroundStyle(Color(nsColor: .textBackgroundColor))
            }
            .buttonStyle(.plain)
            .help("停止（Esc）")
            .accessibilityLabel("停止")
        } else {
            Button(action: send) {
                Image(systemName: isRunning ? "text.line.last.and.arrowtriangle.forward" : "arrow.up")
                    .font(.system(size: 12, weight: .bold))
                    .frame(width: 24, height: 24)
                    .background(Circle().fill(canSend ? Color.accentColor : Color.secondary.opacity(0.25)))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
            .disabled(!canSend)
            // ⌘↩ must be a key equivalent: the text view would take it before onKeyPress.
            .keyboardShortcut(sendWithCommandReturn ? KeyboardShortcut(.return, modifiers: .command) : nil)
            .help(isRunning ? "加入队列（\(sendWithCommandReturn ? "⌘↩" : "↩")）" : "发送（\(sendWithCommandReturn ? "⌘↩" : "↩")）")
            .accessibilityLabel(isRunning ? "加入队列" : "发送")
        }
    }

    private func send() {
        guard canSend else { return }
        onSend(draft)
        draft = ""
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
                        .font(.caption)
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
                .font(.caption.weight(.medium))
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
        .font(.callout)
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
                            Image(systemName: "person.2.fill").font(.caption)
                        }
                        Text("@\(handle)")
                        if let participant {
                            Text(participant.role).foregroundStyle(.secondary)
                        } else {
                            Text("所有成员").foregroundStyle(.secondary)
                        }
                    }
                    .font(.callout)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            Spacer()
            Text("⇥ 补全").font(.caption).foregroundStyle(.tertiary)
        }
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
                            .font(.callout.monospaced())
                        if command.label != command.name {
                            Text(command.label).font(.callout).foregroundStyle(.secondary)
                        }
                        Text(command.detail)
                            .font(.caption)
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
                if !commands.isEmpty {
                    Text("↑↓ 选择 · ↩ 或 ⇥ 补全 · Esc 取消")
                }
            }
            .font(.caption)
            .foregroundStyle(.tertiary)
            .padding(.horizontal, 10)
            .padding(.top, commands.isEmpty ? 6 : 4)
            .padding(.bottom, 6)
        }
        .padding(4)
        .card()
    }
}
