import HyadexCore
import SwiftUI

/// Design tokens. Everything else uses system semantic colours and fonts so
/// the app follows Light/Dark mode, accent colour and accessibility settings.
enum Theme {
    static let readableWidth: CGFloat = 780
    static let cornerRadius: CGFloat = 10
    static let smallRadius: CGFloat = 6
    static let timelineSpacing: CGFloat = 14

    /// Claude's terracotta.
    static let claudeOrange = Color(red: 0.851, green: 0.467, blue: 0.341)

    static func color(for backend: AgentBackend?) -> Color {
        switch backend {
        case .claude: claudeOrange
        case .codex: Color(red: 0.36, green: 0.43, blue: 0.99)
        case nil: .secondary
        }
    }
}

extension ActivityState {
    /// One colour per state, used everywhere (sidebar, pills, steps).
    var tint: Color {
        switch self {
        case .idle: .secondary
        case .queued: .secondary
        case .running: .accentColor
        case .needsAction: .orange
        case .failed: .red
        case .done: .green
        case .paused: .yellow
        }
    }

    var symbol: String {
        switch self {
        case .idle: "circle"
        case .queued: "clock"
        case .running: "circle.dotted"
        case .needsAction: "exclamationmark.circle.fill"
        case .failed: "xmark.circle.fill"
        case .done: "checkmark.circle.fill"
        case .paused: "pause.circle.fill"
        }
    }
}

/// Small rounded "card" background used by tool output, approvals and changes.
struct CardBackground: ViewModifier {
    var tint: Color? = nil

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: Theme.cornerRadius, style: .continuous)
                    .fill(tint.map { $0.opacity(0.07) } ?? Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.cornerRadius, style: .continuous)
                    .strokeBorder(tint.map { $0.opacity(0.45) } ?? Color(nsColor: .separatorColor), lineWidth: tint == nil ? 0.5 : 1)
            )
    }
}

extension View {
    func card(tint: Color? = nil) -> some View { modifier(CardBackground(tint: tint)) }
}

extension Date {
    /// "刚刚", "5 分钟前", "昨天" … for compact lists.
    var relativeShort: String {
        let seconds = Date.now.timeIntervalSince(self)
        if seconds < 60 { return "刚刚" }
        if seconds < 3600 { return "\(Int(seconds / 60)) 分钟" }
        if Calendar.current.isDateInToday(self) { return formatted(date: .omitted, time: .shortened) }
        if Calendar.current.isDateInYesterday(self) { return "昨天" }
        return formatted(.dateTime.month().day())
    }
}

extension Int {
    var compact: String {
        self >= 1000 ? String(format: "%.1fk", Double(self) / 1000) : String(self)
    }
}
