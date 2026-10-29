import Foundation

/// A terminal colour as the program asked for it. The view maps these to its
/// palette, so a theme change never needs the stream to be replayed.
public enum TerminalColor: Hashable, Sendable {
    case `default`
    case indexed(UInt8)
    case rgb(UInt8, UInt8, UInt8)
}

public struct CellStyle: Hashable, Sendable {
    public var foreground: TerminalColor = .default
    public var background: TerminalColor = .default
    public var bold = false
    public var dim = false
    public var italic = false
    public var underline = false
    public var inverse = false
    public var hidden = false
    public var strikethrough = false

    public init() {}

    /// Erased cells keep only the background (xterm's background-colour erase).
    var erased: CellStyle {
        var style = CellStyle()
        style.background = background
        return style
    }
}

/// One grid position. A wide character occupies its cell (width 2) and the
/// following one (width 0, empty text).
public struct TerminalCell: Hashable, Sendable {
    public var text: String
    public var width: UInt8
    public var style: CellStyle

    public static let blank = TerminalCell(text: " ", width: 1, style: CellStyle())

    static func blank(_ style: CellStyle) -> TerminalCell {
        TerminalCell(text: " ", width: 1, style: style.erased)
    }
}

/// An xterm-compatible screen for one tmux client.
///
/// tmux keeps scrollback and draws panes, borders and its status itself, so a
/// single fixed grid (plus the alternate screen) is enough: there is no
/// history, reflow or diffing here. tmux repaints everything on attach and
/// after a resize, which also makes reconnecting self-healing.
public final class TerminalEmulator {
    public private(set) var columns: Int
    public private(set) var rows: Int
    public private(set) var cursorX = 0
    public private(set) var cursorY = 0
    public private(set) var cursorVisible = true
    public private(set) var applicationCursorKeys = false
    public private(set) var bracketedPaste = false
    public private(set) var mouseReporting = false
    public private(set) var usingAlternateScreen = false
    public private(set) var title = ""
    /// Increments on every change, so a view can tell when to redraw.
    public private(set) var revision = 0

    /// Bytes the terminal must answer with (device attributes, cursor reports).
    public var onResponse: ((Data) -> Void)?
    /// OSC 52 clipboard writes.
    public var onClipboard: ((String) -> Void)?

    private var primary: [[TerminalCell]]
    private var alternate: [[TerminalCell]]
    private var style = CellStyle()
    private var pendingWrap = false
    private var autowrap = true
    private var insertMode = false
    private var originMode = false
    private var scrollTop = 0
    private var scrollBottom: Int
    private var tabStops: Set<Int> = []
    private var g0LineDrawing = false
    private var g1LineDrawing = false
    private var shiftedOut = false
    private var saved = SavedCursor()
    private var savedPrimary = SavedCursor()

    private struct SavedCursor {
        var x = 0, y = 0
        var style = CellStyle()
        var originMode = false
        var g0LineDrawing = false
    }

    // Parser state.
    private enum State { case ground, escape, escapeIntermediate, csi, osc, oscEscape, string, stringEscape }
    private var state = State.ground
    private var parameters = ""
    private var intermediates = ""
    private var oscText = ""
    private var utf8 = [UInt8]()
    private var utf8Expected = 0

    public init(columns: Int = 80, rows: Int = 24) {
        self.columns = max(columns, 2)
        self.rows = max(rows, 1)
        primary = Self.grid(self.columns, self.rows)
        alternate = Self.grid(self.columns, self.rows)
        scrollBottom = self.rows - 1
        resetTabStops()
    }

    private static func grid(_ columns: Int, _ rows: Int) -> [[TerminalCell]] {
        Array(repeating: Array(repeating: .blank, count: columns), count: rows)
    }

    /// The visible screen, row by row.
    public var lines: [[TerminalCell]] { usingAlternateScreen ? alternate : primary }

    public func cell(_ x: Int, _ y: Int) -> TerminalCell { lines[y][x] }

