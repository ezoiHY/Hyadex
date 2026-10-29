import AppKit
import HyadexCore
import SwiftUI

/// The menu bar icon shows the app's state at a glance: grey while no
/// server is connected, red for an emergency alert, a count of items waiting
/// for you, and a slow pulse while agents work.
struct MenuBarIcon: Equatable {
    var connected: Bool
    var urgent: Bool
    var waiting: Int
    var running: Bool

    @MainActor
    init(model: AppModel) {
        connected = model.isLive
        urgent = !model.emergencies.isEmpty
        waiting = model.needsActionCount + model.emergencies.count
        running = model.chats.contains { $0.state == .running }
            || model.groups.contains { $0.state == .running }
            || model.runs.contains { $0.status.isActive }
    }

    init(connected: Bool, urgent: Bool, waiting: Int, running: Bool) {
        self.connected = connected
        self.urgent = urgent
        self.waiting = waiting
        self.running = running
    }

    var animates: Bool { connected && running }

    var description: String {
        guard connected else { return "Hyadex：未连接" }
        var parts = ["Hyadex"]
        if urgent { parts.append("有紧急提醒") }
        if waiting > 0 { parts.append("\(waiting) 项需要你处理") }
        if running { parts.append("正在运行") }
        return parts.joined(separator: "，")
    }

    static let frames = 20  // one pulse: 2 s at 10 frames a second

    /// The icon for `frame` of the pulse. Template (follows the menu bar's
    /// colour) unless urgent, which is drawn red.
    @MainActor
    func image(frame: Int) -> NSImage {
        let phase = animates ? frame % Self.frames : 0
        let key = "\(connected)-\(urgent)-\(min(waiting, 10))-\(animates)-\(phase)"
        if let cached = Self.cache[key] { return cached }
        let badge = waiting > 0 && connected ? (waiting > 9 ? "9+" : "\(waiting)") : nil
        let size = NSSize(width: badge == nil ? 18 : badge!.count > 1 ? 28 : 22, height: 16)
        let alpha: CGFloat = !connected ? 0.38
            : animates ? 0.5 + 0.5 * (0.5 + 0.5 * cos(2 * .pi * CGFloat(phase) / CGFloat(Self.frames))) : 1
        let color: NSColor = urgent && connected ? .systemRed : .black
        let image = NSImage(size: size, flipped: false) { _ in
            let configuration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
            if let symbol = NSImage(systemSymbolName: "sparkles.rectangle.stack", accessibilityDescription: nil)?
                .withSymbolConfiguration(configuration) {
                let glyph = NSRect(x: 0, y: (size.height - symbol.size.height) / 2,
                                   width: symbol.size.width, height: symbol.size.height)
                symbol.tinted(color).draw(in: glyph, from: .zero, operation: .sourceOver, fraction: alpha)
            }
            guard let badge else { return true }
            let font = NSFont.monospacedDigitSystemFont(ofSize: 8.5, weight: .bold)
            let text = NSAttributedString(string: badge, attributes: [.font: font, .foregroundColor: NSColor.black])
            let textSize = text.size()
            let width = max(11, textSize.width + 5)
            let pill = NSRect(x: size.width - width, y: size.height - 11, width: width, height: 11)
            let context = NSGraphicsContext.current
            // A clear ring keeps the badge apart from the glyph under it.
            context?.compositingOperation = .clear
            NSBezierPath(roundedRect: pill.insetBy(dx: -1.5, dy: -1.5), xRadius: 7, yRadius: 7).fill()
            context?.compositingOperation = .sourceOver
            (urgent ? NSColor.systemRed : NSColor.black).setFill()
            NSBezierPath(roundedRect: pill, xRadius: 5.5, yRadius: 5.5).fill()
            // The number is cut out of the badge (template) or white on red.
            let origin = NSPoint(x: pill.midX - textSize.width / 2, y: pill.midY - textSize.height / 2 + 0.5)
            if urgent {
                NSAttributedString(string: badge, attributes: [.font: font, .foregroundColor: NSColor.white]).draw(at: origin)
            } else {
                context?.compositingOperation = .destinationOut
                text.draw(at: origin)
                context?.compositingOperation = .sourceOver
            }
            return true
        }
        image.isTemplate = !(urgent && connected)
        image.accessibilityDescription = description
        Self.cache[key] = image
        return image
    }

    @MainActor private static var cache: [String: NSImage] = [:]
}

private extension NSImage {
    func tinted(_ color: NSColor) -> NSImage {
        let image = NSImage(size: size, flipped: false) { rect in
            self.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        return image
    }
}

/// The status item's label: the icon, pulsing frame by frame while agents run.
struct MenuBarLabel: View {
    let model: AppModel
    let delegate: AppDelegate
    @Environment(\.openWindow) private var openWindow
    @State private var frame = 0

    var body: some View {
        let icon = MenuBarIcon(model: model)
        let image = icon.image(frame: frame)
        Image(nsImage: image)
            .renderingMode(image.isTemplate ? .template : .original)
            .accessibilityLabel(icon.description)
            .task(id: icon.animates) {
                frame = 0
                guard icon.animates, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(100))
                    frame += 1
                }
            }
            .onAppear {
                delegate.openMainWindow = { openWindow(id: "main") }
            }
    }
}
