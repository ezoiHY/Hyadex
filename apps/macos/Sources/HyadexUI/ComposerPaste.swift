import AppKit
import SwiftUI

/// What ⌘V in the composer attaches instead of pasting as text.
///
/// Copied files (Finder) become attachments. An image with no text alongside
/// it (a screenshot, "Copy Image" in a browser) is saved as a PNG and
/// attached. Anything else, including text that also carries a picture of
/// itself (Numbers, Word), pastes normally.
enum PastedAttachments {
    static func files(from pasteboard: NSPasteboard, now: Date = .now) throws -> [URL]? {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL], !urls.isEmpty {
            return urls
        }
        let types = pasteboard.types ?? []
        guard !types.contains(.string), let png = imagePNG(from: pasteboard, types: types) else { return nil }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "Hyadex-pasted/\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appending(path: "粘贴的图片 \(formatter.string(from: now)).png")
        try png.write(to: url, options: .atomic)
        return [url]
    }

    private static func imagePNG(from pasteboard: NSPasteboard, types: [NSPasteboard.PasteboardType]) -> Data? {
        if types.contains(.png), let data = pasteboard.data(forType: .png) { return data }
        let imageTypes = NSImage.imageTypes.map { NSPasteboard.PasteboardType($0) }
        guard types.contains(where: imageTypes.contains),
              let image = NSImage(pasteboard: pasteboard),
              let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff) else { return nil }
        return bitmap.representation(using: .png, properties: [:])
    }
}

/// Catches ⌘V while the composer has focus and hands copied files or images
/// to `onFiles`; text pastes reach the text field as usual.
struct PasteInterceptor: NSViewRepresentable {
    var enabled: Bool
    var onFiles: ([URL]) -> Void
    var onError: (String) -> Void = { _ in NSSound.beep() }

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        context.coordinator.view = view
        context.coordinator.install()
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        context.coordinator.parent = self
    }

    static func dismantleNSView(_ nsView: NSView, coordinator: Coordinator) {
        coordinator.uninstall()
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor
    final class Coordinator {
        var parent: PasteInterceptor
        weak var view: NSView?
        private var monitor: Any?

        init(parent: PasteInterceptor) { self.parent = parent }

        func install() {
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                // Local monitors run on the main thread.
                nonisolated(unsafe) let event = event
                let consumed = MainActor.assumeIsolated { self?.consume(event) ?? false }
                return consumed ? nil : event
            }
        }

        func uninstall() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }

        /// Whether ⌘V attached something, so the text field must not also paste.
        private func consume(_ event: NSEvent) -> Bool {
            guard parent.enabled, let window = view?.window, event.window === window,
                  event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers?.lowercased() == "v" else { return false }
            do {
                guard let files = try PastedAttachments.files(from: .general) else { return false }
                parent.onFiles(files)
            } catch {
                parent.onError("无法粘贴图片：\(error.localizedDescription)")
            }
            return true
        }
    }
}
