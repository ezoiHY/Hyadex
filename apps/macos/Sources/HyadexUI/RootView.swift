import HyadexCore
import SwiftUI

/// Main window: sidebar · conversation · inspector.
public struct RootView: View {
    @Bindable var model: AppModel
    @AppStorage(TextSize.key) private var textSize = TextSize.standard

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some View {
        let _ = Theme.scale = textSize.scale
        content
            .font(.uiBody)
            // A new 字号 rebuilds every view with the new sizes.
            .id(textSize)
    }

    private var content: some View {
        NavigationSplitView {
            SidebarView(model: model)
        } detail: {
            // Split-view columns must not take their minimum size from their
            // content: when column minimums exceed the window width, Auto
            // Layout loops and AppKit aborts. The changes panel is an overlay,
            // not a column, so it never changes the layout underneath.
            detail
                .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                .overlay(alignment: .top) {
                    VStack(spacing: 0) {
                        if !model.emergencies.isEmpty { EmergencyBanners(model: model) }
                        ErrorBanner(model: model)
                    }
                }
                .overlay(alignment: .trailing) {
                    if model.showChanges {
                        ChangesPanel(model: model)
                            .containerRelativeFrame(.horizontal) { width, _ in min(max(width * 0.55, 380), 760) }
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                    } else if case .chat(let id)? = model.selection, model.sideChatOpen == id {
                        SideChatPanel(model: model, chatID: id)
                            .id(id)
                            .containerRelativeFrame(.horizontal) { width, _ in min(max(width * 0.4, 340), 520) }
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                    }
                }
                .animation(.snappy(duration: 0.25), value: model.showChanges)
                .animation(.snappy(duration: 0.25), value: model.sideChatOpen)
        }
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                TerminalButton(model: model)
                DetailsButton(model: model)
                ChangesButton(model: model)
            }
        }
        .sheet(item: $model.goalEditor) { target in
            GoalSheet(model: model, target: target)
        }
        .sheet(item: $model.jobEditor) { target in
            JobSheet(model: model, target: target)
        }
        .sheet(item: $model.renaming) { target in
            RenameSheet(model: model, target: target)
        }
        .sheet(item: $model.settingsEditor) { target in
            SettingsSheet(model: model, target: target)
        }
        .confirmationDialog(deletionTitle,
                            isPresented: Binding(get: { model.pendingDeletion != nil },
                                                 set: { if !$0 { model.pendingDeletion = nil } }),
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let item = model.pendingDeletion { model.delete(item) }
                model.pendingDeletion = nil
            }
            Button("取消", role: .cancel) { model.pendingDeletion = nil }
        } message: {
            Text(deletionDetail)
        }
        .sheet(item: $model.sheet) { sheet in
            switch sheet {
            case .newChat: NewChatSheet(model: model)
            case .newGroup: NewGroupSheet(model: model)
            case .newAutopilot: NewAutopilotSheet(model: model)
            case .commandPalette: CommandPalette(model: model)
            case .feedback: EmptyView()
            }
        }
        .frame(minWidth: 900, minHeight: 560)
    }

    private var deletionTitle: String {
        guard let item = model.pendingDeletion else { return "" }
        if case .group = item { return "删除讨论组“\(model.title(of: item))”？" }
        return "删除聊天“\(model.title(of: item))”？"
    }

    private var deletionDetail: String {
        if case .group? = model.pendingDeletion {
            return "讨论组和它的定时任务会被删除，成员聊天会被归档。不能撤销。"
        }
        return "正在运行的回复会停止，排队的消息、终端会话和这个聊天的定时任务都会删除。不能撤销。"
    }

    @ViewBuilder private var detail: some View {
        if !model.isLive && model.chats.isEmpty && model.groups.isEmpty && model.runs.isEmpty {
            ServerWelcomeView(model: model)
        } else {
          switch model.selection {
        case .chat(let id): ChatDetailView(model: model, chatID: id).id(id)
        case .group(let id): GroupDetailView(model: model, groupID: id).id(id)
        case .autopilot(let id): AutopilotView(model: model, runID: id).id(id)
        case .jobs: JobsView(model: model)
        case nil: EmptyDetail(model: model)
          }
        }
    }
}

/// Shows how many files the selection changed and opens the changes panel.
/// Disabled when there is nothing to show.
private struct ChangesButton: View {
    @Bindable var model: AppModel

    var body: some View {
        let count = model.selectedChanges.files.count
        Button {
            model.showChanges.toggle()
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "doc.on.doc")
                if count > 0 { Text("\(count)").monospacedDigit() }
            }
        }
        .disabled(count == 0 && !model.showChanges)
        .help(count == 0 ? "还没有改动" : "查看 \(count) 个文件的改动（⌥⌘I）")
        .accessibilityLabel(count == 0 ? "没有改动" : "改动，\(count) 个文件")
    }
}

/// Shows or hides the selected chat's terminal (⌃`).
private struct TerminalButton: View {
    @Bindable var model: AppModel

    var body: some View {
        let chatID: String? = if case .chat(let id) = model.selection { id } else { nil }
        let open = chatID.map { model.terminalOpen.contains($0) } ?? false
        Button {
            if let chatID { model.toggleTerminal(chatID) }
        } label: {
            Label("终端", systemImage: open ? "terminal.fill" : "terminal")
        }
        .keyboardShortcut("`", modifiers: .control)
        .disabled(chatID == nil)
        .help(open ? "隐藏终端（⌃`）" : "打开终端（⌃`）")
    }
}

