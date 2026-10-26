import HyadexCore
import SwiftUI

// MARK: - Jobs

struct JobsView: View {
    @Bindable var model: AppModel
    @State private var selection: ScheduledJob.ID?

    var body: some View {
        Table(model.jobs, selection: $selection) {
            TableColumn("启用") { job in
                Toggle("", isOn: Binding(get: { job.enabled }, set: { model.setJob(job.id, enabled: $0) }))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .accessibilityLabel("启用 \(job.title)")
            }
            .width(44)
            TableColumn("名称") { job in
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.title).fontWeight(.medium)
                    Text(job.prompt).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                .padding(.vertical, 3)
            }
            .width(min: 180, ideal: 260)
            TableColumn("发到") { job in
                Text(job.target).foregroundStyle(.secondary).lineLimit(1)
            }
            .width(min: 120, ideal: 200)
            TableColumn("计划") { job in
                Text(job.schedule)
            }
            .width(min: 90, ideal: 110)
            TableColumn("下次运行") { job in
                if job.enabled, let next = job.nextRun {
                    Text(next, format: .relative(presentation: .named))
                } else {
                    Text("—").foregroundStyle(.tertiary)
                }
            }
            .width(min: 80, ideal: 100)
            TableColumn("上次结果") { job in
                if let result = job.lastResult {
                    StatePill(state: result, text: result == .done ? "成功" : result.label)
                } else {
                    Text("从未运行").foregroundStyle(.tertiary)
                }
            }
            .width(min: 80, ideal: 90)
        }
        .tableStyle(.inset)
        .alternatingRowBackgrounds()
        .navigationTitle("定时任务")
        .navigationSubtitle("\(model.jobs.filter(\.enabled).count) 个已启用")
        .toolbar {
            ToolbarItemGroup {
                Button { selection.map(model.runJobNow) } label: { Label("立即运行", systemImage: "play") }
                    .disabled(selection == nil)
                    .help("立即运行选中的任务")
                Button {} label: { Label("新建定时任务", systemImage: "plus") }
                    .help("新建定时任务")
            }
        }
    }
}

// MARK: - Changes panel and details

/// Changed files and their diffs, sliding over the conversation from the
/// right. It opens from a change card, a receipt, a task's "查看 diff" or the
/// toolbar, and closes with Esc; the conversation keeps its full width.
struct ChangesPanel: View {
    @Bindable var model: AppModel

    var body: some View {
        let changes = model.selectedChanges
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("改动").font(.headline)
                if !changes.files.isEmpty {
                    Text("\(changes.files.count) 个文件").foregroundStyle(.secondary)
                    DiffStat(added: changes.added, removed: changes.removed)
                }
                Spacer()
                Button {
                    model.showChanges = false
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
            ChangesInspector(model: model)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(alignment: .leading) { Divider() }
        .shadow(color: .black.opacity(0.18), radius: 18, x: -4)
    }
}

/// Settings and usage of the selection, in a toolbar popover.
struct DetailsPopover: View {
    @Bindable var model: AppModel

