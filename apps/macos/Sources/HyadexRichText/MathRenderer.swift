import AppKit
import CoreText
import SwiftUI

/// TeX math laid out natively (TeX's box rules, simplified) and drawn with
/// CoreText in STIX Two Math, which ships with macOS. The image is
/// resolution-independent and uses the text colour of the current appearance.
@MainActor
enum MathRenderer {
    struct Rendered {
        let image: NSImage
        /// Distance from the image bottom to the math baseline.
        let descent: CGFloat
    }

    private final class Entry { let value: Rendered?; init(_ value: Rendered?) { self.value = value } }
    private static let cache: NSCache<NSString, Entry> = {
        let cache = NSCache<NSString, Entry>()
        cache.countLimit = 600
        return cache
    }()

    /// `nil` when the TeX does not parse (the caller shows the source).
    static func render(_ latex: String, fontSize: CGFloat, display: Bool, dark: Bool) -> Rendered? {
        let key = "\(display ? "D" : "T")\(fontSize)\(dark ? "d" : "l")\u{0}\(latex)" as NSString
        if let hit = cache.object(forKey: key) { return hit.value }
        let rendered = make(latex, fontSize: fontSize, display: display, dark: dark)
        cache.setObject(Entry(rendered), forKey: key)
        return rendered
    }

    private static func make(_ latex: String, fontSize: CGFloat, display: Bool, dark: Bool) -> Rendered? {
        var parser = TeXParser(latex)
        guard let nodes = try? parser.parse() else { return nil }
        let color = textColor(dark: dark)
        var layout = TeXLayout(em: fontSize, color: color)
        let box = layout.row(nodes, style: display ? .display : .text)
        guard box.width > 0 else { return nil }
        let pad: CGFloat = 1.5
        let size = CGSize(width: ceil(box.width + pad * 2), height: ceil(box.ascent + box.descent + pad * 2))
        let items = box.items
        let baseline = box.descent + pad
        let image = NSImage(size: size, flipped: false) { _ in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            context.saveGState()
            for item in items { item.draw(in: context, origin: CGPoint(x: pad, y: baseline), color: color) }
            context.restoreGState()
            return true
        }
        image.accessibilityDescription = latex
        return Rendered(image: image, descent: baseline)
    }

    static func textColor(dark: Bool) -> NSColor {
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)!
        var resolved = NSColor.labelColor
        appearance.performAsCurrentDrawingAppearance {
            resolved = NSColor.labelColor.usingColorSpace(.sRGB) ?? NSColor.labelColor
        }
        return resolved
    }
}

// MARK: - Syntax tree

enum AtomClass { case ord, op, bin, rel, open, close, punct, inner }

indirect enum TeXNode {
    case symbol(String, AtomClass)
    case group([TeXNode])
    case fraction(TeXNode, TeXNode, rule: Bool, style: TeXStyle?)
    case radical(TeXNode, index: TeXNode?)
    case scripts(base: TeXNode, sub: TeXNode?, sup: TeXNode?)
    /// ∑, ∫, lim …; `limits`: scripts above and below (nil: in display style).
    case largeOp(String, limits: Bool?, isText: Bool)
    case delimited(left: String, right: String, [TeXNode])
    /// A delimiter of a fixed size (\big …); `scale` in em.
    case sizedDelimiter(String, scale: CGFloat, AtomClass)
    case accent(TeXNode, AccentKind)
    case text(String, bold: Bool, italic: Bool)
    case space(CGFloat)
    case matrix(rows: [[TeXNode]], kind: MatrixKind)
    case styled(TeXStyle, [TeXNode])
    case boxed(TeXNode)
    case stack(over: TeXNode?, base: TeXNode, under: TeXNode?)
}

enum AccentKind { case hat, wideHat, tilde, bar, vector, dot, ddot, check, breve, acute, grave, underline, overbrace, underbrace }

enum MatrixKind { case plain, cases, aligned, small }

enum TeXStyle: Int, Comparable {
    case display, text, script, scriptScript
    static func < (a: TeXStyle, b: TeXStyle) -> Bool { a.rawValue < b.rawValue }
    var scale: CGFloat { self == .script ? 0.7 : self == .scriptScript ? 0.5 : 1 }
    var superscript: TeXStyle { self <= .text ? .script : .scriptScript }
    var fraction: TeXStyle { self == .display ? .text : self == .text ? .script : .scriptScript }
}

struct TeXError: Error {}

// MARK: - Parser

struct TeXParser {
    private let chars: [Character]
    private var index = 0

    init(_ source: String) { chars = Array(source) }

    mutating func parse() throws -> [TeXNode] {
        let nodes = try expression(until: [])
        guard index >= chars.count else { throw TeXError() }
        return nodes
    }

    private var peek: Character? { index < chars.count ? chars[index] : nil }

    private mutating func skipSpaces() {
        while let char = peek, char.isWhitespace { index += 1 }
    }

    /// A list of atoms with scripts, stopping before "}" or a terminator
    /// ("&", "\\", "\right", "\end").
    private mutating func expression(until terminators: Set<String>) throws -> [TeXNode] {
        var nodes: [TeXNode] = []
        while true {
            skipSpaces()
            guard let char = peek else { break }
            if char == "}" { break }
            if char == "&", terminators.contains("&") { break }
            if char == "\\", let name = peekCommand(), terminators.contains(name) { break }
            if char == "^" || char == "_" {
                index += 1
                let script = try argument()
                let base = nodes.popLast() ?? .group([])
                nodes.append(attach(script, to: base, superscript: char == "^"))
                continue
            }
            if char == "'" {
                index += 1
                var primes = "′"
                while peek == "'" { index += 1; primes += "′" }
                // The prime glyph is already raised; it joins its base.
                let base = nodes.popLast() ?? .group([])
                nodes.append(.group([base, .symbol(primes, .ord)]))
                continue
            }
            if char == "\\", let name = peekCommand(), name == "limits" || name == "nolimits" {
                index += name.count + 1
                if case .largeOp(let symbol, _, let isText)? = nodes.last {
                    nodes[nodes.count - 1] = .largeOp(symbol, limits: name == "limits", isText: isText)
                }
                continue
            }
            if let style = styleSwitch() {
                // \displaystyle applies to the rest of the group.
                nodes.append(.styled(style, try expression(until: terminators)))
                break
            }
            nodes.append(try atom())
        }
        return nodes
    }

    private func attach(_ script: TeXNode, to base: TeXNode, superscript: Bool) -> TeXNode {
        if case .scripts(let inner, let sub, let sup) = base {
            if superscript, sup == nil { return .scripts(base: inner, sub: sub, sup: script) }
            if !superscript, sub == nil { return .scripts(base: inner, sub: script, sup: sup) }
        }
        return superscript ? .scripts(base: base, sub: nil, sup: script) : .scripts(base: base, sub: script, sup: nil)
    }

