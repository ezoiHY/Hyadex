import Foundation

/// TeX found in a message before Markdown parsing. The Markdown parser would
/// otherwise eat backslashes and underscores, so math is replaced by private-use
/// placeholders first and restored when rendering.
struct MathSpans: Hashable, Sendable {
    /// Placeholder index → TeX source.
    var latex: [String] = []
    /// Placeholder index → the original text, for contexts that show source (code).
    var original: [String] = []
    /// Indices of display-style ("$$", "\[") spans.
    var display: Set<Int> = []

    static let open: Character = "\u{E000}"
    static let close: Character = "\u{E001}"

    func token(_ index: Int) -> String { "\(Self.open)\(index)\(Self.close)" }

    /// Puts the original source back, e.g. in code blocks.
    func restore(_ text: String) -> String {
        guard text.contains(Self.open) else { return text }
        var result = ""
        var iterator = text.makeIterator()
        while let char = iterator.next() {
            guard char == Self.open else { result.append(char); continue }
            var digits = ""
            while let next = iterator.next(), next != Self.close { digits.append(next) }
            if let index = Int(digits), original.indices.contains(index) {
                result += original[index]
            } else {
                result += String(Self.open) + digits
            }
        }
        return result
    }
}

enum MathExtractor {
    /// Returns the text with every math span outside code replaced by a token.
    static func extract(_ text: String) -> (String, MathSpans) {
        var spans = MathSpans()
        guard text.contains("$") || text.contains("\\(") || text.contains("\\[") else { return (text, spans) }
        var output = ""
        var prose: [Substring] = []
        var fence: (char: Character, count: Int)? = nil

        func flushProse() {
            guard !prose.isEmpty else { return }
            output += scan(prose.joined(separator: "\n"), into: &spans)
            prose.removeAll()
        }

        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        for (index, line) in lines.enumerated() {
            let newline = index < lines.count - 1 ? "\n" : ""
            let trimmed = line.drop { $0 == " " || $0 == "\t" || $0 == ">" }
            if let open = fence {
                output += line + newline
                let run = trimmed.prefix { $0 == open.char }
                if run.count >= open.count && trimmed.dropFirst(run.count).allSatisfy({ $0 == " " }) { fence = nil }
                continue
            }
            if let first = trimmed.first, first == "`" || first == "~" {
                let run = trimmed.prefix { $0 == first }
                if run.count >= 3 {
                    if !prose.isEmpty {
                        flushProse()
                        output += "\n"  // the newline that ended the last prose line
                    }
                    output += line + newline
                    fence = (first, run.count)
                    continue
                }
            }
            prose.append(line)
        }
        if !prose.isEmpty {
            flushProse()
        }
        return (output, spans)
    }

    /// Scans prose (no fenced code), skipping inline code spans and escapes.
    private static func scan(_ prose: String, into spans: inout MathSpans) -> String {
        let chars = Array(prose)
        var result = ""
        result.reserveCapacity(chars.count)
        var i = 0
        let n = chars.count

        func add(_ latex: String, source: String, display: Bool) {
            let index = spans.latex.count
            spans.latex.append(latex)
            spans.original.append(source)
            if display { spans.display.insert(index) }
            result += spans.token(index)
        }

        func find(_ pattern: [Character], from start: Int) -> Int? {
            guard pattern.count > 0, start <= n - pattern.count else { return nil }
            var j = start
            while j <= n - pattern.count {
                if chars[j] == "\\" && pattern.first != "\\" { j += 2; continue }
                if chars[j] == pattern[0] && Array(chars[j..<(j + pattern.count)]) == pattern { return j }
                j += 1
            }
            return nil
        }

        while i < n {
            let c = chars[i]
            // Inline code: copy verbatim up to the matching backtick run.
            if c == "`" {
                var run = 0
                while i + run < n && chars[i + run] == "`" { run += 1 }
                var j = i + run
                var close: Int? = nil
                while j < n {
                    if chars[j] == "`" {
                        var r = 0
                        while j + r < n && chars[j + r] == "`" { r += 1 }
                        if r == run { close = j; break }
                        j += r
                    } else { j += 1 }
                }
                if let close {
                    result += String(chars[i..<(close + run)])
                    i = close + run
                } else {
                    result += String(chars[i..<(i + run)])
                    i += run
                }
                continue
            }
            if c == "\\", i + 1 < n {
                let next = chars[i + 1]
                if next == "[" || next == "(" {
                    let closing: [Character] = next == "[" ? ["\\", "]"] : ["\\", ")"]
                    if let end = find(closing, from: i + 2) {
                        let body = String(chars[(i + 2)..<end])
                        if next == "(" && body.contains("\n") {
                            // Inline math stays on one line.
                        } else if !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            add(body.trimmingCharacters(in: .whitespacesAndNewlines),
                                source: String(chars[i..<(end + 2)]), display: next == "[")
                            i = end + 2
                            continue
                        }
                    }
                }
                // Any other escape (including \$) is copied as is.
                result.append(c)
                result.append(next)
                i += 2
                continue
            }
            if c == "$" {
                if i + 1 < n && chars[i + 1] == "$" {
                    if let end = find(["$", "$"], from: i + 2) {
                        let body = String(chars[(i + 2)..<end]).trimmingCharacters(in: .whitespacesAndNewlines)
                        if !body.isEmpty {
                            add(body, source: String(chars[i..<(end + 2)]), display: true)
                            i = end + 2
                            continue
                        }
                    }
                    // Unterminated (still streaming) or empty: plain text.
                    result += "$$"
                    i += 2
                    continue
                }
                if let end = inlineDollarEnd(chars, open: i) {
                    add(String(chars[(i + 1)..<end]), source: String(chars[i...end]), display: false)
                    i = end + 1
                    continue
                }
            }
            result.append(c)
            i += 1
        }
        return result
    }

    /// Pandoc-style inline `$…$`: a non-space right after the opening `$`, a
    /// non-space right before the closing one, no digit right after it, one line.
    /// Only the next `$` is a candidate, so "costs $5 and $10" stays text.
    private static func inlineDollarEnd(_ chars: [Character], open: Int) -> Int? {
        let n = chars.count
        guard open + 1 < n, !chars[open + 1].isWhitespace else { return nil }
        var j = open + 1
        while j < n {
            let c = chars[j]
            if c == "\n" { return nil }
            if c == "\\" { j += 2; continue }
            if c == "$" {
                guard !chars[j - 1].isWhitespace else { return nil }
                if j + 1 < n, chars[j + 1].isNumber { return nil }
                return j > open + 1 ? j : nil
            }
            j += 1
        }
        return nil
    }
}
