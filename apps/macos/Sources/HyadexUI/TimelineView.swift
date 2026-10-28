import HyadexCore
import HyadexRichText
import SwiftUI

/// A scrolling conversation. Each turn's work (thinking, tool calls,
/// answered approvals) folds into one "process" row, so the answer stays the
/// focus; a live status line shows what the agent is doing right now.
struct TimelineView: View {
    let items: [TimelineItem]
    var mentionHandles: Set<String> = []
    /// Name each agent message. A one-agent chat does not need it.
    var showsAuthors = true
    /// Older history on the server: whether it is loading, and how to load it.
    var older: (loading: Bool, load: () -> Void)? = nil
    /// Sent but not yet acknowledged by the server.
    var pending: [PendingSend] = []
    var activity: LiveActivity? = nil
    /// Finished turn durations keyed by the user message id.
    var turnSeconds: [String: Int] = [:]
    var onOpenFile: (String) -> Void = { _ in }
    var onDecide: (String, ApprovalDecision) -> Void = { _, _ in }
    /// Bytes of an attached or published file.
    var loadMedia: @Sendable (MediaFile) async -> Data? = { _ in nil }
    /// Group members' answers in progress.
    var liveTurns: [MemberTurn] = []
    /// Durations of finished member turns, keyed by their first step id.
    var processSeconds: [String: Int] = [:]
    var onDecideMember: (GroupApproval, ApprovalDecision) -> Void = { _, _ in }
    /// An item to bring into view and highlight briefly (a search hit).
    var focusID: String? = nil
    @State private var highlighted: String?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Theme.timelineSpacing) {
                    if let older {
                        HStack {
                            Spacer()
                            if older.loading {
                                ProgressView().controlSize(.small)
                            } else {
                                Button("加载更早的消息", action: older.load)
                                    .buttonStyle(.borderless)
                                    .font(.uiSecondary)
                            }
                            Spacer()
                        }
                    }
                    ForEach(rows) { row in
                        rowView(row)
                            // Highlight around the row without moving it.
                            .background {
                                RoundedRectangle(cornerRadius: Theme.cornerRadius, style: .continuous)
                                    .fill(Color.accentColor.opacity(row.id == highlighted ? 0.14 : 0))
                                    .padding(-6)
                            }
                            .animation(.easeInOut(duration: 0.4), value: highlighted)
                    }
                    Color.clear.frame(height: 1).id(Self.bottomID)
                }
                .frame(maxWidth: Theme.readableWidth)
                .padding(.horizontal, 28)
                .padding(.vertical, 20)
                .frame(maxWidth: .infinity)
            }
            // Open at the newest message, but let a short conversation start at
            // the top instead of leaving an empty band under the toolbar.
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.bottom, for: .sizeChanges)
            .defaultScrollAnchor(.top, for: .alignment)
            .onChange(of: items.last) { if highlighted == nil { scrollToBottom(proxy) } }
            .onChange(of: focusID, initial: true) { focus(proxy) }
            .onChange(of: pending) { scrollToBottom(proxy) }
            .onChange(of: activity?.kind) { scrollToBottom(proxy) }
            // Follow a member's progress, and bring a new approval into view.
            .onChange(of: liveTurns.map { "\($0.id)|\($0.steps.count)|\($0.approvals.count)" }) { scrollToBottom(proxy) }
        }
    }

    @ViewBuilder private func rowView(_ row: Row) -> some View {
        switch row {
        case .item(let item):
            TimelineRow(item: item, mentionHandles: mentionHandles, showsAuthors: showsAuthors,
                        onOpenFile: onOpenFile, onDecide: onDecide, loadMedia: loadMedia)
        case .process(_, let steps, let seconds, let live, let inProgress):
            ProcessRow(steps: steps, seconds: seconds, live: live ? activity : nil, inProgress: inProgress)
        case .answer(let steps, let seconds, let message):
            AnswerRow(steps: steps, seconds: seconds, message: message, mentionHandles: mentionHandles,
                      onOpenFile: onOpenFile)
        case .liveTurn(let turn):
            LiveTurnRow(turn: turn, onDecide: onDecideMember)
        case .activity:
            if let activity { ActivityLine(activity: activity) }
        }
    }

    /// The row showing an item: the item itself, or the folded work or answer containing it.
    private func rowID(containing itemID: String) -> String? {
        for row in rows {
            switch row {
            case .item(let item) where item.id == itemID: return row.id
            case .process(_, let steps, _, _, _) where steps.contains { $0.id == itemID }: return row.id
            case .answer(let steps, _, let message) where message.id == itemID || steps.contains { $0.id == itemID }:
                return row.id
            default: continue
            }
        }
        return nil
    }

    private func focus(_ proxy: ScrollViewProxy) {
        guard let focusID, let eventID = focusID.split(separator: "|").first.map(String.init),
              let target = rowID(containing: eventID) else { return }
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(150))
            withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(target, anchor: .center) }
            highlighted = target
            try? await Task.sleep(for: .seconds(2.5))
            if highlighted == target { highlighted = nil }
        }
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
    }

    private static let bottomID = "timeline-bottom"

    enum Row: Identifiable {
        case item(TimelineItem)
        /// A turn's work; `live` while the turn is still running.
        case process(String, [TimelineItem], seconds: Int?, live: Bool, inProgress: Bool = false)
        /// A named agent's answer with the work behind it (group discussions).
        case answer([TimelineItem], seconds: Int?, message: TimelineItem)
        /// A group member's answer in progress.
        case liveTurn(MemberTurn)
        case activity

        var id: String {
            switch self {
            case .item(let item): item.id
            case .process(let id, _, _, _, _): id
            case .answer(_, _, let message): message.id
            case .liveTurn(let turn): "live-" + turn.id
            case .activity: "live-activity"
            }
        }
    }

    static func isProcess(_ item: TimelineItem) -> Bool {
        switch item {
        case .thinking, .tool: true
        case .approval(_, _, _, _, _, let decision, _): decision != .pending
        default: false
        }
    }

    private var rows: [Row] {
        var result: [Row] = []
        var steps: [TimelineItem] = []
        var turn: String?  // id of the user message that started the current turn
        let lastUser = items.lastIndex { if case .message(_, let author, _, _, _, _, _) = $0 { author.kind == .user } else { false } }
        var index = 0
        func flush() {
            guard let first = steps.first else { return }
            let current = activity != nil && (lastUser.map { index > $0 } ?? true)
            result.append(.process("process-" + first.id, steps,
                                   seconds: processSeconds[first.id] ?? turn.flatMap { turnSeconds[$0] }, live: false,
                                   inProgress: current))
            steps = []
        }
        for (position, item) in items.enumerated() {
            index = position
            if Self.isProcess(item) {
                steps.append(item)
                continue
            }
            flush()
            if case .message(let id, let author, _, _, _, _, _) = item, author.kind == .user { turn = id }
            result.append(.item(item))
        }
        index = items.count
        if let activity, activity.kind != .waiting, let first = steps.first {
            // The running turn's work so far, headed by the live status.
            result.append(.process("process-" + first.id, steps, seconds: nil, live: true))
            steps = []
        } else {
            flush()
        }
        // In groups, a reply shows its author first, then the work, then the text.
        if showsAuthors {
            var merged: [Row] = []
            for row in result {
                if case .item(let message) = row, case .message(_, let author, _, _, _, _, _) = message, author.kind == .agent,
                   case .process(_, let steps, let seconds, false, false)? = merged.last {
                    merged[merged.count - 1] = .answer(steps, seconds: seconds, message: message)
                } else {
                    merged.append(row)
                }
            }
            result = merged
        }
        for send in pending {
            result.append(.item(.message(id: send.id, author: .user, text: send.text, time: send.time)))
        }
        result += liveTurns.map(Row.liveTurn)
        let streaming: Bool = {
            if case .message(_, _, _, _, _, _, true)? = items.last { return true }
            return false
        }()
        // While an approval card is showing, the card itself is the status.
        if let activity, activity.kind != .waiting, !streaming,
           !result.contains(where: { if case .process(_, _, _, true, _) = $0 { true } else { false } }) {
            result.append(.activity)
        }
        return result
    }
}

