import HyadexCore
import SwiftUI

/// The chat's native goal, pinned above the composer while it is set:
/// Claude Code's `/goal` condition or Codex's persistent objective.
struct GoalBar: View {
    let goal: ChatGoal
    /// "@codex" when the goal belongs to a group member.
    var owner: String? = nil
    var busy = false
    let onEdit: () -> Void
    let onPause: () -> Void
    let onResume: () -> Void
    let onClear: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: goal.isOpen ? "scope" : "checkmark.seal")
                .foregroundStyle(goal.activity == .idle ? Color.secondary : goal.activity.tint)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(owner.map { "\($0) 的目标" } ?? "目标")
                        .font(.uiMeta.weight(.semibold))
                        .foregroundStyle(.secondary)
                    StatePill(state: goal.activity, text: goal.statusLabel)
                    if !metrics.isEmpty {
                        Text(metrics)
                            .font(.uiMeta.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                Text(goal.text)
                    .font(.uiSecondary)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .textSelection(.enabled)
                    .help(goal.text)
                if let reason = goal.reason, goal.isOpen {
                    Text(reason)
                        .font(.uiMeta)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(reason)
                }
            }
            Spacer(minLength: 8)
            HStack(spacing: 2) {
                if busy {
                    ProgressView().controlSize(.small).padding(.horizontal, 4)
                }
                if goal.canPause {
                    Button(action: onPause) { Image(systemName: "pause.fill") }
                        .help("暂停目标")
                        .accessibilityLabel("暂停目标")
                } else if goal.canResume {
                    Button(action: onResume) { Image(systemName: "play.fill") }
                        .help("继续目标")
                        .accessibilityLabel("继续目标")
                }
                Button(action: onEdit) { Image(systemName: "pencil") }
                    .help(goal.isOpen ? "修改目标" : "设定新目标")
                    .accessibilityLabel(goal.isOpen ? "修改目标" : "设定新目标")
                Button(action: onClear) { Image(systemName: "xmark") }
                    .help(goal.isOpen ? "清除目标" : "移除")
                    .accessibilityLabel("清除目标")
            }
            .buttonStyle(.borderless)
            .disabled(busy)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .card(tint: goal.isOpen ? goal.activity.tint : nil)
        .frame(maxWidth: Theme.readableWidth)
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity)
    }

    /// "第 3 次检查 · 12 分钟" (Claude) or "182k / 500k token · 21 / 60 分钟" (Codex).
    private var metrics: String {
        var parts: [String] = []
        if let iterations = goal.iterations, iterations > 0 { parts.append("第 \(iterations) 次检查") }
        if let used = goal.tokensUsed, goal.backend == .codex || used > 0 {
            parts.append(goal.tokenBudget.map { "\(used.compact) / \($0.compact) token" } ?? "\(used.compact) token")
        }
        if let seconds = goal.secondsUsed, seconds > 0 || goal.timeBudgetSeconds != nil {
            let minutes = Int((Double(seconds) / 60).rounded())
            parts.append(goal.timeBudgetSeconds.map { "\(minutes) / \($0 / 60) 分钟" } ?? "\(minutes) 分钟")
        }
        return parts.joined(separator: " · ")
    }
}

/// Sets or changes a chat's or a group member's goal.
struct GoalSheet: View {
    @Bindable var model: AppModel
    let target: GoalEditorTarget
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var tokenBudget = ""
    @State private var timeLimit = ""
    @State private var handle = ""

    private var backend: AgentBackend? {
        switch target {
        case .chat(let id): model.chat(id)?.backend
        case .group(let id): model.group(id)?.participants.first { $0.handle == handle }?.backend
        }
    }

    private var current: ChatGoal? {
        switch target {
        case .chat(let id): model.goals[id]
        case .group(let id): model.goal(forGroup: id)?.state
        }
    }

    private var support: GoalSupport {
        switch target {
        case .chat(let id): model.goalSupport(forChat: id)
        case .group(let id):
            model.group(id)?.participants.first { $0.handle == handle }?.sessionID
                .map { model.goalSupport(forChat: $0) } ?? GoalSupport(available: true)
        }
    }

    private var budgetsValid: Bool {
        [tokenBudget, timeLimit].allSatisfy { field in
            let trimmed = field.trimmingCharacters(in: .whitespaces)
            return trimmed.isEmpty || (Int(trimmed) ?? 0) > 0
        }
    }

    private var canSubmit: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.count <= 4000 && budgetsValid
            && support.available
    }

    var body: some View {
        VStack(spacing: 0) {
            Text(current?.isOpen == true ? "修改目标" : "设定目标")
                .font(.uiHeadline)
                .padding(.top, 16)
            Form {
                if case .group(let id) = target, let group = model.group(id) {
                    Picker("成员", selection: $handle) {
                        ForEach(group.participants) { member in
                            Text("@\(member.handle) · \(member.backend.displayName)").tag(member.handle)
                        }
                    }
                }
                Section {
                    TextField("目标", text: $text, prompt: Text(prompt), axis: .vertical)
                        .labelsHidden()
                        .lineLimit(4...10)
                } header: {
                    Text(backend == .codex ? "目标" : "完成条件")
                        .help(explanation)
                }
                if backend == .codex, case .chat = target {
                    Section("预算（可选）") {
                        LabeledContent("token 上限") {
                            TextField("", text: $tokenBudget, prompt: Text("不限"))
                                .labelsHidden()
                                .multilineTextAlignment(.trailing)
                        }
                        LabeledContent("时间上限（分钟）") {
                            TextField("", text: $timeLimit, prompt: Text("不限"))
                                .labelsHidden()
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }
                if let reason = support.reason, !support.available {
                    Label(reason, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            Divider()
            HStack {
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(current?.isOpen == true ? "更新目标" : "开始") {
                    submit()
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
            .padding(14)
        }
        .frame(width: 520)
        .onAppear(perform: load)
    }

    private var prompt: String {
        backend == .codex ? "Codex 要持续完成什么？" : "例如：所有测试通过，且没有新的 lint 警告"
    }

    private var explanation: String {
        backend == .codex
            ? "Codex 的持久目标：它会一轮接一轮地推进，直到完成、暂停或用完预算。"
            : "Claude Code 的 /goal：每次回复后检查这个条件，未满足就继续工作。"
    }

    private func load() {
        switch target {
        case .chat(let id):
            let goal = model.goals[id]
            if goal?.isOpen == true { text = goal?.text ?? "" }
            tokenBudget = goal?.tokenBudget.map(String.init) ?? ""
            timeLimit = goal?.timeBudgetSeconds.map { String($0 / 60) } ?? ""
        case .group(let id):
            guard let group = model.group(id) else { return }
            if let goal = group.goal {
                handle = goal.handle
                if model.goal(forGroup: id)?.state?.isOpen != false { text = goal.objective }
            } else {
                handle = group.defaultResponder
            }
            if !group.participants.contains(where: { $0.handle == handle }) {
                handle = group.participants.first?.handle ?? ""
            }
        }
    }

    private func submit() {
        let number = { (field: String) in Int(field.trimmingCharacters(in: .whitespaces)) }
        switch target {
        case .chat(let id):
            model.setGoal(text, tokenBudget: number(tokenBudget), timeLimitMinutes: number(timeLimit), forChat: id)
        case .group(let id):
            model.setGroupGoal(text, handle: handle, inGroup: id)
        }
    }
}
