import AppKit
import Foundation
import SwiftUI

/// One piece of inline content. Consecutive text and inline math are drawn
/// as a single `Text`; display math and images break out as blocks.
enum InlinePiece: Hashable {
    case text(AttributedString)
    /// `display`: "$$"/"\\[" source, drawn larger and broken out of paragraphs.
    case math(String, display: Bool)
}

/// Inline content with block-level breakouts (display math, images).
enum InlineFlow: Hashable {
    case line([InlinePiece])
    case displayMath(String)
    case image(source: String, alt: String)
}

struct ListEntry: Hashable {
    var checkbox: Bool?  // nil: no task box; true: checked
    var blocks: [RichBlock]
}

enum RichBlock: Hashable {
    case heading(level: Int, flows: [InlineFlow])
    case paragraph([InlineFlow])
    case code(language: String?, source: String, highlighted: AttributedString)
    case list(ordered: Bool, start: Int, items: [ListEntry])
    case quote([RichBlock])
    case table(header: [[InlinePiece]], rows: [[[InlinePiece]]], alignments: [HorizontalAlignment?])
    case rule
}

extension HorizontalAlignment: @retroactive Hashable {
    public func hash(into hasher: inout Hasher) {
        switch self {
        case .leading: hasher.combine(0)
        case .trailing: hasher.combine(2)
        default: hasher.combine(1)
        }
    }
}

/// A parsed message. Built once per text (see `RichDocument.cache`).
final class RichDocument: @unchecked Sendable {
    let blocks: [RichBlock]

    init(blocks: [RichBlock]) { self.blocks = blocks }

    nonisolated(unsafe) private static let cache: NSCache<NSString, RichDocument> = {
        let cache = NSCache<NSString, RichDocument>()
        cache.countLimit = 400
        return cache
    }()

    static func parsed(_ text: String, mentions: Set<String>) -> RichDocument {
        let key = (mentions.sorted().joined(separator: ",") + "\u{0}" + text) as NSString
        if let hit = cache.object(forKey: key) { return hit }
        var builder = Builder(mentions: mentions)
        let document = builder.build(text)
        cache.setObject(document, forKey: key, cost: text.utf8.count)
        return document
    }
}

// MARK: - Parsed Markdown → blocks

private struct Builder {
    let mentions: Set<String>
    var spans = MathSpans()

    init(mentions: Set<String>) { self.mentions = mentions }

    mutating func build(_ text: String) -> RichDocument {
        let (prepared, spans) = MathExtractor.extract(text)
        self.spans = spans
        return RichDocument(blocks: blocks(MarkdownParser.parse(prepared)))
    }

    func blocks(_ children: [MDBlock]) -> [RichBlock] {
        children.map(block)
    }

    func block(_ block: MDBlock) -> RichBlock {
        switch block {
        case .heading(let level, let inlines):
            return .heading(level: level, flows: flows(inlines))
        case .paragraph(let inlines):
            return .paragraph(flows(inlines))
        case .code(let language, let code):
            let source = spans.restore(code)
            return .code(language: language, source: source,
                         highlighted: SyntaxHighlighter.highlight(source, language: language))
        case .html(let html):
            let source = spans.restore(html)
            return .code(language: "html", source: source, highlighted: SyntaxHighlighter.highlight(source, language: "html"))
        case .list(let ordered, let start, let items):
            return .list(ordered: ordered, start: start,
                         items: items.map { ListEntry(checkbox: $0.checkbox, blocks: blocks($0.blocks)) })
        case .quote(let children):
            return .quote(blocks(children))
        case .rule:
            return .rule
        case .table(let header, let rows, let alignments):
            return .table(header: header.map(pieces), rows: rows.map { $0.map(pieces) },
                          alignments: alignments.map {
                              switch $0 {
                              case .left: .leading
                              case .center: .center
                              case .right: .trailing
                              case nil: nil
                              }
                          })
        }
    }

    // MARK: Inline

    /// Inline content split at display math and images.
    func flows(_ children: [MDInline]) -> [InlineFlow] {
        var result: [InlineFlow] = []
        var line: [InlinePiece] = []
        func flush() {
            // Drop leading/trailing line breaks left around a breakout.
            while case .text(let t)? = line.first, String(t.characters).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { line.removeFirst() }
            while case .text(let t)? = line.last, String(t.characters).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { line.removeLast() }
            if !line.isEmpty { result.append(.line(merge(line))) }
            line = []
        }
        var collector = InlineCollector(builder: self)
        for child in children {
            if case .image(let source, let alt) = child {
                flush()
                result.append(.image(source: spans.restore(source), alt: spans.restore(alt)))
                continue
            }
            collector.pieces = []
            collector.visit(child, style: InlineStyle())
            for piece in collector.pieces {
                if case .math(let latex, true) = piece {
                    flush()
                    result.append(.displayMath(latex))
                } else {
                    line.append(piece)
                }
            }
        }
        flush()
        return result
    }

