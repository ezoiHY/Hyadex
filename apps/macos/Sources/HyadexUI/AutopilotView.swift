import HyadexCore
import SwiftUI

struct AutopilotView: View {
    @Bindable var model: AppModel
    let runID: String
    @State private var showFeedback = false
    @State private var confirmAbort = false

    var body: some View {
        if let run = model.run(runID) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    RunHeader(run: run)
                    PhaseStepper(run: run)
                    ActionBanner(run: run,
                                 onApprove: { model.approve(run: runID) },
                                 onFeedback: { showFeedback = true },
                                 onResume: { model.resume(run: runID) })
                    PlanSection(run: run, onOpenFile: model.openFile)
                    ActivitySection(notes: run.notes)
                }
                .frame(maxWidth: Theme.readableWidth, alignment: .leading)
                .padding(.horizontal, 28)
                .padding(.vertical, 22)
                .frame(maxWidth: .infinity)
            }
            .navigationTitle(run.title)
            .navigationSubtitle("自动接力 · \(run.status.label)")
            .toolbar {
                ToolbarItemGroup {
                    if run.status == .paused {
                        Button { model.resume(run: runID) } label: { Label("继续", systemImage: "play.fill") }
                            .help("继续运行")
                    } else {
                        Button { model.pause(run: runID) } label: { Label("暂停", systemImage: "pause.fill") }
                            .help("当前步骤完成后暂停")
                            .disabled(!run.status.isActive)
                    }
                    Button { showFeedback = true } label: { Label("反馈", systemImage: "text.bubble") }
                        .help("给执行者或规划者补充要求")
                        .disabled(run.status == .done || run.status == .aborted)
                    Button(role: .destructive) { confirmAbort = true } label: { Label("中止", systemImage: "stop.circle") }
                        .help("立即停止；worktree 和分支会保留")
                        .disabled(run.status == .done || run.status == .aborted)
                }
            }
            .confirmationDialog("中止“\(run.title)”？", isPresented: $confirmAbort) {
                Button("中止", role: .destructive) { model.abort(run: runID) }
            } message: {
                Text("正在运行的代理和验证会立即停止。已提交的任务、worktree 和分支都会保留。")
            }
            .sheet(isPresented: $showFeedback) {
                FeedbackSheet(run: run) { model.feedback(run: runID, text: $0) }
            }
        }
    }
}

private struct RunHeader: View {
    let run: AutopilotRun

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(run.goal)
                .font(.title3)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                RoleTag(role: "规划", backend: run.planner)
                Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.tertiary)
                RoleTag(role: "执行", backend: run.executor)
                Divider().frame(height: 14)
                Tag(text: run.approval.label, symbol: "checkmark.seal")
                    .help(run.approval.explanation)
                Tag(text: run.isolation.label, symbol: "arrow.triangle.branch")
                    .help(run.isolation.explanation)
                Tag(text: run.reviewer.label, symbol: "eye")
                    .help(run.reviewer.explanation)
            }
            HStack(spacing: 14) {
                if let branch = run.branch {
                    Label(branch, systemImage: "arrow.triangle.branch")
                }
                Label(run.verifyCommand, systemImage: "checkmark.circle")
                    .help("每一步完成后由服务器运行的验证命令")
                Label(run.cwd, systemImage: "folder")
            }
            .font(.system(.caption, design: .monospaced))
            .foregroundStyle(.secondary)
            .lineLimit(1)
        }
    }
}

private struct RoleTag: View {
    let role: String
    let backend: AgentBackend

    var body: some View {
        HStack(spacing: 4) {
            AgentAvatar(backend: backend, size: 16)
            Text("\(role) · \(backend.displayName)")
                .font(.caption.weight(.medium))
        }
    }
}

/// Where the run is in its loop. The task loop (execute → verify → review)
/// repeats per task, so its steps show the current task number.
private struct PhaseStepper: View {
    let run: AutopilotRun

    private let phases = ["规划", "批准", "执行", "验证", "审查", "完成"]

