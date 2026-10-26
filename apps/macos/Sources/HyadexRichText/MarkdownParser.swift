import Foundation

/// A small GitHub-flavoured Markdown parser: the block and inline syntax
/// agents write (headings, paragraphs, lists with tasks, quotes, fences,
/// tables, rules, HTML blocks, emphasis, code, links, images, autolinks).
/// It never fails: anything it does not recognise stays text.
indirect enum MDBlock: Hashable {
    case heading(Int, [MDInline])
    case paragraph([MDInline])
    case code(language: String?, source: String)
    case html(String)
    case list(ordered: Bool, start: Int, items: [MDListItem])
    case quote([MDBlock])
    case table(header: [[MDInline]], rows: [[[MDInline]]], alignments: [MDAlignment?])
    case rule
}

struct MDListItem: Hashable {
    var checkbox: Bool?
    var blocks: [MDBlock]
}

enum MDAlignment: Hashable { case left, center, right }

indirect enum MDInline: Hashable {
    case text(String)
    case code(String)
    case emphasis([MDInline])
    case strong([MDInline])
    case strikethrough([MDInline])
    case link(destination: String, [MDInline])
    case image(source: String, alt: String)
    case softBreak
    case hardBreak
    case html(String)

    var plainText: String {
        switch self {
        case .text(let text), .code(let text), .html(let text): text
        case .emphasis(let children), .strong(let children), .strikethrough(let children), .link(_, let children):
            children.map(\.plainText).joined()
        case .image(_, let alt): alt
        case .softBreak, .hardBreak: " "
        }
    }
}

enum MarkdownParser {
    static func parse(_ text: String) -> [MDBlock] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\t", with: "    ")
            .components(separatedBy: "\n")
        var parser = BlockParser(lines: lines[...])
        return parser.parse()
    }
}

// MARK: - Blocks

private struct BlockParser {
    var lines: ArraySlice<String>

    mutating func parse() -> [MDBlock] {
        var blocks: [MDBlock] = []
        var paragraph: [String] = []

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            let text = paragraph.map { $0.trimmingCharacters(in: .whitespaces) }.joined(separator: "\n")
            blocks.append(.paragraph(InlineParser.parse(text)))
            paragraph = []
        }

