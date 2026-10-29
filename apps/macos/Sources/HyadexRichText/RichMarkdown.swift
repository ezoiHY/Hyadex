import AppKit
import SwiftUI

/// Renders an agent or user message: GitHub-flavoured Markdown, TeX math,
/// images and highlighted code, laid out like the native Claude and Codex apps.
public struct RichMarkdown: View {
    private let document: RichDocument
    private let context: RichContext
    @Environment(\.colorScheme) private var colorScheme

    public init(_ text: String, mentionHandles: Set<String> = [],
                loadImage: @escaping @Sendable (String) async -> NSImage? = { _ in nil },
                onOpenLink: @escaping (URL) -> Void = { NSWorkspace.shared.open($0) }) {
        document = RichDocument.parsed(text, mentions: mentionHandles)
        context = RichContext(loadImage: loadImage, openLink: onOpenLink)
    }

    public var body: some View {
        BlockStack(blocks: document.blocks, context: context, spacing: RichMetrics.blockSpacing)
            .environment(\.openURL, OpenURLAction { url in
                context.openLink(url)
                return .handled
            })
    }
}

struct RichContext {
    let loadImage: @Sendable (String) async -> NSImage?
    let openLink: (URL) -> Void
}

/// Text sizes of rendered messages. `scale` is the app's 字号 setting
/// (1 for 标准); set it before the views are built.
public enum RichMetrics {
    nonisolated(unsafe) public static var scale: CGFloat = 1
    static var bodySize: CGFloat { 14 * scale }
    static let blockSpacing: CGFloat = 10
    static var lineSpacing: CGFloat { 3.5 * scale }
    static var codeSize: CGFloat { 12.5 * scale }
    static let radius: CGFloat = 8

    static func headingSize(_ level: Int) -> CGFloat {
        switch level {
        case 1: 21 * scale
        case 2: 18 * scale
        case 3: 16 * scale
        default: bodySize
        }
    }
}

struct BlockStack: View {
    let blocks: [RichBlock]
    let context: RichContext
    var spacing: CGFloat = RichMetrics.blockSpacing
    var depth = 0
    var secondary = false

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(RichSegment.split(blocks).enumerated()), id: \.offset) { _, segment in
                switch segment {
                case .text(let blocks):
                    SelectableText(blocks: blocks, context: context, spacing: spacing, depth: depth, secondary: secondary)
                case .block(let block):
                    BlockView(block: block, context: context, depth: depth)
                }
            }
        }
    }
}

struct BlockView: View {
    let block: RichBlock
    let context: RichContext
    let depth: Int

    var body: some View {
        switch block {
        case .heading(let level, let flows):
            FlowView(flows: flows, context: context, fontSize: RichMetrics.headingSize(level),
                     weight: level <= 2 ? .bold : .semibold,
                     color: level >= 6 ? .secondary : .primary)
                .padding(.top, level <= 2 ? 6 : 2)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let flows):
            FlowView(flows: flows, context: context)
        case .code(let language, let source, let highlighted):
            RichCodeBlock(language: language, source: source, highlighted: highlighted)
        case .list(let ordered, let start, let items):
            RichList(ordered: ordered, start: start, items: items, context: context, depth: depth)
        case .quote(let blocks):
            BlockStack(blocks: blocks, context: context, spacing: 8, depth: depth, secondary: true)
                .foregroundStyle(.secondary)
                .padding(.leading, 14)
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1.5).fill(Color.secondary.opacity(0.35)).frame(width: 3)
                }
        case .table(let header, let rows, let alignments):
            RichTable(header: header, rows: rows, alignments: alignments)
        case .rule:
            Divider().padding(.vertical, 4)
        }
    }
}

/// A paragraph or heading: lines of text with inline math, and the display
/// math and images that break out of them.
struct FlowView: View {
    let flows: [InlineFlow]
    let context: RichContext
    var fontSize: CGFloat = RichMetrics.bodySize
    var weight: Font.Weight = .regular
    var color: Color = .primary
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(flows.enumerated()), id: \.offset) { _, flow in
                switch flow {
                case .line(let pieces):
                    InlineText.make(pieces, fontSize: fontSize, dark: colorScheme == .dark)
                        .font(.system(size: fontSize, weight: weight))
                        .foregroundStyle(color)
                        .lineSpacing(RichMetrics.lineSpacing)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                case .displayMath(let latex):
                    DisplayMath(latex: latex)
                case .image(let source, let alt):
                    RichImage(source: source, alt: alt, context: context)
                }
            }
        }
    }
}

