import AppKit
import SwiftUI

/// Draws a `TerminalSession` and turns keyboard, paste and scroll input into
/// terminal bytes. Input methods (Chinese, Japanese…) compose in place at the
/// cursor and send only the committed text.
public final class TerminalNSView: NSView, @preconcurrency NSTextInputClient {
    public var session: TerminalSession {
        didSet { attach() }
    }
    public var fontSize: CGFloat = 12 { didSet { updateFont() } }

    private var font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
    private var boldFont = NSFont.monospacedSystemFont(ofSize: 12, weight: .bold)
    private var cellWidth: CGFloat = 7
    private var cellHeight: CGFloat = 15
    private var baseline: CGFloat = 11
    private let inset: CGFloat = 6
    private var markedText = ""
    private var selection: (start: (x: Int, y: Int), end: (x: Int, y: Int))?
    private var scrollRemainder: CGFloat = 0
    private var focused = false

    public init(session: TerminalSession) {
        self.session = session
        super.init(frame: .zero)
        updateFont()
        attach()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    public override var isFlipped: Bool { true }
    public override var acceptsFirstResponder: Bool { true }
    public override func becomeFirstResponder() -> Bool { focused = true; needsDisplay = true; return true }
    public override func resignFirstResponder() -> Bool { focused = false; needsDisplay = true; return true }

    private func attach() {
        session.onUpdate = { [weak self] in self?.needsDisplay = true }
        needsDisplay = true
    }

    private func updateFont() {
        font = NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
        boldFont = NSFont.monospacedSystemFont(ofSize: fontSize, weight: .bold)
        // The exact advance, not rounded: ASCII runs are drawn as one string,
        // so the grid must use the font's own spacing to stay aligned.
        cellWidth = ("M" as NSString).size(withAttributes: [.font: font]).width
        cellHeight = (font.ascender - font.descender + font.leading).rounded(.up) + 1
        baseline = font.ascender.rounded(.up)
        reportSize()
        needsDisplay = true
    }

    /// The grid that fits the view, sent to the server after a short pause.
    public var gridSize: (columns: Int, rows: Int) {
        (max(Int((bounds.width - inset * 2) / cellWidth), 2), max(Int((bounds.height - inset * 2) / cellHeight), 1))
    }

    public override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        reportSize()
    }

    private func reportSize() {
        guard bounds.width > 0, bounds.height > 0 else { return }
        let size = gridSize
        session.resize(columns: size.columns, rows: size.rows)
    }

    // MARK: Drawing

    public override func draw(_ dirtyRect: NSRect) {
        let palette = TerminalPalette(appearance: effectiveAppearance)
        palette.background.setFill()
        bounds.fill()
        let emulator = session.emulator
        let lines = emulator.lines
        let selected = normalizedSelection
        for (y, row) in lines.enumerated() {
            let top = inset + CGFloat(y) * cellHeight
            guard top < dirtyRect.maxY, top + cellHeight > dirtyRect.minY else { continue }
            // Backgrounds, merged across runs of the same colour.
            var x = 0
            while x < row.count {
                let color = palette.colors(for: row[x].style).background
                var end = x + 1
                while end < row.count && palette.colors(for: row[end].style).background == color { end += 1 }
                if color != palette.background {
                    color.setFill()
                    NSRect(x: inset + CGFloat(x) * cellWidth, y: top, width: CGFloat(end - x) * cellWidth, height: cellHeight).fill()
                }
                x = end
            }
            if let selected, y >= selected.start.y, y <= selected.end.y {
                let from = y == selected.start.y ? selected.start.x : 0
                let to = y == selected.end.y ? selected.end.x : row.count - 1
                if from <= to {
                    NSColor.selectedTextBackgroundColor.setFill()
                    NSRect(x: inset + CGFloat(from) * cellWidth, y: top, width: CGFloat(to - from + 1) * cellWidth, height: cellHeight).fill()
                }
            }
            drawText(row, top: top, palette: palette)
        }
        drawCursor(palette: palette)
    }

