import AppKit
import HyadexCore

/// The system-wide shortcut that captures part of the screen into the open
/// chat (设置 › 通用). Off leaves every key to other apps.
public enum ScreenshotShortcut: String, CaseIterable, Identifiable, Sendable {
    case off
    case optionShiftS
    case controlShiftS
    case controlOptionCommandS

    public static let key = "screenshotShortcut"
    public static let standard = ScreenshotShortcut.optionShiftS

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .off: "关闭"
        case .optionShiftS: "⌥⇧S"
        case .controlShiftS: "⌃⇧S"
        case .controlOptionCommandS: "⌃⌥⌘S"
        }
    }

    public static var current: ScreenshotShortcut {
        UserDefaults.standard.string(forKey: key).flatMap(ScreenshotShortcut.init) ?? standard
    }
}

/// Area screenshots for the composer, taken with the system's own
/// `screencapture` selection (drag an area, Space for a window, Esc cancels).
@MainActor
public enum ScreenCapture {
    /// Captures an area and attaches it to `chatID` (default: the open chat).
    /// From inside the app, Hyadex steps aside so what is behind it can be captured.
    public static func capture(into model: AppModel, chat chatID: String? = nil, hideApp: Bool = false) async {
        guard let target = chatID ?? model.screenshotTarget else {
            model.errorMessage = "先打开一个聊天，截图会添加到它的输入框。"
            NSApp.activate()
            return
        }
        guard CGPreflightScreenCaptureAccess() else {
            // The first request shows the system prompt; later ones need System Settings.
            _ = CGRequestScreenCaptureAccess()
            model.errorMessage = "截图需要“屏幕录制”权限：在 系统设置 › 隐私与安全性 › 屏幕与系统录音 中允许 Hyadex，然后重试。"
            NSApp.activate()
            return
        }
        if hideApp { NSApp.hide(nil) }
        let url = await areaScreenshot()
        NSApp.activate()
        guard let url else { return }  // cancelled
        model.attachScreenshot(url, toChat: target)
    }

    /// A PNG of the area the user picks, or nil when they cancel.
    static func areaScreenshot() async -> URL? {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "Hyadex-screenshots/\(UUID().uuidString)", directoryHint: .isDirectory)
        guard (try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)) != nil else { return nil }
        let url = folder.appending(path: "截图 \(formatter.string(from: .now)).png")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        process.arguments = ["-i", "-x", "-o", url.path]
        let finished: Bool = await withCheckedContinuation { continuation in
            process.terminationHandler = { _ in continuation.resume(returning: true) }
            do { try process.run() } catch { continuation.resume(returning: false) }
        }
        guard finished, let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0 else {
            try? FileManager.default.removeItem(at: folder)
            return nil
        }
        return url
    }
}