    /// Plain text of a rectangle-free, row-major range (selection copy).
    public func text(from start: (x: Int, y: Int), to end: (x: Int, y: Int)) -> String {
        let (a, b) = (start.y, start.x) <= (end.y, end.x) ? (start, end) : (end, start)
        // A selection made before a resize may lie outside the current grid.
        let first = max(a.y, 0), last = min(b.y, rows - 1)
        guard first <= last else { return "" }
        var out: [String] = []
        for y in first...last {
            let from = max(y == a.y ? a.x : 0, 0)
            let to = min(y == b.y ? b.x : columns - 1, columns - 1)
            guard from <= to else { out.append(""); continue }
            let row = lines[y]
            var line = ""
            for x in from...to where row[x].width > 0 { line += row[x].text }
            while line.hasSuffix(" ") { line.removeLast() }
            out.append(line)
        }
        return out.joined(separator: "\n")
    }

    // MARK: Input

    public func feed(_ data: Data) {
        for byte in data { feed(byte) }
        revision &+= 1
    }

    public func feed(_ text: String) { feed(Data(text.utf8)) }

    private func feed(_ byte: UInt8) {
        // Multi-byte UTF-8 only occurs in ground state and strings; frames may
        // split a character, so the decoder keeps its partial sequence.
        if utf8Expected > 0 {
            if byte & 0xC0 == 0x80 {
                utf8.append(byte)
                if utf8.count == utf8Expected {
                    let text = String(decoding: utf8, as: UTF8.self)
                    utf8.removeAll(); utf8Expected = 0
                    for scalar in text.unicodeScalars { receive(scalar) }
                }
                return
            }
            utf8.removeAll(); utf8Expected = 0
            receive("\u{FFFD}")
        }
        switch byte {
        case 0..<0x80: receive(Unicode.Scalar(byte))
        case 0xC2...0xDF: utf8 = [byte]; utf8Expected = 2
        case 0xE0...0xEF: utf8 = [byte]; utf8Expected = 3
        case 0xF0...0xF4: utf8 = [byte]; utf8Expected = 4
        default: receive("\u{FFFD}")
        }
    }

    private func receive(_ scalar: Unicode.Scalar) {
        let value = scalar.value
        switch state {
        case .ground:
            if value < 0x20 || value == 0x7F { control(value) } else { print(scalar) }
        case .escape:
            escape(scalar)
        case .escapeIntermediate:
            designate(scalar)
        case .csi:
            if value == 0x1B { state = .escape; return }
            if value < 0x20 { control(value); return }
            switch value {
            case 0x30...0x3F: parameters.unicodeScalars.append(scalar)
            case 0x20...0x2F: intermediates.unicodeScalars.append(scalar)
            case 0x40...0x7E: csi(Character(scalar)); state = .ground
            default: state = .ground
            }
        case .osc:
            switch value {
            case 0x07: osc(); state = .ground
            case 0x1B: state = .oscEscape
            default: if oscText.unicodeScalars.count < 1 << 20 { oscText.unicodeScalars.append(scalar) }
            }
        case .oscEscape:
            if value == 0x5C { osc() }  // ESC \ = string terminator
            state = .ground
        case .string:
            // DCS, SOS, PM and APC: consumed until the terminator.
            if value == 0x1B { state = .stringEscape } else if value == 0x07 { state = .ground }
        case .stringEscape:
            state = value == 0x5C ? .ground : .string
        }
    }

    // MARK: Printing

    private func print(_ scalar: Unicode.Scalar) {
        var scalar = scalar
        if shiftedOut ? g1LineDrawing : g0LineDrawing, let mapped = Self.lineDrawing[scalar] { scalar = mapped }
        let width = Self.width(of: scalar)
        if width == 0 {
            attachToPreviousCell(scalar)
            return
        }
        if pendingWrap && autowrap {
            cursorX = 0
            index()
        }
        pendingWrap = false
        if width == 2 && cursorX == columns - 1 {
            if autowrap {
                setCell(cursorX, cursorY, .blank(style))
                cursorX = 0
                index()
            } else {
                cursorX = columns - 2
            }
        }
        if insertMode { insertBlanks(width) }
        clearWidePair(at: cursorX, cursorY)
        if width == 2 { clearWidePair(at: cursorX + 1, cursorY) }
        setCell(cursorX, cursorY, TerminalCell(text: String(scalar), width: UInt8(width), style: style))
        if width == 2 { setCell(cursorX + 1, cursorY, TerminalCell(text: "", width: 0, style: style)) }
        if cursorX + width >= columns {
            cursorX = columns - 1
            pendingWrap = autowrap
        } else {
            cursorX += width
        }
    }