        while let line = lines.first {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let indent = line.prefix { $0 == " " }.count

            if trimmed.isEmpty {
                flushParagraph()
                lines.removeFirst()
                continue
            }
            // Setext heading underline right after a paragraph line.
            if !paragraph.isEmpty, indent < 4, Self.isSetext(trimmed) {
                let text = paragraph.map { $0.trimmingCharacters(in: .whitespaces) }.joined(separator: "\n")
                paragraph = []
                blocks.append(.heading(trimmed.hasPrefix("=") ? 1 : 2, InlineParser.parse(text)))
                lines.removeFirst()
                continue
            }
            if indent < 4, let fence = Self.fence(trimmed) {
                flushParagraph()
                lines.removeFirst()
                blocks.append(parseFence(fence, indent: indent))
                continue
            }
            if indent < 4, let heading = Self.atxHeading(trimmed) {
                flushParagraph()
                lines.removeFirst()
                blocks.append(.heading(heading.0, InlineParser.parse(heading.1)))
                continue
            }
            if indent < 4, Self.isRule(trimmed) {
                flushParagraph()
                lines.removeFirst()
                blocks.append(.rule)
                continue
            }
            if indent < 4, trimmed.hasPrefix(">") {
                flushParagraph()
                blocks.append(parseQuote())
                continue
            }
            if indent < 4, let marker = Self.listMarker(line), paragraph.isEmpty || marker.startsList {
                flushParagraph()
                blocks.append(parseList(first: marker))
                continue
            }
            if indent < 4, paragraph.isEmpty, trimmed.contains("|"), lines.count > 1,
               let alignments = Self.tableDelimiter(lines[lines.index(after: lines.startIndex)]) {
                let header = Self.cells(trimmed)
                if header.count == alignments.count {
                    blocks.append(parseTable(header: header, alignments: alignments))
                    continue
                }
            }
            if indent < 4, paragraph.isEmpty, Self.isHTMLStart(trimmed) {
                var html: [String] = []
                while let next = lines.first, !next.trimmingCharacters(in: .whitespaces).isEmpty {
                    html.append(next)
                    lines.removeFirst()
                }
                blocks.append(.html(html.joined(separator: "\n")))
                continue
            }
            if indent >= 4, paragraph.isEmpty {
                var code: [String] = []
                while let next = lines.first {
                    let nextIndent = next.prefix { $0 == " " }.count
                    if next.trimmingCharacters(in: .whitespaces).isEmpty {
                        code.append("")
                    } else if nextIndent >= 4 {
                        code.append(String(next.dropFirst(4)))
                    } else {
                        break
                    }
                    lines.removeFirst()
                }
                while code.last == "" { code.removeLast() }
                blocks.append(.code(language: nil, source: code.joined(separator: "\n")))
                continue
            }
            paragraph.append(line)
            lines.removeFirst()
        }
        flushParagraph()
        return blocks
    }

    // MARK: Fences

    mutating func parseFence(_ fence: (Character, Int, String), indent: Int) -> MDBlock {
        var code: [String] = []
        while let line = lines.first {
            lines.removeFirst()
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let run = trimmed.prefix { $0 == fence.0 }
            if run.count >= fence.1, trimmed.dropFirst(run.count).trimmingCharacters(in: .whitespaces).isEmpty {
                break
            }
            // Content lines lose up to the fence's own indentation.
            var content = Substring(line)
            var removed = 0
            while removed < indent, content.first == " " { content = content.dropFirst(); removed += 1 }
            code.append(String(content))
        }
        let info = fence.2.trimmingCharacters(in: .whitespaces)
        let language = info.split(separator: " ").first.map(String.init)
        return .code(language: language?.isEmpty == false ? language : nil, source: code.joined(separator: "\n"))
    }

    static func fence(_ trimmed: String) -> (Character, Int, String)? {
        guard let first = trimmed.first, first == "`" || first == "~" else { return nil }
        let run = trimmed.prefix { $0 == first }.count
        guard run >= 3 else { return nil }
        let info = String(trimmed.dropFirst(run))
        if first == "`", info.contains("`") { return nil }
        return (first, run, info)
    }

    // MARK: Quotes

    mutating func parseQuote() -> MDBlock {
        var inner: [String] = []
        while let line = lines.first {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix(">") {
                var content = trimmed.dropFirst()
                if content.first == " " { content = content.dropFirst() }
                inner.append(String(content))
            } else if !trimmed.isEmpty, let last = inner.last, !last.trimmingCharacters(in: .whitespaces).isEmpty,
                      Self.listMarker(line) == nil, Self.fence(trimmed) == nil, !Self.isRule(trimmed) {
                inner.append(line)  // lazy continuation
            } else {
                break
            }
            lines.removeFirst()
        }
        var parser = BlockParser(lines: inner[...])
        return .quote(parser.parse())
    }

    // MARK: Lists

    struct ListMarker {
        let ordered: Bool
        let number: Int
        let bullet: Character
        let contentIndent: Int
        let content: String
        /// Whether this marker may interrupt a paragraph (GFM: bullets and "1.").
        var startsList: Bool { !content.trimmingCharacters(in: .whitespaces).isEmpty && (!ordered || number == 1) }
    }

    static func listMarker(_ line: String) -> ListMarker? {
        let indent = line.prefix { $0 == " " }.count
        guard indent < 4 else { return nil }
        let rest = line.dropFirst(indent)
        guard let first = rest.first else { return nil }
        if "-*+".contains(first) {
            let after = rest.dropFirst()
            guard after.isEmpty || after.first == " " else { return nil }
            let spaces = min(max(after.prefix { $0 == " " }.count, 1), 4)
            return ListMarker(ordered: false, number: 1, bullet: first, contentIndent: indent + 1 + spaces,
                              content: String(after.dropFirst(min(spaces, after.count))))
        }
        let digits = rest.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 9 else { return nil }
        let after = rest.dropFirst(digits.count)
        guard let delimiter = after.first, delimiter == "." || delimiter == ")" else { return nil }
        let tail = after.dropFirst()
        guard tail.isEmpty || tail.first == " " else { return nil }
        let spaces = min(max(tail.prefix { $0 == " " }.count, 1), 4)
        return ListMarker(ordered: true, number: Int(digits) ?? 1, bullet: delimiter,
                          contentIndent: indent + digits.count + 1 + spaces,
                          content: String(tail.dropFirst(min(spaces, tail.count))))
    }

    mutating func parseList(first: ListMarker) -> MDBlock {
        var items: [MDListItem] = []
        var marker: ListMarker? = first
        while let current = marker {
            lines.removeFirst()
            var content = [current.content]
            var sawBlank = false
            while let line = lines.first {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                let indent = line.prefix { $0 == " " }.count
                if trimmed.isEmpty {
                    sawBlank = true
                    content.append("")
                    lines.removeFirst()
                    continue
                }
                if indent >= current.contentIndent || (indent >= 2 && indent > current.contentIndent - 3 && !sawBlank
                                                       && Self.listMarker(line) != nil) {
                    content.append(String(line.dropFirst(min(indent, current.contentIndent))))
                    lines.removeFirst()
                    sawBlank = false
                    continue
                }
                if !sawBlank, Self.listMarker(line) == nil, Self.fence(trimmed) == nil, !Self.isRule(trimmed),
                   !trimmed.hasPrefix("#"), !trimmed.hasPrefix(">") {
                    content.append(trimmed)  // lazy continuation of the item's paragraph
                    lines.removeFirst()
                    continue
                }
                break
            }
            while content.last == "" { content.removeLast() }
            var checkbox: Bool?
            if let head = content.first {
                for (box, value) in [("[ ] ", false), ("[x] ", true), ("[X] ", true)] where head.hasPrefix(box) {
                    checkbox = value
                    content[0] = String(head.dropFirst(4))
                }
                if checkbox == nil, ["[ ]", "[x]", "[X]"].contains(head) { checkbox = head != "[ ]"; content[0] = "" }
            }
            var parser = BlockParser(lines: content[...])
            items.append(MDListItem(checkbox: checkbox, blocks: parser.parse()))
            // The next sibling uses the same kind of marker.
            if let line = lines.first, let next = Self.listMarker(line), next.ordered == first.ordered, next.bullet == first.bullet {
                marker = next
            } else {
                marker = nil
            }
        }
        return .list(ordered: first.ordered, start: first.number, items: items)
    }

    // MARK: Tables

    mutating func parseTable(header: [String], alignments: [MDAlignment?]) -> MDBlock {
        lines.removeFirst(2)
        var rows: [[[MDInline]]] = []
        while let line = lines.first {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, trimmed.contains("|") else { break }
            var cells = Self.cells(trimmed)
            if cells.count < header.count { cells += Array(repeating: "", count: header.count - cells.count) }
            rows.append(cells.prefix(max(header.count, 1)).map { InlineParser.parse($0) })
            lines.removeFirst()
        }
        var aligned = alignments
        if aligned.count < header.count { aligned += Array(repeating: nil, count: header.count - aligned.count) }
        return .table(header: header.map { InlineParser.parse($0) }, rows: rows, alignments: Array(aligned.prefix(header.count)))
    }

    /// Cells of a table row; `\|` and pipes inside code spans stay in the cell.
    static func cells(_ row: String) -> [String] {
        var text = Substring(row.trimmingCharacters(in: .whitespaces))
        if text.hasPrefix("|") { text = text.dropFirst() }
        if text.hasSuffix("|"), !text.hasSuffix("\\|") { text = text.dropLast() }
        var cells: [String] = []
        var current = ""
        var inCode = false
        var previous: Character = " "
        for char in text {
            if char == "`" { inCode.toggle() }
            if char == "|", !inCode, previous != "\\" {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
            } else if char == "|", previous == "\\" {
                current.removeLast()
                current.append(char)
            } else {
                current.append(char)
            }
            previous = char
        }
        cells.append(current.trimmingCharacters(in: .whitespaces))
        return cells
    }

    static func tableDelimiter(_ line: String) -> [MDAlignment?]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.contains("-"), trimmed.allSatisfy({ "|-: ".contains($0) }) else { return nil }
        let parts = cells(trimmed)
        guard !parts.isEmpty else { return nil }
        var result: [MDAlignment?] = []
        for part in parts {
            guard part.contains("-"), part.allSatisfy({ $0 == "-" || $0 == ":" }) else { return nil }
            let left = part.hasPrefix(":"), right = part.hasSuffix(":")
            result.append(left && right ? .center : right ? .right : left ? .left : nil)
        }
        return result
    }

    // MARK: Small recognisers

    static func atxHeading(_ trimmed: String) -> (Int, String)? {
        let level = trimmed.prefix { $0 == "#" }.count
        guard (1...6).contains(level) else { return nil }
        let rest = trimmed.dropFirst(level)
        guard rest.isEmpty || rest.first == " " else { return nil }
        var text = rest.trimmingCharacters(in: .whitespaces)
        // Optional closing hashes.
        if let range = text.range(of: #"\s+#+$"#, options: .regularExpression) { text.removeSubrange(range) }
        if text.allSatisfy({ $0 == "#" }) { text = "" }
        return (level, text)
    }

    static func isRule(_ trimmed: String) -> Bool {
        guard let first = trimmed.first, "-*_".contains(first) else { return false }
        let compact = trimmed.filter { $0 != " " }
        return compact.count >= 3 && compact.allSatisfy { $0 == first }
    }

    static func isSetext(_ trimmed: String) -> Bool {
        guard let first = trimmed.first, first == "=" || first == "-" else { return false }
        return trimmed.allSatisfy { $0 == first }
    }

    static func isHTMLStart(_ trimmed: String) -> Bool {
        trimmed.range(of: #"^</?(div|p|table|tr|td|th|thead|tbody|pre|details|summary|section|article|center|ul|ol|li|h[1-6]|img|br|hr|span|picture|video|figure|!--)[\s>/]"#,
                      options: [.regularExpression, .caseInsensitive]) != nil
    }
}

