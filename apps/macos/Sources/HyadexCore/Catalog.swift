import Foundation
import HyadexAPI

/// One selectable model or reasoning effort, with the provider's own name.
/// `value` is what the server stores ("" means the server default).
public struct RuntimeOption: Identifiable, Hashable, Sendable {
    public var id: String { value }
    public let value: String
    public let label: String
    public let limited: Bool

    public init(value: String, label: String, limited: Bool = false) {
        self.value = value
        self.label = label
        self.limited = limited
    }
}

/// The models and reasoning efforts a backend offers, as discovered by the
/// server from the native CLI (`GET /api/runtime/catalog`).
public struct RuntimeCatalog: Hashable, Sendable {
    public var models: [RuntimeOption]
    public var efforts: [RuntimeOption]
    /// Codex: the efforts each model supports (model value → efforts, without the default).
    public var modelEfforts: [String: [RuntimeOption]]
    public var defaultModel: String?
    public var defaultEffort: String?

    public init(models: [RuntimeOption], efforts: [RuntimeOption], modelEfforts: [String: [RuntimeOption]] = [:],
                defaultModel: String? = nil, defaultEffort: String? = nil) {
        self.models = models
        self.efforts = efforts
        self.modelEfforts = modelEfforts
        self.defaultModel = defaultModel
        self.defaultEffort = defaultEffort
    }

    static func options(_ json: JSON) -> [RuntimeOption] {
        json.array.compactMap { item in
            guard let value = item["value"].string else { return nil }
            return RuntimeOption(value: value, label: item["label"].text ?? (value.isEmpty ? "Server default" : value),
                                 limited: item["availability"].string == "limited")
        }
    }

    /// Parses one backend entry of the catalog response.
    public static func parse(_ json: JSON) -> RuntimeCatalog? {
        let models = options(json["models"])
        guard !models.isEmpty else { return nil }
        var modelEfforts: [String: [RuntimeOption]] = [:]
        for (model, efforts) in json["model_efforts"].object ?? [:] {
            modelEfforts[model] = options(efforts)
        }
        return RuntimeCatalog(models: models, efforts: options(json["efforts"]), modelEfforts: modelEfforts,
                              defaultModel: json["default_model"].text, defaultEffort: json["default_effort"].text)
    }

    /// Used for demo data and until the server's catalog arrives.
    public static func fallback(for backend: AgentBackend) -> RuntimeCatalog {
        let efforts = [RuntimeOption(value: "", label: "Server default")] + ["low", "medium", "high", "xhigh", "max"].map {
            RuntimeOption(value: $0, label: $0 == "xhigh" ? "XHigh" : $0.capitalized)
        }
        switch backend {
        case .claude:
            return RuntimeCatalog(models: [RuntimeOption(value: "", label: "Server default"),
                                           RuntimeOption(value: "opus", label: "Opus"),
                                           RuntimeOption(value: "sonnet", label: "Sonnet"),
                                           RuntimeOption(value: "haiku", label: "Haiku")], efforts: efforts)
        case .codex:
            return RuntimeCatalog(models: [RuntimeOption(value: "", label: "Server default")], efforts: efforts)
        }
    }

    // MARK: Choices

    /// Models to offer, keeping a current value the catalog does not list.
    public func modelOptions(current: String) -> [RuntimeOption] {
        models.contains { $0.value == current } ? models : models + [RuntimeOption(value: current, label: current)]
    }

    /// Efforts valid for `model` ("" = the default model), default first.
    public func effortOptions(model: String, current: String) -> [RuntimeOption] {
        let defaultOption = efforts.first { $0.value.isEmpty } ?? RuntimeOption(value: "", label: "Server default")
        let resolved = model.isEmpty ? (defaultModel ?? "") : model
        let specific = modelEfforts[resolved] ?? efforts.filter { !$0.value.isEmpty }
        var result = [defaultOption] + specific
        if !result.contains(where: { $0.value == current }) { result.append(RuntimeOption(value: current, label: current)) }
        return result
    }

    /// Whether `effort` can be used with `model`.
    public func supports(effort: String, model: String) -> Bool {
        effort.isEmpty || effortOptions(model: model, current: "").contains { $0.value == effort }
    }

    // MARK: Names

    /// The model's native name; the default resolves to the model it stands for.
    public func modelName(_ value: String) -> String {
        if value.isEmpty {
            if let resolved = defaultModel, let option = models.first(where: { $0.value == resolved }) { return option.label }
            return "Default"
        }
        return models.first { $0.value == value }?.label ?? value
    }

    public func effortName(_ value: String, model: String) -> String {
        if value.isEmpty {
            if let resolved = defaultEffort,
               let option = efforts.first(where: { $0.value == resolved }) { return option.label }
            return "Default"
        }
        return effortOptions(model: model, current: value).first { $0.value == value }?.label ?? value
    }
}
