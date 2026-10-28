import AppKit
import ApplicationServices

/// Development aid: drives the app's real controls from inside the process,
/// the way assistive technology does. Buttons, fields and rows are found in
/// the window's accessibility tree and pressed or edited through it; menu
/// shortcuts go through the main menu's key-equivalent handling; keys go to
/// the window's first responder. It needs no system permission and never
/// moves the user's pointer or takes their keyboard.
@MainActor
enum UIDriver {
    struct Node {
        let element: NSObject
        let role: String
        let label: String
        let depth: Int
    }

    /// Every accessibility element under a window (and its attached sheet), depth first.
    static func nodes(in window: NSWindow) -> [Node] {
        var result: [Node] = []
        func visit(_ object: Any, depth: Int) {
            guard depth < 60, let element = object as? NSObject else { return }
            let role = (get(element, "accessibilityRole") as? String) ?? ""
            var parts = ["accessibilityLabel", "accessibilityTitle", "accessibilityIdentifier", "accessibilityValue",
                         "accessibilityHelp"]
                .compactMap { get(element, $0) as? String }
            // A SwiftUI Label button carries its words in a child text element.
            if role == "AXButton" || role == "AXMenuButton" || role == "AXRow" || role == "AXCell" {
                for child in (get(element, "accessibilityChildren") as? [NSObject]) ?? [] {
                    if let text = get(child, "accessibilityLabel") as? String { parts.append(text) }
                    if let text = get(child, "accessibilityValue") as? String { parts.append(text) }
                }
            }
            let label = parts.filter { !$0.isEmpty }.joined(separator: " | ")
            result.append(Node(element: element, role: role, label: label, depth: depth))
            for child in (get(element, "accessibilityChildren") as? [Any]) ?? [] {
                visit(child, depth: depth + 1)
            }
        }
        // Start from the views: the window's own element list may not include
        // SwiftUI's hosted content until something asks the hosting view.
        func visitViews(_ view: NSView, depth: Int) {
            visit(view, depth: depth)
            for subview in view.subviews { visitViews(subview, depth: depth + 1) }
        }
        for target in [window, window.attachedSheet].compactMap({ $0 }) {
            if let root = target.contentView?.superview ?? target.contentView { visitViews(root, depth: 0) }
        }
        return result
    }

    /// An object-valued accessibility getter, if the element implements it.
    static func get(_ element: NSObject, _ name: String) -> Any? {
        let selector = NSSelectorFromString(name)
        guard element.responds(to: selector) else { return nil }
        return element.perform(selector)?.takeUnretainedValue()
    }

    /// Calls a void or BOOL accessibility action, if implemented (the result is not read).
    static func call(_ element: NSObject, _ name: String, _ argument: Any? = nil) -> Bool {
        let selector = NSSelectorFromString(name)
        guard element.responds(to: selector) else { return false }
        if let argument { _ = element.perform(selector, with: argument) } else { _ = element.perform(selector) }
        return true
    }

    static func mainWindow() -> NSWindow? {
        NSApp.windows.first { $0.isVisible && $0.canBecomeMain }
    }

    /// The first element whose label contains `text` (and has `role`, when given), in the
    /// main window, its sheet, and any popover or alert the app shows.
    static func find(_ text: String, role: NSAccessibility.Role? = nil, in window: NSWindow? = nil) -> NSObject? {
        func search() -> NSObject? {
            let windows = window.map { [$0] } ?? NSApp.windows.filter(\.isVisible).sorted { a, _ in a.isSheet || a.level != .normal }
            for window in windows {
                if let match = nodes(in: window).first(where: { node in
                    node.label.contains(text) && (role == nil || node.role == role!.rawValue)
                }) {
                    return match.element
                }
            }
            return nil
        }
        if let match = search() { return match }
        // A window that appeared since (a sheet, a popover) builds its tree once asked.
        enable()
        return search()
    }

