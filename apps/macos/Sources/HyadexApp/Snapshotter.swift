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
            model.select(.chat("c1")); model.openFile("Sources/HyadexAPI/EventStream.swift")
        },
        Scene(name: "02-approval", dark: false) { model in
            model.select(.chat("c2"))
        },
        Scene(name: "03-group", dark: false) { model in
            model.select(.group("g1"))
            model.drafts["g1"] = "@co"
        },
        Scene(name: "04-autopilot-running", dark: false) { model in
            model.select(.autopilot("r1"))
        },
        Scene(name: "05-autopilot-approval", dark: true) { model in
            model.select(.autopilot("r2"))
        },
        Scene(name: "06-jobs", dark: false) { model in
            model.select(.jobs)
        },
        Scene(name: "07-chat-dark", dark: true) { model in
            model.select(.chat("c3"))
        },
        Scene(name: "08-new-autopilot", dark: false) { model in
            model.select(.chat("c1")); model.sheet = .newAutopilot
        },
        Scene(name: "09-command-palette", dark: false) { model in
            model.sheet = .commandPalette
        },
        Scene(name: "12-sidebar-work", dark: true, sidebar: true) { model in model.switchSidebar(to: .work) },
        Scene(name: "13-sidebar-group", dark: true, sidebar: true) { model in model.switchSidebar(to: .group) },
        Scene(name: "14-sidebar-autopilot", dark: false, sidebar: true) { model in model.switchSidebar(to: .autopilot) },
        Scene(name: "15-tab-stress", dark: true, stress: true) { _ in },
        Scene(name: "16-new-job", dark: false) { model in
            model.select(.jobs); model.jobEditor = .new
        },
        Scene(name: "17-new-chat", dark: true) { model in
            model.sheet = .newChat
        },
        Scene(name: "18-queue-held", dark: false) { model in
            model.select(.chat("c3"))
            model.queues["c3"] = [
                QueuedMessage(id: "q1", text: "测试跑完后把 CHANGELOG 也检查一下。", hold: .stopped),
                QueuedMessage(id: "q2", text: "然后更新版本号到 1.4.0。", hold: .stopped),
                QueuedMessage(id: "q3", text: "最后发一个总结。"),
            ]
            model.attachments["c3"] = [
                DraftAttachment(name: "crash-report.ips", size: 48_213, state: .ready(fileID: "f1")),
                DraftAttachment(name: "screen-recording.mov", size: 18_400_000),
            ]
        },
        Scene(name: "19-emergency", dark: true) { model in
            model.select(.chat("c1"))
            model.emergencies = [EmergencyAlert(sessionID: "c2", alertID: "emergency_demo", chatTitle: "Android 推送偶发丢失",
                                                message: "生产数据库磁盘只剩 2%，迁移脚本已暂停。需要你确认是否继续。",
                                                raisedAt: .now.addingTimeInterval(-90), count: 2)]
        },
        Scene(name: "20-side-chat", dark: false) { model in
            model.select(.chat("c1"))
            model.sideChatOpen = "c1"
            model.sideChats["c1"] = SideChat(sideChatID: "demo", exchanges: [
                .init(requestID: "r1", question: "为什么退避要加随机抖动？",
                      answer: "多个客户端同时断线时，**固定间隔**会让它们同时重连，形成重连风暴。加上抖动后重连时间被打散。", status: .completed),
                .init(requestID: "r2", question: "最大间隔 30 秒合适吗？", answer: nil, status: .running),
            ], lastRequestID: "r2", contextNote: "Claude 能看到对话内容，但不会调用工具。")
        },
        Scene(name: "21-sidebar-archived", dark: true, sidebar: true) { model in
            UserDefaults.standard.set(true, forKey: "showArchived")
            model.switchSidebar(to: .work)
            model.setArchived(.chat("c6"), true)
            model.setArchived(.chat("c5"), true)
        },
        Scene(name: "22-sidebar-search", dark: false, sidebar: true) { model in
            model.switchSidebar(to: .work)
            model.searchText = "退避"
        },
        Scene(name: "23-jobs-empty", dark: true) { model in
            model.jobs = []; model.select(.jobs)
        },
        // Last: entering full screen moves the window to its own Space.
        Scene(name: "10-fullscreen-dark", dark: true, fullscreen: true) { model in
            model.select(.chat("c1"))
        },
        Scene(name: "11-fullscreen-light", dark: false, fullscreen: true) { model in
            model.select(.chat("c1"))
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
        // HYADEX_SNAPSHOT_ONLY: comma-separated parts of scene names.
        let wanted = only?.split(separator: ",").map(String.init)
        for scene in scenes where wanted == nil || wanted!.contains(where: scene.name.contains) {
            model.sheet = nil
            model.jobEditor = nil
            model.goalEditor = nil
            model.renaming = nil
            model.sideChatOpen = nil
            model.emergencies = []
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
                    model.showChanges = round % 4 == 1
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

    /// Writes one window (for live checks); the file goes next to the live-check log.
    static func captureWindow(_ window: NSWindow, to name: String) {
        guard let directory = ProcessInfo.processInfo.environment["HYADEX_E2E_DIR"] else { return }
        write(capture(window), to: URL(fileURLWithPath: directory).appending(path: name + ".png"))
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
