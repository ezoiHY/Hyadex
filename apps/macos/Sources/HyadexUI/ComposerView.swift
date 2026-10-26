import HyadexCore
import SwiftUI

/// Message input shared by chats and groups.
/// Return sends, Shift-Return adds a line, Esc stops a running turn.
/// While an agent is busy, new messages are queued and stay editable.
struct ComposerView<Accessory: View>: View {
    @Binding var draft: String
    let placeholder: String
    let isRunning: Bool
    let queued: [QueuedMessage]
    var mentionCandidates: [Participant] = []
    let onSend: (String) -> Void
    let onStop: () -> Void
    let onEditQueued: (String) -> Void
    let onWithdrawQueued: (String) -> Void
    @ViewBuilder let accessory: () -> Accessory

    @FocusState private var focused: Bool

    private var trimmed: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var suggestions: [String] {
        guard !mentionCandidates.isEmpty, let partial = MentionParser.partialMention(at: draft) else { return [] }
        let handles = mentionCandidates.map(\.handle) + ["all"]
        return handles.filter { partial.isEmpty || $0.hasPrefix(partial) }
    }

    var body: some View {
        VStack(spacing: 8) {
            if !queued.isEmpty {
                QueueStrip(queued: queued, onEdit: onEditQueued, onWithdraw: onWithdrawQueued)
            }
            if !suggestions.isEmpty {
                MentionSuggestions(handles: suggestions, candidates: mentionCandidates, onPick: complete)
            }
            VStack(alignment: .leading, spacing: 8) {
                TextField(placeholder, text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .lineLimit(1...10)
                    .focused($focused)
                    .onSubmit(send)
                    .onKeyPress(.return, phases: .down) { press in
                        guard press.modifiers.contains(.shift) else { return .ignored }
                        draft.append("\n")
                        return .handled
                    }
                    .onKeyPress(.tab) {
                        guard let first = suggestions.first else { return .ignored }
                        complete(first)
                        return .handled
                    }
                    .onKeyPress(.escape) {
                        guard isRunning else { return .ignored }
                        onStop()
                        return .handled
                    }
                    .padding(.top, 2)
                HStack(spacing: 6) {
                    Button {
                    } label: {
                        Image(systemName: "paperclip")
                    }
                    .buttonStyle(.borderless)
                    .help("添加附件（也可以直接拖入文件）")
                    accessory()
                        .menuStyle(.button)
                        .buttonStyle(.borderless)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .foregroundStyle(.secondary)
                    Spacer()
                    if isRunning && !trimmed.isEmpty {
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
                    .strokeBorder(focused ? Color.accentColor.opacity(0.5) : Color(nsColor: .separatorColor),
                                  lineWidth: focused ? 1.5 : 0.5)
            )
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
        if isRunning && trimmed.isEmpty {
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
                    .background(Circle().fill(trimmed.isEmpty ? Color.secondary.opacity(0.25) : Color.accentColor))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
            .disabled(trimmed.isEmpty)
            .help(isRunning ? "加入队列（↩）" : "发送（↩）")
            .accessibilityLabel(isRunning ? "加入队列" : "发送")
        }
    }

    private func send() {
        guard !trimmed.isEmpty else { return }
        onSend(draft)
        draft = ""
    }

    private func complete(_ handle: String) {
        guard let at = draft.lastIndex(of: "@") else { return }
        draft = String(draft[..<at]) + "@\(handle) "
    }
}

private struct QueueStrip: View {
    let queued: [QueuedMessage]
    let onEdit: (String) -> Void
    let onWithdraw: (String) -> Void

    var body: some View {
        VStack(spacing: 0) {
            ForEach(queued) { message in
                HStack(spacing: 8) {
                    Image(systemName: "clock")
                        .foregroundStyle(.secondary)
                    Text("排队中")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                    Text(message.text)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer()
                    Button("编辑") { onEdit(message.id) }
                        .buttonStyle(.borderless)
                        .help("取回到输入框修改")
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
            }
        }
        .card()
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