    /// Lets SwiftUI build its full accessibility tree: it does so once an
    /// accessibility client has asked about the app.
    static func enable() {
        let app = AXUIElementCreateApplication(getpid())
        var windows: CFTypeRef?
        AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windows)
        for window in (windows as? [AXUIElement]) ?? [] {
            var children: CFTypeRef?
            AXUIElementCopyAttributeValue(window, kAXChildrenAttribute as CFString, &children)
        }
    }

    /// Presses a button (or any pressable element) by its label.
    @discardableResult
    static func press(_ text: String, role: NSAccessibility.Role? = .button) -> Bool {
        guard let element = find(text, role: role) else { return false }
        return call(element, "accessibilityPerformPress")
    }

    /// Types into a text field found by its placeholder or label: the field
    /// becomes first responder and receives the text through `insertText`,
    /// the method key presses and input methods end in, so SwiftUI sees an
    /// ordinary edit. Existing text is replaced.
    @discardableResult
    static func type(_ text: String, into label: String) -> Bool {
        guard let (window, view) = textView(for: label) else { return false }
        window.makeKey()
        guard window.makeFirstResponder(view) else { return false }
        let editor = (window.firstResponder as? NSTextView) ?? (view as? NSTextView)
        guard let editor else { return false }
        editor.selectAll(nil)
        if text.isEmpty {
            editor.deleteBackward(nil)
        } else {
            editor.insertText(text, replacementRange: editor.selectedRange())
        }
        return true
    }

    /// The AppKit text view behind a SwiftUI text field, found by placeholder,
    /// or by hit-testing the frame of its accessibility element.
    static func textView(for label: String) -> (NSWindow, NSView)? {
        for window in NSApp.windows.filter(\.isVisible) {
            guard let root = window.contentView?.superview ?? window.contentView else { continue }
            var stack: [NSView] = [root]
            while let view = stack.popLast() {
                if let field = view as? NSTextField, field.isEditable,
                   (field.placeholderString ?? "").contains(label) || (field.accessibilityLabel() ?? "").contains(label) {
                    return (window, field)
                }
                if let textView = view as? NSTextView, textView.isEditable,
                   (textView.accessibilityPlaceholderValue() ?? "").contains(label)
                    || (textView.accessibilityLabel() ?? "").contains(label) {
                    return (window, textView)
                }
                stack.append(contentsOf: view.subviews)
            }
            // SwiftUI may keep the placeholder on its own element: hit-test that element's frame.
            if let node = nodes(in: window).first(where: { node in
                   (node.role == "AXTextField" || node.role == "AXTextArea")
                       && (node.label.contains(label)
                           || ((get(node.element, "accessibilityPlaceholderValue") as? String) ?? "").contains(label))
               }),
               node.element.responds(to: NSSelectorFromString("accessibilityFrame")),
               let frame = (node.element.value(forKey: "accessibilityFrame") as? NSValue)?.rectValue,
               let content = window.contentView {
                let center = NSPoint(x: frame.midX, y: frame.midY)
                let inWindow = window.convertPoint(fromScreen: center)
                if let hit = content.hitTest(content.convert(inWindow, from: nil)) {
                    var view: NSView? = hit
                    while let candidate = view {
                        if candidate is NSTextView || candidate is NSTextField { return (window, candidate) }
                        view = candidate.superview
                    }
                    // A text view nested below the hit view.
                    var below: [NSView] = [hit]
                    while let candidate = below.popLast() {
                        if candidate is NSTextView || candidate is NSTextField { return (window, candidate) }
                        below.append(contentsOf: candidate.subviews)
                    }
                }
            }
        }
        return nil
    }

    /// Sets a text field's value by its label or placeholder, as dictation or VoiceOver would.
    @discardableResult
    static func setText(_ text: String, inField label: String) -> Bool {
        let candidates = NSApp.windows.filter(\.isVisible).flatMap { nodes(in: $0) }
        let field = candidates.first { node in
            (node.role == NSAccessibility.Role.textField.rawValue || node.role == NSAccessibility.Role.textArea.rawValue)
                && (node.label.contains(label)
                    || ((get(node.element, "accessibilityPlaceholderValue") as? String) ?? "").contains(label))
        }
        guard let element = field?.element else { return false }
        _ = call(element, "setAccessibilityFocused:", NSNumber(value: true))
        return call(element, "setAccessibilityValue:", text)
    }

    /// A menu shortcut, through the same key-equivalent path a key press takes.
    @discardableResult
    static func shortcut(_ key: String, _ modifiers: NSEvent.ModifierFlags, keyCode: UInt16 = 0) -> Bool {
        guard let window = mainWindow(),
              let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0,
                                           windowNumber: window.windowNumber, context: nil,
                                           characters: modifiers.contains(.shift) ? key.uppercased() : key,
                                           charactersIgnoringModifiers: key.lowercased(), isARepeat: false,
                                           keyCode: keyCode)
        else { return false }
        return NSApp.mainMenu?.performKeyEquivalent(with: event) ?? false
    }

    /// The text currently in a field, by label or placeholder.
    static func text(inField label: String) -> String? {
        let candidates = NSApp.windows.filter(\.isVisible).flatMap { nodes(in: $0) }
        let field = candidates.first { node in
            (node.role == "AXTextField" || node.role == "AXTextArea")
                && (node.label.contains(label) || ((get(node.element, "accessibilityPlaceholderValue") as? String) ?? "").contains(label))
        }
        return field.flatMap { get($0.element, "accessibilityValue") as? String }
    }

    static var sheetShown: Bool { mainWindow()?.attachedSheet != nil || NSApp.windows.contains { $0.isSheet && $0.isVisible } }

    /// A key press delivered to the window's first responder (the focused field).
    /// The window becomes key inside the app only; the app is not activated.
    static func key(_ characters: String, keyCode: UInt16, modifiers: NSEvent.ModifierFlags = []) {
        guard let window = NSApp.windows.first(where: { $0.isVisible && $0.firstResponder is NSTextView }) ?? mainWindow()
        else { return }
        window.makeKey()
        // As NSApplication does: key equivalents (menu, then the window's controls) come first.
        if modifiers.contains(.command),
           let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0,
                                        windowNumber: window.windowNumber, context: nil, characters: characters,
                                        charactersIgnoringModifiers: characters, isARepeat: false, keyCode: keyCode),
           (NSApp.mainMenu?.performKeyEquivalent(with: event) == true || window.performKeyEquivalent(with: event)) {
            print("[ui] key equivalent handled: \(characters.debugDescription) \(modifiers.rawValue)")
            return
        }
        if modifiers.contains(.command) { print("[ui] key equivalent NOT handled; sent to \(String(describing: window.firstResponder))") }
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            if let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: modifiers, timestamp: 0,
                                            windowNumber: window.windowNumber, context: nil, characters: characters,
                                            charactersIgnoringModifiers: characters, isARepeat: false, keyCode: keyCode) {
                window.sendEvent(event)
            }
        }
    }

    static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }

    /// Whether the system accessibility API answers for this process about itself.
    static func probeSystemAX() {
        print("[ui] trusted: \(AXIsProcessTrusted())")
        let app = AXUIElementCreateApplication(getpid())
        var windows: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windows)
        print("[ui] AXUIElement windows status: \(status.rawValue) count: \((windows as? [AXUIElement])?.count ?? -1)")
        guard let window = (windows as? [AXUIElement])?.first else { return }
        var count = 0
        var seen = Set<Int>()
        var queue: [(AXUIElement, Int)] = [(window, 0)]
        while !queue.isEmpty, count < 5000 {
            let (element, depth) = queue.removeFirst()
            guard seen.insert(Int(CFHash(element))).inserted else { continue }
            count += 1
            let roleName = attribute(element, kAXRoleAttribute) as? String ?? ""
            let text = [attribute(element, kAXTitleAttribute), attribute(element, kAXDescriptionAttribute),
                        attribute(element, kAXHelpAttribute)]
                .compactMap { $0 as? String }.filter { !$0.isEmpty }
            if ["AXButton", "AXTextArea", "AXTextField", "AXRow", "AXMenuButton", "AXCheckBox", "AXSearchField"]
                .contains(roleName) {
                print("[ui] sys " + String(repeating: " ", count: min(depth, 16)) + roleName + " · " + text.joined(separator: " | "))
            }
            if roleName == "AXStaticText" || roleName == "AXImage" { continue }
            for child in (attribute(element, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
                queue.append((child, depth + 1))
            }
        }
        print("[ui] sys elements visited: \(count)")
    }

    /// Prints the tree (role and label) for finding controls.
    static func dump(_ window: NSWindow? = nil, limit: Int = 400) {
        guard let window = window ?? mainWindow() else { return }
        let interesting: Set<String> = ["AXButton", "AXTextField", "AXTextArea", "AXMenuButton", "AXPopUpButton",
                                         "AXCheckBox", "AXRadioButton", "AXRow", "AXCell", "AXSearchField", "AXToolbar",
                                         "AXOutline", "AXTable", "AXSheet", "AXDisclosureTriangle", "AXGroup"]
        for node in nodes(in: window).filter({ interesting.contains($0.role) && !$0.label.isEmpty }).prefix(limit) {
            print("[ui] " + String(repeating: " ", count: min(node.depth, 20)) + node.role + " · " + node.label)
        }
    }
}