    private func attachToPreviousCell(_ scalar: Unicode.Scalar) {
        var x = pendingWrap ? cursorX : cursorX - 1
        guard x >= 0 else { return }
        if x > 0, lines[cursorY][x].width == 0 { x -= 1 }
        var cell = lines[cursorY][x]
        cell.text.unicodeScalars.append(scalar)
        setCell(x, cursorY, cell)
    }

    /// Writing over one half of a wide character blanks the other half.
    private func clearWidePair(at x: Int, _ y: Int) {
        guard x < columns else { return }
        let cell = lines[y][x]
        if cell.width == 0, x > 0 { setCell(x - 1, y, .blank(cell.style)) }
        if cell.width == 2, x + 1 < columns { setCell(x + 1, y, .blank(cell.style)) }
    }

    private func setCell(_ x: Int, _ y: Int, _ cell: TerminalCell) {
        guard x >= 0, x < columns, y >= 0, y < rows else { return }
        if usingAlternateScreen { alternate[y][x] = cell } else { primary[y][x] = cell }
    }

    private func setRow(_ y: Int, _ row: [TerminalCell]) {
        if usingAlternateScreen { alternate[y] = row } else { primary[y] = row }
    }

    private func blankRow() -> [TerminalCell] { Array(repeating: .blank(style), count: columns) }

    // MARK: Controls

    private func control(_ value: UInt32) {
        switch value {
        case 0x08:
            if pendingWrap { pendingWrap = false } else { cursorX = max(cursorX - 1, 0) }
        case 0x09:
            cursorX = (cursorX + 1..<columns).first { tabStops.contains($0) } ?? columns - 1
            pendingWrap = false
        case 0x0A, 0x0B, 0x0C:
            index()
        case 0x0D:
            cursorX = 0; pendingWrap = false
        case 0x0E: shiftedOut = true
        case 0x0F: shiftedOut = false
        case 0x18, 0x1A: state = .ground
        case 0x1B:
            state = .escape; parameters = ""; intermediates = ""
        default: break  // BEL and the rest are ignored.
        }
    }

    private func index() {
        pendingWrap = false
        if cursorY == scrollBottom {
            scrollUp(1)
        } else if cursorY < rows - 1 {
            cursorY += 1
        }
    }

    private func reverseIndex() {
        pendingWrap = false
        if cursorY == scrollTop {
            scrollDown(1)
        } else if cursorY > 0 {
            cursorY -= 1
        }
    }

    private func scrollUp(_ count: Int) {
        let n = min(max(count, 1), scrollBottom - scrollTop + 1)
        var grid = lines
        grid.removeSubrange(scrollTop..<scrollTop + n)
        grid.insert(contentsOf: Array(repeating: blankRow(), count: n), at: scrollBottom - n + 1)
        if usingAlternateScreen { alternate = grid } else { primary = grid }
    }

    private func scrollDown(_ count: Int) {
        let n = min(max(count, 1), scrollBottom - scrollTop + 1)
        var grid = lines
        grid.removeSubrange(scrollBottom - n + 1...scrollBottom)
        grid.insert(contentsOf: Array(repeating: blankRow(), count: n), at: scrollTop)
        if usingAlternateScreen { alternate = grid } else { primary = grid }
    }

    // MARK: Escape sequences

    private func escape(_ scalar: Unicode.Scalar) {
        state = .ground
        switch scalar {
        case "[": state = .csi; parameters = ""; intermediates = ""
        case "]": state = .osc; oscText = ""
        case "P", "X", "^", "_": state = .string
        case "(", ")", "#", "%", " ": intermediates = String(scalar); state = .escapeIntermediate
        case "7": saveCursor()
        case "8": restoreCursor()
        case "D": index()
        case "E": cursorX = 0; index()
        case "M": reverseIndex()
        case "H": tabStops.insert(cursorX)
        case "c": reset()
        default: break  // = > (keypad modes) and \ (stray terminator).
        }
    }

    private func designate(_ scalar: Unicode.Scalar) {
        state = .ground
        switch intermediates {
        case "(": g0LineDrawing = scalar == "0"
        case ")": g1LineDrawing = scalar == "0"
        case "#" where scalar == "8":
            // DECALN: fill the screen with E for alignment tests.
            for y in 0..<rows { setRow(y, Array(repeating: TerminalCell(text: "E", width: 1, style: CellStyle()), count: columns)) }
        default: break
        }
    }

