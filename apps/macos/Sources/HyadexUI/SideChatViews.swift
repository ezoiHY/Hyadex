import HyadexCore
import HyadexRichText
import SwiftUI

/// Quick questions about the conversation, beside it: they neither
/// interrupt the agent nor enter its transcript (Claude Code's `/btw`;
/// Codex answers from a fork of its thread).
struct SideChatPanel: View {
    @Bindable var model: AppModel
    let chatID: String
    @State private var question = ""
    @FocusState private var focused: Bool

    private var side: SideChat? { model.sideChats[chatID] }
    private var backend: AgentBackend? { model.chat(chatID)?.backend }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("侧问").font(.uiHeadline)
                Text("不打断代理，也不写进对话")
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("清空") { model.clearSide(chatID) }
                    .buttonStyle(.borderless)
                    .disabled(side?.exchanges.isEmpty ?? true)
                    .help(backend == .codex ? "开始新的侧问，Codex 会基于对话的最新内容回答" : "开始新的侧问")
                Button {
                    model.sideChatOpen = nil
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .semibold))
                        .frame(width: 24, height: 24)
                        .background(Circle().fill(Color.primary.opacity(0.08)))
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.cancelAction)
                .help("关闭（Esc）")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if side?.exchanges.isEmpty ?? true {
                        Text(intro)
                            .font(.uiSecondary)
                            .foregroundStyle(.secondary)
                            .padding(.top, 4)
                    }
                    ForEach(side?.exchanges ?? []) { exchange in
                        ExchangeView(exchange: exchange) { model.cancelSide(exchange.requestID, inChat: chatID) }
                    }
                    if let note = side?.contextNote, !(side?.exchanges.isEmpty ?? true) {
                        Text(note).font(.uiMeta).foregroundStyle(.tertiary)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // Like a chat: start at the newest answer and follow new ones.
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.bottom, for: .sizeChanges)
            .defaultScrollAnchor(.top, for: .alignment)
            if let error = model.sideChatErrors[chatID] {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.uiMeta)
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack(alignment: .bottom, spacing: 8) {
                TextField("问点什么（↩ 发送）", text: $question, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...5)
                    .focused($focused)
                    .onSubmit(send)
                    .disabled(side?.isRunning == true)
                Button(action: send) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 11, weight: .bold))
                        .frame(width: 22, height: 22)
                        .background(Circle().fill(canSend ? Color.accentColor : Color.secondary.opacity(0.25)))
                        .foregroundStyle(.white)
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .help("发送侧问")
            }
            .padding(12)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(alignment: .leading) { Divider() }
        .shadow(color: .black.opacity(0.18), radius: 18, x: -4)
        .onAppear {
            focused = true
            model.refreshSide(chatID)
        }
    }

    private var canSend: Bool {
        !question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && side?.isRunning != true
    }

    private var intro: String {
        backend == .codex
            ? "问 Codex 一个和这段对话有关的问题。它从对话的副本里回答，主对话不受影响。"
            : "问 Claude 一个和这段对话有关的问题（同 Claude Code 的 /btw）。它能看到对话内容，但不会调用工具，回答也不会进入对话。"
    }

    private func send() {
        guard canSend else { return }
        model.askSide(question, inChat: chatID)
        question = ""
    }
}

private struct ExchangeView: View {
    let exchange: SideChat.Exchange
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(exchange.question)
                .font(.uiSecondary.weight(.medium))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            switch exchange.status {
            case .running:
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("正在回答…").font(.uiMeta).foregroundStyle(.secondary)
                    Spacer()
                    Button("取消", action: onCancel).buttonStyle(.borderless).font(.uiMeta)
                }
            case .completed:
                RichMarkdown(exchange.answer ?? "")
                    .textSelection(.enabled)
            case .cancelled:
                Text("已取消").font(.uiMeta).foregroundStyle(.secondary)
            case .interrupted:
                Text("服务器重启时中断了，请重新提问。").font(.uiMeta).foregroundStyle(.secondary)
            case .failed:
                Text("没有得到回答" + (exchange.error.map { "（\($0)）" } ?? ""))
                    .font(.uiMeta)
                    .foregroundStyle(.orange)
            }
        }
        .padding(10)
        .card()
    }
}
