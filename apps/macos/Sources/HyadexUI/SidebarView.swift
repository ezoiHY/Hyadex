import HyadexCore
import SwiftUI

/// The sidebar alone, outside the split view, so development snapshots can
/// render it (window-server materials do not render in-process).
public struct SidebarSnapshotView: View {
    let model: AppModel

    public init(model: AppModel) { self.model = model }

    public var body: some View {
        SidebarView(model: model, drawsOwnSelection: true)
            .background(Color(nsColor: .windowBackgroundColor))
    }
}

struct SidebarView: View {
    @Bindable var model: AppModel
    /// Snapshots only: the native selection highlight is composited by the
    /// window server and renders black in process, so draw a plain one.
    var drawsOwnSelection = false
    @AppStorage("showArchived") private var showArchived = false

    var body: some View {
        List(selection: Binding(get: { drawsOwnSelection ? nil : model.selection }, set: { model.selectFromSidebar($0) })) {
            switch model.sidebarMode {
            case .work: workSections
            case .group: groupSections
            case .autopilot: autopilotSections
            }
        }
        .listStyle(.sidebar)
        .navigationSplitViewColumnWidth(min: 240, ideal: 270, max: 380)
        .safeAreaInset(edge: .top, spacing: 0) {
            SidebarHeader(model: model)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ServerFooter(model: model)
        }
        .task(id: model.searchText) {
            // Titles filter as you type; message search waits for a pause.
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            await model.runSearch(model.searchText)
        }
    }