    private func peekCommand() -> String? {
        guard index < chars.count, chars[index] == "\\", index + 1 < chars.count else { return nil }
        let next = chars[index + 1]
        if !next.isLetter { return String(next) }
        var end = index + 1
        while end < chars.count, chars[end].isLetter { end += 1 }
        if end < chars.count, chars[end] == "*" { end += 1 }
        return String(chars[(index + 1)..<end])
    }

    private mutating func command() -> String {
        let name = peekCommand() ?? ""
        index += name.count + 1
        return name
    }

    private mutating func styleSwitch() -> TeXStyle? {
        guard let name = peekCommand() else { return nil }
        let style: TeXStyle? = switch name {
        case "displaystyle": .display
        case "textstyle": .text
        case "scriptstyle": .script
        case "scriptscriptstyle": .scriptScript
        default: nil
        }
        if style != nil { index += name.count + 1 }
        return style
    }

    /// One argument: a group or a single token.
    private mutating func argument() throws -> TeXNode {
        skipSpaces()
        guard let char = peek else { throw TeXError() }
        if char == "{" {
            index += 1
            let nodes = try expression(until: [])
            guard peek == "}" else { throw TeXError() }
            index += 1
            return nodes.count == 1 ? nodes[0] : .group(nodes)
        }
        return try atom()
    }

    /// The raw text of a braced argument (for \text, \begin …).
    private mutating func rawArgument() throws -> String {
        skipSpaces()
        guard peek == "{" else {
            guard let char = peek else { throw TeXError() }
            index += 1
            return String(char)
        }
        index += 1
        var depth = 1
        var text = ""
        while let char = peek {
            index += 1
            if char == "\\", let next = peek { text.append(char); text.append(next); index += 1; continue }
            if char == "{" { depth += 1 }
            if char == "}" { depth -= 1; if depth == 0 { return text } }
            text.append(char)
        }
        throw TeXError()
    }

    private mutating func optionalArgument() throws -> TeXNode? {
        skipSpaces()
        guard peek == "[" else { return nil }
        index += 1
        var nodes: [TeXNode] = []
        while let char = peek, char != "]" {
            if char.isWhitespace { index += 1; continue }
            nodes.append(try atom())
        }
        guard peek == "]" else { throw TeXError() }
        index += 1
        return .group(nodes)
    }

    private mutating func atom() throws -> TeXNode {
        skipSpaces()
        guard let char = peek else { throw TeXError() }
        if char == "{" { return try argument() }
        if char == "\\" { return try commandAtom() }
        index += 1
        if char.isASCII, char.isNumber {
            var number = String(char)
            while let next = peek, next.isASCII,
                  next.isNumber || (next == "." && index + 1 < chars.count && chars[index + 1].isNumber) {
                number.append(next); index += 1
            }
            return .symbol(number, .ord)
        }
        if char.isLetter, char.isASCII { return .symbol(MathAlphabet.italic(char), .ord) }
        switch char {
        case "+": return .symbol("+", .bin)
        case "-": return .symbol("−", .bin)
        case "*": return .symbol("∗", .bin)
        case "/": return .symbol("/", .ord)
        case "=", "<", ">": return .symbol(String(char), .rel)
        case ":": return .symbol(":", .rel)
        case "(", "[": return .symbol(String(char), .open)
        case ")", "]": return .symbol(String(char), .close)
        case ",", ";": return .symbol(String(char), .punct)
        case "|": return .symbol("|", .ord)
        case "!", "?", ".": return .symbol(String(char), .close)
        case "~": return .space(0.33)
        case "&", "#", "$", "%": throw TeXError()
        default: return .symbol(String(char), .ord)  // Unicode symbols and CJK pass through
        }
    }

    private mutating func commandAtom() throws -> TeXNode {
        let name = command()
        if let symbol = Symbols.table[name] { return .symbol(symbol.0, symbol.1) }
        if let op = Symbols.largeOperators[name] { return .largeOp(op, limits: nil, isText: false) }
        if Symbols.operatorNames.contains(name) {
            return .largeOp(name, limits: Symbols.limitNames.contains(name) ? nil : false, isText: true)
        }
        if let space = Symbols.spaces[name] { return .space(space) }
        switch name {
        case "frac", "dfrac", "tfrac", "cfrac":
            let style: TeXStyle? = name == "dfrac" || name == "cfrac" ? .display : name == "tfrac" ? .text : nil
            return .fraction(try argument(), try argument(), rule: true, style: style)
        case "binom", "dbinom", "tbinom":
            let style: TeXStyle? = name == "dbinom" ? .display : name == "tbinom" ? .text : nil
            return .delimited(left: "(", right: ")", [.fraction(try argument(), try argument(), rule: false, style: style)])
        case "sqrt":
            let degree = try optionalArgument()
            return .radical(try argument(), index: degree)
        case "left":
            let left = try delimiter()
            let content = try expression(until: ["right"])
            guard command() == "right" else { throw TeXError() }
            return .delimited(left: left, right: try delimiter(), content)
        case "middle":
            return .symbol(try delimiter(), .rel)
        case "big", "Big", "bigg", "Bigg", "bigl", "Bigl", "biggl", "Biggl", "bigr", "Bigr", "biggr", "Biggr", "bigm", "Bigm":
            let scale: CGFloat = name.hasPrefix("bigg") ? 2.4 : name.hasPrefix("Bigg") ? 3.0 : name.hasPrefix("Big") ? 1.8 : 1.2
            let atom: AtomClass = name.hasSuffix("l") ? .open : name.hasSuffix("r") ? .close : name.hasSuffix("m") ? .rel : .ord
            return .sizedDelimiter(try delimiter(), scale: scale, atom)
        case "text", "textrm", "textnormal", "mbox", "textup", "hbox":
            return .text(try rawArgument().replacingOccurrences(of: "\\ ", with: " "), bold: false, italic: false)
        case "textbf": return .text(try rawArgument(), bold: true, italic: false)
        case "textit", "emph": return .text(try rawArgument(), bold: false, italic: true)
        case "mathrm", "mathup", "rm":
            return .text(try rawArgument(), bold: false, italic: false)
        case "operatorname", "operatorname*":
            return .largeOp(try rawArgument(), limits: name.hasSuffix("*") ? nil : false, isText: true)
        case "mathbf", "bf", "textsf", "mathsf", "mathtt", "texttt", "mathit", "boldsymbol", "bm", "mathbb", "mathcal",
             "mathscr", "mathfrak":
            return MathAlphabet.restyle(try argument(), as: name)
        case "hat": return .accent(try argument(), .hat)
        case "widehat": return .accent(try argument(), .wideHat)
        case "tilde", "widetilde": return .accent(try argument(), .tilde)
        case "bar", "overline": return .accent(try argument(), .bar)
        case "underline": return .accent(try argument(), .underline)
        case "vec", "overrightarrow": return .accent(try argument(), .vector)
        case "dot": return .accent(try argument(), .dot)
        case "ddot": return .accent(try argument(), .ddot)
        case "check": return .accent(try argument(), .check)
        case "breve": return .accent(try argument(), .breve)
        case "acute": return .accent(try argument(), .acute)
        case "grave": return .accent(try argument(), .grave)
        case "overbrace": return .accent(try argument(), .overbrace)
        case "underbrace": return .accent(try argument(), .underbrace)
        case "overset", "stackrel":
            let over = try argument()
            return .stack(over: over, base: try argument(), under: nil)
        case "underset":
            let under = try argument()
            return .stack(over: nil, base: try argument(), under: under)
        case "boxed", "fbox": return .boxed(try argument())
        case "not":
            let next = try atom()
            if case .symbol(let symbol, let atom) = next {
                return .symbol(symbol == "=" ? "≠" : symbol == "∈" ? "∉" : symbol + "\u{0338}", atom)
            }
            return next
        case "color", "textcolor", "colorbox":
            _ = try rawArgument()
            return try argument()
        case "pmod":
            return .group([.space(0.5), .symbol("(", .open), .text("mod", bold: false, italic: false), .space(0.33),
                           try argument(), .symbol(")", .close)])
        case "bmod", "mod":
            return .group([.space(0.28), .text("mod", bold: false, italic: false), .space(0.28)])
        case "tag", "tag*":
            return .group([.space(2), .text("(" + (try rawArgument()) + ")", bold: false, italic: false)])
        case "label":
            _ = try rawArgument()
            return .group([])
        case "nonumber", "notag":
            return .group([])
        case "hspace", "hspace*", "kern", "mkern", "phantom", "hphantom", "vphantom":
            _ = try rawArgument()
            return .space(0.5)
        case "begin":
            return try environment(try rawArgument())
        default:
            throw TeXError()
        }
    }

