import AppKit
import SwiftUI

/// A small, generic highlighter: comments, strings, numbers, keywords and
/// type-like names, with Xcode's default palette in light and dark mode.
enum SyntaxHighlighter {
    enum Token { case plain, keyword, string, number, comment, type, added, removed, meta }

    static func color(_ token: Token) -> Color? {
        switch token {
        case .plain: nil
        case .keyword: Palette.keyword
        case .string: Palette.string
        case .number: Palette.number
        case .comment: Palette.comment
        case .type: Palette.type
        case .added: Palette.added
        case .removed: Palette.removed
        case .meta: Palette.meta
        }
    }

    private struct Grammar {
        var keywords: Set<String> = []
        var lineComments: [String] = []
        var blockComment: (String, String)? = nil
        var quotes: Set<Character> = ["\"", "'"]
        var capitalizedTypes = false
        var caseInsensitive = false
    }

    private static func grammar(for language: String) -> Grammar? {
        let cStyle: Set<String> = ["if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue",
                                    "return", "true", "false", "null", "void", "static", "const", "struct", "enum",
                                    "typedef", "sizeof", "goto", "volatile", "extern", "unsigned", "signed", "int",
                                    "char", "float", "double", "long", "short", "bool", "auto", "inline", "union"]
        switch language {
        case "swift":
            return Grammar(keywords: ["func", "let", "var", "if", "else", "guard", "return", "struct", "class", "enum",
                                      "protocol", "extension", "import", "for", "in", "while", "repeat", "switch", "case",
                                      "default", "break", "continue", "true", "false", "nil", "self", "Self", "init",
                                      "deinit", "public", "private", "internal", "fileprivate", "open", "static", "final",
                                      "override", "mutating", "throws", "throw", "rethrows", "try", "catch", "do", "async",
                                      "await", "actor", "some", "any", "where", "as", "is", "typealias", "associatedtype",
                                      "weak", "unowned", "lazy", "inout", "defer", "super", "nonisolated", "isolated",
                                      "convenience", "required", "subscript", "operator", "get", "set", "willSet", "didSet"],
                           lineComments: ["//"], blockComment: ("/*", "*/"), quotes: ["\""], capitalizedTypes: true)
        case "python", "py":
            return Grammar(keywords: ["def", "class", "return", "if", "elif", "else", "for", "while", "in", "not", "and",
                                      "or", "is", "None", "True", "False", "import", "from", "as", "with", "try", "except",
                                      "finally", "raise", "lambda", "yield", "pass", "break", "continue", "global",
                                      "nonlocal", "async", "await", "del", "assert", "self", "match", "case"],
                           lineComments: ["#"], capitalizedTypes: true)
        case "javascript", "js", "jsx", "typescript", "ts", "tsx", "mjs", "cjs":
            return Grammar(keywords: ["function", "const", "let", "var", "return", "if", "else", "for", "while", "do",
                                      "switch", "case", "default", "break", "continue", "new", "this", "class", "extends",
                                      "super", "import", "export", "from", "as", "async", "await", "try", "catch", "finally",
                                      "throw", "typeof", "instanceof", "in", "of", "true", "false", "null", "undefined",
                                      "yield", "delete", "void", "interface", "type", "enum", "implements", "public",
                                      "private", "protected", "readonly", "static", "declare", "namespace", "keyof",
                                      "satisfies", "abstract"],
                           lineComments: ["//"], blockComment: ("/*", "*/"), quotes: ["\"", "'", "`"], capitalizedTypes: true)
        case "json", "jsonc", "json5":
            return Grammar(keywords: ["true", "false", "null"], lineComments: ["//"], quotes: ["\""])
        case "bash", "sh", "zsh", "shell", "console", "fish":
            return Grammar(keywords: ["if", "then", "else", "elif", "fi", "for", "while", "do", "done", "case", "esac",
                                      "in", "function", "return", "local", "export", "echo", "cd", "exit", "set", "unset",
                                      "source", "sudo", "until", "select", "readonly", "shift", "trap", "eval", "exec"],
                           lineComments: ["#"])
        case "go", "golang":
            return Grammar(keywords: ["func", "package", "import", "var", "const", "type", "struct", "interface", "map",
                                      "chan", "go", "defer", "return", "if", "else", "for", "range", "switch", "case",
                                      "default", "break", "continue", "select", "fallthrough", "goto", "nil", "true",
                                      "false", "make", "new", "len", "cap", "append", "error", "string", "int", "bool",
                                      "byte", "rune", "float64", "int64"],
                           lineComments: ["//"], blockComment: ("/*", "*/"), quotes: ["\"", "'", "`"], capitalizedTypes: true)
        case "rust", "rs":
            return Grammar(keywords: ["fn", "let", "mut", "const", "static", "struct", "enum", "trait", "impl", "for",
                                      "in", "while", "loop", "if", "else", "match", "return", "break", "continue", "use",
                                      "mod", "pub", "crate", "self", "Self", "super", "where", "as", "ref", "move", "async",
                                      "await", "dyn", "unsafe", "extern", "type", "true", "false", "Some", "None", "Ok", "Err"],
                           lineComments: ["//"], blockComment: ("/*", "*/"), quotes: ["\""], capitalizedTypes: true)
        case "c", "h", "cpp", "c++", "cc", "hpp", "cxx", "objc", "objective-c":
            return Grammar(keywords: cStyle.union(["class", "public", "private", "protected", "virtual", "template",
                                                   "typename", "namespace", "using", "new", "delete", "this", "nullptr",
                                                   "try", "catch", "throw", "constexpr", "override", "noexcept",
                                                   "#include", "#define", "#ifdef", "#ifndef", "#endif", "#if", "#else",
                                                   "#pragma", "std"]),
                           lineComments: ["//"], blockComment: ("/*", "*/"), capitalizedTypes: true)
        case "java", "kotlin", "kt", "kts", "scala", "csharp", "cs", "c#", "dart":
            return Grammar(keywords: cStyle.union(["class", "interface", "extends", "implements", "public", "private",
                                                   "protected", "new", "this", "super", "try", "catch", "finally", "throw",
                                                   "throws", "import", "package", "final", "abstract", "fun", "val", "var",
                                                   "when", "is", "in", "object", "companion", "data", "sealed", "override",
                                                   "open", "internal", "suspend", "string", "using", "namespace", "async",
                                                   "await", "let", "lateinit", "instanceof", "synchronized"]),
                           lineComments: ["//"], blockComment: ("/*", "*/"), capitalizedTypes: true)
        case "ruby", "rb":
            return Grammar(keywords: ["def", "end", "class", "module", "if", "elsif", "else", "unless", "while", "until",
                                      "for", "in", "do", "return", "yield", "begin", "rescue", "ensure", "raise", "nil",
                                      "true", "false", "self", "require", "include", "attr_accessor", "attr_reader",
                                      "puts", "then", "case", "when", "and", "or", "not", "lambda", "proc"],
                           lineComments: ["#"], capitalizedTypes: true)
        case "yaml", "yml", "toml", "ini", "conf", "properties", "dockerfile", "makefile", "make":
            return Grammar(keywords: ["true", "false", "null", "yes", "no", "on", "off", "FROM", "RUN", "CMD", "COPY",
                                      "ADD", "ENV", "WORKDIR", "EXPOSE", "ENTRYPOINT", "ARG"],
                           lineComments: ["#"])
        case "sql", "postgres", "postgresql", "mysql", "sqlite":
            return Grammar(keywords: ["select", "from", "where", "and", "or", "not", "insert", "into", "values", "update",
                                      "set", "delete", "create", "table", "drop", "alter", "add", "index", "join", "left",
                                      "right", "inner", "outer", "on", "group", "by", "order", "having", "limit", "offset",
                                      "as", "distinct", "null", "is", "in", "like", "between", "primary", "key", "foreign",
                                      "references", "default", "union", "all", "case", "when", "then", "else", "end",
                                      "exists", "count", "sum", "avg", "min", "max", "integer", "text", "varchar", "with",
                                      "returning", "asc", "desc", "true", "false", "begin", "commit"],
                           lineComments: ["--"], blockComment: ("/*", "*/"), caseInsensitive: true)
        case "html", "xml", "svg", "vue", "plist":
            return Grammar(keywords: [], blockComment: ("<!--", "-->"))
        case "css", "scss", "less":
            return Grammar(keywords: ["important", "media", "import", "keyframes", "from", "to", "root"],
                           lineComments: language == "css" ? [] : ["//"], blockComment: ("/*", "*/"))
        default:
            return nil
        }
    }

