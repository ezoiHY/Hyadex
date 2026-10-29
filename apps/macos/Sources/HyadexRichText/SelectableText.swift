import AppKit
import SwiftUI

/// Where a message's blocks are drawn: runs of text (paragraphs, headings,
/// lists, quotes, display math) share one text view, so a drag selects across
/// them like a document; code, tables, rules and images keep their own views.
enum RichSegment: Hashable {
    case text([RichBlock])
    case block(RichBlock)

    static func split(_ blocks: [RichBlock]) -> [RichSegment] {
        var result: [RichSegment] = []
        var run: [RichBlock] = []
        func flush() {
            if !run.isEmpty { result.append(.text(run)) }
            run = []
        }
        for block in blocks {
            switch block {
            case .paragraph(let flows), .heading(_, let flows):
                // An image breaks out of its paragraph; the text around it stays in the run.
                var current: [InlineFlow] = []
                func keep() {
                    guard !current.isEmpty else { return }
                    if case .heading(let level, _) = block { run.append(.heading(level: level, flows: current)) } else {
                        run.append(.paragraph(current))
                    }
                    current = []
                }
                for flow in flows {
                    if case .image = flow {
                        keep()
                        flush()
                        result.append(.block(.paragraph([flow])))
                    } else {
                        current.append(flow)
                    }
                }
                keep()
            case .list, .quote:
                if block.isText { run.append(block) } else { flush(); result.append(.block(block)) }
            case .code, .table, .rule:
                flush()
                result.append(.block(block))
            }
        }
        flush()
        return result
    }
}

extension RichBlock {
    /// Drawn entirely as text: no code, tables, rules or images inside.
    var isText: Bool {
        switch self {
        case .paragraph(let flows), .heading(_, let flows):
            !flows.contains { if case .image = $0 { true } else { false } }
        case .list(_, _, let items): items.allSatisfy { $0.blocks.allSatisfy(\.isText) }
        case .quote(let blocks): blocks.allSatisfy(\.isText)
        case .code, .table, .rule: false
        }
    }
}

/// A run of text blocks in an AppKit text view: selectable across paragraphs,
/// with links, inline and display math. Copying gives plain text, with math
/// as its TeX source and list markers as Markdown.
struct SelectableText: NSViewRepresentable {
    let blocks: [RichBlock]
    let context: RichContext
    var spacing: CGFloat = RichMetrics.blockSpacing
    var depth = 0
    var secondary = false
    @Environment(\.colorScheme) private var colorScheme

    func makeNSView(context: Context) -> RichTextView { RichTextView.make() }

    func updateNSView(_ view: RichTextView, context: Context) {
        view.openLink = self.context.openLink
        let key = RichTextView.Content(blocks: blocks, spacing: spacing, depth: depth, secondary: secondary,
                                       dark: colorScheme == .dark, scale: RichMetrics.scale)
        guard view.content != key else { return }
        view.content = key
        var builder = TextRunBuilder(dark: key.dark, spacing: spacing, secondary: secondary)
        view.setContent(builder.build(blocks, depth: depth))
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView view: RichTextView, context: Context) -> CGSize? {
        view.fittingSize(width: proposal.width)
    }
}

/// A plain-text stand-in for copied content (math, list markers). An object,
/// so neighbouring stand-ins with the same text stay separate runs.
final class PlainText: NSObject {
    let text: String
    init(_ text: String) { self.text = text }
}

/// The x positions of a quote's bars, drawn beside its lines.
final class QuoteBars: NSObject {
    let positions: [CGFloat]
    init(_ positions: [CGFloat]) { self.positions = positions }
}

extension NSAttributedString.Key {
    static let richPlain = NSAttributedString.Key("HyadexRichPlain")
    static let richQuote = NSAttributedString.Key("HyadexRichQuote")
}

final class RichTextView: NSTextView, NSTextViewDelegate {
    struct Content: Equatable {
        let blocks: [RichBlock]
        let spacing: CGFloat
        let depth: Int
        let secondary: Bool
        let dark: Bool
        let scale: CGFloat
    }

    var content: Content?
    var openLink: (URL) -> Void = { NSWorkspace.shared.open($0) }

