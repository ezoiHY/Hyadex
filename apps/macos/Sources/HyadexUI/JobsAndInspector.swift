import AppKit
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
                    Text(job.prompt).font(.uiMeta).foregroundStyle(.secondary).lineLimit(1)
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
                    StatePill(state: result, text: job.lastResultText ?? result.label)
                        .help(job.lastError.map { "失败原因：\($0)" }
                              ?? job.lastRun.map { "\($0.formatted(date: .abbreviated, time: .shortened))" } ?? "")
                } else {
                    Text("从未运行").foregroundStyle(.tertiary)
                }
            }
            .width(min: 80, ideal: 90)
        }
        .contextMenu(forSelectionType: ScheduledJob.ID.self) { ids in
            if let id = ids.first {
                Button("编辑…") { model.jobEditor = .edit(id) }
                Button("立即运行") { model.runJobNow(id) }
                Divider()
                Button("删除", role: .destructive) { model.deleteJob(id) }
            }
        } primaryAction: { ids in
            if let id = ids.first { model.jobEditor = .edit(id) }
        }
        .tableStyle(.inset)
        .alternatingRowBackgrounds()
        .overlay {
            if model.jobs.isEmpty {
                ContentUnavailableView {
                    Label("还没有定时任务", systemImage: "calendar.badge.clock")
                } description: {
                    Text("按计划给聊天或讨论组发一条消息")
                } actions: {
                    Button("新建定时任务") { model.jobEditor = .new }
                }
            }
        }
        .navigationTitle("定时任务")
        .navigationSubtitle("\(model.jobs.filter(\.enabled).count) 个已启用")
        .toolbar {
            ToolbarItemGroup {
                Button { selection.map(model.runJobNow) } label: { Label("立即运行", systemImage: "play") }
                    .disabled(selection == nil)
                    .help("立即运行选中的任务")
                Button { model.jobEditor = .new } label: { Label("新建定时任务", systemImage: "plus") }
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
                Text("改动").font(.uiHeadline)
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
                .font(.uiTitle3)
                .foregroundStyle(.tertiary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.uiSecondary.weight(.medium)).foregroundStyle(.secondary)
                Text(detail).font(.uiMeta).foregroundStyle(.tertiary)
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
                               detail: "代理改动的文件会出现在这里")
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
                                        .font(.uiSmall)
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
                        Text(file.path).font(.uiMeta.monospaced()).lineLimit(1).truncationMode(.head)
                        Spacer()
                        Button {
                            ChangedFileOpener.open(file, model: model)
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                        }
                        .buttonStyle(.borderless)
                        .help(model.serverIsLocal ? "在编辑器中打开" : "在编辑器中打开服务器上这个文件的只读副本")
                        .accessibilityLabel("在编辑器中打开")
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    Divider()
                    if file.diff.isEmpty {
                        InspectorEmptyNote(symbol: "doc.text", title: "没有 diff 内容",
                                           detail: "完整改动在成员聊天里")
                    } else {
                        DiffView(diff: file.diff)
                            .background(Color(nsColor: .textBackgroundColor))
                    }
                }
            }
        }
    }
}

/// Opens a changed file in the app the system uses for its type. A file on
/// this Mac opens in place; from a remote server the app downloads a read-only
/// copy of the server's file first.
@MainActor
public enum ChangedFileOpener {
    /// Replaced by the live check, which must not open apps on the user's screen.
    public static var openURL: (URL) -> Void = { NSWorkspace.shared.open($0) }

    static func open(_ file: FileChange, model: AppModel) {
        let (path, _) = model.location(of: file)
        if model.serverIsLocal {
            guard FileManager.default.fileExists(atPath: path) else {
                model.errorMessage = "文件已不在磁盘上：\(path)"
                return
            }
            openURL(URL(fileURLWithPath: path))
            return
        }
        Task {
            do {
                let data = try await model.serverCopy(of: file)
                let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                    .appending(path: "Hyadex/server-files/\(UUID().uuidString)", directoryHint: .isDirectory)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let url = folder.appending(path: (path as NSString).lastPathComponent)
                try data.write(to: url, options: .atomic)
                // A copy: edits would not reach the server.
                try FileManager.default.setAttributes([.posixPermissions: 0o444], ofItemAtPath: url.path)
                openURL(url)
            } catch {
                model.errorMessage = "无法打开 \((path as NSString).lastPathComponent)：\(error.localizedDescription)"
            }
        }
    }
}

