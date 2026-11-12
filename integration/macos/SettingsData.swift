import Foundation

// JSON keeps extension settings intact when a profile is edited in the app.
enum SettingValue: Codable, Sendable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: SettingValue]), array([SettingValue]), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let v = try? value.decode(Bool.self) { self = .bool(v) }
        else if let v = try? value.decode(String.self) { self = .string(v) }
        else if let v = try? value.decode(Double.self) { self = .number(v) }
        else if let v = try? value.decode([String: SettingValue].self) { self = .object(v) }
        else { self = .array(try value.decode([SettingValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .string(let v): try value.encode(v)
        case .number(let v): try value.encode(v)
        case .bool(let v): try value.encode(v)
        case .object(let v): try value.encode(v)
        case .array(let v): try value.encode(v)
        case .null: try value.encodeNil()
        }
    }

    var text: String { if case .string(let v) = self { return v }; return "" }
    var integer: Int? { if case .number(let v) = self, v.isFinite, abs(v) < Double(Int.max) { return Int(v) }; return nil }
    var flag: Bool { if case .bool(let v) = self { return v }; return false }
    var object: [String: SettingValue] { if case .object(let v) = self { return v }; return [:] }
    var array: [SettingValue] { if case .array(let v) = self { return v }; return [] }
}

struct ModelRecord: Decodable, Identifiable, Sendable {
    let values: [String: SettingValue]
    init(from decoder: Decoder) throws { values = try decoder.singleValueContainer().decode([String: SettingValue].self) }
    var id: String { values["id"]?.text ?? "" }
    var name: String { values["name"]?.text ?? id }
    var kind: String { values["kind"]?.text ?? "chat" }
    var backend: String { values["backend"]?.text ?? "local" }
    var ready: Bool { values["ready"]?.flag ?? false }
    var selected: Bool { values["selected"]?.flag ?? false }
    var visible: Bool { values["visible"] == nil || values["visible"]?.flag == true }
    var context: Int { values["context"]?.integer ?? 0 }
    var downloadable: Bool { values["download"] != nil }
    var downloadBytes: Int { values["download_bytes"]?.integer ?? 0 }
    var providerID: String { values["provider_id"]?.text ?? "" }
    var category: String { values["category"]?.text.nilIfEmpty ?? (downloadable ? "standard" : "custom") }
    var profile: [String: SettingValue] {
        values.filter { !["id", "ready", "selected", "download_bytes", "index_ready", "category", "auth"].contains($0.key) }
    }
    func text(_ key: String) -> String { values[key]?.text ?? "" }
}

struct ProviderRecord: Decodable, Identifiable, Sendable {
    let values: [String: SettingValue]
    init(from decoder: Decoder) throws { values = try decoder.singleValueContainer().decode([String: SettingValue].self) }
    var id: String { values["id"]?.text ?? "" }
    var name: String { values["name"]?.text ?? id }
    var baseURL: String { values["base_url"]?.text ?? "" }
    var auth: String { values["auth"]?.text ?? "none" }
    var visible: Bool { values["visible"] == nil || values["visible"]?.flag == true }
}

struct SettingsSnapshot: Decodable, Sendable {
    let configRoot: String
    let stateDirectory: String
    let selectedChat: String?
    let selectedEmbedding: String?
    let profiles: [ModelRecord]
    let providers: [ProviderRecord]
    let service: [String: SettingValue]
    let applied: Bool
    let binary: String?
    let settings: [String: SettingValue]
    let revision: String
    let defaults: [String: SettingValue]
    let catalogue: [ModelRecord]

    enum CodingKeys: String, CodingKey {
        case profiles, providers, service, applied, binary, settings, revision, defaults, catalogue
        case configRoot = "config_root", stateDirectory = "state_dir"
        case selectedChat = "selected_chat", selectedEmbedding = "selected_embedding"
    }
}

struct RemoteModelRecord: Decodable, Identifiable, Sendable {
    let id: String
    let name: String?
    let context: Int?
}