/// Shimmering "正在思考" text, with the elapsed time; static under Reduce Motion.
struct ActivityLine: View {
    let activity: LiveActivity

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Image(systemName: symbol)
                    .foregroundStyle(.secondary)
                    .symbolEffect(.pulse, options: .repeating, isActive: activity.kind != .waiting)
                ShimmerText(text: activity.label, active: activity.kind != .waiting)
                if activity.kind == .tool, let detail = activity.detail {
                    Text(detail)
                        .font(.system(.callout, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Elapsed(since: activity.since)
            }
            .font(.uiSecondary)
            if activity.kind == .thinking, let detail = activity.detail {
                Text(detail)
                    .font(.uiSecondary)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .padding(.leading, 22)
                    .transition(.opacity)
            }
            // A provider can stall upstream; after a while, say what the user can do.
            if activity.kind != .waiting {
                SwiftUI.TimelineView(.periodic(from: .now, by: 5)) { context in
                    if context.date.timeIntervalSince(activity.since) > 120 {
                        Text("很久没有新进展 · Esc 停止")
                            .font(.uiMeta)
                            .foregroundStyle(.tertiary)
                            .padding(.leading, 22)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var symbol: String {
        switch activity.kind {
        case .starting: "bolt.horizontal"
        case .thinking: "brain"
        case .tool: "gearshape"
        case .waiting: "hand.raised"
        }
    }
}

struct ShimmerText: View {
    let text: String
    var active = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let base = Text(text).foregroundStyle(.secondary)
        if active && !reduceMotion {
            SwiftUI.TimelineView(.animation) { context in
                let phase = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.6) / 1.6
                base.overlay {
                    LinearGradient(colors: [.clear, .primary.opacity(0.85), .clear],
                                   startPoint: UnitPoint(x: phase * 1.6 - 0.3, y: 0.5),
                                   endPoint: UnitPoint(x: phase * 1.6, y: 0.5))
                        .mask(Text(text))
                }
            }
        } else {
            base
        }
    }
}

/// "12 秒", "1:05", ticking once a second.
struct Elapsed: View {
    let since: Date

    var body: some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { context in
            Text(Self.format(context.date.timeIntervalSince(since)))
                .font(.uiMeta.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
    }

    static func format(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        return total < 60 ? "\(total) 秒" : String(format: "%d:%02d", total / 60, total % 60)
    }
}

/// One turn's work, collapsed to a summary ("已处理 12 秒 · 运行 2 条命令").
/// While the turn runs, its header is the live status line.
private struct ProcessRow: View {
    let steps: [TimelineItem]
    let seconds: Int?
    let live: LiveActivity?
    var inProgress = false
    /// 设置 › 通用: open a turn's work by default.
    @AppStorage("expandThinking") private var expandByDefault = false
    @State private var expandedChoice: Bool?
    private var expanded: Bool {
        get { expandedChoice ?? expandByDefault }
        nonmutating set { expandedChoice = newValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    if let live {
                        ActivityLine(activity: live)
                    } else {
                        Image(systemName: "checklist")
                            .foregroundStyle(.secondary)
                        Text(summary).foregroundStyle(.secondary)
                    }
                    Image(systemName: "chevron.right")
                        .font(.uiSmall.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                    Spacer(minLength: 0)
                }
                .font(.uiSecondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "已展开" : "已收起")
            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(steps) { step in
                        TimelineRow(item: step)
                    }
                }
                .padding(.leading, 10)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.secondary.opacity(0.25)).frame(width: 2)
                }
                .transition(.opacity)
            }
        }
    }