    /// Inline content as one line (table cells): no breakouts.
    func pieces(_ children: [MDInline]) -> [InlinePiece] {
        var collector = InlineCollector(builder: self)
        for child in children { collector.visit(child, style: InlineStyle()) }
        return merge(collector.pieces)
    }

    func merge(_ pieces: [InlinePiece]) -> [InlinePiece] {
        var result: [InlinePiece] = []
        for piece in pieces {
            if case .text(let next) = piece, case .text(let previous)? = result.last {
                result[result.count - 1] = .text(previous + next)
            } else {
                result.append(piece)
            }
        }
        return result.map { piece in
            guard case .text(var string) = piece else { return piece }
            Mentions.highlight(&string, handles: mentions)
            return .text(string)
        }
    }
}

struct InlineStyle {
    var intent: InlinePresentationIntent = []
    var link: URL? = nil
}

/// Walks inline nodes into attributed text and math pieces.
private struct InlineCollector {
    let builder: Builder
    var pieces: [InlinePiece] = []

    mutating func visit(_ node: MDInline, style: InlineStyle) {
        switch node {
        case .text(let text):
            appendText(text, style: style)
        case .softBreak, .hardBreak:
            appendPlain("\n", style: style)
        case .code(let code):
            var string = AttributedString(builder.spans.restore(code))
            string.inlinePresentationIntent = style.intent.union(.code)
            string.backgroundColor = Color.secondary.opacity(0.13)
            if let link = style.link { string.link = link }
            pieces.append(.text(string))
        case .emphasis(let children):
            var next = style; next.intent.insert(.emphasized)
            for child in children { visit(child, style: next) }
        case .strong(let children):
            var next = style; next.intent.insert(.stronglyEmphasized)
            for child in children { visit(child, style: next) }
        case .strikethrough(let children):
            var next = style; next.intent.insert(.strikethrough)
            for child in children { visit(child, style: next) }
        case .link(let destination, let children):
            var next = style
            next.link = LinkTarget.url(builder.spans.restore(destination))
            for child in children { visit(child, style: next) }
        case .image(let source, let alt):
            // Images nested in links or emphasis: show the alt text as a link.
            var next = style
            next.link = next.link ?? LinkTarget.url(builder.spans.restore(source))
            appendPlain(alt.isEmpty ? source : alt, style: next)
        case .html(let html):
            appendPlain(builder.spans.restore(html), style: style)
        }
    }

    /// Text that may contain math placeholders.
    mutating func appendText(_ text: String, style: InlineStyle) {
        guard text.contains(MathSpans.open) else { appendPlain(text, style: style); return }
        var buffer = ""
        var iterator = text.makeIterator()
        while let char = iterator.next() {
            guard char == MathSpans.open else { buffer.append(char); continue }
            var digits = ""
            while let next = iterator.next(), next != MathSpans.close { digits.append(next) }
            guard let index = Int(digits), builder.spans.latex.indices.contains(index) else { continue }
            if !buffer.isEmpty { appendPlain(buffer, style: style); buffer = "" }
            let latex = builder.spans.latex[index]
            pieces.append(.math(latex, display: builder.spans.display.contains(index) && style.link == nil))
        }
        if !buffer.isEmpty { appendPlain(buffer, style: style) }
    }

    mutating func appendPlain(_ text: String, style: InlineStyle) {
        var string = AttributedString(text)
        if !style.intent.isEmpty { string.inlinePresentationIntent = style.intent }
        if style.intent.contains(.strikethrough) { string.strikethroughStyle = .single }
        if let link = style.link {
            string.link = link
            string.foregroundColor = Color.accentColor
        }
        pieces.append(.text(string))
    }
}

enum LinkTarget {
    /// Web and mail links keep their URL; anything else (absolute paths,
    /// "path:line", relative files) becomes a file URL for the app to open.
    static func url(_ destination: String) -> URL? {
        let trimmed = destination.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        if let url = URL(string: trimmed), let scheme = url.scheme?.lowercased(), scheme.count > 1 {
            return url
        }
        let path = trimmed.removingPercentEncoding ?? trimmed
        if path.hasPrefix("~") { return URL(fileURLWithPath: (path as NSString).expandingTildeInPath) }
        return URL(fileURLWithPath: path)
    }
}

enum Mentions {
    static func highlight(_ string: inout AttributedString, handles: Set<String>) {
        guard !handles.isEmpty else { return }
        for handle in handles.union(["all"]) {
            var searchRange = string.startIndex..<string.endIndex
            while let range = string[searchRange].range(of: "@" + handle) {
                string[range].foregroundColor = Color.accentColor
                string[range].inlinePresentationIntent = (string[range].inlinePresentationIntent ?? []).union(.stronglyEmphasized)
                searchRange = range.upperBound..<string.endIndex
            }
        }
    }
}
