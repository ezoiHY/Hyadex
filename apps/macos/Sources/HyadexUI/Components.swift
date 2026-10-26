import HyadexCore
import SwiftUI

/// Round avatar for an agent or the user.
struct AgentAvatar: View {
    let backend: AgentBackend?
    var size: CGFloat = 22

    var body: some View {
        ZStack {
            Circle().fill(backend == nil ? Color.secondary.opacity(0.18) : Theme.color(for: backend).opacity(0.16))
            Image(systemName: backend.map(Theme.symbol(for:)) ?? "person.fill")
                .font(.system(size: size * 0.48, weight: .semibold))
                .foregroundStyle(backend == nil ? Color.secondary : Theme.color(for: backend))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// Overlapping avatars for a group's members.
struct AvatarStack: View {
    let backends: [AgentBackend]
    var size: CGFloat = 22

    var body: some View {
        HStack(spacing: -size * 0.4) {
            ForEach(Array(backends.prefix(3).enumerated()), id: \.offset) { _, backend in
                AgentAvatar(backend: backend, size: size)
                    .background(Circle().fill(Color(nsColor: .windowBackgroundColor)).padding(-1.5))
            }
        }
    }
}

/// Compact state marker: a spinner while running, a coloured symbol otherwise.
struct StateIndicator: View {
    let state: ActivityState
    var size: CGFloat = 12

    var body: some View {
        Group {
            switch state {
            case .running:
                ProgressView().controlSize(.mini)
            case .idle:
                EmptyView()
            default:
                Image(systemName: state.symbol)
                    .font(.system(size: size))
                    .foregroundStyle(state.tint)
            }
        }
        .frame(width: size + 4, height: size + 4)
        .help(state.label)
        .accessibilityLabel(state.label)
    }
}

/// Text pill with a state colour, e.g. "运行中".
struct StatePill: View {
    let state: ActivityState
    var text: String? = nil

    var body: some View {
        HStack(spacing: 4) {
            if state == .running {
                ProgressView().controlSize(.mini).scaleEffect(0.8)
            } else {
                Circle().fill(state.tint).frame(width: 6, height: 6)
            }
            Text(text ?? state.label)
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(state == .idle ? Color.secondary : state.tint)
        .padding(.horizontal, 7)
        .padding(.vertical, 2.5)
        .background(Capsule().fill(state.tint.opacity(0.12)))
    }
}

/// Small grey tag, e.g. a model name or option.
struct Tag: View {
    let text: String
    var symbol: String? = nil

    var body: some View {
        HStack(spacing: 3) {
            if let symbol { Image(systemName: symbol).imageScale(.small) }
            Text(text).lineLimit(1).truncationMode(.middle)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(RoundedRectangle(cornerRadius: 4).fill(Color.secondary.opacity(0.1)))
    }
}

/// "+12 −3" line counts.
struct DiffStat: View {
    let added: Int
    let removed: Int

    var body: some View {
        if added + removed > 0 { counts }
    }

    private var counts: some View {
        HStack(spacing: 4) {
            Text("+\(added)").foregroundStyle(.green)
            Text("−\(removed)").foregroundStyle(.red)
        }
        .font(.caption.monospacedDigit())
        .accessibilityLabel("新增 \(added) 行，删除 \(removed) 行")
    }
}

/// Monospaced, selectable code block with a copy button.
struct CodeBlock: View {
    let code: String
    var language: String? = nil
    var maxHeight: CGFloat? = nil
    var wraps = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language ?? "")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .foregroundStyle(.secondary)
                .help("拷贝")
            }
            .padding(.horizontal, 10)
            .padding(.top, 6)
            if wraps {
                codeText
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                ScrollView(.horizontal) {
                    codeText.fixedSize(horizontal: true, vertical: false)
                }
                .scrollIndicators(.never)
                .frame(maxHeight: maxHeight)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: Theme.smallRadius + 2, style: .continuous)
                .fill(Color(nsColor: .textBackgroundColor).opacity(0.6))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.smallRadius + 2, style: .continuous)
                .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
        )
    }

