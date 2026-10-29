import HyadexCore
import SwiftUI

/// What a consult card needs from its chat: the live record behind an `ask`
/// call, and the approval and Stop actions.
struct ConsultActions {
    var record: (ConsultCall) -> ConsultRecord? = { _ in nil }
    var decide: (_ consultID: String, _ decision: String) -> Void = { _, _ in }
    var cancel: (_ consultID: String) -> Void = { _ in }
    /// Puts an answer the user asked for into the composer, quoted, for this chat's agent.
    var forward: (_ answer: String, _ from: String) -> Void = { _, _ in }
    var agentName = "代理"
}

extension EnvironmentValues {
    @Entry var consultActions = ConsultActions()
}

/// 基础 or 深度, as a small tag.
struct ConsultModeBadge: View {
    let mode: ConsultMode

    var body: some View {
        Text(mode.label)
            .font(.uiSmall.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .foregroundStyle(mode == .deep ? Color.accentColor : .secondary)
            .background(Capsule().fill((mode == .deep ? Color.accentColor : Color.secondary).opacity(0.12)))
            .help(mode == .deep ? "对方的只读副本查看它的文件后回答" : "对方凭它已经知道的回答，不调用工具")
    }
}

/// A question this chat's agent asked another chat: whom, how, its progress
/// while it is answered, and the answer.
struct ConsultCard: View {
    let call: ConsultCall
    @Environment(\.consultActions) private var actions
    @State private var showSteps = false
    @State private var showQuestion = false

    var body: some View {
        let record = actions.record(call)
        let status = status(record)
        let title = record?.targetTitle ?? call.receipt?.chatTitle ?? call.target
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 7) {
                if let backend = record?.targetBackend {
                    ProviderMark(backend: backend, size: 15)
                } else {
                    Image(systemName: "bubble.left.and.text.bubble.right").foregroundStyle(.secondary)
                }
                Text(call.byUser ? "你问「\(title)」" : "询问「\(title)」").font(.uiSecondary.weight(.semibold)).lineLimit(1)
                ConsultModeBadge(mode: call.deep ? .deep : .basic)
                Spacer(minLength: 6)
                if status.isActive && status != .awaitingApproval {
                    ProgressView().controlSize(.mini)
                }
                Text(statusText(status, record: record))
                    .font(.uiMeta)
                    .foregroundStyle(status == .awaitingApproval ? Color.orange : .secondary)
            }
            if !call.question.isEmpty {
                Text(call.question)
                    .font(.uiSecondary)
                    .foregroundStyle(.secondary)
                    .lineLimit(showQuestion ? nil : 2)
                    .textSelection(.enabled)
                    .onTapGesture { showQuestion.toggle() }
                    .help(showQuestion ? "" : "点按展开问题")
            }
            if status == .awaitingApproval, let record {
                approval(record)
            }
            if let record, !record.steps.isEmpty, status.isActive || showSteps {
                steps(record, live: status.isActive)
            }
            if status == .completed, let answer = call.receipt?.answer ?? record?.answer {
                MessageText(text: answer)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if (call.receipt?.confident ?? record?.confident) == false {
                    Label(call.deep ? "「\(title)」对这个回答把握不大" : "「\(title)」把握不大，可以让它深度查看",
                          systemImage: "questionmark.circle")
                        .font(.uiMeta)
                        .foregroundStyle(.secondary)
                }
            } else if !status.isActive, let problem = problem(record) {
                Text(problem).font(.uiMeta).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
            }
            footer(record, status: status)
        }
        .padding(12)
        .card(tint: status == .awaitingApproval ? .orange : nil)
        .frame(maxWidth: 680, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("询问\(title)：\(call.question)")
    }

    /// Why there is no answer: the server's reason, or what the helper printed
    /// when it never reached a consult (a refused or interrupted tool call).
    private func problem(_ record: ConsultRecord?) -> String? {
        if let record { return record.problem }
        return call.receipt?.error
    }

    /// The live record while the call runs; once it returned, its own receipt.
    private func status(_ record: ConsultRecord?) -> ConsultStatus {
        if call.running || call.byUser { return record?.status ?? .queued }
        if let status = call.receipt?.status { return status }
        if call.receipt?.ok == true { return .completed }
        return record.map(\.status).flatMap { $0.isActive ? nil : $0 } ?? .failed
    }

    private func statusText(_ status: ConsultStatus, record: ConsultRecord?) -> String {
        if status == .running, call.deep, let count = record?.steps.count, count > 0 {
            return "正在查看 · \(count) 步"
        }
        if status == .completed, let duration = record?.duration {
            return "已回答 · \(duration < 60 ? "\(Int(duration.rounded())) 秒" : "\(Int(duration / 60)) 分钟")"
        }
        return status.label
    }

