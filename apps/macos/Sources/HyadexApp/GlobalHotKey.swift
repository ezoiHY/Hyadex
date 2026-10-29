import AppKit
import Carbon.HIToolbox
import HyadexUI

/// A system-wide key combination, registered with the Carbon hot-key API:
/// it needs no Accessibility permission and only this combination reaches
/// the app. Registration fails (returns nil) when another app owns it.
@MainActor
final class GlobalHotKey {
    private var reference: EventHotKeyRef?
    private let identifier: UInt32
    private static var actions: [UInt32: () -> Void] = [:]
    private static var handler: EventHandlerRef?
    private static var nextIdentifier: UInt32 = 1

    init?(keyCode: Int, modifiers: Int, action: @escaping () -> Void) {
        Self.installHandler()
        identifier = Self.nextIdentifier
        Self.nextIdentifier += 1
        let id = EventHotKeyID(signature: OSType(0x4859_4458), id: identifier)  // "HYDX"
        let status = RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), id, GetApplicationEventTarget(), 0, &reference)
        guard status == noErr else { return nil }
        Self.actions[identifier] = action
    }

    func unregister() {
        if let reference { UnregisterEventHotKey(reference) }
        reference = nil
        Self.actions[identifier] = nil
    }

    private static func installHandler() {
        guard handler == nil else { return }
        var type = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            let identifier = id.id
            DispatchQueue.main.async { MainActor.assumeIsolated { GlobalHotKey.actions[identifier]?() } }
            return noErr
        }, 1, &type, nil, &handler)
    }
}

extension ScreenshotShortcut {
    /// The Carbon key code and modifiers, or nil when off.
    var carbon: (keyCode: Int, modifiers: Int)? {
        switch self {
        case .off: nil
        case .optionShiftS: (kVK_ANSI_S, optionKey | shiftKey)
        case .controlShiftS: (kVK_ANSI_S, controlKey | shiftKey)
        case .controlOptionCommandS: (kVK_ANSI_S, controlKey | optionKey | cmdKey)
        }
    }
}
