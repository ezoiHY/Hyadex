import AppKit
import HyadexCore
import HyadexUI
import SwiftUI

/// Development aid: `HYADEX_SNAPSHOT_DIR=/tmp/shots Hyadex.app/Contents/MacOS/Hyadex`
/// walks through the main screens, writes one PNG per screen, then quits.
/// It renders the window in-process, so it needs no screen-recording permission;
/// materials that the window server composites (Liquid Glass) come out blank.
@MainActor
enum Snapshotter {
    struct Scene {
        let name: String
        let dark: Bool
        var fullscreen = false
        /// Render only the sidebar, in a plain window (see SidebarSnapshotView).
        var sidebar = false
        /// Switch sidebar tabs and selections rapidly (layout-loop regression check).
        var stress = false
        let setup: (AppModel) -> Void
    }

    static let scenes: [Scene] = [
        Scene(name: "01-chat", dark: false) { model in
            model.select(.chat("c1")); model.showInspector = true; model.inspectorTab = .changes
            model.selectedFilePath = "Sources/HyadexAPI/EventStream.swift"
        },
        Scene(name: "02-approval", dark: false) { model in
            model.select(.chat("c2")); model.inspectorTab = .details
        },
        Scene(name: "03-group", dark: false) { model in
            model.select(.group("g1")); model.inspectorTab = .changes
            model.drafts["g1"] = "@co"
        },
        Scene(name: "04-autopilot-running", dark: false) { model in
            model.select(.autopilot("r1")); model.showInspector = false
        },
        Scene(name: "05-autopilot-approval", dark: true) { model in
            model.select(.autopilot("r2")); model.showInspector = false
        },
        Scene(name: "06-jobs", dark: false) { model in
            model.select(.jobs)
        },
        Scene(name: "07-chat-dark", dark: true) { model in
            model.select(.chat("c3")); model.showInspector = true; model.inspectorTab = .details
        },
        Scene(name: "08-new-autopilot", dark: false) { model in
            model.select(.chat("c1")); model.showInspector = false; model.sheet = .newAutopilot
        },
        Scene(name: "09-command-palette", dark: false) { model in
            model.sheet = .commandPalette
        },
        Scene(name: "12-sidebar-work", dark: true, sidebar: true) { model in model.switchSidebar(to: .work) },
        Scene(name: "13-sidebar-group", dark: true, sidebar: true) { model in model.switchSidebar(to: .group) },
        Scene(name: "14-sidebar-autopilot", dark: false, sidebar: true) { model in model.switchSidebar(to: .autopilot) },
        Scene(name: "15-tab-stress", dark: true, stress: true) { _ in },
        // Last: entering full screen moves the window to its own Space.
        Scene(name: "10-fullscreen-dark", dark: true, fullscreen: true) { model in
            model.select(.chat("c1")); model.showInspector = true; model.inspectorTab = .changes
        },
        Scene(name: "11-fullscreen-light", dark: false, fullscreen: true) { model in
            model.select(.chat("c1")); model.showInspector = true; model.inspectorTab = .changes
        },
    ]

    static func runIfRequested(model: AppModel) async {
        guard let directory = ProcessInfo.processInfo.environment["HYADEX_SNAPSHOT_DIR"] else { return }
        setvbuf(stdout, nil, _IONBF, 0)
        // Keep timers running even if the window is not frontmost (App Nap).
        let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "Rendering snapshots")
        defer { ProcessInfo.processInfo.endActivity(activity) }
        let output = URL(fileURLWithPath: directory, isDirectory: true)
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let only = ProcessInfo.processInfo.environment["HYADEX_SNAPSHOT_ONLY"]
        // Launched from a shell, the app may not be allowed to activate, and the
        // window then stays ordered out; bring it in explicitly.
        for window in NSApp.windows where window.canBecomeMain {
            window.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        try? await Task.sleep(for: .seconds(1.5))
        for scene in scenes where only == nil || scene.name.contains(only!) {
            model.sheet = nil
            NSApp.appearance = NSAppearance(named: scene.dark ? .darkAqua : .aqua)
            try? await Task.sleep(for: .milliseconds(400))
            scene.setup(model)
            try? await Task.sleep(for: .seconds(1.2))
            guard let window = NSApp.windows.first(where: { $0.isVisible && $0.canBecomeMain }) else {
                print("[snapshot] \(scene.name): no main window")
                continue
            }
            if scene.stress {
                if let width = ProcessInfo.processInfo.environment["HYADEX_STRESS_WIDTH"].flatMap(Double.init) {
                    window.setFrame(NSRect(x: 80, y: 80, width: width, height: 720), display: true)
                }
                let modes = SidebarMode.allCases
                for round in 0..<36 {
                    model.switchSidebar(to: modes[round % modes.count])
                    model.showInspector = round % 4 != 3
                    try? await Task.sleep(for: .milliseconds(round % 2 == 0 ? 60 : 350))
                }
                print("[snapshot] stress survived")
                write(capture(window), to: output.appending(path: scene.name + ".png"))
                continue
            }
            if scene.sidebar {
                let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 280, height: 640),
                                     styleMask: [.titled], backing: .buffered, defer: false)
                panel.contentView = NSHostingView(rootView: SidebarSnapshotView(model: model))
                panel.orderFront(nil)
                try? await Task.sleep(for: .seconds(1))
                write(capture(panel), to: output.appending(path: scene.name + ".png"))
                panel.orderOut(nil)
                continue
            }
            if scene.fullscreen {
                if !window.styleMask.contains(.fullScreen) {
                    window.toggleFullScreen(nil)
                    try? await Task.sleep(for: .seconds(3))
                }
                write(captureScreen(of: window), to: output.appending(path: scene.name + ".png"))
                continue
            }
            let target = window.attachedSheet ?? window
            write(capture(target), to: output.appending(path: scene.name + ".png"))
        }
        exit(0)
    }

    static func captureMain(to url: URL) {
        guard let window = NSApp.windows.first(where: { $0.isVisible && $0.canBecomeMain }) else { return }
        write(capture(window.attachedSheet ?? window), to: url)
    }

    private static func capture(_ window: NSWindow) -> NSBitmapImageRep? {
        guard let view = window.contentView?.superview ?? window.contentView else { return nil }
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return nil }
        view.cacheDisplay(in: view.bounds, to: rep)
        return rep
    }

    /// Composites every visible window on the window's screen (back to front),
    /// so full screen also shows the separate toolbar window AppKit uses there.
    private static func captureScreen(of window: NSWindow) -> NSBitmapImageRep? {
        guard let screen = window.screen else { return nil }
        let frame = screen.frame
        let scale = screen.backingScaleFactor
        guard let canvas = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(frame.width * scale),
                                            pixelsHigh: Int(frame.height * scale), bitsPerSample: 8,
                                            samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        canvas.size = frame.size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: canvas)
        let windows = NSApp.windows
            .filter { $0.isVisible && $0.screen == screen }
            .sorted { $0.orderedIndex > $1.orderedIndex }
        for item in windows {
            guard let rep = capture(item) else { continue }
            let origin = NSPoint(x: item.frame.minX - frame.minX, y: item.frame.minY - frame.minY)
            rep.draw(in: NSRect(origin: origin, size: item.frame.size))
        }
        NSGraphicsContext.restoreGraphicsState()
        return canvas
    }

    private static func write(_ rep: NSBitmapImageRep?, to url: URL) {
        guard let data = rep?.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: url)
    }
}