    private var codeText: some View {
        Text(code)
            .font(.system(.callout, design: .monospaced))
            .textSelection(.enabled)
            .padding(.horizontal, 10)
            .padding(.bottom, 10)
            .padding(.top, 2)
    }
}

/// Renders the small Markdown subset agents use: paragraphs with inline
/// styles, bullet and numbered lists, and fenced code blocks.
struct MarkdownText: View {
    let text: String
    var mentionHandles: Set<String> = []

    private enum Block: Hashable {
        case paragraph(String)
        case code(String, String?)
    }

    private var blocks: [Block] {
        var result: [Block] = []
        var paragraph: [String] = []
        var code: [String]? = nil
        var language: String? = nil
        func flush() {
            let joined = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty { result.append(.paragraph(joined)) }
            paragraph = []
        }
        for line in text.components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                if let lines = code {
                    result.append(.code(lines.joined(separator: "\n"), language))
                    code = nil
                } else {
                    flush()
                    let tag = line.trimmingCharacters(in: .whitespaces).dropFirst(3)
                    language = tag.isEmpty ? nil : String(tag)
                    code = []
                }
            } else if code != nil {
                code?.append(line)
            } else if line.trimmingCharacters(in: .whitespaces).isEmpty {
                flush()
            } else {
                paragraph.append(line)
            }
        }
        if let lines = code { result.append(.code(lines.joined(separator: "\n"), language)) }
        flush()
        return result
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .paragraph(let source):
                    Text(attributed(source))
                        .lineSpacing(3)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                case .code(let source, let language):
                    CodeBlock(code: source, language: language)
                }
            }
        }
    }

    private func attributed(_ source: String) -> AttributedString {
        let prepared = source
            .components(separatedBy: "\n")
            .map { line -> String in
                let trimmed = line.drop { $0 == " " }
                if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") { return "•  " + trimmed.dropFirst(2) }
                return line
            }
            .joined(separator: "\n")
        var string = (try? AttributedString(
            markdown: prepared,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(prepared)
        for run in string.runs where run.inlinePresentationIntent?.contains(.code) == true {
            string[run.range].font = .system(.body, design: .monospaced)
            string[run.range].backgroundColor = Color.secondary.opacity(0.12)
        }
        highlightMentions(in: &string)
        return string
    }

    private func highlightMentions(in string: inout AttributedString) {
        guard !mentionHandles.isEmpty else { return }
        for handle in mentionHandles.union(["all"]) {
            var searchRange = string.startIndex..<string.endIndex
            while let range = string[searchRange].range(of: "@" + handle) {
                string[range].foregroundColor = Color.accentColor
                string[range].font = .body.weight(.semibold)
                searchRange = range.upperBound..<string.endIndex
            }
        }
    }
}

/// A unified diff with line colours and a gutter.
struct DiffView: View {
    let diff: String

    var body: some View {
        ScrollView([.vertical, .horizontal]) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(diff.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .font(.system(size: 11.5, design: .monospaced))
                        .foregroundStyle(foreground(line))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(background(line))
                }
            }
            .textSelection(.enabled)
            .fixedSize(horizontal: true, vertical: false)
            .frame(minWidth: 0, alignment: .leading)
        }
        .defaultScrollAnchor(.topLeading)
    }

    private func foreground(_ line: String) -> Color {
        if line.hasPrefix("@@") { return .secondary }
        return .primary
    }

    private func background(_ line: String) -> Color {
        if line.hasPrefix("+") { return Color.green.opacity(0.14) }
        if line.hasPrefix("-") { return Color.red.opacity(0.14) }
        if line.hasPrefix("@@") { return Color.accentColor.opacity(0.07) }
        return .clear
    }
}