    /// ASCII runs of one style draw as a single string (the font is
    /// monospaced); everything else is placed cell by cell so wide and
    /// fallback-font glyphs never shift the rest of the row.
    private func drawText(_ row: [TerminalCell], top: CGFloat, palette: TerminalPalette) {
        var x = 0
        while x < row.count {
            let cell = row[x]
            if cell.width == 0 || cell.style.hidden || cell.text == " " { x += 1; continue }
            let isASCII = cell.width == 1 && cell.text.unicodeScalars.count == 1 && cell.text.unicodeScalars.first!.isASCII
            var text = cell.text
            var end = x + 1
            if isASCII {
                while end < row.count, row[end].style == cell.style, row[end].width == 1,
                      row[end].text.unicodeScalars.count == 1, row[end].text.unicodeScalars.first!.isASCII {
                    text += row[end].text
                    end += 1
                }
            }
            let attributes = attributes(for: cell.style, palette: palette)
            var origin = NSPoint(x: inset + CGFloat(x) * cellWidth, y: top)
            if !isASCII {
                // Wide and fallback glyphs are centred in the cells they own.
                let width = (text as NSString).size(withAttributes: attributes).width
                origin.x += max((CGFloat(cell.width) * cellWidth - width) / 2, 0)
            }
            (text as NSString).draw(at: origin, withAttributes: attributes)
            x = end
        }
    }