    private var summary: String {
        var counts: [ToolKind: Int] = [:]
        var failed = 0
        for case .tool(_, let kind, _, _, let status, _) in steps {
            counts[kind, default: 0] += 1
            if status == .failed { failed += 1 }
        }
        var parts = [inProgress ? "处理中" : seconds.map { "已处理 \(Elapsed.format(TimeInterval($0)))" } ?? "已处理"]
        if let count = counts[.command] { parts.append("运行 \(count) 条命令") }
        if let count = counts[.edit] { parts.append("编辑 \(count) 次") }
        if let count = counts[.read] { parts.append("读取 \(count) 个文件") }
        if let count = counts[.search] { parts.append("搜索 \(count) 次") }
        if let count = counts[.web] { parts.append("访问 \(count) 次网页") }
        if let count = counts[.mcp] { parts.append("调用 \(count) 个工具") }
        if counts.isEmpty { parts.append("思考") }
        if failed > 0 { parts.append("\(failed) 个失败") }
        return parts.joined(separator: " · ")
    }
}

struct TimelineRow: View {
    let item: TimelineItem
    var mentionHandles: Set<String> = []
    var showsAuthors = true
    var onOpenFile: (String) -> Void = { _ in }
    var onDecide: (String, ApprovalDecision) -> Void = { _, _ in }
    var loadMedia: @Sendable (MediaFile) async -> Data? = { _ in nil }