/// Settings and usage of the selection, on demand.
private struct DetailsButton: View {
    @Bindable var model: AppModel
    @State private var showing = false

    var body: some View {
        Button {
            showing.toggle()
        } label: {
            Label("详情", systemImage: "info.circle")
        }
        .disabled(model.selection == nil || model.selection == .jobs)
        .help("详情")
        .popover(isPresented: $showing, arrowEdge: .bottom) {
            DetailsPopover(model: model)
        }
    }
}

/// Action errors (a send that failed, an unreachable server…), dismissible.
private struct ErrorBanner: View {
    @Bindable var model: AppModel

    var body: some View {
        if let message = model.errorMessage {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Text(message).lineLimit(3)
                Spacer(minLength: 8)
                Button {
                    model.errorMessage = nil
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("关闭")
            }
            .font(.uiSecondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.orange.opacity(0.4)))
            .frame(maxWidth: 560)
            .padding(.top, 8)
            .padding(.horizontal, 20)
            .transition(.move(edge: .top).combined(with: .opacity))
            .task(id: message) {
                try? await Task.sleep(for: .seconds(8))
                if model.errorMessage == message { model.errorMessage = nil }
            }
        }
    }
}

private struct EmptyDetail: View {
    @Bindable var model: AppModel

    var body: some View {
        ContentUnavailableView {
            Label("Hyadex", systemImage: "sparkles.rectangle.stack")
        } description: {
            Text("在左侧选择一项，或者新建一个。")
        } actions: {
            Button(model.sidebarMode.newItemTitle) { model.newItemForCurrentMode() }
                .buttonStyle(.borderedProminent)
        }
    }
}

// MARK: - Command palette

/// ⌘K: jump to any conversation or run a command by typing.
struct CommandPalette: View {
    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var highlighted = 0
    @FocusState private var focused: Bool

    struct Entry: Identifiable {
        let id: String
        let title: String
        let subtitle: String
        let symbol: String
        let action: @MainActor () -> Void
    }

    private var entries: [Entry] {
        var all: [Entry] = model.isLive ? [
            Entry(id: "new-chat", title: "新建聊天", subtitle: "命令 · ⌘N", symbol: "bubble.left") { model.sheet = .newChat },
            Entry(id: "new-group", title: "新建讨论组", subtitle: "命令 · ⇧⌘N", symbol: "person.2") { model.sheet = .newGroup },
            Entry(id: "new-run", title: "新建自动接力", subtitle: "命令 · ⌥⌘N", symbol: "arrow.triangle.2.circlepath") { model.sheet = .newAutopilot },
            Entry(id: "jobs", title: "定时任务", subtitle: "前往", symbol: "calendar.badge.clock") { model.select(.jobs) },
        ] : []
        all += model.chats.map { chat in
            Entry(id: chat.id, title: chat.title, subtitle: "聊天 · \(chat.backend.displayName)", symbol: "bubble.left") {
                model.select(.chat(chat.id))
            }
        }
        all += model.groups.map { group in
            Entry(id: group.id, title: group.title, subtitle: "讨论组", symbol: "person.2") { model.select(.group(group.id)) }
        }
        all += model.runs.map { run in
            Entry(id: run.id, title: run.title, subtitle: "自动接力 · \(run.status.label)", symbol: "arrow.triangle.2.circlepath") {
                model.select(.autopilot(run.id))
            }
        }
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return Array(all.prefix(9)) }
        return all.filter { $0.title.localizedCaseInsensitiveContains(trimmed) || $0.subtitle.localizedCaseInsensitiveContains(trimmed) }
    }

    var body: some View {
        let results = entries
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("跳转到会话或执行命令", text: $query)
                    .textFieldStyle(.plain)
                    .font(.uiTitle3)
                    .focused($focused)
                    .onSubmit { run(results) }
                    .onKeyPress(.downArrow) { highlighted = min(highlighted + 1, max(results.count - 1, 0)); return .handled }
                    .onKeyPress(.upArrow) { highlighted = max(highlighted - 1, 0); return .handled }
                    .onChange(of: query) { highlighted = 0 }
            }
            .padding(14)
            Divider()
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(Array(results.enumerated()), id: \.element.id) { index, entry in
                        Button {
                            highlighted = index
                            run(results)
                        } label: {
                            HStack(spacing: 10) {
                                Image(systemName: entry.symbol).frame(width: 18).foregroundStyle(.secondary)
                                Text(entry.title)
                                Spacer()
                                Text(entry.subtitle).font(.uiMeta).foregroundStyle(.secondary)
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 7)
                            .background(RoundedRectangle(cornerRadius: 6)
                                .fill(index == highlighted ? Color.accentColor.opacity(0.18) : .clear))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    if results.isEmpty {
                        Text("没有匹配项").foregroundStyle(.secondary).padding(20)
                    }
                }
                .padding(6)
            }
            .frame(height: 300)
        }
        .frame(width: 560)
        .onAppear { focused = true }
        .onExitCommand { dismiss() }
    }

    private func run(_ results: [Entry]) {
        guard results.indices.contains(highlighted) else { return }
        let entry = results[highlighted]
        dismiss()
        DispatchQueue.main.async { entry.action() }
    }
}