    private func saveCursor() {
        saved = SavedCursor(x: cursorX, y: cursorY, style: style, originMode: originMode, g0LineDrawing: g0LineDrawing)
    }

    private func restoreCursor() {
        cursorX = min(saved.x, columns - 1)
        cursorY = min(saved.y, rows - 1)
        style = saved.style
        originMode = saved.originMode
        g0LineDrawing = saved.g0LineDrawing
        pendingWrap = false
    }

    private func reset() {
        style = CellStyle()
        primary = Self.grid(columns, rows)
        alternate = Self.grid(columns, rows)
        usingAlternateScreen = false
        cursorX = 0; cursorY = 0
        scrollTop = 0; scrollBottom = rows - 1
        pendingWrap = false; autowrap = true; insertMode = false; originMode = false
        cursorVisible = true; applicationCursorKeys = false; bracketedPaste = false; mouseReporting = false
        g0LineDrawing = false; g1LineDrawing = false; shiftedOut = false
        saved = SavedCursor()
        resetTabStops()
    }

    private func resetTabStops() {
        tabStops = Set(stride(from: 8, to: columns, by: 8))
    }

    // MARK: CSI

    /// Parameters split on `;`; each may carry `:` sub-parameters (SGR colours).
    private func parameterGroups() -> (privateMarker: Character?, groups: [[Int?]]) {
        var text = Substring(parameters)
        var marker: Character?
        if let first = text.first, "?<=>".contains(first) {
            marker = first
            text = text.dropFirst()
        }
        let groups = text.split(separator: ";", omittingEmptySubsequences: false).map { group in
            group.split(separator: ":", omittingEmptySubsequences: false).map { Int($0) }
        }
        return (marker, groups)
    }

    private func csi(_ final: Character) {
        let (marker, groups) = parameterGroups()
        let values = groups.map { $0.first ?? nil }
        func p(_ i: Int, _ fallback: Int = 1) -> Int {
            guard i < values.count, let v = values[i], v != 0 else { return fallback }
            return v
        }
        if marker == "?" {
            switch final {
            case "h": values.forEach { setPrivateMode($0 ?? 0, true) }
            case "l": values.forEach { setPrivateMode($0 ?? 0, false) }
            default: break
            }
            return
        }
        if marker == ">" || marker == "=" {
            if final == "c" { onResponse?(Data("\u{1B}[>0;0;0c".utf8)) }
            return  // XTVERSION, modifyOtherKeys and similar are not answered.
        }
        if !intermediates.isEmpty {
            if intermediates == "!" && final == "p" { softReset() }
            return  // Cursor style (SP q) and others are cosmetic.
        }
        switch final {
        case "@": insertBlanks(p(0))
        case "A": moveCursor(y: cursorY - p(0), clampToRegion: true)
        case "B", "e": moveCursor(y: cursorY + p(0), clampToRegion: true)
        case "C", "a": moveCursor(x: cursorX + p(0))
        case "D": moveCursor(x: cursorX - p(0))
        case "E": moveCursor(x: 0, y: cursorY + p(0), clampToRegion: true)
        case "F": moveCursor(x: 0, y: cursorY - p(0), clampToRegion: true)
        case "G", "`": moveCursor(x: p(0) - 1)
        case "H", "f": setCursor(row: p(0) - 1, column: p(1) - 1)
        case "I": for _ in 0..<p(0) { control(0x09) }
        case "J": eraseDisplay(values.first.flatMap { $0 } ?? 0)
        case "K": eraseLine(values.first.flatMap { $0 } ?? 0)
        case "L": insertLines(p(0))
        case "M": deleteLines(p(0))
        case "P": deleteCharacters(p(0))
        case "S": scrollUp(p(0))
        case "T": scrollDown(p(0))
        case "X": eraseCharacters(p(0))
        case "Z":
            for _ in 0..<p(0) { cursorX = (0..<cursorX).last { tabStops.contains($0) } ?? 0 }
        case "b": repeatPrevious(p(0))
        case "c": onResponse?(Data("\u{1B}[?62;22c".utf8))
        case "d": setCursor(row: p(0) - 1, column: cursorX, absolute: true)
        case "g":
            switch values.first.flatMap({ $0 }) ?? 0 {
            case 0: tabStops.remove(cursorX)
            case 3: tabStops.removeAll()
            default: break
            }
        case "h": values.forEach { if $0 == 4 { insertMode = true } }
        case "l": values.forEach { if $0 == 4 { insertMode = false } }
        case "m": selectGraphicRendition(groups)
        case "n":
            switch values.first.flatMap({ $0 }) {
            case 5: onResponse?(Data("\u{1B}[0n".utf8))
            case 6: onResponse?(Data("\u{1B}[\(cursorY + 1);\(cursorX + 1)R".utf8))
            default: break
            }
        case "r":
            let top = p(0) - 1
            let bottom = (values.count > 1 ? (values[1] ?? rows) : rows) - 1
            if top < bottom, bottom < rows {
                scrollTop = top; scrollBottom = bottom
            } else {
                scrollTop = 0; scrollBottom = rows - 1
            }
            setCursor(row: 0, column: 0)
        case "s": saveCursor()
        case "u": restoreCursor()
        default: break  // t (window ops), q and the rest are ignored.
        }
    }