    var body: some View {
        switch item {
        case .message(_, let author, let text, let time, _, let receipt, let streaming):
            switch author.kind {
            case .user:
                UserBubble(text: text, mentionHandles: mentionHandles)
            case .orchestrator, .system:
                OrchestratorNote(text: text, time: time)
            case .agent:
                AgentMessage(author: author, text: text, time: time, receipt: receipt, streaming: streaming,
                             showsAuthor: showsAuthors, mentionHandles: mentionHandles, onOpenFile: onOpenFile)
            }
        case .thinking(_, let seconds, let text):
            ThinkingRow(seconds: seconds, text: text)
        case .tool(_, let kind, let title, let output, let status, let seconds):
            ToolRow(kind: kind, title: title, output: output, status: status, seconds: seconds)
        case .changes(_, let set):
            ChangesCard(set: set, onOpenFile: onOpenFile)
        case .approval(let id, let author, let title, let command, let reason, let decision, let allowsSession):
            ApprovalCard(author: author, title: title, command: command, reason: reason, decision: decision,
                         allowsSession: allowsSession, showsAuthor: showsAuthors) {
                onDecide(id, $0)
            }
        case .media(_, let author, let files):
            MediaGroup(files: files, alignTrailing: author.kind == .user, load: loadMedia)
        case .divider(_, let text):
            HStack(spacing: 10) {
                VStack { Divider() }
                Text(text).font(.uiMeta).foregroundStyle(.tertiary).fixedSize()
                VStack { Divider() }
            }
            .padding(.vertical, 2)
        }
    }
}

private struct UserBubble: View {
    let text: String
    let mentionHandles: Set<String>

    var body: some View {
        HStack {
            Spacer(minLength: 80)
            // As in the native apps, your own message shows as you typed it.
            Text(highlighted)
                .lineSpacing(3)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 13)
                .padding(.vertical, 9)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color.secondary.opacity(0.12))
                )
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("你：\(text)")
    }

    private var highlighted: AttributedString {
        var string = AttributedString(text)
        guard !mentionHandles.isEmpty else { return string }
        for handle in mentionHandles.union(["all"]) {
            var searchRange = string.startIndex..<string.endIndex
            while let range = string[searchRange].range(of: "@" + handle) {
                string[range].foregroundColor = Color.accentColor
                string[range].font = .body.weight(.semibold)
                searchRange = range.upperBound..<string.endIndex
            }
        }
        return string
    }
}

private struct AgentMessage: View {
    let author: Author
    let text: String
    let time: Date
    let receipt: FileChangeSet?
    let streaming: Bool
    let showsAuthor: Bool
    let mentionHandles: Set<String>
    let onOpenFile: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if showsAuthor {
                AuthorHeader(author: author, time: time)
            }
            if text.isEmpty && streaming {
                Text("正在回复…").foregroundStyle(.secondary)
            } else {
                MessageText(text: text, mentionHandles: mentionHandles)
            }
            if let receipt, !receipt.files.isEmpty {
                ReceiptChip(set: receipt, onOpenFile: onOpenFile)
            }
        }
        .accessibilityElement(children: .contain)
    }
}

/// The files a group member's turn changed, computed by the server.
private struct ReceiptChip: View {
    let set: FileChangeSet
    let onOpenFile: (String) -> Void

    var body: some View {
        Menu {
            ForEach(set.files) { file in
                Button("\(file.path)  +\(file.added) −\(file.removed)") { onOpenFile(file.path) }
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "doc.badge.ellipsis")
                Text("改动了 \(set.files.count) 个文件")
                DiffStat(added: set.added, removed: set.removed)
            }
            .font(.uiMeta)
        }
        .menuStyle(.button)
        .buttonStyle(.bordered)
        .controlSize(.small)
        .fixedSize()
    }
}

