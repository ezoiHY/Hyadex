import HyadexCore
import SwiftUI

/// An agent's provider mark, or a round placeholder for the user.
struct AgentAvatar: View {
    let backend: AgentBackend?
    var size: CGFloat = 22

    var body: some View {
        if let backend {
            ProviderMark(backend: backend, size: size)
        } else {
            ZStack {
                Circle().fill(Color.secondary.opacity(0.18))
                Image(systemName: "person.fill")
                    .font(.system(size: size * 0.48, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .frame(width: size, height: size)
            .accessibilityHidden(true)
        }
    }
}

/// Overlapping avatars for a group's members.
struct AvatarStack: View {
    let backends: [AgentBackend]
    var size: CGFloat = 22

    var body: some View {
        HStack(spacing: -size * 0.25) {
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
        .font(.uiMeta.weight(.medium))
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
        .font(.uiMeta)
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
        .font(.uiMeta.monospacedDigit())
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
                    .font(.uiSmall)
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

/// A unified diff with line colours and a gutter.
struct DiffView: View {
    let diff: String

    var body: some View {
        ScrollView([.vertical, .horizontal]) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(diff.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .font(.system(size: 12.5 * Theme.scale, design: .monospaced))
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
