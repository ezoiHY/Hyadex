import AppKit
import HyadexCore
import HyadexUI
import SwiftUI

@main
struct HyadexApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model: AppModel = {
        if ProcessInfo.processInfo.environment["HYADEX_SNAPSHOT_DIR"] != nil {
            return AppModel(snapshotFixturesAt: .now)
        }
        return AppModel()
    }()

    var body: some Scene {
        Window("Hyadex", id: "main") {
            RootView(model: model)
                .onChange(of: model.needsActionCount + model.emergencies.count, initial: true) { _, count in
                    SystemNotifier.updateBadge(count)
                }
                .task {
                    appDelegate.model = model
                    if ProcessInfo.processInfo.environment["HYADEX_SNAPSHOT_DIR"] == nil {
                        await model.autoConnect()
                    }

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
    weak var model: AppModel?
    private var terminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare binary (swift run) rather than a bundle.
        NSApp.setActivationPolicy(.regular)
        // A live check runs in the background so the user's typing stays in their own app.
        let environment = ProcessInfo.processInfo.environment
        if environment["HYADEX_E2E_DIR"] == nil && environment["HYADEX_BACKGROUND_LAUNCH"] != "1" { NSApp.activate() }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else { return .terminateNow }
        if terminating { return .terminateLater }
        // Only this app's process is stopped. Remote/installed servers are
        // unaffected, and closing the window does not stop the local server.
        let ownsDisplayedServer = model.isLive && model.currentServerID == model.localServer.endpoint?.id
        let activeWork = model.chats.contains(where: { [.running, .needsAction, .queued].contains($0.state) })
            || model.groups.contains(where: { [.running, .needsAction, .queued].contains($0.state) })
            || model.runs.contains(where: { $0.status.isActive })
        if model.localServer.isRunning && (!ownsDisplayedServer || activeWork) {
            let alert = NSAlert()
            alert.messageText = "退出并停止本地服务？"
            alert.informativeText = ownsDisplayedServer
                ? "本地服务仍有任务正在进行。退出会中断这些任务，聊天记录会保留。"
                : "本地服务仍在后台运行，当前窗口未连接它。退出会停止该服务及其任务，聊天记录会保留。"
            alert.addButton(withTitle: "取消")
            alert.addButton(withTitle: "退出")
            if alert.runModal() != .alertSecondButtonReturn { return .terminateCancel }
        }
        terminating = true
        Task {
            model.disconnect()
            await model.localServer.shutdown()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

struct AppCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("新建聊天") { model.sheet = .newChat }
                .keyboardShortcut("n")
                .disabled(!model.isLive)
            Button("新建讨论组") { model.sheet = .newGroup }
                .keyboardShortcut("n", modifiers: [.command, .shift])
                .disabled(!model.isLive)
            Button("新建自动接力") { model.sheet = .newAutopilot }
                .keyboardShortcut("n", modifiers: [.command, .option])
                .disabled(!model.isLive)
        }
        CommandGroup(after: .sidebar) {
            ForEach(Array(SidebarMode.allCases.enumerated()), id: \.offset) { index, mode in
                Button(mode.title) { model.switchSidebar(to: mode) }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: [.command, .control])
            }
            Divider()
            Button(model.showChanges ? "隐藏改动" : "显示改动") { model.showChanges.toggle() }
                .disabled(!model.showChanges && model.selectedChanges.files.isEmpty)
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
            Button("重命名…") {
                if let item = model.selectedManageable {
                    model.renaming = RenameTarget(item: item, title: model.title(of: item))
                }
            }
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .disabled(model.selectedManageable == nil)
            Button(model.selectedManageable.map(model.isArchived) == true ? "取消归档" : "归档") {
                if let item = model.selectedManageable { model.setArchived(item, !model.isArchived(item)) }
            }
            .keyboardShortcut("e", modifiers: [.command, .shift])
            .disabled(model.selectedManageable == nil)
            Button("删除…") { model.pendingDeletion = model.selectedManageable }
                .keyboardShortcut(.delete, modifiers: [.command])
                .disabled(model.selectedManageable == nil)
            Divider()
            Button("分叉聊天") { if case .chat(let id)? = model.selection { model.fork(id) } }
                .keyboardShortcut("d", modifiers: [.command, .shift])
                .disabled(selectedChat == nil)
            Button("侧问…") { if case .chat(let id)? = model.selection { model.toggleSideChat(id) } }
                .keyboardShortcut("b", modifiers: [.command, .shift])
                .disabled(selectedChat == nil)
            Button("标为未读") { if case .chat(let id)? = model.selection { model.markUnread(id) } }
                .keyboardShortcut("u", modifiers: [.command, .shift])
                .disabled(selectedChat == nil)
            Button("为此聊天新建定时任务…") { model.jobEditor = .new }
                .keyboardShortcut("j", modifiers: [.command, .shift])
                .disabled(selectedChat == nil)
            Divider()
            ForEach(Array(navigationTargets.prefix(9).enumerated()), id: \.offset) { index, target in
                Button(target.title) { model.select(target.item) }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))))
            }
        }
    }

    private var selectedChat: ChatSummary? {
        if case .chat(let id)? = model.selection { model.chat(id) } else { nil }
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