private struct DetailsInspector: View {
    @Bindable var model: AppModel
    /// Closes the details popover before a settings sheet opens.
    @Environment(\.dismiss) private var dismiss

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
                        LabeledContent("目录") { Text(chat.cwd).font(.uiSecondary.monospaced()).textSelection(.enabled) }
                        LabeledContent("命令与技能") {
                            if let inventory = model.commands[chat.id], inventory.available {
                                Text("\(inventory.commands.count) 个 · 输入 / 使用")
                            } else {
                                Text("输入 / 查看")
                            }
                        }
                    }
                    MCPSection(model: model, chat: chat)
                    SubagentSection(model: model, chat: chat)
                    InfoSection("用量") {
                        LabeledContent("token") {
                            if let totals = chat.tokenTotals {
                                Text("\(totals.total.wan) · 缓存 \(totals.cachedInput.wan)")
                                    .help("输入 \(totals.input.formatted())（缓存 \(totals.cachedInput.formatted())）· 输出 \(totals.output.formatted())")
                            } else {
                                Text("—").foregroundStyle(.secondary)
                            }
                        }
                    }
                    InfoSection("高级") {
                        Button("高级设置…") { dismiss(); model.settingsEditor = .chat(chat.id) }
                            .disabled(chat.archived)
                            .help("系统提示词、子代理上限、自动标题、代理对定时任务的权限")
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
                        LabeledContent("每轮最多接力", value: "\(group.settings.maxAgentHops) 次")
                        LabeledContent("共享摘要") {
                            Text(group.settings.brief.isEmpty ? "无" : group.settings.brief)
                                .lineLimit(3)
                                .foregroundStyle(group.settings.brief.isEmpty ? .secondary : .primary)
                        }
                        LabeledContent("目录") { Text(group.cwd).font(.uiSecondary.monospaced()) }
                        Button("编辑设置…") { dismiss(); model.settingsEditor = .group(group.id) }
                            .disabled(group.archived)
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
                        LabeledContent("验证命令") { Text(run.verifyCommand).font(.uiSecondary.monospaced()) }
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

/// The MCP servers the chat's agent loaded from its own configuration, with
/// their connection state. Claude chats can reconnect, enable and disable them.
private struct MCPSection: View {
    @Bindable var model: AppModel
    let chat: ChatSummary

    var body: some View {
        let status = model.mcp[chat.id]
        let busy = model.mcpBusy.contains(chat.id)
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Text("MCP 服务器")
                    .font(.uiMeta.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if busy { ProgressView().controlSize(.mini) }
                if status?.controllable == true,
                   let failed = status?.servers.filter({ $0.status == .failed }).count, failed > 0 {
                    Button("重连失败的（\(failed)）") { model.reconnectFailedMCP(forChat: chat.id) }
                        .font(.uiMeta)
                        .disabled(busy)
                }
                Button {
                    model.refreshMCP(forChat: chat.id)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("刷新")
                .disabled(busy)
            }
            .buttonStyle(.borderless)
            if let status {
                if !status.available {
                    Text(status.reason ?? "MCP 状态暂不可用。")
                        .font(.uiSecondary)
                        .foregroundStyle(.secondary)
                } else if status.servers.isEmpty {
                    Text("\(chat.backend.displayName) 在这个目录没有加载 MCP 服务器。")
                        .font(.uiSecondary)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(status.servers) { server in
                        MCPServerRow(server: server, controllable: status.controllable && !busy) { action in
                            model.controlMCP(action, server: server.name, forChat: chat.id)
                        }
                    }
                    if status.truncated {
                        Text("只显示了部分服务器。").font(.uiMeta).foregroundStyle(.tertiary)
                    }
                }
            } else if !busy {
                Text("正在读取…").font(.uiSecondary).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task(id: chat.id) {
            if model.mcp[chat.id] == nil { model.refreshMCP(forChat: chat.id) }
        }
    }
}

/// Helper agents the chat's agent started (Claude's Task agents, Codex's collaborators).
private struct SubagentSection: View {
    @Bindable var model: AppModel
    let chat: ChatSummary

    var body: some View {
        let agents = model.subagents[chat.id] ?? []
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text("子代理")
                    .font(.uiMeta.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if agents.contains(where: { $0.state == .running }) {
                    Text("\(agents.filter { $0.state == .running }.count) 个在运行")
                        .font(.uiMeta).foregroundStyle(.secondary)
                }
            }
            if agents.isEmpty {
                Text("这个聊天还没有启动子代理。").font(.uiSecondary).foregroundStyle(.secondary)
            }
            ForEach(agents.prefix(12)) { agent in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 3) {
                        if let summary = agent.summary {
                            Text(summary).font(.uiMeta).textSelection(.enabled)
                        }
                        ForEach(Array(agent.log.suffix(8).enumerated()), id: \.offset) { _, line in
                            Text(line).font(.uiMeta.monospaced()).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } label: {
                    HStack(spacing: 6) {
                        StateIndicator(state: agent.state, size: 10)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(agent.name).font(.uiSecondary).lineLimit(1)
                            Text(agent.activity ?? agent.statusLabel)
                                .font(.uiMeta).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task(id: chat.id) {
            // Claude reports subagents only through this list; follow it while the chat works.
            while !Task.isCancelled {
                model.refreshSubagents(chat.id)
                try? await Task.sleep(for: .seconds(model.chat(chat.id)?.state == .running ? 4 : 30))
            }
        }
    }
}

private struct MCPServerRow: View {
    let server: MCPServer
    let controllable: Bool
    let onAction: (String) -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle()
                .fill(server.activity == .idle ? Color.secondary.opacity(0.5) : server.activity.tint)
                .frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 2) {
                Text(server.name)
                    .font(.uiSecondary)
                    .foregroundStyle(server.enabled ? .primary : .secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(subtitle)
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 6)
            if controllable {
                Menu { actions } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.button)
                .buttonStyle(.borderless)
                .menuIndicator(.hidden)
                .fixedSize()
            }
        }
        .help(help)
        .contextMenu { if controllable { actions } }
    }

    @ViewBuilder private var actions: some View {
        if server.enabled {
            // A sign-in happens in Claude Code itself; reconnecting cannot do it.
            if server.status != .needsAuth {
                Button("重新连接") { onAction("reconnect") }
            }
            Button("停用") { onAction("disable") }
        } else {
            Button("启用") { onAction("enable") }
        }
    }

    private var help: String {
        if server.status == .needsAuth { return "需要登录：在终端运行 claude，用 /mcp 完成登录。" }
        return server.error ?? server.serverInfo ?? server.name
    }

    private var subtitle: String {
        var parts = [server.statusLabel]
        if let count = server.toolCount, server.status == .connected || count > 0 { parts.append("\(count) 个工具") }
        if let scope = server.scopeLabel { parts.append(scope) }
        return parts.joined(separator: " · ")
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
                .font(.uiMeta.weight(.semibold))
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
        .font(.uiSecondary)
    }
}