    private mutating func delimiter() throws -> String {
        skipSpaces()
        guard let char = peek else { throw TeXError() }
        if char == "\\" {
            switch command() {
            case "{", "lbrace": return "{"
            case "}", "rbrace": return "}"
            case "|", "Vert": return "‖"
            case "vert": return "|"
            case "langle": return "⟨"
            case "rangle": return "⟩"
            case "lfloor": return "⌊"
            case "rfloor": return "⌋"
            case "lceil": return "⌈"
            case "rceil": return "⌉"
            case "lbrack": return "["
            case "rbrack": return "]"
            case "backslash": return "\\"
            default: throw TeXError()
            }
        }
        index += 1
        return char == "." ? "" : String(char)
    }

    private mutating func environment(_ name: String) throws -> TeXNode {
        let bare = name.replacingOccurrences(of: "*", with: "")
        if bare == "array" || bare == "alignat" { _ = try rawArgument() }  // column spec
        var rows: [[TeXNode]] = []
        var row: [TeXNode] = []
        while true {
            let cell = try expression(until: ["&", "\\", "end", "cr"])
            row.append(.group(cell))
            if peek == "&" { index += 1; continue }
            guard let next = peekCommand() else { throw TeXError() }
            _ = command()
            if next == "\\" || next == "cr" {
                _ = try optionalArgument()  // \\[2pt]
                rows.append(row)
                row = []
                continue
            }
            guard next == "end" else { throw TeXError() }
            _ = try rawArgument()
            let emptyLast: Bool = {
                guard row.count == 1, case .group(let nodes) = row[0] else { return false }
                return nodes.isEmpty
            }()
            if !emptyLast || rows.isEmpty { rows.append(row) }
            break
        }
        switch bare {
        case "pmatrix": return .delimited(left: "(", right: ")", [.matrix(rows: rows, kind: .plain)])
        case "bmatrix": return .delimited(left: "[", right: "]", [.matrix(rows: rows, kind: .plain)])
        case "Bmatrix": return .delimited(left: "{", right: "}", [.matrix(rows: rows, kind: .plain)])
        case "vmatrix": return .delimited(left: "|", right: "|", [.matrix(rows: rows, kind: .plain)])
        case "Vmatrix": return .delimited(left: "‖", right: "‖", [.matrix(rows: rows, kind: .plain)])
        case "cases", "dcases": return .delimited(left: "{", right: "", [.matrix(rows: rows, kind: .cases)])
        case "rcases": return .delimited(left: "", right: "}", [.matrix(rows: rows, kind: .cases)])
        case "aligned", "align", "alignat", "split", "eqnarray":
            return .styled(.display, [.matrix(rows: rows, kind: .aligned)])
        case "gather", "gathered", "equation", "multline":
            return .styled(.display, [.matrix(rows: rows, kind: .plain)])
        case "smallmatrix": return .matrix(rows: rows, kind: .small)
        case "matrix", "array", "subarray": return .matrix(rows: rows, kind: .plain)
        default: throw TeXError()
        }
    }
}

// MARK: - Symbols