    private var current: Int {
        switch run.status {
        case .planning: 0
        case .awaitingApproval: 1
        case .executing, .paused, .needsAttention: 2
        case .verifying: 3
        case .reviewing: 4
        case .done, .aborted: 5
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(phases.enumerated()), id: \.offset) { index, phase in
                HStack(spacing: 6) {
                    ZStack {
                        Circle()
                            .fill(fill(index))
                            .frame(width: 22, height: 22)
                        if index < current || run.status == .done {
                            Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
                        } else if index == current && run.status.isActive {
                            ProgressView().controlSize(.mini).tint(.white)
                        } else {
                            Text("\(index + 1)").font(.caption2.weight(.semibold))
                                .foregroundStyle(index == current ? Color.white : Color.secondary)
                        }
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        Text(phase)
                            .lineLimit(1)
                            .font(.callout.weight(index == current ? .semibold : .regular))
                            .foregroundStyle(index <= current ? Color.primary : Color.secondary)
                        if (2...4).contains(index), index == current, !run.tasks.isEmpty, run.status != .done {
                            Text("任务 \(min(run.completedTasks + 1, run.tasks.count))/\(run.tasks.count)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                if index < phases.count - 1 {
                    Rectangle()
                        .fill(index < current ? Color.accentColor.opacity(0.6) : Color.secondary.opacity(0.2))
                        .frame(height: 1.5)
                        .frame(minWidth: 4)
                        .padding(.horizontal, 4)
                }
            }
        }
        .padding(14)
        .card()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("当前阶段：\(phases[current])，\(run.status.label)")
    }

    private func fill(_ index: Int) -> Color {
        if run.status == .done || index < current { return .accentColor }
        if index == current {
            switch run.status.activity {
            case .needsAction: return .orange
            case .failed: return .red
            case .paused: return .yellow
            default: return .accentColor
            }
        }
        return Color.secondary.opacity(0.15)
    }
}

/// The one thing the user should do next, if anything.
private struct ActionBanner: View {
    let run: AutopilotRun
    let onApprove: () -> Void
    let onFeedback: () -> Void
    let onResume: () -> Void

    var body: some View {
        switch run.status {
        case .awaitingApproval:
            banner(tint: .orange, symbol: "hand.raised.fill",
                   title: run.completedTasks == 0 ? "计划已就绪，等你批准" : "这一步已通过审查，等你批准",
                   detail: run.completedTasks == 0
                    ? "批准后 \(run.executor.displayName) 会按顺序执行 \(run.tasks.count) 个任务，每一步都经过验证和审查。"
                    : "批准后提交这一步并继续下一个任务。") {
                Button("提出修改…", action: onFeedback)
                Button("批准并开始", action: onApprove)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.return, modifiers: .command)
                    .help("批准（⌘↩）")
            }
        case .needsAttention:
            banner(tint: .orange, symbol: "exclamationmark.triangle.fill", title: "需要你处理",
                   detail: "运行遇到了需要你判断的情况。") {
                Button("提出修改…", action: onFeedback)
                Button("重试", action: onResume).buttonStyle(.borderedProminent)
            }
        case .paused:
            banner(tint: .yellow, symbol: "pause.circle.fill", title: "已暂停", detail: "当前步骤已完成，继续后从下一步开始。") {
                Button("继续", action: onResume).buttonStyle(.borderedProminent)
            }
        case .done:
            banner(tint: .green, symbol: "checkmark.circle.fill", title: "全部完成",
                   detail: run.branch.map { "分支 \($0) 上有 \(run.tasks.compactMap(\.commit).count) 个提交，等你审阅后合并。" }
                    ?? "改动已写入工作目录。") {
                if let branch = run.branch {
                    Button("拷贝分支名") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(branch, forType: .string)
                    }
                }
            }
        case .aborted:
            banner(tint: .red, symbol: "stop.circle.fill", title: "已中止", detail: "worktree 和分支已保留。") { EmptyView() }
        default:
            EmptyView()
        }
    }

    private func banner<Actions: View>(tint: Color, symbol: String, title: String, detail: String,
                                       @ViewBuilder actions: () -> Actions) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: symbol)
                .font(.title2)
                .foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            actions()
        }
        .padding(14)
        .card(tint: tint)
    }
}

private struct PlanSection: View {
    let run: AutopilotRun
    let onOpenFile: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("计划").font(.headline)
                Spacer()
                if !run.tasks.isEmpty {
                    Text("\(run.completedTasks)/\(run.tasks.count) 已完成")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if run.tasks.isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("\(run.planner.displayName) 正在阅读代码并制定计划…").foregroundStyle(.secondary)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .card()
            } else {
                if !run.planSummary.isEmpty {
                    Text(run.planSummary).foregroundStyle(.secondary)
                }
                VStack(spacing: 0) {
                    ForEach(Array(run.tasks.enumerated()), id: \.element.id) { index, task in
                        if index > 0 { Divider().padding(.leading, 44) }
                        TaskRow(number: index + 1, task: task,
                                startExpanded: run.status == .awaitingApproval
                                    || [.executing, .verifying, .reviewing, .revising].contains(task.state),
                                onOpenFile: onOpenFile)
                    }
                }
                .card()
            }
        }
    }
}

