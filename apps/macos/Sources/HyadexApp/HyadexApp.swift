import AppKit
import HyadexCore
import HyadexUI
import SwiftUI

@main
struct HyadexApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    var body: some Scene {
        Window("Hyadex", id: "main") {
            RootView(model: model)
                .task {
                    await model.autoConnect()

                    await Snapshotter.runIfRequested(model: model)
                    await LiveCheck.runIfRequested(model: model)
                }
        }
        .defaultSize(width: 1280, height: 800)
        // The main window is the app. Always present it at launch, even if the
        // previous session ended (or crashed) with it closed.
        .defaultLaunchBehavior(.presented)
        .windowToolbarStyle(.unified)
        .commands { AppCommands(model: model) }

        Settings {
            SettingsView(model: model)
        }

        MenuBarExtra {
            MenuBarStatus(model: model)
        } label: {
            Image(systemName: model.needsActionCount > 0 ? "sparkles.rectangle.stack.fill" : "sparkles.rectangle.stack")
        }
        .menuBarExtraStyle(.menu)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare binary (swift run) rather than a bundle.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }
}

struct AppCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("新建聊天") { model.sheet = .newChat }
                .keyboardShortcut("n")
            Button("新建讨论组") { model.sheet = .newGroup }
                .keyboardShortcut("n", modifiers: [.command, .shift])
            Button("新建自动接力") { model.sheet = .newAutopilot }
                .keyboardShortcut("n", modifiers: [.command, .option])
        }
        CommandGroup(after: .sidebar) {
            ForEach(Array(SidebarMode.allCases.enumerated()), id: \.offset) { index, mode in
                Button(mode.title) { model.switchSidebar(to: mode) }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: [.command, .control])
            }
            Divider()
            Button("显示/隐藏检查器") { model.showInspector.toggle() }
                .keyboardShortcut("i", modifiers: [.command, .option])
            Divider()
        }
        CommandMenu("会话") {
            Button("快速跳转…") { model.sheet = .commandPalette }
                .keyboardShortcut("k")
            Divider()
            Button("停止") {
                if let key = model.selectedTimelineKey { model.stop(key) }
            }
            .keyboardShortcut(".")
            .disabled(model.selectedTimelineKey == nil)
            Divider()
            ForEach(Array(navigationTargets.prefix(9).enumerated()), id: \.offset) { index, target in
                Button(target.title) { model.select(target.item) }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))))
            }
        }
    }

    private var navigationTargets: [(title: String, item: SidebarItem)] {
        model.visiblePinned.map { ($0.title, SidebarItem.chat($0.id)) }
            + model.visibleChats.map { ($0.title, SidebarItem.chat($0.id)) }
            + model.visibleGroups.map { ($0.title, SidebarItem.group($0.id)) }
    }
}

/// Menu bar: what is running and what needs you, without opening the window.
struct MenuBarStatus: View {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let waiting = model.chats.filter { $0.state == .needsAction }
        let running = model.chats.filter { $0.state == .running }
        if !waiting.isEmpty {
            Section("需要你处理") {
                ForEach(waiting) { chat in
                    Button(chat.title) { open(.chat(chat.id)) }
                }
                ForEach(model.runs.filter { $0.status.activity == .needsAction }) { run in
                    Button("自动接力 · \(run.title)") { open(.autopilot(run.id)) }
                }
            }
        }
        if !running.isEmpty {
            Section("运行中") {
                ForEach(running) { chat in
                    Button(chat.title) { open(.chat(chat.id)) }
                }
                ForEach(model.runs.filter(\.status.isActive)) { run in
                    Button("自动接力 · \(run.title)") { open(.autopilot(run.id)) }
                }
            }
        }
        Divider()
        Button("打开 Hyadex") { openWindow(id: "main"); NSApp.activate() }
        Button("退出") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func open(_ item: SidebarItem) {
        model.select(item)
        openWindow(id: "main")
        NSApp.activate()
    }
}