enum Symbols {
    static let table: [String: (String, AtomClass)] = {
        var table: [String: (String, AtomClass)] = [:]
        let greek: [(String, UInt32)] = [
            ("alpha", 0x1D6FC), ("beta", 0x1D6FD), ("gamma", 0x1D6FE), ("delta", 0x1D6FF), ("epsilon", 0x1D716),
            ("varepsilon", 0x1D700), ("zeta", 0x1D701), ("eta", 0x1D702), ("theta", 0x1D703), ("vartheta", 0x1D717),
            ("iota", 0x1D704), ("kappa", 0x1D705), ("lambda", 0x1D706), ("mu", 0x1D707), ("nu", 0x1D708),
            ("xi", 0x1D709), ("pi", 0x1D70B), ("varpi", 0x1D71B), ("rho", 0x1D70C), ("varrho", 0x1D71A),
            ("sigma", 0x1D70E), ("varsigma", 0x1D70D), ("tau", 0x1D70F), ("upsilon", 0x1D710), ("phi", 0x1D719),
            ("varphi", 0x1D711), ("chi", 0x1D712), ("psi", 0x1D713), ("omega", 0x1D714),
        ]
        for (name, code) in greek { table[name] = (String(UnicodeScalar(code)!), .ord) }
        let upper: [(String, String)] = [("Gamma", "Γ"), ("Delta", "Δ"), ("Theta", "Θ"), ("Lambda", "Λ"), ("Xi", "Ξ"),
                                         ("Pi", "Π"), ("Sigma", "Σ"), ("Upsilon", "Υ"), ("Phi", "Φ"), ("Psi", "Ψ"), ("Omega", "Ω")]
        for (name, symbol) in upper { table[name] = (symbol, .ord) }
        let binary: [(String, String)] = [
            ("pm", "±"), ("mp", "∓"), ("times", "×"), ("div", "÷"), ("cdot", "⋅"), ("ast", "∗"), ("star", "⋆"),
            ("circ", "∘"), ("bullet", "∙"), ("oplus", "⊕"), ("ominus", "⊖"), ("otimes", "⊗"), ("oslash", "⊘"),
            ("odot", "⊙"), ("cup", "∪"), ("cap", "∩"), ("wedge", "∧"), ("land", "∧"), ("vee", "∨"), ("lor", "∨"),
            ("setminus", "∖"), ("amalg", "⨿"), ("dagger", "†"), ("ddagger", "‡"), ("uplus", "⊎"), ("sqcup", "⊔"),
            ("sqcap", "⊓"), ("triangleleft", "◁"), ("triangleright", "▷"),
        ]
        for (name, symbol) in binary { table[name] = (symbol, .bin) }
        let relations: [(String, String)] = [
            ("leq", "≤"), ("le", "≤"), ("geq", "≥"), ("ge", "≥"), ("neq", "≠"), ("ne", "≠"), ("equiv", "≡"),
            ("approx", "≈"), ("sim", "∼"), ("simeq", "≃"), ("cong", "≅"), ("propto", "∝"), ("ll", "≪"), ("gg", "≫"),
            ("subset", "⊂"), ("supset", "⊃"), ("subseteq", "⊆"), ("supseteq", "⊇"), ("subsetneq", "⊊"), ("in", "∈"),
            ("ni", "∋"), ("notin", "∉"), ("mid", "∣"), ("parallel", "∥"), ("perp", "⊥"), ("to", "→"),
            ("rightarrow", "→"), ("leftarrow", "←"), ("gets", "←"), ("Rightarrow", "⇒"), ("Leftarrow", "⇐"),
            ("leftrightarrow", "↔"), ("Leftrightarrow", "⇔"), ("implies", "⟹"), ("impliedby", "⟸"), ("iff", "⟺"),
            ("mapsto", "↦"), ("longrightarrow", "⟶"), ("longleftarrow", "⟵"), ("Longrightarrow", "⟹"),
            ("longmapsto", "⟼"), ("uparrow", "↑"), ("downarrow", "↓"), ("updownarrow", "↕"), ("nearrow", "↗"),
            ("searrow", "↘"), ("models", "⊨"), ("vdash", "⊢"), ("dashv", "⊣"), ("prec", "≺"), ("succ", "≻"),
            ("preceq", "⪯"), ("succeq", "⪰"), ("doteq", "≐"), ("asymp", "≍"), ("leqslant", "⩽"), ("geqslant", "⩾"),
            ("lesssim", "≲"), ("gtrsim", "≳"), ("hookrightarrow", "↪"), ("rightleftharpoons", "⇌"), ("coloneqq", "≔"),
        ]
        for (name, symbol) in relations { table[name] = (symbol, .rel) }
        let ordinary: [(String, String)] = [
            ("infty", "∞"), ("partial", "∂"), ("nabla", "∇"), ("forall", "∀"), ("exists", "∃"), ("nexists", "∄"),
            ("emptyset", "∅"), ("varnothing", "∅"), ("hbar", "ℏ"), ("ell", "ℓ"), ("Re", "ℜ"), ("Im", "ℑ"),
            ("aleph", "ℵ"), ("prime", "′"), ("angle", "∠"), ("triangle", "△"), ("neg", "¬"), ("lnot", "¬"),
            ("top", "⊤"), ("bot", "⊥"), ("ldots", "…"), ("dots", "…"), ("cdots", "⋯"), ("vdots", "⋮"),
            ("ddots", "⋱"), ("checkmark", "✓"), ("degree", "°"), ("%", "%"), ("$", "$"), ("#", "#"), ("&", "&"),
            ("_", "_"), ("surd", "√"), ("square", "□"), ("blacksquare", "■"), ("diamond", "◇"), ("clubsuit", "♣"),
            ("imath", "𝚤"), ("jmath", "𝚥"), ("wp", "℘"), ("mho", "℧"), ("S", "§"), ("P", "¶"),
            ("backslash", "∖"), ("vert", "|"), ("Vert", "‖"), ("|", "‖"), ("lvert", "|"), ("rvert", "|"),
            ("therefore", "∴"), ("because", "∵"), ("dag", "†"),
        ]
        for (name, symbol) in ordinary { table[name] = (symbol, .ord) }
        let fences: [(String, String, AtomClass)] = [
            ("{", "{", .open), ("}", "}", .close), ("lbrace", "{", .open), ("rbrace", "}", .close),
            ("langle", "⟨", .open), ("rangle", "⟩", .close), ("lfloor", "⌊", .open), ("rfloor", "⌋", .close),
            ("lceil", "⌈", .open), ("rceil", "⌉", .close), ("lbrack", "[", .open), ("rbrack", "]", .close),
        ]
        for (name, symbol, atom) in fences { table[name] = (symbol, atom) }
        table["colon"] = (":", .punct)
        table["ldotp"] = (".", .punct)
        table["cdotp"] = ("⋅", .punct)
        return table
    }()

    static let largeOperators: [String: String] = [
        "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
        "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁", "bigotimes": "⨂", "bigvee": "⋁", "bigwedge": "⋀",
        "bigsqcup": "⨆", "bigodot": "⨀", "biguplus": "⨄",
    ]

    static let operatorNames: Set<String> = [
        "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "coth",
        "log", "ln", "lg", "exp", "lim", "limsup", "liminf", "max", "min", "sup", "inf", "det", "deg", "dim",
        "gcd", "hom", "ker", "arg", "Pr", "argmax", "argmin", "sgn", "tr", "rank", "Var", "Cov",
    ]

    /// Operator names that take limits above and below in display style.
    static let limitNames: Set<String> = ["lim", "limsup", "liminf", "max", "min", "sup", "inf", "det", "gcd", "Pr", "argmax", "argmin"]

    static let spaces: [String: CGFloat] = [
        ",": 3.0 / 18, "thinspace": 3.0 / 18, ":": 4.0 / 18, ">": 4.0 / 18, "medspace": 4.0 / 18,
        ";": 5.0 / 18, "thickspace": 5.0 / 18, "!": -3.0 / 18, "negthinspace": -3.0 / 18,
        " ": 0.33, "quad": 1, "qquad": 2, "enspace": 0.5, "space": 0.33,
    ]
}

/// Unicode mathematical alphanumerics, which STIX Two Math covers.
enum MathAlphabet {
    static func italic(_ char: Character) -> String {
        if char == "h" { return "ℎ" }
        return shifted(char, upper: 0x1D434, lower: 0x1D44E) ?? String(char)
    }