private struct TaskRow: View {
    let number: Int
    let task: AutopilotTask
    let onOpenFile: (String) -> Void
    @State private var expanded: Bool

    init(number: Int, task: AutopilotTask, startExpanded: Bool, onOpenFile: @escaping (String) -> Void) {
        self.number = number
        self.task = task
        self.onOpenFile = onOpenFile
        _expanded = State(initialValue: startExpanded) // one-time seed; the user toggles afterwards
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    statusIcon.frame(width: 22)
                    Text("\(number). \(task.title)")
                        .fontWeight(.medium)
                        .strikethrough(false)
                    if task.revisions > 0 {
                        Tag(text: "第 \(task.revisions + 1) 次", symbol: "arrow.uturn.backward")
                            .help("审查要求修改过 \(task.revisions) 次")
                    }
                    Spacer()
                    Text(stateLabel).font(.caption).foregroundStyle(stateColor)
                    if let commit = task.commit {
                        Text(commit).font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    if !task.detail.isEmpty { Text(task.detail) }
                    if !task.acceptance.isEmpty {
                        LabeledContent("验收标准") { Text(task.acceptance) }
                    }
                    HStack(spacing: 5) {
                        ForEach(task.files, id: \.self) { file in
                            Tag(text: file, symbol: "doc")
                        }
                    }
                    if let verification = task.verification {
                        HStack(spacing: 6) {
                            Image(systemName: verification.passed ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundStyle(verification.passed ? .green : .red)
                            Text(verification.passed ? "验证通过" : "验证失败")
                            Text(verification.summary).foregroundStyle(.secondary).lineLimit(1)
                            if verification.duration >= 1 {
                                Text("· \(Int(verification.duration)) 秒").foregroundStyle(.tertiary)
                            }
                        }
                    } else if task.state == .verifying {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini)
                            Text("正在运行验证").foregroundStyle(.secondary)
                        }
                    }
                    if let note = task.reviewNote {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Image(systemName: "eye").foregroundStyle(.secondary)
                            Text("审查：").foregroundStyle(.secondary)
                            Text(note)
                        }
                    }
                    if let changes = task.changes, let first = changes.files.first {
                        Button {
                            onOpenFile(first.path)
                        } label: {
                            HStack(spacing: 6) {
                                Text("查看 diff")
                                DiffStat(added: changes.added, removed: changes.removed)
                            }
                        }
                        .buttonStyle(.link)
                    }
                }
                .font(.callout)
                .padding(.leading, 32)
                .transition(.opacity)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    @ViewBuilder private var statusIcon: some View {
        switch task.state {
        case .approved:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).font(.title3)
        case .executing, .verifying, .reviewing, .revising:
            ProgressView().controlSize(.small)
        case .blocked:
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange).font(.title3)
        case .pending:
            Image(systemName: "circle").foregroundStyle(.tertiary).font(.title3)
        }
    }

    private var stateLabel: String {
        switch task.state {
        case .pending: "待执行"
        case .executing: "执行中"
        case .verifying: "验证中"
        case .reviewing: "审查中"
        case .revising: "修改中"
        case .approved: "已提交"
        case .blocked: "受阻"
        }
    }

    private var stateColor: Color {
        switch task.state {
        case .approved: .green
        case .blocked: .orange
        case .pending: .secondary
        default: .accentColor
        }
    }
}

private struct ActivitySection: View {
    let notes: [TimelineItem]

    var body: some View {
        if !notes.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("动态").font(.headline)
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(notes.reversed()) { note in
                        if case .message(_, let author, let text, let time, _, _, _) = note {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(time.formatted(date: .omitted, time: .shortened))
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.tertiary)
                                    .frame(width: 44, alignment: .leading)
                                Image(systemName: author.kind == .user ? "person.fill" : "gearshape.2")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text(text).font(.callout)
                            }
                        }
                    }
                }
            }
        }
    }
}

private struct FeedbackSheet: View {
    let run: AutopilotRun
    let onSubmit: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("提出修改").font(.title3.weight(.semibold))
            Text(run.completedTasks == 0
                 ? "你的意见会交给 \(run.planner.displayName) 修改计划。"
                 : "你的意见会交给 \(run.executor.displayName) 修改当前任务；已完成的任务会保留。")
                .foregroundStyle(.secondary)
            TextEditor(text: $text)
                .font(.body)
                .frame(minHeight: 120)
                .scrollContentBackground(.hidden)
                .padding(6)
                .card()
            HStack {
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("发送") {
                    onSubmit(text)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}