    @ViewBuilder private var searchSection: some View {
        let query = model.searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        if query.count >= 2, !model.searchHits.isEmpty || (model.isLive && model.searching) {
            Section {
                if model.searchHits.isEmpty {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text("正在搜索消息…").font(.uiMeta).foregroundStyle(.secondary)
                    }
                }
                ForEach(model.searchHits) { hit in
                    Button {
                        model.open(hit)
                    } label: {
                        SearchHitRow(hit: hit, title: model.chat(hit.sessionID)?.title ?? "聊天",
                                     backend: model.chat(hit.sessionID)?.backend, terms: SearchHit.terms(in: query))
                    }
                    .buttonStyle(.plain)
                }
            } header: {
                Text("消息内容")
            }
        }
    }

    @ViewBuilder private var workSections: some View {
        searchSection
        if !model.visiblePinned.isEmpty {
            Section("置顶") {
                ForEach(model.visiblePinned) { chat in
                    ChatRow(chat: chat, alert: model.emergencies.contains { $0.sessionID == chat.id }).tag(SidebarItem.chat(chat.id)).modifier(OwnSelection(item: SidebarItem.chat(chat.id), on: drawsOwnSelection, model: model))
                        .contextMenu { chatMenu(chat) }
                }
            }
        }
        Section(model.visiblePinned.isEmpty ? "" : "最近") {
            ForEach(model.visibleChats) { chat in
                ChatRow(chat: chat, alert: model.emergencies.contains { $0.sessionID == chat.id }).tag(SidebarItem.chat(chat.id)).modifier(OwnSelection(item: SidebarItem.chat(chat.id), on: drawsOwnSelection, model: model))
                    .contextMenu { chatMenu(chat) }
            }
            if model.visibleChats.isEmpty && model.visiblePinned.isEmpty {
                EmptyRow(text: !model.isLive ? "连接服务器后查看聊天" : model.searchText.isEmpty ? "还没有聊天，点右上角的 + 开始" : "没有匹配的聊天")
            }
        }
        if !model.archivedChats.isEmpty {
            Section(isExpanded: $showArchived) {
                ForEach(model.archivedChats) { chat in
                    ChatRow(chat: chat, alert: model.emergencies.contains { $0.sessionID == chat.id }).tag(SidebarItem.chat(chat.id)).modifier(OwnSelection(item: SidebarItem.chat(chat.id), on: drawsOwnSelection, model: model))
                        .contextMenu { chatMenu(chat) }
                }
            } header: {
                Text("已归档 · \(model.archivedChats.count)")
            }
        }
        Section {
            Label {
                HStack {
                    Text("定时任务")
                    Spacer()
                    Text("\(model.jobs.filter(\.enabled).count)")
                        .font(.uiMeta.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            } icon: {
                Image(systemName: "calendar.badge.clock")
            }
            .tag(SidebarItem.jobs).modifier(OwnSelection(item: SidebarItem.jobs, on: drawsOwnSelection, model: model))
        }
    }

    @ViewBuilder private var groupSections: some View {
        Section {
            ForEach(model.visibleGroups) { group in
                GroupRow(group: group).tag(SidebarItem.group(group.id)).modifier(OwnSelection(item: SidebarItem.group(group.id), on: drawsOwnSelection, model: model))
                    .contextMenu { groupMenu(group) }
            }
            if model.visibleGroups.isEmpty {
                EmptyRow(text: !model.isLive ? "连接服务器后查看讨论组" : model.searchText.isEmpty ? "还没有讨论组，点右上角的 + 开始" : "没有匹配的讨论组")
            }
        }
        if !model.archivedGroups.isEmpty {
            Section(isExpanded: $showArchived) {
                ForEach(model.archivedGroups) { group in
                    GroupRow(group: group).tag(SidebarItem.group(group.id)).modifier(OwnSelection(item: SidebarItem.group(group.id), on: drawsOwnSelection, model: model))
                        .contextMenu { groupMenu(group) }
                }
            } header: {
                Text("已归档 · \(model.archivedGroups.count)")
            }
        }
    }

    @ViewBuilder private var autopilotSections: some View {
        let active = model.visibleRuns.filter { $0.status != .done && $0.status != .aborted }
        let finished = model.visibleRuns.filter { $0.status == .done || $0.status == .aborted }
        if !active.isEmpty {
            Section("进行中") {
                ForEach(active) { run in RunRow(run: run).tag(SidebarItem.autopilot(run.id)).modifier(OwnSelection(item: SidebarItem.autopilot(run.id), on: drawsOwnSelection, model: model)) }
            }
        }
        if !finished.isEmpty {
            Section("已结束") {
                ForEach(finished) { run in RunRow(run: run).tag(SidebarItem.autopilot(run.id)).modifier(OwnSelection(item: SidebarItem.autopilot(run.id), on: drawsOwnSelection, model: model)) }
            }
        }
        if model.visibleRuns.isEmpty {
            Section {
                EmptyRow(text: model.searchText.isEmpty ? "还没有自动接力，点右上角的 + 开始" : "没有匹配的自动接力")
            }
        }
    }

    @ViewBuilder
    private func chatMenu(_ chat: ChatSummary) -> some View {
        let item = SidebarItem.chat(chat.id)
        Button(chat.unread ? "标为已读" : "标为未读") {
            if chat.unread { model.markRead(chat.id) } else { model.markUnread(chat.id) }
        }
        Button("分叉") { model.fork(chat.id) }
            .disabled(chat.archived)
        Divider()
        Button("重命名…") { model.renaming = RenameTarget(item: item, title: chat.title) }
        Button("高级设置…") { model.settingsEditor = .chat(chat.id) }
            .disabled(chat.archived)
        if !chat.archived {
            Button(chat.pinned ? "取消置顶" : "置顶") { model.togglePinned(chat.id) }
        }
        Divider()
        Button(chat.archived ? "取消归档" : "归档") { model.setArchived(item, !chat.archived) }
        Button("删除…", role: .destructive) { model.pendingDeletion = item }
    }

    @ViewBuilder
    private func groupMenu(_ group: GroupSummary) -> some View {
        let item = SidebarItem.group(group.id)
        if !group.archived {
            Button("重命名…") { model.renaming = RenameTarget(item: item, title: group.title) }
            Button("讨论组设置…") { model.settingsEditor = .group(group.id) }
            Divider()
        }
        Button(group.archived ? "取消归档" : "归档") { model.setArchived(item, !group.archived) }
        Button("删除…", role: .destructive) { model.pendingDeletion = item }
    }
}

private struct EmptyRow: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.uiSecondary)
            .foregroundStyle(.secondary)
            .padding(.vertical, 6)
            .selectionDisabled()
    }
}

