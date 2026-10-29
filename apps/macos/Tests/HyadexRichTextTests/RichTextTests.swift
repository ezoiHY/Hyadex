import AppKit
import Testing
@testable import HyadexRichText

struct MarkdownParserTests {
    @Test func blocksAgentsWrite() {
        let blocks = MarkdownParser.parse("""
        # Title
        Some *text* and **bold**.

        1. first
           - nested
        2. second

        - [x] done
        - [ ] todo

        > quote

        | a | b |
        |:--|--:|
        | 1 | `x|y` |

        ---
        ```swift
        let a = 1
        ```
        """)
        let kinds = blocks.map { block -> String in
            switch block {
            case .heading(let level, _): "h\(level)"
            case .paragraph: "p"
            case .code(let language, _): "code:\(language ?? "")"
            case .html: "html"
            case .list(let ordered, _, let items): "\(ordered ? "ol" : "ul")\(items.count)"
            case .quote: "quote"
            case .table(let header, let rows, let alignments): "table\(header.count)x\(rows.count):\(alignments.map { "\($0.map { "\($0)" } ?? "-")" }.joined(separator: ","))"
            case .rule: "hr"
            }
        }
        #expect(kinds == ["h1", "p", "ol2", "ul2", "quote", "table2x1:left,right", "hr", "code:swift"])
        if case .list(_, _, let items) = blocks[2] {
            #expect(items[0].blocks.count == 2)  // paragraph + nested list
        }
        if case .list(_, _, let items) = blocks[3] {
            #expect(items.map(\.checkbox) == [true, false])
        }
        if case .table(_, let rows, _) = blocks[5] {
            #expect(rows[0][1] == [.code("x|y")])
        }
    }

    @Test func unterminatedFenceIsStillCode() {
        let blocks = MarkdownParser.parse("text\n```bash\necho hi")
        guard case .code(let language, let source)? = blocks.last else { Issue.record("no code"); return }
        #expect(language == "bash")
        #expect(source == "echo hi")
    }

    @Test func inlineSyntax() {
        let inlines = InlineParser.parse("a **b** *c* ~~d~~ `e` [f](/p/x.swift:3) ![g](img.png) https://x.dev. snake_case_name")
        #expect(inlines.contains(.strong([.text("b")])))
        #expect(inlines.contains(.emphasis([.text("c")])))
        #expect(inlines.contains(.strikethrough([.text("d")])))
        #expect(inlines.contains(.code("e")))
        #expect(inlines.contains(.link(destination: "/p/x.swift:3", [.text("f")])))
        #expect(inlines.contains(.image(source: "img.png", alt: "g")))
        #expect(inlines.contains(.link(destination: "https://x.dev", [.text("https://x.dev")])))
        // Intraword underscores are not emphasis.
        #expect(inlines.last == .text(". snake_case_name"))
    }
}

struct MathTests {
    @Test func pricesAreNotMath() {
        let (_, spans) = MathExtractor.extract("costs $5 and $10, but $x^2$ is math and $$\\frac{a}{b}$$ too")
        #expect(spans.latex == ["x^2", "\\frac{a}{b}"])
        #expect(spans.display == [1])
    }

    @Test func formulasAgentsWriteParse() throws {
        let formulas = [
            #"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}"#,
            #"\sum_{i=1}^{n} i = \frac{n(n+1)}{2}"#,
            #"\int_0^\infty e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}"#,
            #"\lim_{n\to\infty}\left(1+\frac{1}{n}\right)^n = e"#,
            #"A=\begin{pmatrix}1 & 2\\ 3 & 4\end{pmatrix}"#,
            #"f(x)=\begin{cases} x, & x\ge 0\\ -x, & x<0\end{cases}"#,
            #"\begin{aligned} a &= b \\ &= c \end{aligned}"#,
            #"\mathbb{R}^n, \mathcal{L}, \mathbf{F}=m\mathbf{a}, \vec{v}, \hat{x}, \operatorname{softmax}(z)_i"#,
            #"\binom{n}{k} \le \sqrt[3]{x} \quad f'(x) \not= 0 \text{ 当 } x \in [0,1]"#,
        ]
        for formula in formulas {
            var parser = TeXParser(formula)
            #expect(throws: Never.self, "\(formula)") { _ = try parser.parse() }
        }
        var unknown = TeXParser(#"\unknownmacro{x}"#)
        #expect(throws: TeXError.self) { _ = try unknown.parse() }
    }

    @MainActor @Test func rendersToAnImageOnItsBaseline() throws {
        let rendered = try #require(MathRenderer.render(#"\frac{a}{b}"#, fontSize: 14, display: true, dark: false))
        #expect(rendered.image.size.width > 10)
        #expect(rendered.descent > 4)  // the denominator hangs below the baseline
        #expect(MathRenderer.render(#"\frac{a}"#, fontSize: 14, display: false, dark: false) == nil)
    }
}

struct SelectableTextTests {
    @Test func textBlocksShareARunUntilCodeTablesOrImages() {
        let document = RichDocument.parsed("""
        Intro ![shot](a.png) after

        - item

        ```swift
        let a = 1
        ```

        - with code:

          ```
          x
          ```

        > quote
        """, mentions: [])
        let kinds = RichSegment.split(document.blocks).map { segment -> String in
            switch segment {
            case .text(let blocks): "text\(blocks.count)"
            case .block(.paragraph): "image"
            case .block(.code): "code"
            case .block(.list): "list"
            case .block: "other"
            }
        }
        // The image breaks its paragraph; a list holding code keeps its own views.
        #expect(kinds == ["text1", "image", "text2", "code", "list", "text1"])
    }

    @MainActor @Test func copiesMathAsTeXAndMarkersAsMarkdown() {
        let document = RichDocument.parsed("""
        Solve $x^2 = 4$:

        $$x = \\pm 2$$

        1. first
           - [x] done
        2. second
        """, mentions: [])
        guard case .text(let blocks)? = RichSegment.split(document.blocks).first else {
            Issue.record("no text run"); return
        }
        var builder = TextRunBuilder(dark: false)
        let view = RichTextView.make()
        view.setContent(builder.build(blocks))
        let all = NSRange(location: 0, length: view.textStorage?.length ?? 0)
        #expect(view.plainText(in: all) == """
        Solve $x^2 = 4$:
        $$x = \\pm 2$$
        1. first
          - [x] done
        2. second
        """)
        // Text wraps to the proposed width.
        let long = RichDocument.parsed(String(repeating: "A sentence long enough to wrap. ", count: 12), mentions: [])
        view.setContent(builder.build(long.blocks))
        let wide = view.fittingSize(width: 600)
        let narrow = view.fittingSize(width: 120)
        #expect(wide.width == 600 && narrow.height > wide.height * 3)
    }
}
