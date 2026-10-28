import AppKit
import Foundation
@preconcurrency import UserNotifications

/// Posts an agent's emergency alert as a macOS notification, and asks for the
/// user's attention while the app is in the background.
@MainActor
public enum SystemNotifier {
    static func post(_ alert: EmergencyAlert) {
        // Unit tests and `swift run` have no app bundle to notify from.
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        NSApp?.requestUserAttention(.criticalRequest)
        let title = "紧急提醒 · \(alert.chatTitle)"
        let body = alert.message
        let identifier = alert.alertID
        deliver(title: title, body: body, identifier: identifier, critical: true)
    }

    /// A chat changed state: it needs an approval, or its reply finished.
    /// Follows the 通知 settings (each kind, and "only when Hyadex is in the background").
    static func chatChanged(_ chat: ChatSummary, from old: ActivityState, to new: ActivityState) {
        guard Bundle.main.bundleURL.pathExtension == "app", old != new else { return }
        let defaults = UserDefaults.standard
        func enabled(_ key: String) -> Bool { defaults.object(forKey: key) as? Bool ?? true }
        if enabled("notifyOnlyInBackground"), NSApp?.isActive == true { return }
        if new == .needsAction, enabled("notifyApproval") {
            deliver(title: "需要你批准 · \(chat.title)", body: chat.preview, identifier: "approval-\(chat.id)", critical: false)
        } else if old == .running, new == .idle, enabled("notifyFinished") {
            deliver(title: "回复完成 · \(chat.title)", body: chat.preview, identifier: "finished-\(chat.id)", critical: false)
        }
    }

    /// The Dock badge: chats and groups waiting for you, plus unacknowledged emergencies.
    public static func updateBadge(_ count: Int) {
        let enabled = UserDefaults.standard.object(forKey: "dockBadge") as? Bool ?? true
        NSApp?.dockTile.badgeLabel = enabled && count > 0 ? String(count) : nil
    }

    private static func deliver(title: String, body: String, identifier: String, critical: Bool) {
        Task.detached {
            let center = UNUserNotificationCenter.current()
            guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = critical ? .defaultCritical : .default
            try? await center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: nil))
        }
    }
}