// MARK: - Menus and drops

extension UIDriver {
    /// Opens a menu with `open` (a right-click menu or a menu button, through
    /// accessibility) and chooses the item titled `title` while the menu is
    /// tracking, as a click on it would. Returns whether the item was chosen.
    static func choose(_ title: String, opening open: () -> Bool) async -> Bool {
        /// Menu tracking runs on the main thread; this only crosses closures there.
        final class State: @unchecked Sendable {
            var chosen = false
            var titles: [String] = []
        }
        let state = State()
        let observer = NotificationCenter.default.addObserver(forName: NSMenu.didBeginTrackingNotification,
                                                              object: nil, queue: nil) { note in
            nonisolated(unsafe) let menu = note.object as? NSMenu
            let timer = Timer(timeInterval: 0.4, repeats: false) { _ in
                MainActor.assumeIsolated {
                    guard let menu else { return }
                    state.titles = menu.items.map(\.title)
                    if let (owner, index) = item(titled: title, in: menu) {
                        owner.performActionForItem(at: index)
                        state.chosen = true
                    }
                    menu.cancelTracking()
                }
            }
            RunLoop.main.add(timer, forMode: .common)
        }
        defer { NotificationCenter.default.removeObserver(observer) }
        for attempt in 0..<3 where !state.chosen {
            if attempt > 0 { try? await Task.sleep(for: .milliseconds(500)) }
            _ = open()  // a right-click menu tracks inside this call
            // A menu button may open its menu on the next turn of the run loop.
            for _ in 0..<10 where !state.chosen && state.titles.isEmpty { try? await Task.sleep(for: .milliseconds(200)) }
            if !state.titles.isEmpty { break }  // the menu opened; the item was or was not there
        }
        if !state.chosen { print("[ui] menu item \(title) not found in \(state.titles)") }
        return state.chosen
    }