private struct OrchestratorNote: View {
    let text: String
    let time: Date

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "gearshape.2")
                .foregroundStyle(.secondary)
            Text(text).foregroundStyle(.secondary)
            Spacer()
            Text(time.formatted(date: .omitted, time: .shortened)).foregroundStyle(.tertiary)
        }
        .font(.uiSecondary)
    }
}

private struct ThinkingRow: View {
    let seconds: Int
    let text: String
    @AppStorage("expandThinking") private var expandByDefault = false
    @State private var expandedChoice: Bool?
    private var expanded: Bool {
        get { expandedChoice ?? expandByDefault }
        nonmutating set { expandedChoice = newValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "brain")
                    Text(seconds > 0 ? "思考了 \(seconds) 秒" : "思考过程")
                    Image(systemName: "chevron.right")
                        .font(.uiSmall.weight(.semibold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .font(.uiSecondary)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "已展开" : "已收起")
            if expanded {
                Text(text)
                    .font(.uiSecondary)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .padding(.leading, 10)
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Color.secondary.opacity(0.3)).frame(width: 2)
                    }
                    .transition(.opacity)
            }
        }
    }
}

struct ToolRow: View {
    let kind: ToolKind
    let title: String
    let output: String
    let status: ToolStatus
    let seconds: Double
    @AppStorage("expandThinking") private var expandByDefault = false
    @State private var expandedChoice: Bool?
    private var expanded: Bool {
        get { expandedChoice ?? expandByDefault }
        nonmutating set { expandedChoice = newValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                guard !output.isEmpty else { return }
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: kind.symbol)
                        .frame(width: 16)
                        .foregroundStyle(.secondary)
                    Text(verb).foregroundStyle(.secondary)
                    Text(title)
                        .font(.system(.callout, design: kind == .command ? .monospaced : .default))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 8)
                    statusView
                    if !output.isEmpty {
                        Image(systemName: "chevron.right")
                            .font(.uiSmall.weight(.semibold))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .font(.uiSecondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                CodeBlock(code: output, maxHeight: 220)
                    .transition(.opacity)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var verb: String {
        switch kind {
        case .command: "运行"
        case .read: "读取"
        case .edit: "编辑"
        case .search: "搜索"
        case .mcp: "调用"
        case .web: "访问"
        }
    }

    @ViewBuilder private var statusView: some View {
        switch status {
        case .running:
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text("\(Int(seconds)) 秒").font(.uiMeta.monospacedDigit()).foregroundStyle(.secondary)
            }
        case .succeeded:
            if seconds >= 1 {
                Text(String(format: "%.1f 秒", seconds)).font(.uiMeta.monospacedDigit()).foregroundStyle(.tertiary)
            }
            Image(systemName: "checkmark").font(.uiMeta.weight(.semibold)).foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark").font(.uiMeta.weight(.semibold)).foregroundStyle(.red)
        }
    }
}

private struct ChangesCard: View {
    let set: FileChangeSet
    let onOpenFile: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "doc.on.doc")
                    .foregroundStyle(.secondary)
                Text("改动了 \(set.files.count) 个文件").fontWeight(.medium)
                DiffStat(added: set.added, removed: set.removed)
                Spacer()
                Button("查看改动") { onOpenFile(set.files.first?.path ?? "") }
                    .buttonStyle(.link)
                    .font(.uiSecondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            Divider()
            ForEach(set.files) { file in
                Button {
                    onOpenFile(file.path)
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: file.removed == 0 && file.diff.contains("@@ -0,0") ? "doc.badge.plus" : "doc")
                            .foregroundStyle(.secondary)
                            .frame(width: 16)
                        Text(file.path)
                            .font(.system(.callout, design: .monospaced))
                            .lineLimit(1)
                            .truncationMode(.head)
                        Spacer()
                        DiffStat(added: file.added, removed: file.removed)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("在检查器中查看 diff")
            }
            .padding(.vertical, 3)
        }
        .card()
    }
}