    static func shifted(_ char: Character, upper: UInt32, lower: UInt32, digits: UInt32? = nil,
                        exceptions: [Character: String] = [:]) -> String? {
        if let exception = exceptions[char] { return exception }
        guard char.unicodeScalars.count == 1, let scalar = char.unicodeScalars.first else { return nil }
        let value = scalar.value
        let mapped: UInt32? = switch value {
        case 65...90: upper + value - 65
        case 97...122: lower + value - 97
        case 48...57: digits.map { $0 + value - 48 }
        default: nil
        }
        return mapped.flatMap(UnicodeScalar.init).map { String($0) }
    }

    /// Applies \mathbf, \mathbb, \mathcal … to the letters and digits of `node`.
    static func restyle(_ node: TeXNode, as command: String) -> TeXNode {
        func map(_ text: String) -> String {
            text.map { char -> String in
                let plain = plainLetter(char)
                switch command {
                case "mathbf", "bf":
                    return shifted(plain, upper: 0x1D400, lower: 0x1D41A, digits: 0x1D7CE) ?? String(char)
                case "boldsymbol", "bm":
                    return shifted(plain, upper: 0x1D468, lower: 0x1D482, digits: 0x1D7CE) ?? String(char)
                case "mathbb":
                    return shifted(plain, upper: 0x1D538, lower: 0x1D552, digits: 0x1D7D8,
                                   exceptions: ["C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"]) ?? String(char)
                case "mathcal", "mathscr":
                    return shifted(plain, upper: 0x1D49C, lower: 0x1D4B6,
                                   exceptions: ["B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ",
                                                "R": "ℛ", "e": "ℯ", "g": "ℊ", "o": "ℴ"]) ?? String(char)
                case "mathfrak":
                    return shifted(plain, upper: 0x1D504, lower: 0x1D51E,
                                   exceptions: ["C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"]) ?? String(char)
                case "mathsf", "textsf":
                    return shifted(plain, upper: 0x1D5A0, lower: 0x1D5BA, digits: 0x1D7E2) ?? String(char)
                case "mathtt", "texttt":
                    return shifted(plain, upper: 0x1D670, lower: 0x1D68A, digits: 0x1D7F6) ?? String(char)
                default:  // mathit
                    return italic(plain)
                }
            }.joined()
        }
        switch node {
        case .symbol(let text, let atom): return .symbol(map(text), atom)
        case .group(let nodes): return .group(nodes.map { restyle($0, as: command) })
        case .scripts(let base, let sub, let sup): return .scripts(base: restyle(base, as: command), sub: sub, sup: sup)
        case .text(let text, _, _): return .symbol(map(text), .ord)
        default: return node
        }
    }

    /// The ASCII letter behind a math-italic letter.
    static func plainLetter(_ char: Character) -> Character {
        guard let value = char.unicodeScalars.first?.value else { return char }
        if char == "ℎ" { return "h" }
        if (0x1D434...0x1D44D).contains(value) { return Character(UnicodeScalar(value - 0x1D434 + 65)!) }
        if (0x1D44E...0x1D467).contains(value) { return Character(UnicodeScalar(value - 0x1D44E + 97)!) }
        return char
    }
}

// MARK: - Boxes

/// A laid-out piece of math. Coordinates: x to the right, y up, baseline at 0.
struct MathBox {
    var width: CGFloat = 0
    var ascent: CGFloat = 0
    var descent: CGFloat = 0
    var items: [MathItem] = []
    var atom: AtomClass = .ord
    /// Whether the box ends in a slanted glyph (scripts move right a little).
    var italic = false

    var height: CGFloat { ascent + descent }

    mutating func append(_ box: MathBox, x: CGFloat, y: CGFloat = 0) {
        items += box.items.map { $0.offset(x: x, y: y) }
        width = max(width, x + box.width)
        ascent = max(ascent, y + box.ascent)
        descent = max(descent, box.descent - y)
    }
}

enum MathItem {
    case glyphs(CTLine, CGPoint, scaleX: CGFloat = 1, scaleY: CGFloat = 1)
    case rule(CGRect)
    case stroke(CGPath, CGPoint, width: CGFloat)

    func offset(x: CGFloat, y: CGFloat) -> MathItem {
        switch self {
        case .glyphs(let line, let point, let sx, let sy):
            .glyphs(line, CGPoint(x: point.x + x, y: point.y + y), scaleX: sx, scaleY: sy)
        case .rule(let rect): .rule(rect.offsetBy(dx: x, dy: y))
        case .stroke(let path, let point, let width): .stroke(path, CGPoint(x: point.x + x, y: point.y + y), width: width)
        }
    }

    func draw(in context: CGContext, origin: CGPoint, color: NSColor) {
        switch self {
        case .glyphs(let line, let point, let sx, let sy):
            context.saveGState()
            context.translateBy(x: origin.x + point.x, y: origin.y + point.y)
            context.scaleBy(x: sx, y: sy)
            context.textPosition = .zero
            CTLineDraw(line, context)
            context.restoreGState()
        case .rule(let rect):
            context.setFillColor(color.cgColor)
            context.fill(rect.offsetBy(dx: origin.x, dy: origin.y))
        case .stroke(let path, let point, let width):
            context.saveGState()
            context.translateBy(x: origin.x + point.x, y: origin.y + point.y)
            context.addPath(path)
            context.setStrokeColor(color.cgColor)
            context.setLineWidth(width)
            context.setLineCap(.round)
            context.setLineJoin(.round)
            context.strokePath()
            context.restoreGState()
        }
    }
}

// MARK: - Layout

struct TeXLayout {
    let em: CGFloat
    let color: NSColor

    private static let mathFontName = "STIXTwoMath-Regular"

    func size(_ style: TeXStyle) -> CGFloat { em * style.scale }
    func axis(_ style: TeXStyle) -> CGFloat { size(style) * 0.25 }
    func rule(_ style: TeXStyle) -> CGFloat { max(0.6, size(style) * 0.055) }

    func font(_ size: CGFloat) -> CTFont { CTFontCreateWithName(Self.mathFontName as CFString, size, nil) }

    // MARK: Glyph runs