    private static func item(titled title: String, in menu: NSMenu) -> (NSMenu, Int)? {
        for (index, item) in menu.items.enumerated() {
            if item.title == title || item.title.hasPrefix(title), item.isEnabled { return (menu, index) }
            if let submenu = item.submenu, let found = Self.item(titled: title, in: submenu) { return found }
        }
        return nil
    }

    /// Right-click menu of an element (a sidebar row), through accessibility's "show menu".
    static func showMenu(_ text: String) -> Bool {
        // A sidebar row (or its cell/text), never the window whose title also has the name.
        guard let window = mainWindow() else { return false }
        enable()
        let element = nodes(in: window).first { node in
            node.label.contains(text) && ["AXRow", "AXCell", "AXStaticText", "AXGroup"].contains(node.role)
                && !node.label.contains(" – ")
        }?.element
        guard let element else { return false }
        // The row itself, or the first ancestor that offers a menu.
        var candidate: NSObject? = element
        while let current = candidate {
            if call(current, "accessibilityPerformShowMenu") { return true }
            candidate = get(current, "accessibilityParent") as? NSObject
        }
        return false
    }

    /// Drops files on the window at a point in window coordinates, calling the
    /// destination the way AppKit does during a drag: entered, updated,
    /// prepare, perform, conclude.
    @discardableResult
    static func drop(_ urls: [URL], at point: NSPoint) -> Bool {
        guard let window = mainWindow(), let content = window.contentView else { return false }
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("hyadex-ui-drop-\(UUID().uuidString)"))
        pasteboard.clearContents()
        pasteboard.writeObjects(urls as [NSURL])
        let info = DragInfo(window: window, location: point, pasteboard: pasteboard)
        // SwiftUI's drop regions are overlay views that hit-testing skips: take the
        // smallest view that accepts file drags and covers the point, as AppKit does.
        let fileTypes: Set<NSPasteboard.PasteboardType> = [.fileURL, .URL, NSPasteboard.PasteboardType("public.item"),
                                                          NSPasteboard.PasteboardType("public.data")]
        var candidates: [NSView] = []
        var stack: [NSView] = [window.contentView?.superview ?? content]
        while let view = stack.popLast() {
            if !Set(view.registeredDraggedTypes).isDisjoint(with: fileTypes), !view.isHiddenOrHasHiddenAncestor,
               view.convert(view.bounds, to: nil).contains(point), !(view is NSTextView) {
                candidates.append(view)
            }
            stack.append(contentsOf: view.subviews)
        }
        guard let destination = candidates.min(by: { $0.bounds.width * $0.bounds.height < $1.bounds.width * $1.bounds.height })
        else {
            print("[ui] drop: no file destination covers the point")
            return false
        }
        print("[ui] drop: destination \(Swift.type(of: destination))")
        let entered = destination.draggingEntered(info)
        print("[ui] drop: entered -> \(entered.rawValue)")
        guard entered != [] else { return false }
        _ = destination.draggingUpdated(info)
        guard destination.prepareForDragOperation(info) else { return false }
        let accepted = destination.performDragOperation(info)
        destination.concludeDragOperation(info)
        pasteboard.releaseGlobally()
        return accepted
    }
}

