import Foundation
import HyadexAPI

/// Answers a provider is still writing, from the server's non-durable
/// `assistant_text_stream` lane (a session socket opened with `text_stream`).
/// Each shows as a streaming message until its durable `assistant_text` (or,
/// for Codex commentary, `reasoning_summary`) arrives.
public struct LiveText: Hashable, Sendable {
    public struct Item: Hashable, Sendable {
        /// The provider's id for the answer (a Codex item, a Claude content block).
        public let id: String
        public let runID: String
        public var text: String
        /// The chat's last durable event when the answer began.
        public let afterSeq: Int
        public let started: Date
    }

    public private(set) var items: [Item] = []

    public init() {}

    /// Replaces the live answers with a server snapshot (a complete list).
    public mutating func apply(snapshot: JSON) {
        let previous = Dictionary(items.map { ($0.id, $0.started) }, uniquingKeysWith: { first, _ in first })
        items = snapshot["items"].array.compactMap { item in
            guard let id = item["item_id"].string, let text = item["text"].string, !text.isEmpty else { return nil }
            return Item(id: id, runID: item["run_id"].string ?? "", text: text, afterSeq: item["after_seq"].int ?? 0,
                        started: previous[id] ?? .now)
        }
    }

    /// A durable event arrived: drop the live answers it now records.
    public mutating func settle(with event: JSON) {
        switch event["type"].string {
        case "assistant_text":
            let seq = event["seq"].int ?? 0
            let run = event["run_id"].string
            let itemID = event["item_id"].string
            // Codex names the item; Claude's finished block is the run's answer
            // that began before it.
            items.removeAll { $0.id == itemID || ($0.runID == run && $0.afterSeq < seq) }
        case "reasoning_summary":
            if let itemID = event["item_id"].string { items.removeAll { $0.id == itemID } }
        case "turn_started", "turn_finished", "turn_stopped", "turn_aborted", "turn_failed":
            items.removeAll()
        default:
            break
        }
    }

    /// Streaming messages for the live answers, in the order they began.
    public func messages(author: Author) -> [TimelineItem] {
        items.sorted { ($0.afterSeq, $0.started) < ($1.afterSeq, $1.started) }.map {
            .message(id: "live-" + $0.id, author: author, text: $0.text, time: $0.started, streaming: true)
        }
    }
}