// MARK: - Inlines

enum InlineParser {
    static func parse(_ text: String) -> [MDInline] {
        var parser = Scanner(chars: Array(text))
        return merge(parser.parse(until: nil))
    }

    static func merge(_ nodes: [MDInline]) -> [MDInline] {
        var result: [MDInline] = []
        for node in nodes {
            if case .text(let next) = node, case .text(let previous)? = result.last {
                result[result.count - 1] = .text(previous + next)
            } else {
                result.append(node)
            }
        }
        return result
    }

    fileprivate struct Scanner {
        let chars: [Character]
        var index = 0
        var buffer = ""
        var nodes: [MDInline] = []

        init(chars: [Character], index: Int = 0) {
            self.chars = chars
            self.index = index
        }

        static let escapable = Set("\\`*_{}[]()#+-.!|~<>\"'$&")

        mutating func flush() {
            if !buffer.isEmpty { nodes.append(.text(buffer)); buffer = "" }
        }

        /// Parses until `end` (exclusive) or the end of input.
        mutating func parse(until end: Int?) -> [MDInline] {
            let limit = end ?? chars.count
            while index < limit {
                let char = chars[index]
                switch char {
                case "\\":
                    if index + 1 < limit, chars[index + 1] == "\n" {
                        flush(); nodes.append(.hardBreak); index += 2
                    } else if index + 1 < limit, Self.escapable.contains(chars[index + 1]) {
                        buffer.append(chars[index + 1]); index += 2
                    } else {
                        buffer.append(char); index += 1
                    }
                case "\n":
                    if buffer.hasSuffix("  ") {
                        while buffer.hasSuffix(" ") { buffer.removeLast() }
                        flush(); nodes.append(.hardBreak)
                    } else {
                        while buffer.hasSuffix(" ") { buffer.removeLast() }
                        flush(); nodes.append(.softBreak)
                    }
                    index += 1
                case "`":
                    if !codeSpan(limit: limit) { buffer.append(char); index += 1 }
                case "*", "_", "~":
                    if !delimited(char, limit: limit) { buffer.append(char); index += 1 }
                case "!":
                    if index + 1 < limit, chars[index + 1] == "[", let (alt, destination, next) = linkParts(from: index + 1, limit: limit) {
                        flush()
                        nodes.append(.image(source: destination, alt: InlineParser.parse(alt).map(\.plainText).joined()))
                        index = next
                    } else {
                        buffer.append(char); index += 1
                    }
                case "[":
                    if let (label, destination, next) = linkParts(from: index, limit: limit) {
                        flush()
                        nodes.append(.link(destination: destination, InlineParser.parse(label)))
                        index = next
                    } else {
                        buffer.append(char); index += 1
                    }
                case "<":
                    if !angle(limit: limit) { buffer.append(char); index += 1 }
                case "h", "w":
                    if !bareURL(limit: limit) { buffer.append(char); index += 1 }
                default:
                    buffer.append(char); index += 1
                }
            }
            flush()
            return nodes
        }

        // `code`
        mutating func codeSpan(limit: Int) -> Bool {
            var run = 0
            while index + run < limit, chars[index + run] == "`" { run += 1 }
            var search = index + run
            while search < limit {
                if chars[search] == "`" {
                    var closing = 0
                    while search + closing < limit, chars[search + closing] == "`" { closing += 1 }
                    if closing == run {
                        var code = String(chars[(index + run)..<search]).replacingOccurrences(of: "\n", with: " ")
                        if code.count > 2, code.hasPrefix(" "), code.hasSuffix(" "), code.contains(where: { $0 != " " }) {
                            code = String(code.dropFirst().dropLast())
                        }
                        flush()
                        nodes.append(.code(code))
                        index = search + closing
                        return true
                    }
                    search += closing
                } else {
                    search += 1
                }
            }
            return false
        }

        // *em*, **strong**, ***both***, _em_, __strong__, ~~strike~~
        mutating func delimited(_ char: Character, limit: Int) -> Bool {
            var run = 0
            while index + run < limit, chars[index + run] == char { run += 1 }
            if char == "~", run != 2 && run != 1 { return false }
            let before: Character? = index > 0 ? chars[index - 1] : nil
            let after: Character? = index + run < limit ? chars[index + run] : nil
            guard let after, !after.isWhitespace else { return false }
            if char == "_", let before, before.isLetter || before.isNumber { return false }
            // Try the longest usable run first.
            for size in stride(from: min(run, 3), through: 1, by: -1) {
                if char == "~", size != 2 && !(size == 1 && run == 1) { continue }
                guard let close = closing(char, size: size, from: index + size, limit: limit) else { continue }
                let inner = InlineParser.Scanner.subparse(chars, from: index + size, to: close)
                flush()
                if run > size { buffer.append(String(repeating: char, count: run - size)) ; flush() }
                switch (char, size) {
                case ("~", _): nodes.append(.strikethrough(inner))
                case (_, 3): nodes.append(.strong([.emphasis(inner)]))
                case (_, 2): nodes.append(.strong(inner))
                default: nodes.append(.emphasis(inner))
                }
                index = close + size
                return true
            }
            return false
        }

        func closing(_ char: Character, size: Int, from start: Int, limit: Int) -> Int? {
            var position = start
            var inCode = false
            while position < limit {
                let current = chars[position]
                if current == "\\" { position += 2; continue }
                if current == "`" { inCode.toggle() }
                if !inCode, current == char {
                    var run = 0
                    while position + run < limit, chars[position + run] == char { run += 1 }
                    let before = chars[position - 1]
                    let after: Character? = position + run < limit ? chars[position + run] : nil
                    let flanking = !before.isWhitespace && position > start
                    let wordAfter = after.map { $0.isLetter || $0.isNumber } ?? false
                    if flanking, run >= size, !(char == "_" && wordAfter) {
                        // A longer closing run closes the outer delimiter first (***a** b*).
                        return position + (run - size)
                    }
                    position += run
                    continue
                }
                position += 1
            }
            return nil
        }

        static func subparse(_ chars: [Character], from start: Int, to end: Int) -> [MDInline] {
            var scanner = Scanner(chars: chars, index: start)
            return InlineParser.merge(scanner.parse(until: end))
        }

        // [label](destination "title")
        func linkParts(from open: Int, limit: Int) -> (String, String, Int)? {
            var depth = 0
            var position = open
            var closeBracket: Int?
            while position < limit {
                let char = chars[position]
                if char == "\\" { position += 2; continue }
                if char == "`" {  // brackets inside code spans do not count
                    if let end = chars[(position + 1)..<limit].firstIndex(of: "`") { position = end + 1; continue }
                }
                if char == "[" { depth += 1 }
                if char == "]" {
                    depth -= 1
                    if depth == 0 { closeBracket = position; break }
                }
                position += 1
            }
            guard let close = closeBracket, close + 1 < limit, chars[close + 1] == "(" else { return nil }
            var cursor = close + 2
            var parens = 1
            var destination = ""
            var angled = false
            if cursor < limit, chars[cursor] == "<" { angled = true; cursor += 1 }
            while cursor < limit {
                let char = chars[cursor]
                if angled {
                    if char == ">" { cursor += 1; break }
                } else {
                    if char == "(" { parens += 1 }
                    if char == ")" { parens -= 1; if parens == 0 { break } }
                    if char == " " || char == "\n" { break }
                }
                destination.append(char)
                cursor += 1
            }
            // Skip an optional title up to the closing parenthesis.
            while cursor < limit, chars[cursor] != ")" {
                if chars[cursor] == "\n" && destination.isEmpty { return nil }
                cursor += 1
            }
            guard cursor < limit, chars[cursor] == ")" else { return nil }
            return (String(chars[(open + 1)..<close]), destination, cursor + 1)
        }

        // <https://…>, <mail@x.y>, inline HTML
        mutating func angle(limit: Int) -> Bool {
            guard let end = chars[(index + 1)..<limit].firstIndex(of: ">") else { return false }
            let inner = String(chars[(index + 1)..<end])
            if inner.contains(" ") == false, inner.range(of: #"^[a-zA-Z][a-zA-Z0-9+.-]{1,31}:\S+$"#, options: .regularExpression) != nil {
                flush(); nodes.append(.link(destination: inner, [.text(inner)])); index = end + 1; return true
            }
            if inner.range(of: #"^[^\s@<>]+@[^\s@<>]+\.[a-zA-Z]{2,}$"#, options: .regularExpression) != nil {
                flush(); nodes.append(.link(destination: "mailto:" + inner, [.text(inner)])); index = end + 1; return true
            }
            if inner.range(of: #"^/?[a-zA-Z][a-zA-Z0-9-]*(\s[^<>]*)?/?$"#, options: .regularExpression) != nil {
                flush(); nodes.append(.html("<" + inner + ">")); index = end + 1; return true
            }
            return false
        }

        // https://example.com, www.example.com
        mutating func bareURL(limit: Int) -> Bool {
            if index > 0 {
                let before = chars[index - 1]
                if before.isLetter || before.isNumber || before == "/" { return false }
            }
            let rest = String(chars[index..<min(limit, index + 2048)])
            guard let range = rest.range(of: #"^(https?://|www\.)[^\s<]+"#, options: .regularExpression) else { return false }
            var url = String(rest[range])
            // Trailing punctuation is not part of the link; nor is an unbalanced ")".
            while let last = url.last, "?!.,:*_~'\";".contains(last) { url.removeLast() }
            while url.hasSuffix(")"), url.filter({ $0 == ")" }).count > url.filter({ $0 == "(" }).count { url.removeLast() }
            guard url.count > 4 else { return false }
            flush()
            nodes.append(.link(destination: url.hasPrefix("www.") ? "https://" + url : url, [.text(url)]))
            index += url.count
            return true
        }
    }
}
