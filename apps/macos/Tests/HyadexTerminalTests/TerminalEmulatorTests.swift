import Foundation
import Testing
@testable import HyadexTerminal

@Suite struct TerminalEmulatorTests {
    private func row(_ terminal: TerminalEmulator, _ y: Int) -> String {
        terminal.text(from: (0, y), to: (terminal.columns - 1, y))
    }

    @Test func printsWrapsAndScrolls() {
        let terminal = TerminalEmulator(columns: 5, rows: 2)
        terminal.feed("abcdefgh")
        #expect(row(terminal, 0) == "abcde")
        #expect(row(terminal, 1) == "fgh")
        terminal.feed("ij\r\nxy")
        #expect(row(terminal, 0) == "fghij")
        #expect(row(terminal, 1) == "xy")
    }

    @Test func copyingASelectionFromBeforeAResizeStaysInTheGrid() {
        let terminal = TerminalEmulator(columns: 10, rows: 3)
        terminal.feed("0123456789\r\nabcdefghij")
        terminal.resize(columns: 6, rows: 2)
        // Selected near the right edge, then the window narrowed.
        #expect(terminal.text(from: (8, 0), to: (9, 1)) == "\nabcdef")
        #expect(terminal.text(from: (8, 0), to: (9, 0)) == "")
        #expect(terminal.text(from: (0, 5), to: (3, 7)) == "")
        #expect(terminal.text(from: (-3, -1), to: (2, 0)) == "012")
    }

    @Test func deferredWrapKeepsCursorOnLastColumn() {
        let terminal = TerminalEmulator(columns: 4, rows: 2)
        terminal.feed("abcd")
        #expect(terminal.cursorX == 3 && terminal.cursorY == 0)
        terminal.feed("\r")
        #expect(terminal.cursorX == 0 && terminal.cursorY == 0)
    }

    @Test func cursorAddressingAndErase() {
        let terminal = TerminalEmulator(columns: 10, rows: 3)
        terminal.feed("0123456789\u{1B}[2;3HXY\u{1B}[1;5H\u{1B}[K")
        #expect(row(terminal, 0) == "0123")
        #expect(row(terminal, 1) == "  XY")
        terminal.feed("\u{1B}[2J")
        #expect((0..<3).allSatisfy { row(terminal, $0).isEmpty })
    }

    @Test func sgrColoursAndReset() {
        let terminal = TerminalEmulator(columns: 10, rows: 1)
        terminal.feed("\u{1B}[1;31mA\u{1B}[38;5;200mB\u{1B}[38;2;1;2;3mC\u{1B}[38:2::4:5:6mD\u{1B}[0mE")
        #expect(terminal.cell(0, 0).style.bold)
        #expect(terminal.cell(0, 0).style.foreground == .indexed(1))
        #expect(terminal.cell(1, 0).style.foreground == .indexed(200))
        #expect(terminal.cell(2, 0).style.foreground == .rgb(1, 2, 3))
        #expect(terminal.cell(3, 0).style.foreground == .rgb(4, 5, 6))
        #expect(terminal.cell(4, 0).style == CellStyle())
    }

    @Test func wideCharactersTakeTwoCellsAndWrapEarly() {
        let terminal = TerminalEmulator(columns: 5, rows: 2)
        terminal.feed("ab中文")
        #expect(terminal.cell(2, 0).text == "中" && terminal.cell(2, 0).width == 2)
        #expect(terminal.cell(3, 0).width == 0)
        // 文 does not fit in the last column, so it starts the next row.
        #expect(terminal.cell(0, 1).text == "文")
        #expect(row(terminal, 0) == "ab中")
    }

    @Test func utf8SplitAcrossFramesAndCombiningMarks() {
        let terminal = TerminalEmulator(columns: 6, rows: 1)
        let bytes = Array("é中".utf8)
        terminal.feed(Data(bytes[0..<1]))
        terminal.feed(Data(bytes[1..<3]))
        terminal.feed(Data(bytes[3...]))
        #expect(terminal.cell(0, 0).text == "é")
        terminal.feed("e\u{301}")
        #expect(terminal.cell(3, 0).text == "e\u{301}")
        #expect(terminal.cursorX == 4)
    }

    @Test func scrollRegionAndReverseIndex() {
        let terminal = TerminalEmulator(columns: 3, rows: 4)
        terminal.feed("a\r\nb\r\nc\r\nd")
        terminal.feed("\u{1B}[2;3r\u{1B}[3;1H\n")  // scroll lines 2–3 only
        #expect([row(terminal, 0), row(terminal, 1), row(terminal, 2), row(terminal, 3)] == ["a", "c", "", "d"])
        terminal.feed("\u{1B}[2;1H\u{1B}M")
        #expect([row(terminal, 1), row(terminal, 2)] == ["", "c"])
    }

    @Test func alternateScreenRestoresPrimaryContentAndCursor() {
        let terminal = TerminalEmulator(columns: 6, rows: 2)
        terminal.feed("shell$ ")
        let cursor = (terminal.cursorX, terminal.cursorY)
        terminal.feed("\u{1B}[?1049h\u{1B}[HVIM")
        #expect(terminal.usingAlternateScreen && row(terminal, 0) == "VIM")
        terminal.feed("\u{1B}[?1049l")
        #expect(!terminal.usingAlternateScreen)
        #expect(row(terminal, 0) == "shell$")
        #expect((terminal.cursorX, terminal.cursorY) == cursor)
    }

    @Test func modesAndInsertDelete() {
        let terminal = TerminalEmulator(columns: 6, rows: 1)
        terminal.feed("\u{1B}[?1h\u{1B}[?2004h\u{1B}[?25l")
        #expect(terminal.applicationCursorKeys && terminal.bracketedPaste && !terminal.cursorVisible)
        terminal.feed("abcdef\u{1B}[1;2H\u{1B}[2P")
        #expect(row(terminal, 0) == "adef")
        terminal.feed("\u{1B}[1;2H\u{1B}[1@Z")
        #expect(row(terminal, 0) == "aZdef")
    }

    @Test func lineDrawingCharsetAndTitles() {
        let terminal = TerminalEmulator(columns: 4, rows: 1)
        terminal.feed("\u{1B}(0lqk\u{1B}(Bx\u{1B}]2;build log\u{07}")
        #expect(row(terminal, 0) == "┌─┐x")
        #expect(terminal.title == "build log")
    }

    @Test func answersDeviceQueries() {
        let terminal = TerminalEmulator(columns: 10, rows: 5)
        var replies: [String] = []
        terminal.onResponse = { replies.append(String(decoding: $0, as: UTF8.self)) }
        terminal.feed("\u{1B}[3;4H\u{1B}[6n\u{1B}[c")
        #expect(replies.first == "\u{1B}[3;4R")
        #expect(replies.count == 2)
    }

    @Test func resizeKeepsTopLeftAndClampsCursor() {
        let terminal = TerminalEmulator(columns: 6, rows: 3)
        terminal.feed("abcdef\r\nghij\u{1B}[3;6H")
        terminal.resize(columns: 3, rows: 2)
        #expect(row(terminal, 0) == "abc" && row(terminal, 1) == "ghi")
        #expect(terminal.cursorX == 2 && terminal.cursorY == 1)
    }

    @Test func oscClipboardIsDecoded() {
        let terminal = TerminalEmulator(columns: 4, rows: 1)
        var copied: String?
        terminal.onClipboard = { copied = $0 }
        terminal.feed("\u{1B}]52;c;\(Data("复制".utf8).base64EncodedString())\u{1B}\\")
        #expect(copied == "复制")
    }
}