/// An agent is blocked until the user decides. This is the most important
/// thing on screen, so it is tinted, states the exact command, and binds keys.
struct ApprovalCard: View {
    let author: Author
    let title: String
    let command: String
    let reason: String
    let decision: ApprovalDecision
    let allowsSession: Bool
    var showsAuthor = true
    let onDecide: (ApprovalDecision) -> Void

    private var decisionLabel: String {
        switch decision {
        case .denied: "已拒绝"
        case .allowedForSession: "已允许（本会话）"
        case .resolved: "已处理"
        default: "已允许"
        }
    }

    var body: some View {
        if decision == .pending {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                    Text(showsAuthor ? "@\(author.name) 请求\(title)" : "请求\(title)").font(.uiHeadline)
                    Spacer()
                    Text("等待你的决定").font(.uiMeta).foregroundStyle(.orange)
                }
                if !reason.isEmpty {
                    Text(reason).font(.uiSecondary).foregroundStyle(.secondary)
                }
                CodeBlock(code: command, language: title == "运行命令" ? "shell" : nil, wraps: true)
                HStack(spacing: 8) {
                    Button("拒绝") { onDecide(.denied) }
                        .keyboardShortcut(.delete, modifiers: .command)
                        .help("拒绝（⌘⌫）")
                    Spacer()
                    if allowsSession {
                        Button("本会话内始终允许") { onDecide(.allowedForSession) }
                    }
                    Button("允许") { onDecide(.allowed) }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                        .keyboardShortcut(.return, modifiers: .command)
                        .help("允许（⌘↩）")
                }
                .controlSize(.regular)
            }
            .padding(12)
            .card(tint: .orange)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("\(showsAuthor ? author.name : "") 请求\(title)：\(command)")
        } else {
            HStack(spacing: 6) {
                Image(systemName: decision == .denied ? "hand.raised.slash" : "checkmark.shield")
                    .foregroundStyle(decision == .denied ? Color.red : Color.green)
                Text(decisionLabel)
                Text(command)
                    .font(.system(.callout, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(.secondary)
            }
            .font(.uiSecondary)
        }
    }
}

/// Images as thumbnails and other files as chips. Clicking opens the file
/// in its default app (downloaded through the authenticated client).
private struct MediaGroup: View {
    let files: [MediaFile]
    let alignTrailing: Bool
    let load: @Sendable (MediaFile) async -> Data?

    var body: some View {
        HStack {
            if alignTrailing { Spacer(minLength: 80) }
            VStack(alignment: alignTrailing ? .trailing : .leading, spacing: 8) {
                let images = files.filter(\.isImage)
                if !images.isEmpty {
                    FlowRow(spacing: 8) {
                        ForEach(images) { file in MediaThumbnail(file: file, single: images.count == 1, load: load) }
                    }
                }
                ForEach(files.filter { !$0.isImage }) { file in
                    Button {
                        Task { await MediaOpener.open(file, load: load) }
                    } label: {
                        Label(file.name, systemImage: file.contentType.hasPrefix("video/") ? "film" : "doc")
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("打开 \(file.name)")
                }
            }
            if !alignTrailing { Spacer(minLength: 0) }
        }
    }
}

private struct MediaThumbnail: View {
    let file: MediaFile
    let single: Bool
    let load: @Sendable (MediaFile) async -> Data?
    @State private var image: NSImage?
    @State private var failed = false