/// Tabs for the three kinds of work, then search with the "new" button beside it.
private struct SidebarHeader: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(spacing: 10) {
            ModeSwitcher(model: model)
            HStack(spacing: 8) {
                SearchField(text: $model.searchText, prompt: model.sidebarMode.searchPrompt)
                Button {
                    model.newItemForCurrentMode()
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 13, weight: .semibold))
                        .frame(width: 30, height: 30)
                        .background(Circle().fill(Color.primary.opacity(0.08)))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .disabled(!model.isLive)
                .help(model.sidebarMode.newItemTitle)
                .accessibilityLabel(model.sidebarMode.newItemTitle)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 2)
        .padding(.bottom, 8)
        .frame(minWidth: 0, maxWidth: .infinity)
    }
}

private struct ModeSwitcher: View {
    @Bindable var model: AppModel
    @Namespace private var selection

    var body: some View {
        HStack(spacing: 2) {
            ForEach(SidebarMode.allCases, id: \.self) { mode in
                let selected = model.sidebarMode == mode
                let waiting = model.needsActionCount(in: mode)
                Button {
                    withAnimation(.snappy(duration: 0.22)) { model.switchSidebar(to: mode) }
                } label: {
                    tabLabel(mode, waiting: waiting > 0)
                    .font(.system(size: 12.5, weight: .medium))  // three tabs must fit the narrowest sidebar
                    .foregroundStyle(selected ? Color.primary : Color.secondary)
                    .frame(maxWidth: .infinity)
                    .frame(height: 28)
                    .help(waiting > 0 ? "\(mode.title)：\(waiting) 项需要你处理" : mode.title)
                    .background {
                        if selected {
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .fill(Color.primary.opacity(0.1))
                                .matchedGeometryEffect(id: "tab", in: selection)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(mode.title)
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityValue(waiting > 0 ? "\(waiting) 项需要处理" : "")
            }
        }
        .padding(3)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.05)))
    }

    private func tabLabel(_ mode: SidebarMode, waiting: Bool) -> some View {
        HStack(spacing: 4) {
            Image(systemName: mode.symbol).imageScale(.small)
                // The waiting dot sits on the icon so it takes no width from the title.
                .overlay(alignment: .topTrailing) {
                    if waiting {
                        Circle().fill(.orange).frame(width: 6, height: 6).offset(x: 3, y: -2)
                    }
                }
            Text(mode.title).lineLimit(1).minimumScaleFactor(0.8)
        }
        .padding(.horizontal, 4)
    }
}

private struct SearchField: View {
    @Binding var text: String
    let prompt: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
            TextField(prompt, text: $text)
                .textFieldStyle(.plain)
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
                .help("清除")
            }
        }
        .padding(.horizontal, 9)
        .frame(height: 30)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.06)))
    }
}

private struct ChatRow: View {
    let chat: ChatSummary
    /// An agent's unacknowledged emergency alert.
    var alert = false

    var body: some View {
        HStack(spacing: 9) {
            AgentAvatar(backend: chat.backend, size: 18)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(chat.title)
                        .fontWeight(chat.unread ? .semibold : .regular)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    trailing
                }
                Text(chat.preview)
                    .font(.uiMeta)
                    .foregroundStyle(chat.state == .needsAction ? Color.orange : Color.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
        .accessibilityValue(chat.state == .idle ? "" : chat.state.label)
    }

    @ViewBuilder private var trailing: some View {
        if alert {
            Image(systemName: "exclamationmark.octagon.fill")
                .font(.system(size: 11))
                .foregroundStyle(.red)
                .help("紧急提醒")
                .accessibilityLabel("紧急提醒")
        } else if chat.state != .idle {
            StateIndicator(state: chat.state, size: 11)
        } else if chat.unread {
            Circle().fill(Color.accentColor).frame(width: 7, height: 7).accessibilityLabel("未读")
        } else {
            Text(chat.lastActivity.relativeShort)
                .font(.uiSmall)
                .foregroundStyle(.tertiary)
        }
    }
}

private struct GroupRow: View {
    let group: GroupSummary