    private func setPrivateMode(_ mode: Int, _ on: Bool) {
        switch mode {
        case 1: applicationCursorKeys = on
        case 6: originMode = on; setCursor(row: 0, column: 0)
        case 7: autowrap = on
        case 25: cursorVisible = on
        case 47, 1047: switchScreen(alternate: on, clear: mode == 1047 && on)
        case 1048: on ? saveCursor() : restoreCursor()
        case 1049:
            if on {
                savedPrimary = SavedCursor(x: cursorX, y: cursorY, style: style, originMode: originMode, g0LineDrawing: g0LineDrawing)
                switchScreen(alternate: true, clear: true)
            } else {
                switchScreen(alternate: false, clear: false)
                saved = savedPrimary
                restoreCursor()
            }
        case 1000, 1002, 1003: mouseReporting = on
        case 2004: bracketedPaste = on
        default: break
        }
    }

    private func switchScreen(alternate on: Bool, clear: Bool) {
        guard on != usingAlternateScreen || clear else { return }
        usingAlternateScreen = on
        if on && clear { alternate = Self.grid(columns, rows).map { $0.map { _ in .blank(style) } } }
        pendingWrap = false
    }

    private func softReset() {
        style = CellStyle()
        insertMode = false; originMode = false; autowrap = true
        cursorVisible = true; applicationCursorKeys = false
        scrollTop = 0; scrollBottom = rows - 1
        g0LineDrawing = false; g1LineDrawing = false; shiftedOut = false
        saved = SavedCursor()
    }

    private func moveCursor(x: Int? = nil, y: Int? = nil, clampToRegion: Bool = false) {
        if let x { cursorX = min(max(x, 0), columns - 1) }
        if let y {
            if clampToRegion && cursorY >= scrollTop && cursorY <= scrollBottom {
                cursorY = min(max(y, scrollTop), scrollBottom)
            } else {
                cursorY = min(max(y, 0), rows - 1)
            }
        }
        pendingWrap = false
    }

    private func setCursor(row: Int, column: Int, absolute: Bool = false) {
        let top = originMode && !absolute ? scrollTop : 0
        let bottom = originMode && !absolute ? scrollBottom : rows - 1
        cursorY = min(max(row + top, top), bottom)
        cursorX = min(max(column, 0), columns - 1)
        pendingWrap = false
    }

    private func eraseDisplay(_ mode: Int) {
        switch mode {
        case 0:
            eraseLine(0)
            for y in cursorY + 1..<max(rows, cursorY + 1) { setRow(y, blankRow()) }
        case 1:
            for y in 0..<cursorY { setRow(y, blankRow()) }
            eraseLine(1)
        case 2, 3:
            for y in 0..<rows { setRow(y, blankRow()) }
        default: break
        }
        pendingWrap = false
    }

    private func eraseLine(_ mode: Int) {
        let range: Range<Int>
        switch mode {
        case 0: range = cursorX..<columns
        case 1: range = 0..<min(cursorX + 1, columns)
        default: range = 0..<columns
        }
        for x in range { setCell(x, cursorY, .blank(style)) }
        if mode == 0, cursorX > 0, lines[cursorY][cursorX - 1].width == 2 { setCell(cursorX - 1, cursorY, .blank(style)) }
        pendingWrap = false
    }