    static func make() -> RichTextView {
        // TextKit 1: attachment cells size math to the line, and the layout
        // manager draws quote bars.
        let storage = NSTextStorage()
        let layout = RichLayoutManager()
        storage.addLayoutManager(layout)
        let container = NSTextContainer(size: NSSize(width: 400, height: CGFloat.greatestFiniteMagnitude))
        container.lineFragmentPadding = 0
        container.widthTracksTextView = true
        layout.addTextContainer(container)
        let view = RichTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 20), textContainer: container)
        view.isEditable = false
        view.isSelectable = true
        view.drawsBackground = false
        view.textContainerInset = .zero
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.usesFontPanel = false
        view.isAutomaticLinkDetectionEnabled = false
        view.linkTextAttributes = [.foregroundColor: NSColor.controlAccentColor, .cursor: NSCursor.pointingHand]
        view.delegate = view
        // A second layout of the same text measures proposed widths: the
        // view's own container always takes the view's width.
        view.measuringContainer.lineFragmentPadding = 0
        view.measuringLayout.backgroundLayoutEnabled = false
        view.measuringLayout.addTextContainer(view.measuringContainer)
        storage.addLayoutManager(view.measuringLayout)
        return view
    }

    private let measuringLayout = NSLayoutManager()
    private let measuringContainer = NSTextContainer(size: NSSize(width: 400, height: CGFloat.greatestFiniteMagnitude))

    /// Measured sizes for the current content, by proposed width (-1: ideal).
    private var measured: [CGFloat: CGSize] = [:]

    func setContent(_ text: NSAttributedString) {
        measured = [:]
        textStorage?.setAttributedString(text)
    }

    func fittingSize(width: CGFloat?) -> CGSize {
        let proposed = width.flatMap { $0.isFinite ? $0 : nil }
        if let size = measured[proposed ?? -1] { return size }
        measuringContainer.size = NSSize(width: proposed ?? 100_000, height: CGFloat.greatestFiniteMagnitude)
        measuringLayout.ensureLayout(for: measuringContainer)
        let used = measuringLayout.usedRect(for: measuringContainer)
        let size = CGSize(width: proposed ?? ceil(used.maxX), height: ceil(used.maxY))
        measured[proposed ?? -1] = size
        return size
    }

    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        if let url = link as? URL { openLink(url) } else if let text = link as? String, let url = URL(string: text) {
            openLink(url)
        }
        return true
    }

    override var writablePasteboardTypes: [NSPasteboard.PasteboardType] { [.string] }

    override func writeSelection(to pboard: NSPasteboard, types: [NSPasteboard.PasteboardType]) -> Bool {
        let text = selectedRanges.map(\.rangeValue).filter { $0.length > 0 }.map(plainText(in:)).joined(separator: "\n")
        guard !text.isEmpty else { return false }
        pboard.declareTypes([.string], owner: nil)
        return pboard.setString(text, forType: .string)
    }

    /// The selection as the user would type it: math as TeX, markers as Markdown.
    func plainText(in range: NSRange) -> String {
        guard let storage = textStorage else { return "" }
        var result = ""
        storage.enumerateAttribute(.richPlain, in: range) { value, part, _ in
            if let plain = value as? PlainText {
                result += plain.text
            } else {
                result += (storage.string as NSString).substring(with: part)
            }
        }
        return result.replacingOccurrences(of: "\u{2028}", with: "\n")
    }
}

final class RichLayoutManager: NSLayoutManager {
    override func drawBackground(forGlyphRange glyphsToShow: NSRange, at origin: NSPoint) {
        super.drawBackground(forGlyphRange: glyphsToShow, at: origin)
        guard let storage = textStorage else { return }
        let characters = characterRange(forGlyphRange: glyphsToShow, actualGlyphRange: nil)
        storage.enumerateAttribute(.richQuote, in: characters) { value, range, _ in
            guard let bars = value as? QuoteBars else { return }
            let glyphs = glyphRange(forCharacterRange: range, actualCharacterRange: nil)
            NSColor.secondaryLabelColor.withAlphaComponent(0.35).setFill()
            enumerateLineFragments(forGlyphRange: glyphs) { rect, used, _, lineGlyphs, _ in
                // The quote's last line stops at its text, not the gap after it.
                let last = NSMaxRange(lineGlyphs) >= NSMaxRange(glyphs)
                let height = last ? used.maxY - rect.minY : rect.height
                for x in bars.positions {
                    NSBezierPath(roundedRect: NSRect(x: origin.x + x, y: origin.y + rect.minY, width: 3, height: height),
                                 xRadius: 1.5, yRadius: 1.5).fill()
                }
            }
        }
    }
}