    func glyphs(_ text: String, size: CGFloat, font explicit: CTFont? = nil) -> MathBox {
        let font = explicit ?? font(size)
        let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attributes))
        let width = CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil))
        // Ink bounds keep fractions and scripts tight, but never less than
        // the x-height band, so rows line up.
        let ink = CTLineGetBoundsWithOptions(line, .useGlyphPathBounds)
        let xHeight = CTFontGetXHeight(font)
        var box = MathBox(width: width, items: [.glyphs(line, .zero)])
        if ink.isNull || ink.isEmpty {
            box.ascent = xHeight
        } else {
            box.ascent = max(ink.maxY, xHeight)
            box.descent = max(-ink.minY, 0)
        }
        box.italic = text.unicodeScalars.last.map {
            (0x1D434...0x1D467).contains($0.value) || (0x1D6FC...0x1D71B).contains($0.value) || $0 == "ℎ"
        } ?? false
        return box
    }

    func textBox(_ text: String, size: CGFloat, bold: Bool, italic: Bool) -> MathBox {
        var traits: NSFontDescriptor.SymbolicTraits = []
        if bold { traits.insert(.bold) }
        if italic { traits.insert(.italic) }
        let base = NSFont(name: "STIXTwoText", size: size) ?? NSFont.systemFont(ofSize: size)
        let styled = traits.isEmpty ? base : NSFont(descriptor: base.fontDescriptor.withSymbolicTraits(traits), size: size) ?? base
        return glyphs(text, size: size, font: styled as CTFont)
    }

    // MARK: Rows

    /// Lays out a list of atoms with TeX's inter-atom spacing.
    mutating func row(_ nodes: [TeXNode], style: TeXStyle) -> MathBox {
        var boxes: [MathBox] = []
        for node in nodes {
            if case .styled(let switched, let inner) = node {
                boxes.append(row(inner, style: switched))
            } else {
                boxes.append(layout(node, style: style))
            }
        }
        // A binary operator with nothing to operate on is ordinary (unary minus).
        for index in boxes.indices where boxes[index].atom == .bin {
            let before = index > 0 ? boxes[index - 1].atom : nil
            let after = index + 1 < boxes.count ? boxes[index + 1].atom : nil
            if before == nil || [.bin, .op, .rel, .open, .punct].contains(before!)
                || after == nil || [.rel, .close, .punct].contains(after!) {
                boxes[index].atom = .ord
            }
        }
        var result = MathBox()
        var x: CGFloat = 0
        var previous: AtomClass?
        for box in boxes {
            if let previous { x += spacing(previous, box.atom, style: style) }
            result.append(box, x: x)
            x += box.width
            previous = box.atom
        }
        result.width = x
        if boxes.count == 1 { result.atom = boxes[0].atom; result.italic = boxes[0].italic }
        return result
    }

    func spacing(_ left: AtomClass, _ right: AtomClass, style: TeXStyle) -> CGFloat {
        let mu = size(style) / 18
        let tight = style >= .script
        switch (left, right) {
        case (.ord, .op), (.op, .ord), (.op, .op), (.close, .op), (.inner, .op): return 3 * mu
        case (.ord, .bin), (.close, .bin), (.inner, .bin), (.bin, .ord), (.bin, .open), (.bin, .op), (.bin, .inner):
            return tight ? 0 : 4 * mu
        case (.rel, .rel), (.rel, .punct), (.open, .rel): return 0
        case (_, .rel), (.rel, _): return tight ? 0 : 5 * mu
        case (.punct, _): return tight ? 0 : 3 * mu
        case (.inner, .ord), (.inner, .open), (.inner, .inner), (.ord, .inner), (.close, .inner): return tight ? 0 : 3 * mu
        default: return 0
        }
    }

    // MARK: Nodes

    mutating func layout(_ node: TeXNode, style: TeXStyle) -> MathBox {
        let size = size(style)
        switch node {
        case .symbol(let text, let atom):
            var box = glyphs(text, size: size)
            box.atom = atom
            return box
        case .group(let nodes):
            var box = row(nodes, style: style)
            if nodes.count != 1 { box.atom = .ord }
            return box
        case .styled(let switched, let nodes):
            return row(nodes, style: switched)
        case .text(let text, let bold, let italic):
            return textBox(text, size: size, bold: bold, italic: italic)
        case .space(let amount):
            return MathBox(width: amount * size, atom: .ord)
        case .fraction(let numerator, let denominator, let hasRule, let forced):
            return fraction(numerator, denominator, rule: hasRule, style: forced ?? style)
        case .radical(let radicand, let degree):
            return radical(radicand, degree: degree, style: style)
        case .scripts(let base, let sub, let sup):
            return scripts(base, sub: sub, sup: sup, style: style)
        case .largeOp(let symbol, _, let isText):
            return largeOperator(symbol, isText: isText, style: style)
        case .delimited(let left, let right, let content):
            return delimited(left: left, right: right, content: row(content, style: style), style: style)
        case .sizedDelimiter(let symbol, let scale, let atom):
            var box = stretched(symbol, height: scale * size, style: style)
            box.atom = atom
            return box
        case .accent(let base, let kind):
            return accent(base, kind: kind, style: style)
        case .matrix(let rows, let kind):
            return matrix(rows, kind: kind, style: kind == .small ? .script : style)
        case .boxed(let inner):
            let content = layout(inner, style: style)
            let pad = size * 0.25, line = rule(style)
            var box = MathBox(width: content.width + pad * 2, ascent: content.ascent + pad, descent: content.descent + pad)
            box.append(content, x: pad)
            let frame = CGPath(rect: CGRect(x: line / 2, y: -content.descent - pad + line / 2,
                                            width: content.width + pad * 2 - line, height: content.height + pad * 2 - line),
                               transform: nil)
            box.items.append(.stroke(frame, .zero, width: line))
            return box
        case .stack(let over, let base, let under):
            let center = layout(base, style: style)
            let gap = size * 0.1
            let top = over.map { layout($0, style: style.superscript) }
            let bottom = under.map { layout($0, style: style.superscript) }
            let width = max(center.width, top?.width ?? 0, bottom?.width ?? 0)
            var box = MathBox(atom: center.atom == .rel ? .rel : .ord)
            box.append(center, x: (width - center.width) / 2)
            if let top { box.append(top, x: (width - top.width) / 2, y: center.ascent + gap + top.descent) }
            if let bottom { box.append(bottom, x: (width - bottom.width) / 2, y: -center.descent - gap - bottom.ascent) }
            box.width = width
            return box
        }
    }

    mutating func fraction(_ numerator: TeXNode, _ denominator: TeXNode, rule hasRule: Bool, style: TeXStyle) -> MathBox {
        let inner = style.fraction
        let top = layout(numerator, style: inner)
        let bottom = layout(denominator, style: inner)
        let thickness = hasRule ? rule(style) : 0
        let gap = style == .display ? rule(style) * 2.6 : rule(style) * 1.4
        let axis = axis(style)
        let pad = size(style) * 0.12
        let width = max(top.width, bottom.width) + pad * 2
        let topShift = max(axis + thickness / 2 + gap + top.descent, size(style) * (style == .display ? 0.68 : 0.4))
        let bottomShift = max(-axis + thickness / 2 + gap + bottom.ascent, size(style) * (style == .display ? 0.69 : 0.35))
        var box = MathBox(atom: .inner)
        box.append(top, x: (width - top.width) / 2, y: topShift)
        box.append(bottom, x: (width - bottom.width) / 2, y: -bottomShift)
        if hasRule {
            box.items.append(.rule(CGRect(x: pad * 0.4, y: axis - thickness / 2, width: width - pad * 0.8, height: thickness)))
        }
        box.width = width
        return box
    }

    mutating func radical(_ radicand: TeXNode, degree: TeXNode?, style: TeXStyle) -> MathBox {
        let content = layout(radicand, style: style)
        let size = size(style)
        let line = rule(style)
        let gap = style == .display ? line * 3 : line * 1.8
        let top = content.ascent + gap + line / 2
        let bottom = -max(content.descent, size * 0.05) - line
        let height = top - bottom
        let surd = size * (0.5 + min(height / size, 3) * 0.08)
        let path = CGMutablePath()
        path.move(to: CGPoint(x: 0, y: bottom + height * 0.42))
        path.addLine(to: CGPoint(x: surd * 0.22, y: bottom + height * 0.5))
        path.addLine(to: CGPoint(x: surd * 0.52, y: bottom))
        path.addLine(to: CGPoint(x: surd, y: top))
        path.addLine(to: CGPoint(x: surd + content.width + size * 0.1, y: top))
        var box = MathBox(atom: .ord)
        var x: CGFloat = 0
        if let degree {
            let index = layout(degree, style: .scriptScript)
            box.append(index, x: 0, y: bottom + height * 0.55 + index.descent)
            x = max(0, index.width - surd * 0.35)
        }
        box.items.append(.stroke(path, CGPoint(x: x, y: 0), width: line))
        box.append(content, x: x + surd + size * 0.05)
        box.ascent = max(box.ascent, top + line)
        box.descent = max(box.descent, -bottom + line / 2)
        box.width = x + surd + content.width + size * 0.12
        return box
    }

    mutating func scripts(_ baseNode: TeXNode, sub: TeXNode?, sup: TeXNode?, style: TeXStyle) -> MathBox {
        // Large operators with limits put them above and below.
        if case .largeOp(let symbol, let limits, let isText) = baseNode,
           limits ?? (style == .display && !["∫", "∬", "∭", "∮"].contains(symbol)) {
            return limitsBox(largeOperator(symbol, isText: isText, style: style), sub: sub, sup: sup, style: style)
        }
        let base = layout(baseNode, style: style)
        let size = size(style)
        let scriptStyle = style.superscript
        let supBox = sup.map { layout($0, style: scriptStyle) }
        let subBox = sub.map { layout($0, style: scriptStyle) }
        let tall = base.height > size * 1.1
        var supShift: CGFloat = 0, subShift: CGFloat = 0
        if let supBox {
            supShift = max(tall ? base.ascent - size * 0.32 : size * (style == .display ? 0.42 : 0.38),
                           supBox.descent + size * 0.11)
        }
        if let subBox {
            subShift = max(tall ? base.descent + size * 0.1 : size * 0.16, subBox.ascent - size * 0.36)
        }
        if let supBox, let subBox {
            let gap = (supShift - supBox.descent) - (subBox.ascent - subShift)
            let minimum = rule(style) * 4
            if gap < minimum { subShift += minimum - gap }
        }
        var box = MathBox(atom: base.atom)
        box.append(base, x: 0)
        let integral = base.atom == .op && base.italic
        let kern = base.italic ? size * 0.06 : 0
        var right = base.width
        if let supBox {
            box.append(supBox, x: base.width + kern, y: supShift)
            right = max(right, base.width + kern + supBox.width)
        }
        if let subBox {
            let x = base.width - (integral ? size * 0.18 : 0)
            box.append(subBox, x: x, y: -subShift)
            right = max(right, x + subBox.width)
        }
        box.width = right + size * 0.04
        return box
    }

    mutating func limitsBox(_ op: MathBox, sub: TeXNode?, sup: TeXNode?, style: TeXStyle) -> MathBox {
        let gap = size(style) * 0.12
        let top = sup.map { layout($0, style: style.superscript) }
        let bottom = sub.map { layout($0, style: style.superscript) }
        let width = max(op.width, top?.width ?? 0, bottom?.width ?? 0)
        var box = MathBox(atom: .op)
        box.append(op, x: (width - op.width) / 2)
        if let top { box.append(top, x: (width - top.width) / 2, y: op.ascent + gap + top.descent) }
        if let bottom { box.append(bottom, x: (width - bottom.width) / 2, y: -op.descent - gap - bottom.ascent) }
        box.width = width
        return box
    }

    func largeOperator(_ symbol: String, isText: Bool, style: TeXStyle) -> MathBox {
        let size = size(style)
        if isText {
            var box = textBox(symbol, size: size, bold: false, italic: false)
            box.atom = .op
            return box
        }
        let integral = ["∫", "∬", "∭", "∮"].contains(symbol)
        let scale: CGFloat = style == .display ? (integral ? 2.0 : 1.55) : (integral ? 1.3 : 1.1)
        var box = glyphs(symbol, size: size * scale)
        // Centre on the math axis.
        let shift = axis(style) - (box.ascent - box.descent) / 2
        box.items = box.items.map { $0.offset(x: 0, y: shift) }
        box.ascent += shift
        box.descent -= shift
        box.atom = .op
        box.italic = integral
        return box
    }

    mutating func delimited(left: String, right: String, content: MathBox, style: TeXStyle) -> MathBox {
        let axis = axis(style)
        // Delimiters cover the content symmetrically around the axis.
        let half = max(content.ascent - axis, content.descent + axis)
        let height = max(half * 2 * 1.05, size(style))
        var box = MathBox(atom: .inner)
        var x: CGFloat = 0
        if !left.isEmpty {
            let delimiter = stretched(left, height: height, style: style)
            box.append(delimiter, x: 0)
            x = delimiter.width + size(style) * 0.04
        }
        box.append(content, x: x)
        x += content.width
        if !right.isEmpty {
            let delimiter = stretched(right, height: height, style: style)
            box.append(delimiter, x: x + size(style) * 0.04)
            x += delimiter.width + size(style) * 0.04
        }
        box.width = x
        return box
    }

    /// A delimiter scaled to `height`, centred on the axis.
    func stretched(_ symbol: String, height: CGFloat, style: TeXStyle) -> MathBox {
        let size = size(style)
        let base = glyphs(symbol, size: size)
        let natural = max(base.height, size * 0.5)
        guard height > natural * 1.08, case .glyphs(let line, _, _, _)? = base.items.first else {
            var box = base
            let shift = axis(style) - (box.ascent - box.descent) / 2
            box.items = box.items.map { $0.offset(x: 0, y: shift) }
            box.ascent += shift
            box.descent -= shift
            return box
        }
        let scaleY = height / natural
        let scaleX = min(1 + (scaleY - 1) * 0.12, 1.5)
        let center = (base.ascent - base.descent) / 2 * scaleY
        return MathBox(width: base.width * scaleX, ascent: axis(style) + height / 2, descent: height / 2 - axis(style),
                       items: [.glyphs(line, CGPoint(x: 0, y: axis(style) - center), scaleX: scaleX, scaleY: scaleY)])
    }

    mutating func accent(_ baseNode: TeXNode, kind: AccentKind, style: TeXStyle) -> MathBox {
        let base = layout(baseNode, style: style)
        let size = size(style)
        let line = rule(style)
        var box = MathBox(atom: base.atom)
        box.append(base, x: 0)
        box.width = base.width
        let top = max(base.ascent, size * 0.45) + size * 0.08
        let skew = base.italic ? size * 0.08 : 0
        func mark(_ glyph: String, scale: CGFloat = 1) {
            var accent = glyphs(glyph, size: size * scale)
            guard case .glyphs(let run, _, _, _)? = accent.items.first else { return }
            let ink = CTLineGetBoundsWithOptions(run, .useGlyphPathBounds)
            // The accent's ink sits just above the base, centred on its ink.
            accent.ascent = ink.maxY
            accent.descent = -ink.minY
            box.append(accent, x: (base.width - ink.width) / 2 - ink.minX + skew, y: top - ink.minY)
        }
        switch kind {
        case .bar:
            box.items.append(.rule(CGRect(x: 0, y: top + line, width: base.width, height: line)))
            box.ascent = max(box.ascent, top + line * 2.5)
        case .underline:
            box.items.append(.rule(CGRect(x: 0, y: -base.descent - line * 2.5, width: base.width, height: line)))
            box.descent = max(box.descent, base.descent + line * 3)
        case .vector:
            let arrow = CGMutablePath()
            let y = top + size * 0.12
            let length = max(base.width * 0.9, size * 0.45)
            let start = (base.width - length) / 2 + skew
            arrow.move(to: CGPoint(x: start, y: y))
            arrow.addLine(to: CGPoint(x: start + length, y: y))
            arrow.move(to: CGPoint(x: start + length - size * 0.14, y: y + size * 0.09))
            arrow.addLine(to: CGPoint(x: start + length, y: y))
            arrow.addLine(to: CGPoint(x: start + length - size * 0.14, y: y - size * 0.09))
            box.items.append(.stroke(arrow, .zero, width: line))
            box.ascent = max(box.ascent, y + size * 0.12)
        case .wideHat where base.width > size * 0.8, .hat where base.width > size * 1.2:
            let hat = CGMutablePath()
            let y = top + size * 0.05
            hat.move(to: CGPoint(x: 0, y: y))
            hat.addLine(to: CGPoint(x: base.width / 2, y: y + size * 0.22))
            hat.addLine(to: CGPoint(x: base.width, y: y))
            box.items.append(.stroke(hat, .zero, width: line))
            box.ascent = max(box.ascent, y + size * 0.26)
        case .hat, .wideHat: mark("ˆ", scale: 1.1)
        case .tilde: mark("˜", scale: 1.1)
        case .dot: mark("˙")
        case .ddot: mark("¨")
        case .check: mark("ˇ")
        case .breve: mark("˘")
        case .acute: mark("ˊ")
        case .grave: mark("ˋ")
        case .overbrace, .underbrace:
            let brace = CGMutablePath()
            let over = kind == .overbrace
            let y = over ? top + size * 0.05 : -base.descent - size * 0.08
            let depth = (over ? 1 : -1) * size * 0.16
            let w = base.width
            brace.move(to: CGPoint(x: 0, y: y))
            brace.addQuadCurve(to: CGPoint(x: w * 0.12, y: y + depth * 0.6), control: CGPoint(x: 0, y: y + depth * 0.6))
            brace.addLine(to: CGPoint(x: w * 0.42, y: y + depth * 0.6))
            brace.addQuadCurve(to: CGPoint(x: w / 2, y: y + depth), control: CGPoint(x: w / 2, y: y + depth * 0.6))
            brace.addQuadCurve(to: CGPoint(x: w * 0.58, y: y + depth * 0.6), control: CGPoint(x: w / 2, y: y + depth * 0.6))
            brace.addLine(to: CGPoint(x: w * 0.88, y: y + depth * 0.6))
            brace.addQuadCurve(to: CGPoint(x: w, y: y), control: CGPoint(x: w, y: y + depth * 0.6))
            box.items.append(.stroke(brace, .zero, width: line))
            if over { box.ascent = max(box.ascent, y + depth + line) } else { box.descent = max(box.descent, -(y + depth) + line) }
        }
        return box
    }

    mutating func matrix(_ rows: [[TeXNode]], kind: MatrixKind, style: TeXStyle) -> MathBox {
        let cellStyle: TeXStyle = kind == .aligned || kind == .cases ? style : (style == .display ? .text : style)
        // In aligned equations the right-hand cells start as if after an
        // empty atom ({}=), so a leading relation keeps its space.
        let cells = rows.map { row in
            row.enumerated().map { column, cell in
                guard kind == .aligned, column % 2 == 1 else { return layout(cell, style: cellStyle) }
                let nodes: [TeXNode] = if case .group(let inner) = cell { inner } else { [cell] }
                return layout(.group([.group([])] + nodes), style: cellStyle)
            }
        }
        let columns = cells.map(\.count).max() ?? 0
        guard columns > 0 else { return MathBox() }
        let size = size(style)
        var widths = Array(repeating: CGFloat(0), count: columns)
        for row in cells { for (index, cell) in row.enumerated() { widths[index] = max(widths[index], cell.width) } }
        let columnGap: CGFloat = kind == .aligned ? 0 : kind == .cases ? size * 1.0 : size * 0.9
        let rowGap = size * (kind == .small ? 0.15 : 0.28)
        let strut = (ascent: size * 0.72, descent: size * 0.28)
        let metrics: [(CGFloat, CGFloat)] = cells.map { row in
            (max(row.map(\.ascent).max() ?? 0, strut.ascent), max(row.map(\.descent).max() ?? 0, strut.descent))
        }
        let total = metrics.reduce(0) { $0 + $1.0 + $1.1 } + rowGap * CGFloat(max(metrics.count - 1, 0))
        var box = MathBox(atom: .inner)
        // The block's vertical centre sits on the math axis.
        var y = axis(style) + total / 2
        for (rowIndex, row) in cells.enumerated() {
            let (ascent, descent) = metrics[rowIndex]
            y -= ascent
            var x: CGFloat = 0
            for column in 0..<columns {
                if column < row.count {
                    let cell = row[column]
                    let offset: CGFloat = switch kind {
                    case .cases: 0
                    case .aligned: column % 2 == 0 ? widths[column] - cell.width : 0
                    default: (widths[column] - cell.width) / 2
                    }
                    box.append(cell, x: x + offset, y: y)
                }
                x += widths[column] + (column < columns - 1 ? columnGap : 0)
            }
            y -= descent + rowGap
        }
        box.width = widths.reduce(0, +) + columnGap * CGFloat(columns - 1)
        box.ascent = max(box.ascent, axis(style) + total / 2)
        box.descent = max(box.descent, total / 2 - axis(style))
        return box
    }
}