/// What AppKit hands a drop destination during a drag.
private final class DragInfo: NSObject, NSDraggingInfo {
    let window: NSWindow
    let location: NSPoint
    let pasteboard: NSPasteboard

    init(window: NSWindow, location: NSPoint, pasteboard: NSPasteboard) {
        self.window = window
        self.location = location
        self.pasteboard = pasteboard
    }

    var draggingDestinationWindow: NSWindow? { window }
    var draggingSourceOperationMask: NSDragOperation { [.copy, .generic, .link] }
    var draggingLocation: NSPoint { location }
    var draggedImageLocation: NSPoint { location }
    var draggedImage: NSImage? { nil }
    var draggingPasteboard: NSPasteboard { pasteboard }
    var draggingSource: Any? { nil }
    var draggingSequenceNumber: Int { 1 }
    func slideDraggedImage(to screenPoint: NSPoint) {}
    var draggingFormation: NSDraggingFormation = .default
    var animatesToDestination = false
    var numberOfValidItemsForDrop = 1
    func enumerateDraggingItems(options enumOpts: NSDraggingItemEnumerationOptions = [], for view: NSView?,
                                classes classArray: [AnyClass], searchOptions: [NSPasteboard.ReadingOptionKey: Any] = [:],
                                using block: (NSDraggingItem, Int, UnsafeMutablePointer<ObjCBool>) -> Void) {
        let objects = pasteboard.readObjects(forClasses: classArray, options: searchOptions) ?? []
        var stop: ObjCBool = false
        for (index, object) in objects.enumerated() {
            guard let writer = object as? NSPasteboardWriting else { continue }
            block(NSDraggingItem(pasteboardWriter: writer), index, &stop)
            if stop.boolValue { break }
        }
    }
    var springLoadingHighlight: NSSpringLoadingHighlight { .none }
    func resetSpringLoading() {}
}

// MARK: - File panel

extension UIDriver {
    /// The open panel the app is showing, if any (the app is not sandboxed, so it runs in process).
    static var openPanel: NSOpenPanel? {
        NSApp.windows.compactMap { $0 as? NSOpenPanel }.first { $0.isVisible }
            ?? (mainWindow()?.attachedSheet as? NSOpenPanel)
    }

    /// Picks a file in the open panel the way a user can from the keyboard:
    /// ⇧⌘G (Go to Folder), type the file's path, Return to reveal it, Return to open.
    static func pick(_ file: URL, log: (String) -> Void) async -> Bool {
        guard let panel = openPanel else { log("no open panel"); return false }
        panel.makeKey()
        func send(_ characters: String, _ keyCode: UInt16, _ modifiers: NSEvent.ModifierFlags = [], to window: NSWindow) {
            guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0,
                                               windowNumber: window.windowNumber, context: nil, characters: characters,
                                               charactersIgnoringModifiers: characters.lowercased(), isARepeat: false,
                                               keyCode: keyCode) else { return }
            window.sendEvent(event)
        }
        send("G", 5, [.command, .shift], to: panel)
        try? await Task.sleep(for: .seconds(1))
        let goTo = panel.attachedSheet ?? NSApp.windows.first { $0.isVisible && $0.isSheet && $0 !== panel }
        log("go-to sheet: \(goTo.map { String(describing: Swift.type(of: $0)) } ?? "none"), responder: \(String(describing: (goTo ?? panel).firstResponder))")
        let target = goTo ?? panel
        if let editor = target.firstResponder as? NSTextView {
            editor.selectAll(nil)
            editor.insertText(file.path, replacementRange: editor.selectedRange())
        } else {
            // The panel's contents live in the system's panel service; key
            // events sent to the panel window are forwarded to it.
            log("typing the path into the panel service")
            for character in file.path {
                send(String(character), 0, to: target)
                for type in [NSEvent.EventType.keyUp] {
                    if let up = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: [], timestamp: 0,
                                                 windowNumber: target.windowNumber, context: nil,
                                                 characters: String(character), charactersIgnoringModifiers: String(character),
                                                 isARepeat: false, keyCode: 0) {
                        target.sendEvent(up)
                    }
                }
            }
        }
        try? await Task.sleep(for: .milliseconds(600))
        send("\r", 36, to: target)
        try? await Task.sleep(for: .seconds(1.2))
        log("after go-to: panel visible \(panel.isVisible), url \(panel.url?.path ?? "-")")
        if panel.isVisible {
            panel.makeKey()
            send("\r", 36, to: panel)
            try? await Task.sleep(for: .seconds(1))
        }
        let picked = !panel.isVisible
        if !picked { panel.cancel(nil) }  // never leave the panel on screen
        return picked
    }
}