    private func attributes(for style: CellStyle, palette: TerminalPalette) -> [NSAttributedString.Key: Any] {
        var result: [NSAttributedString.Key: Any] = [
            .font: style.bold ? boldFont : font,
            .foregroundColor: palette.colors(for: style).foreground,
        ]
        if style.italic { result[.obliqueness] = 0.18 }
        if style.underline { result[.underlineStyle] = NSUnderlineStyle.single.rawValue }
        if style.strikethrough { result[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
        return result
    }

    private func drawCursor(palette: TerminalPalette) {
        let emulator = session.emulator
        guard session.phase == .connected else { return }
        let rect = NSRect(x: inset + CGFloat(emulator.cursorX) * cellWidth, y: inset + CGFloat(emulator.cursorY) * cellHeight,
                          width: cellWidth, height: cellHeight)
        if !markedText.isEmpty {
            let attributes: [NSAttributedString.Key: Any] = [
                .font: font, .foregroundColor: palette.foreground,
                .backgroundColor: palette.background,
                .underlineStyle: NSUnderlineStyle.single.rawValue,
            ]
            (markedText as NSString).draw(at: rect.origin, withAttributes: attributes)
            return
        }
        guard emulator.cursorVisible else { return }
        if focused {
            palette.cursor.setFill()
            rect.fill()
            let cell = emulator.cell(emulator.cursorX, emulator.cursorY)
            if cell.width > 0, cell.text != " " {
                var attributes = self.attributes(for: cell.style, palette: palette)
                attributes[.foregroundColor] = palette.background
                (cell.text as NSString).draw(at: rect.origin, withAttributes: attributes)
            }
        } else {
            palette.cursor.setStroke()
            NSBezierPath(rect: rect.insetBy(dx: 0.5, dy: 0.5)).stroke()
        }
    }

    // MARK: Keyboard

    public override func keyDown(with event: NSEvent) {
        selection = nil
        if markedText.isEmpty, let bytes = TerminalKeys.encode(event, applicationCursor: session.emulator.applicationCursorKeys) {
            session.send(bytes)
            return
        }
        interpretKeyEvents([event])
    }

    public override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard window?.firstResponder === self, event.modifierFlags.contains(.command) else { return super.performKeyEquivalent(with: event) }
        switch event.charactersIgnoringModifiers {
        case "c" where selection != nil: copy(nil); return true
        case "v": paste(nil); return true
        case "a": selectAll(nil); return true
        default: return super.performKeyEquivalent(with: event)
        }
    }

    @objc public func copy(_ sender: Any?) {
        guard let selected = normalizedSelection else { return }
        let text = session.emulator.text(from: selected.start, to: selected.end)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc public func paste(_ sender: Any?) {
        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else { return }
        var body = text.replacingOccurrences(of: "\r\n", with: "\r").replacingOccurrences(of: "\n", with: "\r")
        if session.emulator.bracketedPaste {
            // A pasted end marker would let the text escape the paste.
            body = "\u{1B}[200~" + body.replacingOccurrences(of: "\u{1B}[201~", with: "") + "\u{1B}[201~"
        }
        session.send(body)
    }

    public override func selectAll(_ sender: Any?) {
        let emulator = session.emulator
        selection = ((0, 0), (emulator.columns - 1, emulator.rows - 1))
        needsDisplay = true
    }

    public override func doCommand(by selector: Selector) {
        let map: [Selector: String] = [
            #selector(insertNewline(_:)): "\r",
            #selector(insertTab(_:)): "\t",
            #selector(insertBacktab(_:)): "\u{1B}[Z",
            #selector(deleteBackward(_:)): "\u{7F}",
            #selector(cancelOperation(_:)): "\u{1B}",
        ]
        if let text = map[selector] { session.send(text) }
    }

    // MARK: NSTextInputClient

    public func insertText(_ string: Any, replacementRange: NSRange) {
        let text = (string as? NSAttributedString)?.string ?? (string as? String) ?? ""
        markedText = ""
        needsDisplay = true
        session.send(text.replacingOccurrences(of: "\n", with: "\r"))
    }

    public func setMarkedText(_ string: Any, selectedRange: NSRange, replacementRange: NSRange) {
        markedText = (string as? NSAttributedString)?.string ?? (string as? String) ?? ""
        needsDisplay = true
    }

    public func unmarkText() { markedText = ""; needsDisplay = true }
    public func selectedRange() -> NSRange { NSRange(location: 0, length: 0) }
    public func markedRange() -> NSRange {
        markedText.isEmpty ? NSRange(location: NSNotFound, length: 0) : NSRange(location: 0, length: (markedText as NSString).length)
    }
    public func hasMarkedText() -> Bool { !markedText.isEmpty }
    public func attributedSubstring(forProposedRange range: NSRange, actualRange: NSRangePointer?) -> NSAttributedString? { nil }
    public func validAttributesForMarkedText() -> [NSAttributedString.Key] { [] }
    public func characterIndex(for point: NSPoint) -> Int { 0 }

    public func firstRect(forCharacterRange range: NSRange, actualRange: NSRangePointer?) -> NSRect {
        let emulator = session.emulator
        let rect = NSRect(x: inset + CGFloat(emulator.cursorX) * cellWidth, y: inset + CGFloat(emulator.cursorY) * cellHeight,
                          width: cellWidth, height: cellHeight)
        guard let window else { return .zero }
        return window.convertToScreen(convert(rect, to: nil))
    }

    // MARK: Mouse

    private func gridPoint(_ event: NSEvent) -> (x: Int, y: Int) {
        let point = convert(event.locationInWindow, from: nil)
        let emulator = session.emulator
        return (min(max(Int((point.x - inset) / cellWidth), 0), emulator.columns - 1),
                min(max(Int((point.y - inset) / cellHeight), 0), emulator.rows - 1))
    }

    private var normalizedSelection: (start: (x: Int, y: Int), end: (x: Int, y: Int))? {
        guard let selection else { return nil }
        let a = selection.start, b = selection.end
        if a.x == b.x && a.y == b.y { return nil }
        return (a.y, a.x) <= (b.y, b.x) ? (a, b) : (b, a)
    }

    public override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        let point = gridPoint(event)
        selection = (point, point)
        needsDisplay = true
    }

    public override func mouseDragged(with event: NSEvent) {
        guard let start = selection?.start else { return }
        selection = (start, gridPoint(event))
        needsDisplay = true
    }

    public override func scrollWheel(with event: NSEvent) {
        let delta = event.hasPreciseScrollingDeltas ? event.scrollingDeltaY / cellHeight : event.scrollingDeltaY * 3
        scrollRemainder += delta
        let lines = Int(scrollRemainder.rounded(.towardZero))
        guard lines != 0 else { return }
        scrollRemainder -= CGFloat(lines)
        // Positive deltas move toward earlier output, which tmux calls "up".
        session.scroll(lines: -lines)
    }

    public override func menu(for event: NSEvent) -> NSMenu? {
        let menu = NSMenu()
        menu.addItem(withTitle: "拷贝", action: #selector(copy(_:)), keyEquivalent: "c").isEnabled = normalizedSelection != nil
        menu.addItem(withTitle: "粘贴", action: #selector(paste(_:)), keyEquivalent: "v")
        menu.addItem(withTitle: "全选", action: #selector(selectAll(_:)), keyEquivalent: "a")
        return menu
    }
}

/// xterm key encoding. Option acts as Meta, as in the desktop client and in
/// terminals set up for shells and TUIs.
enum TerminalKeys {
    static func encode(_ event: NSEvent, applicationCursor: Bool) -> Data? {
        let flags = event.modifierFlags
        if flags.contains(.command) { return nil }
        let shift = flags.contains(.shift), option = flags.contains(.option), control = flags.contains(.control)
        let modifier = 1 + (shift ? 1 : 0) + (option ? 2 : 0) + (control ? 4 : 0)
        func csi(_ final: String) -> Data {
            if modifier > 1 { return Data("\u{1B}[1;\(modifier)\(final)".utf8) }
            return Data((applicationCursor ? "\u{1B}O" : "\u{1B}[") .appending(final).utf8)
        }
        func tilde(_ number: Int) -> Data {
            Data((modifier > 1 ? "\u{1B}[\(number);\(modifier)~" : "\u{1B}[\(number)~").utf8)
        }
        switch event.keyCode {
        case 126: return csi("A")
        case 125: return csi("B")
        case 124: return csi("C")
        case 123: return csi("D")
        case 115: return modifier > 1 ? Data("\u{1B}[1;\(modifier)H".utf8) : Data("\u{1B}[H".utf8)
        case 119: return modifier > 1 ? Data("\u{1B}[1;\(modifier)F".utf8) : Data("\u{1B}[F".utf8)
        case 116: return tilde(5)
        case 121: return tilde(6)
        case 117: return tilde(3)
        case 122: return Data("\u{1B}OP".utf8)
        case 120: return Data("\u{1B}OQ".utf8)
        case 99: return Data("\u{1B}OR".utf8)
        case 118: return Data("\u{1B}OS".utf8)
        case 96: return tilde(15)
        case 97: return tilde(17)
        case 98: return tilde(18)
        case 100: return tilde(19)
        case 101: return tilde(20)
        case 109: return tilde(21)
        case 103: return tilde(23)
        case 111: return tilde(24)
        case 53: return Data([0x1B])
        case 36, 76: return Data((option ? "\u{1B}\r" : "\r").utf8)
        case 48: return Data((shift ? "\u{1B}[Z" : "\t").utf8)
        case 51: return Data(option ? [0x1B, 0x7F] : (control ? [0x08] : [0x7F]))
        default: break
        }
        guard control || option, let base = event.charactersIgnoringModifiers, let scalar = base.unicodeScalars.first else { return nil }
        var bytes: [UInt8] = []
        if control {
            let value = scalar.value
            switch value {
            case 0x61...0x7A: bytes = [UInt8(value - 0x60)]
            case 0x41...0x5A: bytes = [UInt8(value - 0x40)]
            case 0x20, 0x40, 0x32: bytes = [0]
            case 0x5B, 0x33: bytes = [0x1B]
            case 0x5C, 0x34: bytes = [0x1C]
            case 0x5D, 0x35: bytes = [0x1D]
            case 0x5E, 0x36: bytes = [0x1E]
            case 0x5F, 0x2D, 0x37: bytes = [0x1F]
            case 0x2F: bytes = [0x1F]
            case 0x3F, 0x38: bytes = [0x7F]
            default: return nil
            }
        } else {
            bytes = Array(base.utf8)
        }
        return Data(option ? [0x1B] + bytes : bytes)
    }
}

/// Colours follow the window's appearance; the 16 ANSI colours are tuned for
/// contrast on both, and the rest come from the standard xterm 256 palette.
struct TerminalPalette {
    let foreground: NSColor
    let background: NSColor
    let cursor: NSColor
    private let ansi: [NSColor]

    init(appearance: NSAppearance) {
        let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        foreground = dark ? NSColor(white: 0.88, alpha: 1) : NSColor(white: 0.12, alpha: 1)
        background = dark ? NSColor(calibratedRed: 0.11, green: 0.11, blue: 0.12, alpha: 1) : NSColor(white: 0.985, alpha: 1)
        cursor = dark ? NSColor(white: 0.85, alpha: 1) : NSColor(white: 0.2, alpha: 1)
        func rgb(_ value: UInt32) -> NSColor {
            NSColor(srgbRed: CGFloat((value >> 16) & 0xFF) / 255, green: CGFloat((value >> 8) & 0xFF) / 255,
                    blue: CGFloat(value & 0xFF) / 255, alpha: 1)
        }
        ansi = (dark
            ? [0x1E1E22, 0xF2777A, 0x99CC99, 0xFFCC66, 0x6699CC, 0xCC99CC, 0x66CCCC, 0xCCCCCC,
               0x6E6E76, 0xFF8C8F, 0xB5E0B5, 0xFFDD88, 0x88BBEE, 0xE0B0E0, 0x88E0E0, 0xFFFFFF]
            : [0x000000, 0xC23621, 0x25A034, 0xA97B00, 0x1F5FBF, 0xA23DAD, 0x1C8E9A, 0x7A7A7A,
               0x666666, 0xE0443A, 0x2EB84A, 0xC99A00, 0x3B7CE0, 0xBF50CC, 0x22A8B5, 0x2B2B2B]).map(rgb)
    }

    func color(_ color: TerminalColor, isForeground: Bool) -> NSColor {
        switch color {
        case .default: return isForeground ? foreground : background
        case .rgb(let r, let g, let b):
            return NSColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: 1)
        case .indexed(let index):
            let i = Int(index)
            if i < 16 { return ansi[i] }
            if i < 232 {
                let levels: [CGFloat] = [0, 95, 135, 175, 215, 255]
                let n = i - 16
                return NSColor(srgbRed: levels[n / 36] / 255, green: levels[(n / 6) % 6] / 255, blue: levels[n % 6] / 255, alpha: 1)
            }
            let gray = CGFloat(8 + (i - 232) * 10) / 255
            return NSColor(srgbRed: gray, green: gray, blue: gray, alpha: 1)
        }
    }

    func colors(for style: CellStyle) -> (foreground: NSColor, background: NSColor) {
        var fg = color(style.foreground, isForeground: true)
        var bg = color(style.background, isForeground: false)
        if style.inverse { swap(&fg, &bg) }
        if style.dim { fg = fg.withAlphaComponent(0.6) }
        return (fg, bg)
    }
}

/// SwiftUI host for a terminal session.
public struct TerminalSurface: NSViewRepresentable {
    let session: TerminalSession
    var focusOnAppear = true

    public init(session: TerminalSession, focusOnAppear: Bool = true) {
        self.session = session
        self.focusOnAppear = focusOnAppear
    }

    public func makeNSView(context: Context) -> TerminalNSView {
        let view = TerminalNSView(session: session)
        if focusOnAppear {
            DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        }
        return view
    }

    public func updateNSView(_ view: TerminalNSView, context: Context) {
        if view.session !== session { view.session = session }
    }
}