    private func eraseCharacters(_ count: Int) {
        clearWidePair(at: cursorX, cursorY)
        for x in cursorX..<min(cursorX + count, columns) { setCell(x, cursorY, .blank(style)) }
        pendingWrap = false
    }

    private func insertBlanks(_ count: Int) {
        var row = lines[cursorY]
        let n = min(count, columns - cursorX)
        row.insert(contentsOf: Array(repeating: .blank(style), count: n), at: cursorX)
        row.removeLast(n)
        setRow(cursorY, row)
        pendingWrap = false
    }

    private func deleteCharacters(_ count: Int) {
        var row = lines[cursorY]
        let n = min(count, columns - cursorX)
        row.removeSubrange(cursorX..<cursorX + n)
        row.append(contentsOf: Array(repeating: .blank(style), count: n))
        setRow(cursorY, row)
        pendingWrap = false
    }

    private func insertLines(_ count: Int) {
        guard cursorY >= scrollTop && cursorY <= scrollBottom else { return }
        let top = scrollTop
        scrollTop = cursorY
        scrollDown(count)
        scrollTop = top
        cursorX = 0; pendingWrap = false
    }

    private func deleteLines(_ count: Int) {
        guard cursorY >= scrollTop && cursorY <= scrollBottom else { return }
        let top = scrollTop
        scrollTop = cursorY
        scrollUp(count)
        scrollTop = top
        cursorX = 0; pendingWrap = false
    }

    private func repeatPrevious(_ count: Int) {
        let x = pendingWrap ? cursorX : cursorX - 1
        guard x >= 0, let scalar = lines[cursorY][x].text.unicodeScalars.first else { return }
        for _ in 0..<min(count, columns * rows) { print(scalar) }
    }

    // MARK: SGR

    private func selectGraphicRendition(_ groups: [[Int?]]) {
        var i = 0
        let flat: [[Int?]] = groups.isEmpty ? [[0]] : groups
        while i < flat.count {
            let group = flat[i]
            let code = group.first.flatMap { $0 } ?? 0
            switch code {
            case 0: style = CellStyle()
            case 1: style.bold = true
            case 2: style.dim = true
            case 3: style.italic = true
            case 4: style.underline = (group.count > 1 ? (group[1] ?? 1) : 1) != 0
            case 7: style.inverse = true
            case 8: style.hidden = true
            case 9: style.strikethrough = true
            case 21: style.underline = true
            case 22: style.bold = false; style.dim = false
            case 23: style.italic = false
            case 24: style.underline = false
            case 27: style.inverse = false
            case 28: style.hidden = false
            case 29: style.strikethrough = false
            case 30...37: style.foreground = .indexed(UInt8(code - 30))
            case 39: style.foreground = .default
            case 40...47: style.background = .indexed(UInt8(code - 40))
            case 49: style.background = .default
            case 90...97: style.foreground = .indexed(UInt8(code - 90 + 8))
            case 100...107: style.background = .indexed(UInt8(code - 100 + 8))
            case 38, 48, 58:
                let (color, consumed) = extendedColor(group, flat, i)
                if let color {
                    if code == 38 { style.foreground = color } else if code == 48 { style.background = color }
                }
                i += consumed
            default: break
            }
            i += 1
        }
    }

    /// `38;5;n`, `38;2;r;g;b` and the colon forms `38:5:n`, `38:2::r:g:b`.
    /// Returns how many following `;` groups were consumed.
    private func extendedColor(_ group: [Int?], _ all: [[Int?]], _ index: Int) -> (TerminalColor?, Int) {
        func byte(_ v: Int?) -> UInt8 { UInt8(clamping: v ?? 0) }
        if group.count > 1 {
            switch group[1] {
            case 5 where group.count > 2: return (.indexed(byte(group[2])), 0)
            case 2:
                let rgb = group.count >= 6 ? Array(group[3...5]) : Array(group.dropFirst(2).prefix(3))
                guard rgb.count == 3 else { return (nil, 0) }
                return (.rgb(byte(rgb[0]), byte(rgb[1]), byte(rgb[2])), 0)
            default: return (nil, 0)
            }
        }
        let rest = all.dropFirst(index + 1).map { $0.first ?? nil }
        switch rest.first ?? nil {
        case 5:
            return rest.count > 1 ? (.indexed(byte(rest[1])), 2) : (nil, rest.count)
        case 2:
            return rest.count > 3 ? (.rgb(byte(rest[1]), byte(rest[2]), byte(rest[3])), 4) : (nil, rest.count)
        default:
            return (nil, 0)
        }
    }