    var body: some View {
        HStack(spacing: 9) {
            AvatarStack(backends: group.participants.map(\.backend), size: 20)
                .frame(width: 24, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(group.title).lineLimit(1)
                    Spacer(minLength: 4)
                    if group.state != .idle {
                        StateIndicator(state: group.state, size: 11)
                    } else {
                        Text(group.lastActivity.relativeShort).font(.uiSmall).foregroundStyle(.tertiary)
                    }
                }
                Text(group.participants.map { "@\($0.handle)" }.joined(separator: " ") + " · " + group.preview)
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }
}

private struct RunRow: View {
    let run: AutopilotRun

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: "arrow.triangle.2.circlepath")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(run.status.activity.tint == .secondary ? Color.secondary : run.status.activity.tint)
                .frame(width: 24, height: 24)
                .background(Circle().fill(Color.secondary.opacity(0.12)))
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(run.title).lineLimit(1)
                    Spacer(minLength: 4)
                    if run.status.activity != .done {
                        StateIndicator(state: run.status.activity, size: 11)
                    }
                }
                HStack(spacing: 6) {
                    Text(run.status.label)
                        .foregroundStyle(run.status.activity == .needsAction ? Color.orange : Color.secondary)
                    if !run.tasks.isEmpty {
                        ProgressView(value: Double(run.completedTasks), total: Double(run.tasks.count))
                            .progressViewStyle(.linear)
                            .frame(width: 44)
                            .controlSize(.mini)
                        Text("\(run.completedTasks)/\(run.tasks.count)")
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.uiMeta)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }
}

/// Connection status is always visible at the bottom of the sidebar.
private struct ServerFooter: View {
    @Bindable var model: AppModel
    @Environment(\.openSettings) private var openSettings
    @AppStorage("showUsage") private var showUsage = true
    @AppStorage("settingsTab") private var settingsTab = "general"

    var body: some View {
        VStack(spacing: 0) {
            Divider()
            if showUsage, !model.usage.isEmpty {
                UsageMeter(model: model)
                    .padding(.horizontal, 14)
                    .padding(.top, 9)
            }
            Menu {
                ForEach(model.servers) { server in
                    Button {
                        Task { await model.reconnect(to: server) }
                    } label: {
                        if server.id == model.currentServerID {
                            Label(server.name, systemImage: "checkmark")
                        } else {
                            Text(server.name)
                        }
                    }
                }
                Divider()
                Button(model.localServer.isStarting ? "正在启动本地服务…" : "启动并连接本地服务") {
                    Task { await model.startLocalServer() }
                }
                .disabled(model.localServer.isStarting)
                if model.isLive {
                    Button("断开连接") { model.disconnect() }
                }
                Button("连接与管理服务器…") { settingsTab = "server"; openSettings() }
            } label: {
                HStack(spacing: 8) {
                    Circle()
                        .fill(dotColor)
                        .frame(width: 8, height: 8)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(model.currentServer?.name ?? "未连接")
                            .font(.uiSecondary.weight(.medium))
                        Text(model.connection.label)
                            .font(.uiSmall)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.uiSmall)
                        .foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .menuIndicator(.hidden)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .accessibilityLabel("服务器：\(model.currentServer?.name ?? "未连接")，\(model.connection.label)")
        }
    }

    private var dotColor: Color {
        switch model.connection {
        case .connected: .green
        case .connecting: .yellow
        case .disconnected: .secondary
        case .offline: .red
        }
    }
}

/// Plan usage of each provider account, as its own CLI reports it (Claude
/// Code's `/usage`, Codex's rate limits). Each row shows the window closest
/// to its limit; the popover lists every window.
private struct UsageMeter: View {
    @Bindable var model: AppModel
    @State private var showing = false