/// TeX drawn in the line: on the baseline when inline; display math shrinks
/// to fit the column instead of overflowing it.
final class MathCell: NSTextAttachmentCell {
    private let picture: NSImage
    private let descent: CGFloat
    private let fitsLine: Bool

    init(image: NSImage, descent: CGFloat, fitsLine: Bool) {
        picture = image
        self.descent = descent
        self.fitsLine = fitsLine
        super.init(imageCell: image)
    }

    required init(coder: NSCoder) { fatalError("not used") }

    override func cellSize() -> NSSize { picture.size }

    override func cellBaselineOffset() -> NSPoint { NSPoint(x: 0, y: -descent) }

    override func cellFrame(for textContainer: NSTextContainer, proposedLineFragment lineFrag: NSRect,
                            glyphPosition position: NSPoint, characterIndex charIndex: Int) -> NSRect {
        var size = picture.size
        var descent = self.descent
        if fitsLine, lineFrag.width > 0, size.width > lineFrag.width {
            let scale = lineFrag.width / size.width
            size = NSSize(width: size.width * scale, height: size.height * scale)
            descent *= scale
        }
        return NSRect(x: 0, y: -descent, width: size.width, height: size.height)
    }

    override func draw(withFrame cellFrame: NSRect, in controlView: NSView?) {
        picture.draw(in: cellFrame, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true,
                     hints: [.interpolation: NSImageInterpolation.high])
    }

    override func wantsToTrackMouse() -> Bool { false }
}

/// Text blocks to attributed text with the sizes and spacing of the SwiftUI
/// rendering (`RichMetrics`).
@MainActor
struct TextRunBuilder {
    let dark: Bool
    /// Space between the run's blocks.
    let spacing: CGFloat
    let secondary: Bool

    private struct Paragraph {
        var text: NSMutableAttributedString
        var style: NSMutableParagraphStyle
        /// Space above, from the previous paragraph.
        var gap: CGFloat
    }

    private struct Indent {
        var first: CGFloat = 0
        var rest: CGFloat = 0
        var quoteBars: [CGFloat] = []
    }

    private var paragraphs: [Paragraph] = []

    init(dark: Bool, spacing: CGFloat = RichMetrics.blockSpacing, secondary: Bool = false) {
        self.dark = dark
        self.spacing = spacing
        self.secondary = secondary
    }