    /// Highlighted text; plain for unknown languages and very large blocks.
    static func highlight(_ source: String, language: String?) -> AttributedString {
        let lang = (language ?? "").lowercased().split(separator: " ").first.map(String.init) ?? ""
        if lang == "diff" || lang == "patch" || (lang.isEmpty && looksLikeDiff(source)) {
            return diff(source)
        }
        guard source.utf8.count < 120_000, let grammar = grammar(for: lang) else { return AttributedString(source) }
        if ["html", "xml", "svg", "vue", "plist"].contains(lang) { return markup(source, grammar: grammar) }
        return tokens(source, grammar: grammar, cssLike: ["css", "scss", "less"].contains(lang))
    }

    private static func looksLikeDiff(_ source: String) -> Bool {
        source.hasPrefix("diff --git") || source.hasPrefix("--- ") || source.hasPrefix("@@ ")
    }

    private static func diff(_ source: String) -> AttributedString {
        var result = AttributedString()
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
        for (index, line) in lines.enumerated() {
            var piece = AttributedString(line + (index < lines.count - 1 ? "\n" : ""))
            if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("diff ") || line.hasPrefix("index ") {
                piece.foregroundColor = Palette.comment
                piece.inlinePresentationIntent = .stronglyEmphasized
            } else if line.hasPrefix("+") {
                piece.foregroundColor = Palette.added
                piece.backgroundColor = Palette.added.opacity(0.12)
            } else if line.hasPrefix("-") {
                piece.foregroundColor = Palette.removed
                piece.backgroundColor = Palette.removed.opacity(0.12)
            } else if line.hasPrefix("@@") {
                piece.foregroundColor = Palette.meta
            }
            result += piece
        }
        return result
    }

