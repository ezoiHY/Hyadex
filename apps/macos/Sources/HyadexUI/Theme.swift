import HyadexCore
import HyadexRichText
import SwiftUI

/// 设置 › 通用 › 字号.
enum TextSize: String, CaseIterable, Identifiable {
    case standard, large

    var id: String { rawValue }
    var label: String { self == .standard ? "标准" : "大" }
    var scale: CGFloat { self == .standard ? 1 : 1.15 }
    static let key = "textSize"

    static var current: TextSize {
        UserDefaults.standard.string(forKey: key).flatMap(TextSize.init(rawValue:)) ?? .standard
    }
}

/// The app's type scale. Use these instead of the system text styles: macOS's
/// caption styles are 10 pt, too small for anything that has to be read.
/// Views rebuild when 字号 changes (the root view is keyed by it).
extension Font {
    /// Messages and ordinary UI text (14 pt).
    static var uiBody: Font { .system(size: 14 * Theme.scale) }
    /// Secondary lines: previews, labels next to values (13 pt).
    static var uiSecondary: Font { .system(size: 13 * Theme.scale) }
    /// Metadata: status lines, times, counts (12 pt).
    static var uiMeta: Font { .system(size: 12 * Theme.scale) }
    /// Badges and tags only (11 pt).
    static var uiSmall: Font { .system(size: 11 * Theme.scale) }
    static var uiHeadline: Font { .system(size: 14 * Theme.scale, weight: .semibold) }
    static var uiTitle3: Font { .system(size: 16 * Theme.scale) }
    static var uiTitle2: Font { .system(size: 19 * Theme.scale) }
}

/// Design tokens. Everything else uses system semantic colours and fonts so
/// the app follows Light/Dark mode, accent colour and accessibility settings.
enum Theme {
    /// The 字号 setting's factor; RootView sets it before building its views.
    nonisolated(unsafe) static var scale: CGFloat = TextSize.current.scale {
        didSet { RichMetrics.scale = scale }
    }
    static let readableWidth: CGFloat = 800
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
    /// 8,420 · 2.2 万 · 1,234 万: counts the way Chinese UI reads them.
    var wan: String {
        guard self >= 10_000 else { return formatted() }
        let value = Double(self) / 10_000
        return value >= 100 ? "\(Int(value.rounded()).formatted()) 万" : String(format: "%.1f 万", value)
    }

    var compact: String {
        self >= 1000 ? String(format: "%.1fk", Double(self) / 1000) : String(self)
    }
}