    mutating func build(_ blocks: [RichBlock], depth: Int = 0) -> NSAttributedString {
        paragraphs = []
        emit(blocks, gap: 0, spacing: spacing, indent: Indent(), depth: depth, secondary: secondary)
        let result = NSMutableAttributedString()
        for (index, paragraph) in paragraphs.enumerated() {
            let style = paragraph.style
            style.paragraphSpacing = index + 1 < paragraphs.count ? paragraphs[index + 1].gap : 0
            let text = paragraph.text
            if index + 1 < paragraphs.count {
                let attributes = text.length > 0 ? text.attributes(at: text.length - 1, effectiveRange: nil) : [:]
                text.append(NSAttributedString(string: "\n", attributes: attributes.filter { $0.key != .attachment && $0.key != .richPlain && $0.key != .link }))
            }
            text.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: text.length))
            result.append(text)
        }
        return result
    }

    // MARK: Blocks

    private mutating func emit(_ blocks: [RichBlock], gap: CGFloat, spacing: CGFloat, indent: Indent, depth: Int,
                               secondary: Bool, marker: NSAttributedString? = nil) {
        var marker = marker
        for (index, block) in blocks.enumerated() {
            let before = index == 0 ? gap : spacing
            switch block {
            case .heading(let level, let flows):
                let size = RichMetrics.headingSize(level)
                let extra: CGFloat = paragraphs.isEmpty ? 0 : level <= 2 ? 6 : 2
                emit(flows, gap: before + extra, indent: indent, size: size, weight: level <= 2 ? .bold : .semibold,
                     secondary: secondary || level >= 6, marker: &marker)
            case .paragraph(let flows):
                emit(flows, gap: before, indent: indent, size: RichMetrics.bodySize, weight: .regular,
                     secondary: secondary, marker: &marker)
            case .list(let ordered, let start, let items):
                list(ordered: ordered, start: start, items: items, gap: before, indent: indent, depth: depth,
                     secondary: secondary, marker: &marker)
            case .quote(let children):
                var inner = indent
                inner.quoteBars.append(indent.rest)
                inner.first = indent.rest + 14
                inner.rest = indent.rest + 14
                if let pending = marker {
                    // A list item that opens with a quote keeps its marker on a line of its own.
                    add(NSMutableAttributedString(attributedString: pending), gap: before, indent: indent)
                    marker = nil
                }
                emit(children, gap: before, spacing: 8, indent: inner, depth: depth, secondary: true)
            case .code, .table, .rule:
                break  // drawn by their own views (see RichSegment)
            }
        }
        if let marker {
            add(NSMutableAttributedString(attributedString: marker), gap: gap, indent: indent)
        }
    }

    private mutating func emit(_ flows: [InlineFlow], gap: CGFloat, indent: Indent, size: CGFloat,
                               weight: NSFont.Weight, secondary: Bool, marker: inout NSAttributedString?) {
        var afterMath: CGFloat = 0  // display math keeps 4 pt clear of its neighbours
        for (index, flow) in flows.enumerated() {
            let before = (index == 0 ? gap : 8) + afterMath
            afterMath = 0
            switch flow {
            case .line(let pieces):
                let text = NSMutableAttributedString()
                if let pending = marker {
                    text.append(pending)
                    marker = nil
                }
                text.append(inline(pieces, size: size, weight: weight, secondary: secondary))
                add(text, gap: before, indent: indent)
            case .displayMath(let latex):
                if let pending = marker {
                    add(NSMutableAttributedString(attributedString: pending), gap: before, indent: indent)
                    marker = nil
                }
                add(math(latex, size: RichMetrics.bodySize * 1.3, display: true, fits: true), gap: before + 4,
                    indent: indent, centered: true)
                afterMath = 4
            case .image:
                break  // broken out by RichSegment
            }
        }
    }

    private mutating func list(ordered: Bool, start: Int, items: [ListEntry], gap: CGFloat, indent: Indent, depth: Int,
                               secondary: Bool, marker outer: inout NSAttributedString?) {
        let scale = RichMetrics.scale
        let width = (ordered ? CGFloat(String(start + items.count - 1).count) * 7.5 + 8 : 12) * scale
        let left = indent.rest + (depth == 0 ? 2 : 0)
        let content = left + width + 6
        for (index, item) in items.enumerated() {
            var inner = indent
            inner.first = content
            inner.rest = content
            let mark: NSAttributedString = {
                // "\t1.\t": right-aligned in the gutter, then the item's text.
                let text = NSMutableAttributedString(string: "\t")
                let prefix = String(repeating: "  ", count: depth)
                let plain: String
                if let checked = item.checkbox {
                    text.append(checkbox(checked))
                    plain = prefix + (checked ? "- [x] " : "- [ ] ")
                } else {
                    let symbol = ordered ? "\(start + index)." : ["•", "◦", "▪"][min(depth, 2)]
                    let font = NSFont.monospacedDigitSystemFont(ofSize: RichMetrics.bodySize, weight: ordered ? .regular : .bold)
                    text.append(NSAttributedString(string: symbol, attributes: [.font: font,
                                                                                .foregroundColor: NSColor.secondaryLabelColor]))
                    plain = prefix + (ordered ? "\(start + index). " : "- ")
                }
                text.append(NSAttributedString(string: "\t", attributes: [.font: NSFont.systemFont(ofSize: RichMetrics.bodySize)]))
                text.addAttribute(.richPlain, value: PlainText(plain), range: NSRange(location: 0, length: text.length))
                return text
            }()
            if index == 0, let outerMarker = outer {
                // A list that opens a list item: the outer marker gets its own line.
                add(NSMutableAttributedString(attributedString: outerMarker), gap: gap, indent: indent)
                outer = nil
            }
            let tabs = [NSTextTab(textAlignment: .right, location: left + width),
                        NSTextTab(textAlignment: .left, location: content)]
            let startCount = paragraphs.count
            emit(item.blocks, gap: index == 0 ? gap : 5, spacing: 6, indent: inner, depth: depth + 1,
                 secondary: secondary, marker: mark)
            // The item's first paragraph starts with its marker, in the gutter.
            if paragraphs.count > startCount {
                paragraphs[startCount].style.tabStops = tabs
                paragraphs[startCount].style.firstLineHeadIndent = left
            }
        }
    }

    private mutating func add(_ text: NSMutableAttributedString, gap: CGFloat, indent: Indent, centered: Bool = false) {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = RichMetrics.lineSpacing
        style.firstLineHeadIndent = centered ? indent.rest : indent.first
        style.headIndent = indent.rest
        style.alignment = centered ? .center : .natural
        style.tabStops = []
        if !indent.quoteBars.isEmpty {
            text.addAttribute(.richQuote, value: QuoteBars(indent.quoteBars), range: NSRange(location: 0, length: text.length))
        }
        paragraphs.append(Paragraph(text: text, style: style, gap: gap))
    }

    // MARK: Inline

    private func inline(_ pieces: [InlinePiece], size: CGFloat, weight: NSFont.Weight, secondary: Bool) -> NSAttributedString {
        let result = NSMutableAttributedString()
        let color = secondary ? NSColor.secondaryLabelColor : NSColor.labelColor
        for piece in pieces {
            switch piece {
            case .text(let string):
                for run in string.runs {
                    let text = String(string[run.range].characters).replacingOccurrences(of: "\n", with: "\u{2028}")
                    let intent = run.inlinePresentationIntent ?? []
                    var font = NSFont.systemFont(ofSize: size, weight: intent.contains(.stronglyEmphasized) ? heavier(weight) : weight)
                    var attributes: [NSAttributedString.Key: Any] = [.foregroundColor: color]
                    if intent.contains(.code) {
                        font = NSFont.monospacedSystemFont(ofSize: size * 0.9, weight: intent.contains(.stronglyEmphasized) ? .semibold : .regular)
                        attributes[.backgroundColor] = NSColor.labelColor.withAlphaComponent(0.08)
                    }
                    if intent.contains(.emphasized) {
                        font = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask)
                    }
                    if intent.contains(.strikethrough) { attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
                    if run[AttributeScopes.SwiftUIAttributes.ForegroundColorAttribute.self] != nil {
                        attributes[.foregroundColor] = NSColor.controlAccentColor  // mentions
                    }
                    if let link = run.link { attributes[.link] = link }
                    attributes[.font] = font
                    result.append(NSAttributedString(string: text, attributes: attributes))
                }
            case .math(let latex, let display):
                result.append(math(latex, size: size * (display ? 1.25 : 1.12), display: display, fits: false,
                                   fallbackSize: size))
            }
        }
        return result
    }

    private func heavier(_ weight: NSFont.Weight) -> NSFont.Weight {
        weight == .regular ? .bold : .heavy
    }

    private func math(_ latex: String, size: CGFloat, display: Bool, fits: Bool,
                      fallbackSize: CGFloat = RichMetrics.bodySize) -> NSMutableAttributedString {
        let source = display ? "$$\(latex)$$" : "$\(latex)$"
        guard let rendered = MathRenderer.render(latex, fontSize: size, display: display, dark: dark) else {
            return NSMutableAttributedString(string: source, attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: fallbackSize * 0.92, weight: .regular),
                .foregroundColor: NSColor.secondaryLabelColor,
            ])
        }
        let attachment = NSTextAttachment()
        attachment.attachmentCell = MathCell(image: rendered.image, descent: rendered.descent, fitsLine: fits)
        let text = NSMutableAttributedString(attachment: attachment)
        text.addAttributes([.richPlain: PlainText(source), .font: NSFont.systemFont(ofSize: fallbackSize)],
                           range: NSRange(location: 0, length: text.length))
        return text
    }

    private func checkbox(_ checked: Bool) -> NSAttributedString {
        let size = RichMetrics.bodySize
        let color = checked ? NSColor.controlAccentColor : NSColor.secondaryLabelColor
        let configuration = NSImage.SymbolConfiguration(pointSize: size, weight: .regular)
            .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        guard let image = NSImage(systemSymbolName: checked ? "checkmark.square.fill" : "square",
                                  accessibilityDescription: checked ? "已完成" : "未完成")?
            .withSymbolConfiguration(configuration) else {
            return NSAttributedString(string: checked ? "☑" : "☐")
        }
        let descent = (image.size.height - size) / 2 + 2
        let attachment = NSTextAttachment()
        attachment.attachmentCell = MathCell(image: image, descent: descent, fitsLine: false)
        return NSAttributedString(attachment: attachment)
    }
}
