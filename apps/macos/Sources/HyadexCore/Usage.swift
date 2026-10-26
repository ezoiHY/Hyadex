import Foundation
import HyadexAPI

/// One plan allowance window as the provider reports it ("Current session",
/// "Weekly limit" …).
public struct UsageWindow: Identifiable, Hashable, Sendable {
    public let id: String
    public let label: String
    /// 0–100.
    public let usedPercent: Double
    public let resetsAt: Date?
    public let windowMinutes: Int?

    public init(id: String, label: String, usedPercent: Double, resetsAt: Date? = nil, windowMinutes: Int? = nil) {
        self.id = id
        self.label = label
        self.usedPercent = usedPercent
        self.resetsAt = resetsAt
        self.windowMinutes = windowMinutes
    }

    /// Short name for the compact meter: "5h", "周".
    public var shortLabel: String {
        switch windowMinutes {
        case 300?: "5h"
        case 10080?: "周"
        case let minutes? where minutes < 1440: "\(minutes / 60)h"
        case let minutes?: "\(minutes / 1440)d"
        case nil: label
        }
    }
}

/// A provider account's plan usage (`GET /api/runtime/usage`).
public struct ProviderUsage: Hashable, Sendable {
    public var available: Bool
    public var windows: [UsageWindow]
    public var plan: String?
    /// Why usage is unavailable (`account_usage_unavailable`, `custom_endpoint` …).
    public var reason: String?
    public var observedAt: Date?

    public init(available: Bool, windows: [UsageWindow] = [], plan: String? = nil, reason: String? = nil,
                observedAt: Date? = nil) {
        self.available = available
        self.windows = windows
        self.plan = plan
        self.reason = reason
        self.observedAt = observedAt
    }

    /// The window closest to its limit, shown in the compact meter.
    public var tightest: UsageWindow? { windows.max { $0.usedPercent < $1.usedPercent } }

    public var unavailableText: String {
        switch reason {
        case "account_usage_unavailable": "当前登录方式不提供套餐用量（API key）"
        case "custom_endpoint": "自定义服务端点不提供套餐用量"
        case "unsupported_transport": "当前 Codex 运行方式不提供用量"
        case "not_reported": "还没有用量数据"
        default: "暂时读不到用量"
        }
    }

    public static func parse(_ json: JSON, backend: AgentBackend) -> ProviderUsage {
        guard json["status"].string == "available" else {
            return ProviderUsage(available: false, reason: json["reason"].string)
        }
        let windows: [UsageWindow] = json["windows"].array.compactMap { item in
            guard let used = item["used_percent"].double else { return nil }
            let minutes = item["window_minutes"].double.map { Int($0) }
            return UsageWindow(id: item["id"].string ?? UUID().uuidString,
                               label: item["label"].text ?? Self.nativeLabel(backend: backend, minutes: minutes),
                               usedPercent: min(max(used, 0), 100),
                               resetsAt: item["resets_at"].double.map { Date(timeIntervalSince1970: $0) },
                               windowMinutes: minutes)
        }
        return ProviderUsage(available: !windows.isEmpty, windows: windows, plan: json["plan"].string,
                             reason: windows.isEmpty ? "not_reported" : nil, observedAt: json["observed_at"].date)
    }

    /// Codex's own names for its windows (as in its `/status`).
    static func nativeLabel(backend: AgentBackend, minutes: Int?) -> String {
        switch minutes {
        case 300?: backend == .codex ? "5h limit" : "Current session"
        case 10080?: backend == .codex ? "Weekly limit" : "Current week"
        case let minutes?: minutes < 1440 ? "\(minutes / 60)h limit" : "\(minutes / 1440)d limit"
        case nil: "Limit"
        }
    }
}

/// How full a chat's context window is, as the provider last reported it.
public struct ContextUsage: Hashable, Sendable {
    public var tokens: Int
    public var window: Int
    /// 0–100.
    public var percent: Double

    public init(tokens: Int, window: Int, percent: Double) {
        self.tokens = tokens
        self.window = window
        self.percent = percent
    }

    /// From a runtime response's `context_usage`, or a socket signal's.
    public static func parse(_ json: JSON) -> ContextUsage? {
        guard let tokens = json["context_tokens"].int ?? json["totalTokens"].int,
              let window = json["effective_context_window"].int ?? json["context_window"].int ?? json["maxTokens"].int,
              window > 0 else { return nil }
        let percent = json["context_percent"].double ?? json["percentage"].double ?? Double(tokens) * 100 / Double(window)
        return ContextUsage(tokens: tokens, window: window, percent: min(max(percent, 0), 100))
    }
}