@MainActor
enum InlineText {
    static func make(_ pieces: [InlinePiece], fontSize: CGFloat, dark: Bool) -> Text {
        var result = Text(verbatim: "")
        for piece in pieces {
            switch piece {
            case .text(let string):
                result = result + Text(string)
            case .math(let latex, let display):
                let size = fontSize * (display ? 1.25 : 1.12)
                if let rendered = MathRenderer.render(latex, fontSize: size, display: display, dark: dark) {
                    result = result + Text(Image(nsImage: rendered.image)).baselineOffset(-rendered.descent)
                } else {
                    result = result + Text(verbatim: display ? "$$\(latex)$$" : "$\(latex)$")
                        .font(.system(size: fontSize * 0.92, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            }
        }
        return result
    }
}

/// "$$…$$": centred, scrolling sideways when wider than the column.
struct DisplayMath: View {
    let latex: String
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        if let rendered = MathRenderer.render(latex, fontSize: RichMetrics.bodySize * 1.3, display: true,
                                              dark: colorScheme == .dark) {
            let image = Image(nsImage: rendered.image)
                .accessibilityLabel(latex)
            ViewThatFits(in: .horizontal) {
                image.frame(maxWidth: .infinity)
                ScrollView(.horizontal) { image.padding(.vertical, 2) }
                    .scrollIndicators(.automatic)
            }
            .padding(.vertical, 4)
            .contextMenu {
                Button("拷贝 LaTeX") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(latex, forType: .string)
                }
            }
        } else {
            RichCodeBlock(language: "latex", source: latex, highlighted: AttributedString(latex))
        }
    }
}

// MARK: - Lists

struct RichList: View {
    let ordered: Bool
    let start: Int
    let items: [ListEntry]
    let context: RichContext
    let depth: Int

    var body: some View {
        let markerWidth: CGFloat = ordered ? CGFloat(String(start + items.count - 1).count) * 7.5 + 8 : 12
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    marker(index: index, item: item)
                        .frame(width: item.checkbox == nil ? markerWidth : nil, alignment: .trailing)
                    BlockStack(blocks: item.blocks, context: context, spacing: 6, depth: depth + 1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(.leading, depth == 0 ? 2 : 0)
    }

    @ViewBuilder private func marker(index: Int, item: ListEntry) -> some View {
        if let checked = item.checkbox {
            Image(systemName: checked ? "checkmark.square.fill" : "square")
                .foregroundStyle(checked ? Color.accentColor : Color.secondary)
                .accessibilityLabel(checked ? "已完成" : "未完成")
        } else if ordered {
            Text(verbatim: "\(start + index).")
                .monospacedDigit()
                .foregroundStyle(.secondary)
        } else {
            Text(verbatim: ["•", "◦", "▪"][min(depth, 2)])
                .fontWeight(.bold)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Tables

struct RichTable: View {
    let header: [[InlinePiece]]
    let rows: [[[InlinePiece]]]
    let alignments: [HorizontalAlignment?]
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ViewThatFits(in: .horizontal) {
            grid
            ScrollView(.horizontal) { grid.padding(.bottom, 2) }
        }
    }

    private var columns: Int { max(header.count, rows.map(\.count).max() ?? 0) }

    private var grid: some View {
        Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
            GridRow {
                ForEach(0..<columns, id: \.self) { column in
                    cell(header, column, bold: true)
                        .gridColumnAlignment(alignment(column))
                }
            }
            .background(Color.secondary.opacity(0.08))
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                Divider().gridCellUnsizedAxes(.horizontal)
                GridRow {
                    ForEach(0..<columns, id: \.self) { column in
                        cell(row, column, bold: false)
                    }
                }
            }
        }
        // Columns take their content's width (each capped by `WidthCap`).
        .fixedSize()
        .clipShape(RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous)
                .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
        )
    }

    private func alignment(_ column: Int) -> HorizontalAlignment {
        (alignments.indices.contains(column) ? alignments[column] : nil) ?? .leading
    }

