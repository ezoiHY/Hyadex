import HyadexCore
import SwiftUI

/// A scrolling conversation. Long runs of finished tool calls are folded into
/// one row so the answer stays the focus.
struct TimelineView: View {
    let items: [TimelineItem]
    var mentionHandles: Set<String> = []
    var onOpenFile: (String) -> Void = { _ in }
    var onDecide: (String, ApprovalDecision) -> Void = { _, _ in }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Theme.timelineSpacing) {
                    ForEach(rows) { row in
                        switch row {
                        case .item(let item):
                            TimelineRow(item: item, mentionHandles: mentionHandles, onOpenFile: onOpenFile, onDecide: onDecide)
                        case .tools(_, let tools):
                            ToolGroupRow(tools: tools)
                        }
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
            .onChange(of: items.last) {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
            }
        }
    }

    private static let bottomID = "timeline-bottom"

    enum Row: Identifiable {
        case item(TimelineItem)
        case tools(String, [TimelineItem])

        var id: String {
            switch self {
            case .item(let item): item.id
            case .tools(let id, _): id
            }
        }
    }

    private var rows: [Row] {
        var result: [Row] = []
        var pending: [TimelineItem] = []
        func flush() {
            let finished = pending.allSatisfy {
                if case .tool(_, _, _, _, .running, _) = $0 { return false }
                return true
            }
            if pending.count >= 3 && finished {
                result.append(.tools("tools-" + pending[0].id, pending))
            } else {
                result.append(contentsOf: pending.map(Row.item))
            }
            pending = []
        }
        for item in items {
            if case .tool = item {
                pending.append(item)
            } else {
                flush()
                result.append(.item(item))
            }
        }
        flush()
        return result
    }
}

struct TimelineRow: View {
    let item: TimelineItem
    var mentionHandles: Set<String> = []
    var onOpenFile: (String) -> Void = { _ in }
    var onDecide: (String, ApprovalDecision) -> Void = { _, _ in }

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
                             mentionHandles: mentionHandles, onOpenFile: onOpenFile)
            }
        case .thinking(_, let seconds, let text):
            ThinkingRow(seconds: seconds, text: text)
        case .tool(_, let kind, let title, let output, let status, let seconds):
            ToolRow(kind: kind, title: title, output: output, status: status, seconds: seconds)
        case .changes(_, let set):
            ChangesCard(set: set, onOpenFile: onOpenFile)
        case .approval(let id, let author, let title, let command, let reason, let decision, let allowsSession):
            ApprovalCard(author: author, title: title, command: command, reason: reason, decision: decision,
                         allowsSession: allowsSession) {
                onDecide(id, $0)
            }
        case .divider(_, let text):
            HStack(spacing: 10) {
                VStack { Divider() }
                Text(text).font(.caption).foregroundStyle(.tertiary).fixedSize()
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
            MarkdownText(text: text, mentionHandles: mentionHandles)
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
}

private struct AgentMessage: View {
    let author: Author
    let text: String
    let time: Date
    let receipt: FileChangeSet?
    let streaming: Bool
    let mentionHandles: Set<String>
    let onOpenFile: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                AgentAvatar(backend: author.backend, size: 18)
                Text("@" + author.name)
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Theme.color(for: author.backend))
                Text(time.formatted(date: .omitted, time: .shortened))
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                if streaming {
                    ProgressView().controlSize(.mini)
                }
            }
            if text.isEmpty && streaming {
                Text("正在回复…").foregroundStyle(.secondary)
            } else {
                MarkdownText(text: text, mentionHandles: mentionHandles)
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
            .font(.caption)
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
        .font(.callout)
    }
}

private struct ThinkingRow: View {
    let seconds: Int
    let text: String
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "brain")
                    Text(seconds > 0 ? "思考了 \(seconds) 秒" : "思考过程")
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .font(.callout)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "已展开" : "已收起")
            if expanded {
                Text(text)
                    .font(.callout)
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
    @State private var expanded = false

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
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .font(.callout)
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
                Text("\(Int(seconds)) 秒").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        case .succeeded:
            if seconds >= 1 {
                Text(String(format: "%.1f 秒", seconds)).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
            }
            Image(systemName: "checkmark").font(.caption.weight(.semibold)).foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark").font(.caption.weight(.semibold)).foregroundStyle(.red)
        }
    }
}

private struct ToolGroupRow: View {
    let tools: [TimelineItem]
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "square.stack.3d.up")
                    Text(summary)
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .font(.callout)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(tools) { item in
                        TimelineRow(item: item)
                    }
                }
                .padding(.leading, 10)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.secondary.opacity(0.25)).frame(width: 2)
                }
            }
        }
    }

    private var summary: String {
        var counts: [ToolKind: Int] = [:]
        for case .tool(_, let kind, _, _, _, _) in tools { counts[kind, default: 0] += 1 }
        let parts: [String] = [
            counts[.read].map { "读取 \($0) 个文件" },
            counts[.edit].map { "编辑 \($0) 次" },
            counts[.search].map { "搜索 \($0) 次" },
            counts[.command].map { "运行 \($0) 条命令" },
        ].compactMap { $0 }
        return parts.joined(separator: "，")
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
                    .font(.callout)
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
private struct ApprovalCard: View {
    let author: Author
    let title: String
    let command: String
    let reason: String
    let decision: ApprovalDecision
    let allowsSession: Bool
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
                    Text("@\(author.name) 请求\(title)").font(.headline)
                    Spacer()
                    Text("等待你的决定").font(.caption).foregroundStyle(.orange)
                }
                if !reason.isEmpty {
                    Text(reason).font(.callout).foregroundStyle(.secondary)
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
            .accessibilityLabel("\(author.name) 请求\(title)：\(command)")
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
            .font(.callout)
        }
    }
}