    private struct Builder {
        var result = AttributedString()
        var pending = ""
        var pendingToken: Token = .plain

        mutating func add(_ text: some StringProtocol, _ token: Token) {
            if token == pendingToken { pending += text; return }
            flush()
            pending = String(text)
            pendingToken = token
        }

        mutating func flush() {
            guard !pending.isEmpty else { return }
            var piece = AttributedString(pending)
            if let color = SyntaxHighlighter.color(pendingToken) { piece.foregroundColor = color }
            result += piece
            pending = ""
        }
    }

    private static func tokens(_ source: String, grammar: Grammar, cssLike: Bool) -> AttributedString {
        let chars = Array(source)
        let n = chars.count
        var out = Builder()
        var i = 0
        func starts(_ s: String, at index: Int) -> Bool {
            let pattern = Array(s)
            guard index + pattern.count <= n else { return false }
            for k in 0..<pattern.count where chars[index + k] != pattern[k] { return false }
            return true
        }
        while i < n {
            let c = chars[i]
            // Comments
            if let comment = grammar.lineComments.first(where: { starts($0, at: i) }) {
                // "#" in shell only starts a comment at a word boundary.
                if comment == "#" && i > 0 && !(chars[i - 1].isWhitespace) {
                    out.add(String(c), .plain); i += 1; continue
                }
                var j = i
                while j < n && chars[j] != "\n" { j += 1 }
                out.add(String(chars[i..<j]), .comment)
                i = j
                continue
            }
            if let (open, close) = grammar.blockComment, starts(open, at: i) {
                var j = i + open.count
                while j < n && !starts(close, at: j) { j += 1 }
                j = min(n, j + close.count)
                out.add(String(chars[i..<j]), .comment)
                i = j
                continue
            }
            // Strings
            if grammar.quotes.contains(c) {
                let triple = i + 2 < n && chars[i + 1] == c && chars[i + 2] == c
                var j = i + (triple ? 3 : 1)
                while j < n {
                    if chars[j] == "\\" { j += 2; continue }
                    if triple {
                        if chars[j] == c && j + 2 < n && chars[j + 1] == c && chars[j + 2] == c { j += 3; break }
                    } else if chars[j] == c { j += 1; break }
                    else if chars[j] == "\n" && c != "`" { break }
                    j += 1
                }
                j = min(j, n)
                out.add(String(chars[i..<j]), .string)
                i = j
                continue
            }
            // Numbers
            if c.isNumber && (i == 0 || !(chars[i - 1].isLetter || chars[i - 1] == "_")) {
                var j = i
                while j < n && (chars[j].isHexDigit || chars[j] == "." || chars[j] == "_" || chars[j] == "x" || chars[j] == "X") {
                    j += 1
                }
                if cssLike { while j < n && (chars[j].isLetter || chars[j] == "%") { j += 1 } }
                out.add(String(chars[i..<j]), .number)
                i = j
                continue
            }
            // Words
            if c.isLetter || c == "_" || c == "#" || c == "@" || (c == "$" && i + 1 < n && chars[i + 1].isLetter) {
                var j = i + 1
                while j < n && (chars[j].isLetter || chars[j].isNumber || chars[j] == "_" || (cssLike && chars[j] == "-")) {
                    j += 1
                }
                let word = String(chars[i..<j])
                let key = grammar.caseInsensitive ? word.lowercased() : word
                if grammar.keywords.contains(key) || (c == "@" && word.count > 1) {
                    out.add(word, .keyword)
                } else if c == "$" {
                    out.add(word, .type)
                } else if grammar.capitalizedTypes, let first = word.first, first.isUppercase, word.count > 1 {
                    out.add(word, .type)
                } else {
                    out.add(word, .plain)
                }
                i = j
                continue
            }
            out.add(String(c), .plain)
            i += 1
        }
        out.flush()
        return out.result
    }

