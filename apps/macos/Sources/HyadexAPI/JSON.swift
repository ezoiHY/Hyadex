import Foundation

/// A loosely typed JSON value. The server's event log is open-ended (new
/// event types and fields appear over time), so events are kept as JSON and
/// projected by the client instead of being forced into rigid structs.
public enum JSON: Hashable, Sendable, Codable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([JSON].self) { self = .array(value) }
        else { self = .object(try container.decode([String: JSON].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    public subscript(key: String) -> JSON {
        if case .object(let object) = self { return object[key] ?? .null }
        return .null
    }

    public subscript(index: Int) -> JSON {
        if case .array(let array) = self, array.indices.contains(index) { return array[index] }
        return .null
    }

    public var string: String? {
        switch self {
        case .string(let value): value
        case .number(let value): value.rounded() == value ? String(Int(value)) : String(value)
        default: nil
        }
    }

    public var int: Int? {
        switch self {
        case .number(let value): Int(value)
        case .string(let value): Int(value)
        default: nil
        }
    }

    public var double: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public var bool: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var array: [JSON] { if case .array(let value) = self { return value } else { return [] } }
    public var object: [String: JSON]? { if case .object(let value) = self { return value } else { return nil } }
    public var isNull: Bool { self == .null }

    /// Text if non-empty after trimming.
    public var text: String? {
        guard let value = string?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }

    /// Dates arrive as ISO-8601 strings (sessions, events) or Unix seconds (groups, jobs).
    public var date: Date? {
        switch self {
        case .number(let seconds): return Date(timeIntervalSince1970: seconds)
        case .string(let value):
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = formatter.date(from: value) { return date }
            formatter.formatOptions = [.withInternetDateTime]
            return formatter.date(from: value)
        default: return nil
        }
    }

    public static func compact(_ pairs: [String: JSON?]) -> JSON {
        .object(pairs.compactMapValues { $0 })
    }
}

extension JSON: ExpressibleByStringLiteral, ExpressibleByBooleanLiteral, ExpressibleByIntegerLiteral,
                ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral, ExpressibleByNilLiteral {
    public init(stringLiteral value: String) { self = .string(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .number(Double(value)) }
    public init(arrayLiteral elements: JSON...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, JSON)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { $1 }))
    }
    public init(nilLiteral: ()) { self = .null }
}