    // MARK: OSC

    private func osc() {
        let parts = oscText.split(separator: ";", maxSplits: 2, omittingEmptySubsequences: false)
        guard let code = parts.first.flatMap({ Int($0) }) else { return }
        switch code {
        case 0, 2:
            if parts.count > 1 { title = String(parts.dropFirst().joined(separator: ";").prefix(256)) }
        case 52 where parts.count == 3 && parts[2] != "?":
            if let data = Data(base64Encoded: String(parts[2])), let text = String(data: data, encoding: .utf8) {
                onClipboard?(text)
            }
        default: break
        }
    }

    // MARK: Resize

    /// Keeps the top-left of both screens; tmux repaints after the resize.
    public func resize(columns newColumns: Int, rows newRows: Int) {
        let c = max(newColumns, 2), r = max(newRows, 1)
        guard c != columns || r != rows else { return }
        func fit(_ grid: [[TerminalCell]]) -> [[TerminalCell]] {
            (0..<r).map { y in
                guard y < grid.count else { return Array(repeating: .blank, count: c) }
                var row = Array(grid[y].prefix(c))
                if let last = row.last, last.width == 2 { row[row.count - 1] = .blank }
                row.append(contentsOf: Array(repeating: .blank, count: max(c - row.count, 0)))
                return row
            }
        }
        primary = fit(primary)
        alternate = fit(alternate)
        columns = c; rows = r
        cursorX = min(cursorX, c - 1); cursorY = min(cursorY, r - 1)
        scrollTop = 0; scrollBottom = r - 1
        pendingWrap = false
        resetTabStops()
        revision &+= 1
    }

    /// Clears the screen for a fresh attach; tmux paints the whole screen next.
    public func restart(columns: Int, rows: Int) {
        resize(columns: columns, rows: rows)
        reset()
        title = ""
        state = .ground
        utf8.removeAll(); utf8Expected = 0
        revision &+= 1
    }

    // MARK: Character data

    /// Display width in cells: 0 for combining marks and format characters,
    /// 2 for East Asian wide/fullwidth and emoji presentation, else 1.
    public static func width(of scalar: Unicode.Scalar) -> Int {
        let v = scalar.value
        if v < 0x300 { return 1 }
        switch scalar.properties.generalCategory {
        case .nonspacingMark, .enclosingMark, .format: return 0
        default: break
        }
        if (0xFE00...0xFE0F).contains(v) || (0xE0100...0xE01EF).contains(v) { return 0 }
        if scalar.properties.isEmojiPresentation { return 2 }
        let wide: [ClosedRange<UInt32>] = [
            0x1100...0x115F, 0x231A...0x231B, 0x2329...0x232A, 0x2E80...0x303E, 0x3041...0x33FF,
            0x3400...0x4DBF, 0x4E00...0x9FFF, 0xA000...0xA4CF, 0xA960...0xA97F, 0xAC00...0xD7A3,
            0xF900...0xFAFF, 0xFE10...0xFE19, 0xFE30...0xFE6F, 0xFF00...0xFF60, 0xFFE0...0xFFE6,
            0x1F300...0x1F64F, 0x1F900...0x1F9FF, 0x20000...0x2FFFD, 0x30000...0x3FFFD,
        ]
        return wide.contains { $0.contains(v) } ? 2 : 1
    }

    /// DEC special graphics, used for borders when a program asks for it.
    static let lineDrawing: [Unicode.Scalar: Unicode.Scalar] = [
        "`": "◆", "a": "▒", "f": "°", "g": "±", "i": "␋", "j": "┘", "k": "┐", "l": "┌",
        "m": "└", "n": "┼", "o": "⎺", "p": "⎻", "q": "─", "r": "⎼", "s": "⎽", "t": "├",
        "u": "┤", "v": "┴", "w": "┬", "x": "│", "y": "≤", "z": "≥", "{": "π", "|": "≠",
        "}": "£", "~": "·",
    ]
}