    private static func markup(_ source: String, grammar: Grammar) -> AttributedString {
        let chars = Array(source)
        let n = chars.count
        var out = Builder()
        var i = 0
        while i < n {
            if chars[i] == "<" {
                if i + 3 < n && String(chars[i..<(i + 4)]) == "<!--" {
                    var j = i + 4
                    while j + 2 < n && !(chars[j] == "-" && chars[j + 1] == "-" && chars[j + 2] == ">") { j += 1 }
                    j = min(n, j + 3)
                    out.add(String(chars[i..<j]), .comment); i = j; continue
                }
                // Tag name
                var j = i + 1
                if j < n && (chars[j] == "/" || chars[j] == "!" || chars[j] == "?") { j += 1 }
                while j < n && (chars[j].isLetter || chars[j].isNumber || chars[j] == "-" || chars[j] == ":") { j += 1 }
                out.add(String(chars[i..<j]), .keyword)
                i = j
                // Attributes until '>'
                while i < n && chars[i] != ">" {
                    if chars[i] == "\"" || chars[i] == "'" {
                        let q = chars[i]
                        var k = i + 1
                        while k < n && chars[k] != q { k += 1 }
                        k = min(n, k + 1)
                        out.add(String(chars[i..<k]), .string); i = k; continue
                    }
                    if chars[i].isLetter {
                        var k = i
                        while k < n && (chars[k].isLetter || chars[k] == "-" || chars[k] == ":") { k += 1 }
                        out.add(String(chars[i..<k]), .type); i = k; continue
                    }
                    out.add(String(chars[i]), .plain); i += 1
                }
                if i < n { out.add(">", .keyword); i += 1 }
                continue
            }
            out.add(String(chars[i]), .plain)
            i += 1
        }
        out.flush()
        return out.result
    }
}

/// Xcode's default (light) and default (dark) source colours.
enum Palette {
    static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let hex = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                           blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        })
    }

    static let keyword = dynamic(light: 0xAD3DA4, dark: 0xFF7AB2)
    static let string = dynamic(light: 0xC41A16, dark: 0xFF8170)
    static let number = dynamic(light: 0x1C00CF, dark: 0xD9C97C)
    static let comment = dynamic(light: 0x5D6C79, dark: 0x7F8C98)
    static let type = dynamic(light: 0x3E8087, dark: 0x5DD8FF)
    static let added = dynamic(light: 0x1A7F37, dark: 0x56D364)
    static let removed = dynamic(light: 0xCF222E, dark: 0xF85149)
    static let meta = dynamic(light: 0x8250DF, dark: 0xBC8CFF)
}