    var body: some View {
        Button {
            showing.toggle()
        } label: {
            VStack(spacing: 5) {
                ForEach(AgentBackend.allCases, id: \.self) { backend in
                    if let usage = model.usage[backend] { row(backend, usage) }
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("套餐用量")
        .popover(isPresented: $showing, arrowEdge: .trailing) {
            UsageDetails(model: model)
        }
    }

    private func row(_ backend: AgentBackend, _ usage: ProviderUsage) -> some View {
        HStack(spacing: 7) {
            AgentAvatar(backend: backend, size: 12)
            if let window = usage.tightest {
                UsageBar(percent: window.usedPercent)
                Text("\(Int(window.usedPercent.rounded()))%")
                    .font(.uiSmall.monospacedDigit())
                    .frame(width: 30, alignment: .trailing)
                Text(window.shortLabel)
                    .font(.uiSmall)
                    .foregroundStyle(.tertiary)
                    .frame(width: 18, alignment: .leading)
            } else {
                Text("—").font(.uiSmall).foregroundStyle(.tertiary)
                Spacer()
            }
        }
        .foregroundStyle(.secondary)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(backend.displayName) 用量 \(usage.tightest.map { "\(Int($0.usedPercent.rounded()))%" } ?? "未知")")
    }
}

struct UsageBar: View {
    let percent: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule().fill(tint).frame(width: max(3, proxy.size.width * percent / 100))
            }
        }
        .frame(height: 4)
    }

    private var tint: Color { percent >= 90 ? .red : percent >= 70 ? .orange : .secondary.opacity(0.75) }
}

private struct UsageDetails: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(AgentBackend.allCases, id: \.self) { backend in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 6) {
                        AgentAvatar(backend: backend, size: 14)
                        Text(backend.displayName).font(.uiHeadline)
                        if let plan = model.usage[backend]?.plan {
                            Text(plan.capitalized).font(.uiMeta).foregroundStyle(.secondary)
                        }
                    }
                    if let usage = model.usage[backend], usage.available {
                        ForEach(usage.windows) { window in
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Text(window.label).font(.uiSecondary)
                                    Spacer()
                                    Text("\(Int(window.usedPercent.rounded()))% used").font(.uiSecondary.monospacedDigit())
                                }
                                UsageBar(percent: window.usedPercent)
                                if let reset = window.resetsAt {
                                    Text("\(reset.formatted(.relative(presentation: .named))) 重置 · \(reset.formatted(date: .abbreviated, time: .shortened))")
                                        .font(.uiMeta)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    } else {
                        Text(model.usage[backend]?.unavailableText ?? "暂时读不到用量")
                            .font(.uiSecondary)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Divider()
            HStack {
                Text("数据来自各自 CLI 的原生用量接口").font(.uiMeta).foregroundStyle(.tertiary)
                Spacer()
                Button("刷新") { model.refreshUsage() }
                    .controlSize(.small)
                    .disabled(!model.isLive)
            }
        }
        .padding(16)
        .frame(width: 300)
    }
}


/// A chat whose messages match the search, with the newest matching line.
private struct SearchHitRow: View {
    let hit: SearchHit
    let title: String
    let backend: AgentBackend?
    let terms: [String]

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Group {
                if let backend { AgentAvatar(backend: backend, size: 18) } else { Image(systemName: "text.magnifyingglass") }
            }
            .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(title).lineLimit(1)
                    Spacer(minLength: 4)
                    if hit.matches > 1 {
                        Text("\(hit.matches) 处").font(.uiSmall).foregroundStyle(.tertiary)
                    }
                }
                Text(highlighted)
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .help("打开到这条消息")
    }

    /// "你：…snippet…" with the searched words in bold.
    private var highlighted: AttributedString {
        var text = AttributedString(hit.roleLabel + "：" + hit.snippet)
        let plain = String(text.characters)
        for term in terms {
            var searchStart = plain.startIndex
            while let range = plain.range(of: term, options: [.caseInsensitive, .diacriticInsensitive],
                                          range: searchStart..<plain.endIndex) {
                if let lower = AttributedString.Index(range.lowerBound, within: text),
                   let upper = AttributedString.Index(range.upperBound, within: text) {
                    text[lower..<upper].inlinePresentationIntent = .stronglyEmphasized
                    text[lower..<upper].foregroundColor = .primary
                }
                searchStart = range.upperBound
            }
        }
        return text
    }
}

/// A plain selection background for in-process snapshots (see `drawsOwnSelection`).
private struct OwnSelection: ViewModifier {
    let item: SidebarItem
    let on: Bool
    let model: AppModel

    func body(content: Content) -> some View {
        if on && model.selection == item {
            content.listRowBackground(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color.primary.opacity(0.1))
                    .padding(.horizontal, 10))
        } else {
            content
        }
    }
}
