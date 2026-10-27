import AppKit
import Testing
@testable import HyadexTerminal

@Suite @MainActor struct TerminalKeysTests {
    private func key(_ code: UInt16, _ characters: String, _ flags: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0,
                         context: nil, characters: characters, charactersIgnoringModifiers: characters,
                         isARepeat: false, keyCode: code)!
    }

    private func encoded(_ event: NSEvent, applicationCursor: Bool = false) -> String? {
        TerminalKeys.encode(event, applicationCursor: applicationCursor).map { String(decoding: $0, as: UTF8.self) }
    }

    @Test func arrowsFollowCursorKeyModeAndModifiers() {
        #expect(encoded(key(126, "")) == "\u{1B}[A")
        #expect(encoded(key(126, ""), applicationCursor: true) == "\u{1B}OA")
        #expect(encoded(key(123, "", .shift)) == "\u{1B}[1;2D")
        #expect(encoded(key(124, "", [.option, .control])) == "\u{1B}[1;7C")
    }

    @Test func controlOptionAndSpecialKeys() {
        #expect(encoded(key(8, "c", .control)) == "\u{03}")
        #expect(encoded(key(0, "a", .option)) == "\u{1B}a")
        #expect(encoded(key(36, "\r")) == "\r")
        #expect(encoded(key(48, "\t", .shift)) == "\u{1B}[Z")
        #expect(encoded(key(51, "\u{7F}")) == "\u{7F}")
        #expect(encoded(key(116, "")) == "\u{1B}[5~")
        #expect(encoded(key(96, "")) == "\u{1B}[15~")
    }

    @Test func plainTextAndCommandShortcutsAreNotEncoded() {
        // Plain text goes through the input method; ⌘ keys stay menu shortcuts.
        #expect(encoded(key(0, "a")) == nil)
        #expect(encoded(key(9, "v", .command)) == nil)
    }
}