    var body: some View {
        Button {
            Task { await MediaOpener.open(file, load: load) }
        } label: {
            Group {
                if let image {
                    Image(nsImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: single ? 420 : 200, maxHeight: single ? 320 : 160)
                } else {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.secondary.opacity(0.1))
                        .frame(width: 160, height: 110)
                        .overlay {
                            if failed {
                                Label(file.name, systemImage: "photo").font(.uiMeta).foregroundStyle(.secondary).padding(6)
                            } else {
                                ProgressView().controlSize(.small)
                            }
                        }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
        }
        .buttonStyle(.plain)
        .help(file.name)
        .accessibilityLabel("图片 \(file.name)")
        .task(id: file.id) {
            guard image == nil, let data = await load(file), let loaded = NSImage(data: data) else {
                if image == nil { failed = true }
                return
            }
            image = loaded
        }
    }
}

/// Saves a served file under Caches and opens it with its default app.
enum MediaOpener {
    @MainActor
    static func open(_ file: MediaFile, load: @Sendable (MediaFile) async -> Data?) async {
        guard let data = await load(file) else { return }
        let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appending(path: "Hyadex/files/\(file.id)", directoryHint: .isDirectory)
        let name = (file.name as NSString).lastPathComponent.isEmpty ? file.id : (file.name as NSString).lastPathComponent
        let url = folder.appending(path: name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
            NSWorkspace.shared.open(url)
        } catch {
            NSSound.beep()
        }
    }
}

/// Lays children out in rows, wrapping when the width runs out.
struct FlowRow: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += row + spacing; row = 0 }
            x += size.width + spacing
            widest = max(widest, x - spacing)
            row = max(row, size.height)
        }
        return CGSize(width: widest, height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX { x = bounds.minX; y += row + spacing; row = 0 }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            row = max(row, size.height)
        }
    }
}

/// How message content reaches files: images a message embeds, and links it
/// contains. Set by the conversation that shows the timeline.
struct MessageActions {
    var loadImage: @Sendable (String) async -> NSImage? = { _ in nil }
    var openLink: (URL) -> Void = { NSWorkspace.shared.open($0) }
}

extension EnvironmentValues {
    @Entry var messageActions = MessageActions()
}

/// A message's Markdown, math, code and images.
struct MessageText: View {
    let text: String
    var mentionHandles: Set<String> = []
    @Environment(\.messageActions) private var actions

    var body: some View {
        RichMarkdown(text, mentionHandles: mentionHandles, loadImage: actions.loadImage, onOpenLink: actions.openLink)
    }
}

/// A group member's reply: who answered, the work behind it (folded), then
/// the answer and what it changed.
private struct AnswerRow: View {
    let steps: [TimelineItem]
    let seconds: Int?
    let message: TimelineItem
    let mentionHandles: Set<String>
    let onOpenFile: (String) -> Void

    var body: some View {
        if case .message(_, let author, let text, let time, _, let receipt, let streaming) = message {
            VStack(alignment: .leading, spacing: 7) {
                AuthorHeader(author: author, time: time)
                ProcessRow(steps: steps, seconds: seconds, live: nil)
                if text.isEmpty && streaming {
                    Text("正在回复…").foregroundStyle(.secondary)
                } else {
                    MessageText(text: text, mentionHandles: mentionHandles)
                }
                if let receipt, !receipt.files.isEmpty {
                    ReceiptChip(set: receipt, onOpenFile: onOpenFile)
                }
            }
            .accessibilityElement(children: .contain)
        }
    }
}

/// A group member still answering: its live status, the work so far, and
/// any approval it is waiting for, where it happens.
private struct LiveTurnRow: View {
    let turn: MemberTurn
    let onDecide: (GroupApproval, ApprovalDecision) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            AuthorHeader(author: turn.author, time: nil)
            if let activity = turn.activity {
                if turn.steps.isEmpty {
                    ActivityLine(activity: activity)
                } else {
                    ProcessRow(steps: turn.steps, seconds: nil, live: activity)
                }
            } else if !turn.steps.isEmpty {
                ProcessRow(steps: turn.steps, seconds: nil, live: nil, inProgress: true)
            }
            ForEach(turn.approvals) { approval in
                ApprovalCard(author: turn.author, title: approval.title, command: approval.detail,
                             reason: approval.reason, decision: .pending, allowsSession: approval.allowsSession,
                             showsAuthor: false) { onDecide(approval, $0) }
            }
        }
    }
}

/// "✳ @claude 19:24" above a group member's message.
struct AuthorHeader: View {
    let author: Author
    let time: Date?

    var body: some View {
        HStack(spacing: 6) {
            AgentAvatar(backend: author.backend, size: 16)
            Text("@" + author.name)
                .font(.uiSecondary.weight(.semibold))
                .foregroundStyle(Theme.color(for: author.backend))
            if let time {
                Text(time.formatted(date: .omitted, time: .shortened))
                    .font(.uiMeta)
                    .foregroundStyle(.tertiary)
            }
        }
    }
}