    private func cell(_ cells: [[InlinePiece]], _ column: Int, bold: Bool) -> some View {
        let pieces = cells.indices.contains(column) ? cells[column] : []
        return WidthCap(maxWidth: 320) {
            InlineText.make(pieces, fontSize: RichMetrics.bodySize, dark: colorScheme == .dark)
                .font(.system(size: RichMetrics.bodySize, weight: bold ? .semibold : .regular))
                .lineSpacing(2)
                .textSelection(.enabled)
                .multilineTextAlignment(alignment(column) == .trailing ? .trailing : alignment(column) == .center ? .center : .leading)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: Alignment(horizontal: alignment(column), vertical: .top))
    }
}

/// Takes its content's ideal width up to `maxWidth`, wrapping beyond it.
struct WidthCap: Layout {
    let maxWidth: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let subview = subviews.first else { return .zero }
        let limit = min(proposal.width ?? .infinity, maxWidth)
        let ideal = subview.sizeThatFits(.unspecified)
        if ideal.width <= limit { return ideal }
        return subview.sizeThatFits(ProposedViewSize(width: limit, height: nil))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, proposal: ProposedViewSize(width: bounds.width, height: bounds.height))
    }
}

// MARK: - Code

/// A fenced code block: language label, copy button, highlighted source.
struct RichCodeBlock: View {
    let language: String?
    let source: String
    let highlighted: AttributedString
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Text(verbatim: language?.lowercased() ?? "")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(source, forType: .string)
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: {
                    Label(copied ? "已拷贝" : "拷贝", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .labelStyle(.iconOnly)
                        .contentTransition(.symbolEffect(.replace))
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .foregroundStyle(.secondary)
                .help(copied ? "已拷贝" : "拷贝代码")
            }
            .padding(.leading, 12)
            .padding(.trailing, 8)
            .padding(.top, 6)
            .padding(.bottom, 2)
            ScrollView(.horizontal) {
                Text(highlighted)
                    .font(.system(size: RichMetrics.codeSize, design: .monospaced))
                    .lineSpacing(2)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                    .padding(.top, 2)
            }
            .scrollIndicators(.automatic)
        }
        .background(
            RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous)
                .fill(Color(nsColor: .textBackgroundColor).opacity(0.6))
        )
        .overlay(
            RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous)
                .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
        )
    }
}

// MARK: - Images

struct RichImage: View {
    let source: String
    let alt: String
    let context: RichContext

    enum Phase { case loading, loaded(NSImage), failed }
    @State private var phase: Phase = .loading

    var body: some View {
        Group {
            switch phase {
            case .loading:
                RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous)
                    .fill(Color.secondary.opacity(0.08))
                    .frame(width: 240, height: 150)
                    .overlay { ProgressView().controlSize(.small) }
                    .accessibilityLabel("正在载入图片 \(label)")
            case .loaded(let image):
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: min(image.size.width, 640), maxHeight: 480, alignment: .leading)
                    .clipShape(RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: RichMetrics.radius, style: .continuous)
                            .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
                    )
                    .contentShape(Rectangle())
                    .onTapGesture { open() }
                    .help(label)
                    .accessibilityLabel(alt.isEmpty ? "图片" : alt)
                    .accessibilityAddTraits(.isButton)
            case .failed:
                Button(action: open) {
                    HStack(spacing: 6) {
                        Image(systemName: "photo")
                        Text(verbatim: label).lineLimit(1).truncationMode(.middle)
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: RichMetrics.radius).fill(Color.secondary.opacity(0.08)))
                }
                .buttonStyle(.plain)
                .help("无法载入图片：\(source)")
            }
        }
        .task(id: source) { await load() }
    }

    private var label: String {
        if !alt.isEmpty { return alt }
        return URL(string: source)?.lastPathComponent ?? (source as NSString).lastPathComponent
    }

    private var url: URL? { LinkTarget.url(source) }

    private func open() {
        if let url { context.openLink(url) }
    }

    private func load() async {
        if let cached = ImageCache.shared.object(forKey: source as NSString) {
            phase = .loaded(cached)
            return
        }
        let image: NSImage?
        if let url = URL(string: source), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            image = await (try? URLSession.shared.data(from: url)).flatMap { NSImage(data: $0.0) }
        } else if source.hasPrefix("data:"), let comma = source.firstIndex(of: ","),
                  let data = Data(base64Encoded: String(source[source.index(after: comma)...])) {
            image = NSImage(data: data)
        } else {
            image = await context.loadImage(source)
        }
        guard !Task.isCancelled else { return }
        if let image, image.isValid {
            ImageCache.shared.setObject(image, forKey: source as NSString)
            phase = .loaded(image)
        } else {
            phase = .failed
        }
    }
}

enum ImageCache {
    nonisolated(unsafe) static let shared: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 120
        return cache
    }()
}