    private func approval(_ record: ConsultRecord) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("深度询问会让「\(record.targetTitle)」的只读副本查看它的文件再回答，比基础询问慢，也更费 token。")
                .font(.uiMeta)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("拒绝") { actions.decide(record.id, "deny") }
                Spacer()
                Button("总是允许询问「\(record.targetTitle)」") { actions.decide(record.id, "always") }
                    .help("这个聊天以后深度询问「\(record.targetTitle)」时不再问你")
                Button("允许") { actions.decide(record.id, "allow") }
                    .buttonStyle(.borderedProminent)
                    .tint(.orange)
            }
            .controlSize(.regular)
        }
    }

    private func steps(_ record: ConsultRecord, live: Bool) -> some View {
        let shown = live ? Array(record.steps.suffix(3)) : record.steps
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(shown.enumerated()), id: \.offset) { _, step in
                Label {
                    Text(step.title).lineLimit(1).truncationMode(.middle)
                } icon: {
                    Image(systemName: step.symbol).frame(width: 14)
                }
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
            }
        }
        .padding(.leading, 2)
    }

    @ViewBuilder
    private func footer(_ record: ConsultRecord?, status: ConsultStatus) -> some View {
        let tokens = [record?.inputTokens.map { "输入 \(Self.compact($0))" }, record?.outputTokens.map { "输出 \(Self.compact($0))" }]
            .compactMap { $0 }
        if status.isActive && status != .awaitingApproval, let record {
            HStack {
                Spacer()
                Button("停止") { actions.cancel(record.id) }
                    .buttonStyle(.borderless)
                    .font(.uiMeta)
            }
        } else if !status.isActive, record?.steps.isEmpty == false || !tokens.isEmpty || forwardable(record, status) != nil {
            HStack(spacing: 10) {
                if let count = record?.steps.count, count > 0 {
                    Button(showSteps ? "收起查看过程" : "查看过程 · \(count) 步") { showSteps.toggle() }
                        .buttonStyle(.borderless)
                }
                if let answer = forwardable(record, status), let record {
                    Button("转给 \(actions.agentName)") { actions.forward(answer, record.targetTitle) }
                        .buttonStyle(.borderless)
                        .help("把这个回答引用到输入框，接着写要 \(actions.agentName) 做什么")
                }
                Spacer()
                if !tokens.isEmpty { Text(tokens.joined(separator: " · ") + " token") }
            }
            .font(.uiMeta)
            .foregroundStyle(.tertiary)
        }
    }

    /// An answer the user asked for: theirs to pass on to this chat's agent.
    private func forwardable(_ record: ConsultRecord?, _ status: ConsultStatus) -> String? {
        guard call.byUser, status == .completed else { return nil }
        return record?.answer
    }

    static func compact(_ value: Int) -> String {
        value >= 1000 ? String(format: "%.1fk", Double(value) / 1000) : "\(value)"
    }
}

/// A question this chat was asked (by another chat, or by you in depth), in its side panel.
struct IncomingConsultRow: View {
    let record: ConsultRecord
    let onCancel: () -> Void
    @State private var showSteps = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: record.sourceID == nil ? "person.crop.circle" : "arrow.down.left.circle")
                    .foregroundStyle(.secondary)
                Text(record.sourceID == nil ? "你问" : "「\(record.sourceTitle ?? "聊天")」问")
                    .font(.uiMeta.weight(.medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                ConsultModeBadge(mode: record.mode)
                Spacer()
                if record.status.isActive && record.status != .awaitingApproval {
                    ProgressView().controlSize(.mini)
                }
                Text(record.status == .running && !record.steps.isEmpty
                     ? "正在查看 · \(record.steps.count) 步" : record.status.label)
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
            }
            Text(record.question)
                .font(.uiSecondary.weight(.medium))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let answer = record.answer, record.status == .completed {
                MessageText(text: answer)
            } else if let problem = record.problem {
                Text(problem).font(.uiMeta).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                if !record.steps.isEmpty {
                    Button(showSteps ? "收起查看过程" : "查看过程 · \(record.steps.count) 步") { showSteps.toggle() }
                        .buttonStyle(.borderless)
                }
                Spacer()
                if record.status.isActive && record.status != .awaitingApproval {
                    Button("停止", action: onCancel).buttonStyle(.borderless)
                }
            }
            .font(.uiMeta)
            if showSteps {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(record.steps.enumerated()), id: \.offset) { _, step in
                        Label(step.title, systemImage: step.symbol)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
    }
}