    var body: some View {
        DetailsInspector(model: model)
            .frame(width: 320)
    }
}

/// A quiet, top-aligned note for an empty inspector pane.
private struct InspectorEmptyNote: View {
    let symbol: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.title3)
                .foregroundStyle(.tertiary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.callout.weight(.medium)).foregroundStyle(.secondary)
                Text(detail).font(.caption).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct ChangesInspector: View {
    @Bindable var model: AppModel

    var body: some View {
        let changes = model.selectedChanges
        if changes.files.isEmpty {
            InspectorEmptyNote(symbol: "doc.text.magnifyingglass", title: "还没有改动",
                               detail: "代理修改的文件和 diff 会出现在这里。")
        } else {
            let selectedPath = model.selectedFilePath ?? changes.files[0].path
            VStack(spacing: 0) {
                List(selection: Binding(get: { selectedPath }, set: { model.selectedFilePath = $0 })) {
                    Section {
                        ForEach(changes.files) { file in
                            HStack(spacing: 6) {
                                Image(systemName: "doc").foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 0) {
                                    Text((file.path as NSString).lastPathComponent).lineLimit(1)
                                    Text((file.path as NSString).deletingLastPathComponent)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                        .truncationMode(.head)
                                }
                                Spacer()
                                DiffStat(added: file.added, removed: file.removed)
                            }
                            .tag(file.path)
                        }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .frame(height: min(CGFloat(changes.files.count) * 44 + 8, 200))
                Divider()
                if let file = changes.files.first(where: { $0.path == selectedPath }) {
                    HStack {
                        Text(file.path).font(.caption.monospaced()).lineLimit(1).truncationMode(.head)
                        Spacer()
                        Button {
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                        }
                        .buttonStyle(.borderless)
                        .help("在编辑器中打开")
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    Divider()
                    if file.diff.isEmpty {
                        InspectorEmptyNote(symbol: "doc.text", title: "没有 diff 内容",
                                           detail: "讨论组的回执只记录改了哪些文件；在成员聊天里可以看到完整改动。")
                    } else {
                        DiffView(diff: file.diff)
                            .background(Color(nsColor: .textBackgroundColor))
                    }
                }
            }
        }
    }
}

private struct DetailsInspector: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack {
            VStack(alignment: .leading, spacing: 18) {
            switch model.selection {
            case .chat(let id):
                if let chat = model.chat(id) {
                    InfoSection("会话") {
                        LabeledContent("代理", value: chat.backend.displayName)
                        LabeledContent("模型", value: model.catalog(for: chat.backend).modelName(chat.model))
                        LabeledContent("推理强度",
                                       value: model.catalog(for: chat.backend).effortName(chat.effort, model: chat.model))
                        LabeledContent("权限", value: chat.permission)
                        LabeledContent("状态") { StatePill(state: chat.state) }
                    }
                    InfoSection("工作区") {
                        LabeledContent("目录") { Text(chat.cwd).font(.callout.monospaced()).textSelection(.enabled) }
                        LabeledContent("MCP 与技能", value: "沿用代理自己的配置")
                    }
                    InfoSection("用量") {
                        LabeledContent("本会话 token", value: chat.tokensUsed.formatted())
                    }
                }
            case .group(let id):
                if let group = model.group(id) {
                    InfoSection("成员") {
                        ForEach(group.participants) { member in
                            HStack {
                                AgentAvatar(backend: member.backend, size: 18)
                                Text("@\(member.handle)")
                                Text(member.role).foregroundStyle(.secondary)
                                Spacer()
                                StatePill(state: member.state)
                            }
                        }
                    }
                    InfoSection("设置") {
                        LabeledContent("默认回答者", value: "@\(group.defaultResponder)")
                        LabeledContent("每轮最多接力", value: "4 次")
                        LabeledContent("目录") { Text(group.cwd).font(.callout.monospaced()) }
                    }
                    InfoSection("用量") {
                        LabeledContent("派发次数", value: "\(group.dispatches)")
                        LabeledContent("提示词字符", value: group.promptChars.formatted())
                    }
                }
            case .autopilot(let id):
                if let run = model.run(id) {
                    InfoSection("选项") {
                        LabeledContent("审批", value: run.approval.label)
                        LabeledContent("隔离", value: run.isolation.label)
                        LabeledContent("审查", value: run.reviewer.label)
                        LabeledContent("验证命令") { Text(run.verifyCommand).font(.callout.monospaced()) }
                    }
                    InfoSection("角色") {
                        LabeledContent("规划", value: run.planner.displayName)
                        LabeledContent("执行", value: run.executor.displayName)
                    }
                    InfoSection("用量") {
                        LabeledContent("提示词字符", value: run.promptChars.formatted())
                    }
                }
            default:
                Text("选择一个会话查看详情").foregroundStyle(.secondary)
            }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .labeledContentStyle(InfoRowStyle())
    }
}

/// A titled group of label–value rows, flat like the timeline (no cards).
private struct InfoSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    init(_ title: String, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct InfoRowStyle: LabeledContentStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            configuration.label
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            configuration.content
                .foregroundStyle(.primary)
                .multilineTextAlignment(.trailing)
                .lineLimit(2)
                .truncationMode(.middle)
        }
        .font(.callout)
    }
}
