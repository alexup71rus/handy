import Foundation
import SwiftUI

// Each operation is a short-lived child with private stdin. Secrets never enter
// command arguments, environment, log files, or the model's chat history.
final class SettingsJob: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    func cancel() {
        lock.lock()
        cancelled = true
        let active = process
        lock.unlock()
        if let active, active.isRunning {
            DispatchQueue.global(qos: .utility).async { stopOwnedProcess(active) }
        }
    }

    func run(control: Control, request: [String: SettingValue], progress: @escaping @Sendable ([String: SettingValue]) -> Void) throws -> [String: SettingValue] {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appendingPathComponent("handy-settings-\(UUID().uuidString)")
        try files.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? files.removeItem(at: directory) }
        let errors = directory.appendingPathComponent("stderr")
        files.createFile(atPath: errors.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let stderr = try FileHandle(forWritingTo: errors)
        defer { try? stderr.close() }
        let child = Process()
        child.executableURL = control.python
        child.currentDirectoryURL = control.checkout
        child.arguments = ["-m", "folder_memory.model_settings", "--state-dir", control.stateDirectory.path]
        var environment = ProcessInfo.processInfo.environment
        if !control.toolPath.isEmpty { environment["PATH"] = control.toolPath }
        environment["PYTHONUNBUFFERED"] = "1"
        child.environment = environment
        let input = Pipe(), output = Pipe()
        child.standardInput = input
        child.standardOutput = output
        child.standardError = stderr
        let payload = try JSONEncoder().encode(request) + Data([10])
        lock.lock()
        if cancelled { lock.unlock(); throw CancellationError() }
        do { try OwnedBridgeProcesses.shared.run(child) } catch { lock.unlock(); throw error }
        process = child
        lock.unlock()
        defer {
            try? input.fileHandleForWriting.close()
            try? output.fileHandleForReading.close()
            stopOwnedProcess(child)
            OwnedBridgeProcesses.shared.release(child)
            lock.lock(); process = nil; lock.unlock()
        }
        try input.fileHandleForWriting.write(contentsOf: payload)
        try input.fileHandleForWriting.close()
        var buffered = Data()
        var result: [String: SettingValue]?
        var failure: String?
        while let chunk = try output.fileHandleForReading.read(upToCount: 65_536), !chunk.isEmpty {
            buffered.append(chunk)
            guard buffered.count <= 4 * 1024 * 1024 else { throw ControlError(message: "Ответ настроек слишком большой.") }
            while let newline = buffered.firstIndex(of: 10) {
                let line = buffered[..<newline]
                buffered.removeSubrange(...newline)
                guard !line.isEmpty else { continue }
                let event = try JSONDecoder().decode([String: SettingValue].self, from: Data(line))
                switch event["event"]?.text {
                case "result": result = event["data"]?.object
                case "error": failure = event["message"]?.text
                case "progress": progress(event)
                default: break
                }
            }
        }
        child.waitUntilExit()
        lock.lock(); let stopped = cancelled; lock.unlock()
        if stopped { throw CancellationError() }
        if let failure { throw ControlError(message: failure) }
        guard child.terminationStatus == 0, let result else {
            if child.terminationStatus != 0 {
                let diagnostic = try FileHandle(forReadingFrom: errors)
                defer { try? diagnostic.close() }
                let size = try diagnostic.seekToEnd()
                try diagnostic.seek(toOffset: size > 4096 ? size - 4096 : 0)
                let details = String(decoding: try diagnostic.read(upToCount: 4096) ?? Data(), as: UTF8.self)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if !details.isEmpty { throw ControlError(message: details) }
            }
            throw ControlError(message: "Не удалось завершить операцию настроек. Попробуй ещё раз.")
        }
        return result
    }
}

@MainActor
final class SettingsModel: ObservableObject {
    @Published private(set) var snapshot: SettingsSnapshot?
    @Published private(set) var busy: String?
    @Published private(set) var progress: [String: SettingValue]?
    @Published var error: String?
    @Published var notice: String?
    @Published private(set) var providerModelCounts: [String: Int] = [:]
    @Published private(set) var providerModels: [String: [RemoteModelRecord]] = [:]
    @Published private(set) var canCancel = false
    private var job: SettingsJob?
    private let control: Control?
    private var savedSnapshot: SettingsSnapshot?
    private var savedSettings: [String: SettingValue] = [:]
    private var draftSettings: [String: SettingValue] = [:]
    private var savedRevision = ""
    // Entered keys belong only to this draft, never to its JSON document.
    private var pendingKeys: [String: String] = [:]

    init(control: Control? = try? Control()) { self.control = control }

    var working: Bool { busy != nil }
    var hasChanges: Bool { semanticSettings(draftSettings) != semanticSettings(savedSettings) || !pendingKeys.isEmpty }
    var canApply: Bool {
        !working && snapshot != nil && (hasChanges || (savedSnapshot?.service["status"]?.text == "running" && savedSnapshot?.applied == false))
    }
    var canReset: Bool { snapshot != nil && semanticSettings(factorySettings()) != semanticSettings(draftSettings) }

    func hasPendingKey(_ providerID: String) -> Bool { pendingKeys[providerID] != nil }

    func canResetProfile(_ id: String) -> Bool {
        guard let factory = snapshot?.defaults[id], let current = draftSettings["profiles"]?.object[id] else { return false }
        return semanticProfile(factory.object) != semanticProfile(current.object)
    }

    func discardDraft() {
        guard !working else { return }
        draftSettings = savedSettings
        pendingKeys.removeAll()
        snapshot = savedSnapshot
        providerModelCounts.removeAll()
        providerModels.removeAll()
        clearMessages()
    }

    func selectDraft(_ id: String?, kind: String) async {
        await run("select", ["id": id.map(SettingValue.string) ?? .null, "kind": .string(kind)], title: "Обновляю выбор…")
    }

    func modelIsVisible(_ id: String) -> Bool {
        guard let profile = draftSettings["profiles"]?.object[id]?.object ?? snapshot?.catalogue.first(where: { $0.id == id })?.profile else { return false }
        return profile["visible"]?.flag != false && visibilityOwner(profile).map { ownerIsVisible($0, in: draftSettings) } != false
    }

    func providerVisibilityState(providerID: String, kind: String) -> VisibilityState {
        if providerID.hasPrefix("legacy:") { return modelIsVisible(String(providerID.dropFirst(7))) ? .on : .off }
        let children = visibilityProfiles(draftSettings).filter {
            ($0.value.object["kind"]?.text ?? "chat") == kind && visibilityOwner($0.value.object) == providerID
        }
        guard !children.isEmpty else { return ownerIsVisible(providerID, in: draftSettings) ? .on : .off }
        let parentVisible = ownerIsVisible(providerID, in: draftSettings)
        return VisibilityState(children.values.map { $0.object["visible"]?.flag != false && parentVisible })
    }

    func setVisibility(_ id: String, provider: Bool, visible: Bool) async {
        var candidate = draftSettings
        var profiles = candidate["profiles"]?.object ?? [:]
        if provider {
            guard id == "builtin-standard" || candidate["providers"]?.object[id] != nil else {
                error = "Элемент больше не существует. Обнови настройки."
                return
            }
            for (childID, value) in visibilityProfiles(candidate) where visibilityOwner(value.object) == id {
                var profile = value.object
                profile["visible"] = .bool(visible)
                profiles[childID] = .object(profile)
            }
            setOwnerVisibility(id, visible: visible, in: &candidate)
        } else {
            guard var profile = visibilityProfiles(candidate)[id]?.object else {
                error = "Элемент больше не существует. Обнови настройки."
                return
            }
            if visible, let owner = visibilityOwner(profile), !ownerIsVisible(owner, in: candidate) {
                // Old documents may mask children whose individual flags are
                // still true. Enabling one must not silently enable its siblings.
                clearMaskedChildren(owner, in: &candidate, profiles: &profiles)
            }
            profile["visible"] = .bool(visible)
            profiles[id] = .object(profile)
        }
        candidate["profiles"] = .object(profiles)
        await stage(candidate, keys: pendingKeys)
    }

    func setAllModelsVisibility(_ visible: Bool, kind: String) async {
        guard kind == "chat" || kind == "embedding" else {
            error = "Выбери модели или эмбеддинги."
            return
        }
        var candidate = draftSettings
        var profiles = candidate["profiles"]?.object ?? [:]
        let children = visibilityProfiles(candidate).filter { ($0.value.object["kind"]?.text ?? "chat") == kind }
        if visible {
            for owner in Set(children.values.compactMap { visibilityOwner($0.object) }) where !ownerIsVisible(owner, in: candidate) {
                // Unmasking a provider must retain the effective hidden state of
                // its other kind (chat/embedding), including absent catalogue rows.
                clearMaskedChildren(owner, in: &candidate, profiles: &profiles)
            }
        }
        for (id, value) in children {
            var profile = value.object
            profile["visible"] = .bool(visible)
            profiles[id] = .object(profile)
        }
        candidate["profiles"] = .object(profiles)
        await stage(candidate, keys: pendingKeys)
    }

    private func visibilityProfiles(_ settings: [String: SettingValue]) -> [String: SettingValue] {
        var profiles = settings["profiles"]?.object ?? [:]
        for standard in snapshot?.catalogue ?? [] where profiles[standard.id] == nil {
            profiles[standard.id] = .object(standard.profile)
        }
        return profiles
    }

    private func visibilityOwner(_ profile: [String: SettingValue]) -> String? {
        if (profile["backend"]?.text ?? "local") == "local" { return "builtin-standard" }
        return profile["provider_id"]?.text.nilIfEmpty
    }

    private func ownerIsVisible(_ owner: String, in settings: [String: SettingValue]) -> Bool {
        if owner == "builtin-standard" { return settings["standard_provider_visible"]?.flag != false }
        return settings["providers"]?.object[owner]?.object["visible"]?.flag != false
    }

    private func setOwnerVisibility(_ owner: String, visible: Bool, in settings: inout [String: SettingValue]) {
        if owner == "builtin-standard" { settings["standard_provider_visible"] = .bool(visible) }
        else if var value = settings["providers"]?.object[owner]?.object {
            var providers = settings["providers"]?.object ?? [:]
            value["visible"] = .bool(visible)
            providers[owner] = .object(value)
            settings["providers"] = .object(providers)
        }
    }

    private func clearMaskedChildren(_ owner: String, in settings: inout [String: SettingValue], profiles: inout [String: SettingValue]) {
        for (id, value) in visibilityProfiles(settings) where visibilityOwner(value.object) == owner {
            var profile = value.object
            profile["visible"] = .bool(false)
            profiles[id] = .object(profile)
        }
        setOwnerVisibility(owner, visible: true, in: &settings)
    }

    func resetDraftProfile(_ id: String) async {
        guard let factory = snapshot?.defaults[id] else { return }
        var candidate = draftSettings
        var profiles = candidate["profiles"]?.object ?? [:]
        profiles[id] = factory
        candidate["profiles"] = .object(profiles)
        await stage(candidate, keys: pendingKeys)
    }

    func resetDefaults() async { await stage(factorySettings(), keys: pendingKeys) }

    func discoverModels(providerID: String, importForChat: Bool = true) async {
        // A newly staged provider can become selected while its editor's preview
        // is still finishing. Keep that first discovery instead of dropping it.
        while working {
            do { try await Task.sleep(nanoseconds: 50_000_000) }
            catch { return }
        }
        guard !Task.isCancelled, draftSettings["providers"]?.object[providerID] != nil else { return }
        await run("discover", ["provider_id": .string(providerID), "import_for_chat": .bool(importForChat)], title: "Получаю модели…")
    }

    @discardableResult
    func addRemoteModel(providerID: String, remoteModel: RemoteModelRecord, kind: String) async -> String? {
        if let existing = snapshot?.profiles.first(where: { $0.providerID == providerID && $0.kind == kind && $0.text("model_id") == remoteModel.id }) {
            return existing.id
        }
        let identifier = "api-" + UUID().uuidString.lowercased()
        let profile = remoteProfile(providerID: providerID, remoteModel: remoteModel, kind: kind)
        return await run("save_profile", ["id": .string(identifier), "profile": .object(profile)]) == nil ? nil : identifier
    }

    @discardableResult
    func run(_ operation: String, _ fields: [String: SettingValue] = [:], title: String = "Сохраняю…", message: String? = nil) async -> [String: SettingValue]? {
        guard !working else { return nil }
        error = nil
        notice = nil
        busy = title
        progress = nil
        canCancel = ["snapshot", "discover", "test_provider", "test_profile", "download", "rebuild_index"].contains(operation)
        defer { busy = nil; progress = nil; job = nil; canCancel = false }
        do {
            if ["save_profile", "save_provider", "delete_profile", "delete_provider", "select", "restore_catalogue"].contains(operation) {
                let changed = try stagedOperation(operation, fields)
                return try await preview(changed.settings, keys: changed.keys)
            }
            if operation == "snapshot" {
                guard !hasChanges else { throw ControlError(message: "Сначала примени или отмени изменения.") }
                let result = try await backend("snapshot")
                try acceptSaved(result)
                return result
            }
            if operation == "apply" {
                guard !savedRevision.isEmpty else { throw ControlError(message: "Сначала загрузи настройки.") }
                var fields = draftRequest(draftSettings, keys: pendingKeys)
                fields["revision"] = .string(savedRevision)
                let result = try await backend("apply_settings", fields)
                try acceptSaved(result)
                notice = "Изменения применены."
                return result
            }
            if operation == "download" || operation == "rebuild_index" {
                return try await assetOperation(operation, fields)
            }
            if operation == "discover", fields["import_for_chat"]?.flag == true,
               let providerID = fields["provider_id"]?.text.nilIfEmpty,
               savedConnectionUnchanged(providerID) {
                return try await refreshSavedModels(providerID)
            }
            var request = fields
            let providerID = fields["provider_id"]?.text.nilIfEmpty ?? fields["original_id"]?.text.nilIfEmpty
            if operation == "discover" || operation == "test_provider" {
                request = try connectionRequest(fields)
                if let providerID {
                    providerModelCounts.removeValue(forKey: providerID)
                    providerModels.removeValue(forKey: providerID)
                }
            }
            if operation == "test_profile" {
                request.merge(draftRequest(draftSettings, keys: pendingKeys)) { _, new in new }
            }
            let result = try await backend(operation, request)
            if operation == "discover" || operation == "test_provider", let providerID {
                let models = try JSONDecoder().decode([RemoteModelRecord].self, from: JSONEncoder().encode(result["models"] ?? .array([])))
                if operation == "discover", fields["import_for_chat"]?.flag == true {
                    var candidate = draftSettings
                    var profiles = candidate["profiles"]?.object ?? [:]
                    for remoteModel in models {
                        let exists = profiles.values.contains { value in
                            value.object["provider_id"]?.text == providerID && value.object["model_id"]?.text == remoteModel.id && value.object["kind"]?.text == "chat"
                        }
                        if !exists {
                            profiles["api-" + UUID().uuidString.lowercased()] = .object(remoteProfile(providerID: providerID, remoteModel: remoteModel, kind: "chat"))
                        }
                    }
                    candidate["profiles"] = .object(profiles)
                    if candidate != draftSettings { _ = try await preview(candidate, keys: pendingKeys) }
                }
                providerModels[providerID] = models
                providerModelCounts[providerID] = models.count
            }
            notice = result["message"]?.text.nilIfEmpty ?? message
            return result
        } catch is CancellationError {
            notice = "Операция отменена."
        } catch {
            self.error = error.localizedDescription
        }
        return nil
    }

    func refresh() async { await run("snapshot", title: "Обновляю…") }
    func cancel() { if canCancel { job?.cancel() } }
    func clearMessages() { error = nil; notice = nil }

    private func backend(_ operation: String, _ fields: [String: SettingValue] = [:]) async throws -> [String: SettingValue] {
        guard let control else { throw ControlError(message: "Пересобери приложение из папки Handy.") }
        var prepared = fields
        prepared["operation"] = .string(operation)
        let request = prepared
        let active = SettingsJob()
        let owner = self
        job = active
        return try await Task.detached(priority: .userInitiated) {
            try active.run(control: control, request: request) { event in
                Task { @MainActor in if owner.job === active { owner.progress = event } }
            }
        }.value
    }

    private func draftRequest(_ settings: [String: SettingValue], keys: [String: String]) -> [String: SettingValue] {
        var request: [String: SettingValue] = ["settings": .object(settings)]
        if !keys.isEmpty { request["credentials"] = .object(keys.mapValues(SettingValue.string)) }
        return request
    }

    private func acceptSaved(_ result: [String: SettingValue], preservingDraft: Bool = false) throws {
        let updated = try JSONDecoder().decode(SettingsSnapshot.self, from: JSONEncoder().encode(result))
        savedSnapshot = updated
        savedSettings = updated.settings
        savedRevision = updated.revision
        if !preservingDraft {
            draftSettings = updated.settings
            pendingKeys.removeAll()
            invalidateProviderCaches(updated)
            snapshot = updated
        }
    }

    private func invalidateProviderCaches(_ updated: SettingsSnapshot) {
        for provider in snapshot?.providers ?? [] {
            if updated.providers.first(where: { $0.id == provider.id })?.values != provider.values {
                providerModelCounts.removeValue(forKey: provider.id)
                providerModels.removeValue(forKey: provider.id)
            }
        }
    }

    private func preview(_ settings: [String: SettingValue], keys: [String: String]) async throws -> [String: SettingValue] {
        var request = draftRequest(settings, keys: keys)
        request["revision"] = .string(savedRevision)
        var result = try await backend("preview_settings", request)
        // Preview never probes the service. Retain the last real status and its
        // application state rather than turning an unchanged draft into dirty UI.
        if let savedSnapshot { result["service"] = .object(savedSnapshot.service) }
        let validated = result["settings"]?.object ?? settings
        result["applied"] = .bool(semanticSettings(validated) == semanticSettings(savedSettings) && keys.isEmpty && savedSnapshot?.applied == true)
        let updated = try JSONDecoder().decode(SettingsSnapshot.self, from: JSONEncoder().encode(result))
        for id in Set(pendingKeys.keys).union(keys.keys) where pendingKeys[id] != keys[id] {
            providerModelCounts.removeValue(forKey: id)
            providerModels.removeValue(forKey: id)
        }
        draftSettings = validated
        pendingKeys = keys
        invalidateProviderCaches(updated)
        snapshot = updated
        return result
    }

    private func stage(_ settings: [String: SettingValue], keys: [String: String]) async {
        guard !working else { return }
        error = nil
        notice = nil
        busy = "Обновляю…"
        defer { busy = nil; job = nil }
        do {
            _ = try await preview(settings, keys: keys)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func ensureCatalogue(_ id: String, in settings: inout [String: SettingValue]) {
        var profiles = settings["profiles"]?.object ?? [:]
        if profiles[id] == nil, let standard = snapshot?.catalogue.first(where: { $0.id == id }) {
            profiles[id] = .object(standard.profile)
            settings["profiles"] = .object(profiles)
        }
    }

    private func stagedOperation(_ operation: String, _ fields: [String: SettingValue]) throws -> (settings: [String: SettingValue], keys: [String: String]) {
        guard snapshot != nil else { throw ControlError(message: "Сначала загрузи настройки.") }
        var candidate = draftSettings
        var keys = pendingKeys
        let id = fields["id"]?.text ?? ""
        var profiles = candidate["profiles"]?.object ?? [:]
        var providers = candidate["providers"]?.object ?? [:]
        switch operation {
        case "save_profile":
            guard !id.isEmpty, let value = fields["profile"], !value.object.isEmpty else { throw ControlError(message: "Укажи настройки модели.") }
            if fields["original_id"] == nil, profiles[id] != nil { throw ControlError(message: "Такая модель уже добавлена.") }
            profiles[id] = value
            candidate["profiles"] = .object(profiles)
        case "save_provider":
            guard !id.isEmpty else { throw ControlError(message: "Не удалось добавить провайдера.") }
            if fields["original_id"] == nil, providers[id] != nil { throw ControlError(message: "Такой провайдер уже добавлен.") }
            let entered = fields["provider"]?.object ?? [:]
            var provider = providers[id]?.object ?? [:]
            provider["name"] = entered["name"]
            provider["base_url"] = entered["base_url"]
            let auth = entered["auth"]?.text ?? "none"
            if auth == "env" {
                provider["api_key_env"] = entered["api_key_env"]
                provider.removeValue(forKey: "api_key_file")
                keys.removeValue(forKey: id)
            } else if auth == "file" {
                provider["api_key_file"] = entered["api_key_file"]
                provider.removeValue(forKey: "api_key_env")
                keys.removeValue(forKey: id)
            } else if auth == "key" {
                if let key = fields["api_key"]?.text.nilIfEmpty { keys[id] = key }
                if keys[id] == nil, provider["api_key_file"] == nil { throw ControlError(message: "Введи API-ключ.") }
            } else {
                provider.removeValue(forKey: "api_key_env")
                provider.removeValue(forKey: "api_key_file")
                keys.removeValue(forKey: id)
            }
            providers[id] = .object(provider)
            candidate["providers"] = .object(providers)
        case "delete_profile":
            profiles.removeValue(forKey: id)
            candidate["profiles"] = .object(profiles)
            repairSelection(&candidate)
        case "delete_provider":
            providers.removeValue(forKey: id)
            keys.removeValue(forKey: id)
            profiles = profiles.filter { $0.value.object["provider_id"]?.text != id }
            candidate["providers"] = .object(providers)
            candidate["profiles"] = .object(profiles)
            repairSelection(&candidate)
        case "select":
            let kind = fields["kind"]?.text ?? "chat"
            if !id.isEmpty {
                ensureCatalogue(id, in: &candidate)
                var profiles = candidate["profiles"]?.object ?? [:]
                guard var profile = profiles[id]?.object, profile["kind"]?.text == kind else { throw ControlError(message: "Выбери подходящую модель.") }
                profile["enabled"] = .bool(true)
                profiles[id] = .object(profile)
                candidate["profiles"] = .object(profiles)
            } else if kind != "embedding" { throw ControlError(message: "Выбери основную модель.") }
            candidate["selected_" + kind] = id.isEmpty ? .null : .string(id)
        case "restore_catalogue":
            for standard in snapshot?.catalogue ?? [] { ensureCatalogue(standard.id, in: &candidate) }
        default: break
        }
        return (candidate, keys)
    }

    private func repairSelection(_ settings: inout [String: SettingValue]) {
        let profiles = settings["profiles"]?.object ?? [:]
        for kind in ["chat", "embedding"] {
            if let selected = settings["selected_" + kind]?.text.nilIfEmpty, profiles[selected] == nil {
                let fallback = kind == "chat" ? snapshot?.profiles.first(where: { record in
                    profiles[record.id] != nil && record.backend == "local" && record.kind == "chat" && record.ready && profiles[record.id]?.object["enabled"]?.flag != false
                })?.id : nil
                settings["selected_" + kind] = fallback.map(SettingValue.string) ?? .null
            }
        }
    }

    private func factorySettings() -> [String: SettingValue] {
        guard let snapshot else { return draftSettings }
        var result = draftSettings
        var profiles = result["profiles"]?.object ?? [:]
        for (id, factory) in snapshot.defaults where profiles[id] != nil { profiles[id] = factory }
        for standard in snapshot.catalogue where profiles[standard.id] == nil { profiles[standard.id] = .object(standard.profile) }
        for (id, value) in profiles {
            var profile = value.object
            profile["visible"] = .bool(true)
            profiles[id] = .object(profile)
        }
        var providers = result["providers"]?.object ?? [:]
        for (id, value) in providers {
            var provider = value.object
            provider["visible"] = .bool(true)
            providers[id] = .object(provider)
        }
        result["profiles"] = .object(profiles)
        result["providers"] = .object(providers)
        result["standard_provider_visible"] = .bool(true)
        if profiles["lfm3-vl"] != nil { result["selected_chat"] = .string("lfm3-vl") }
        if profiles["qwen3-embedding"] != nil { result["selected_embedding"] = .string("qwen3-embedding") }
        return result
    }

    private func semanticSettings(_ settings: [String: SettingValue]) -> [String: SettingValue] {
        var result = settings
        result["standard_provider_visible"] = result["standard_provider_visible"] ?? .bool(true)
        for collection in ["profiles", "providers"] {
            var items = result[collection]?.object ?? [:]
            for (id, value) in items {
                items[id] = .object(collection == "profiles" ? semanticProfile(value.object) : value.object.merging(["visible": .bool(true)]) { current, _ in current })
            }
            result[collection] = .object(items)
        }
        return result
    }

    private func semanticProfile(_ profile: [String: SettingValue]) -> [String: SettingValue] {
        var result = profile
        result["visible"] = result["visible"] ?? .bool(true)
        if result["backend"]?.text == "openai", result["kind"]?.text == "chat" {
            result["enable_thinking"] = result["enable_thinking"] ?? .bool(false)
            result["max_output_tokens"] = result["max_output_tokens"] ?? .number(1536)
        }
        return result
    }

    private func connectionRequest(_ fields: [String: SettingValue]) throws -> [String: SettingValue] {
        var request = fields
        let id = fields["provider_id"]?.text.nilIfEmpty ?? fields["original_id"]?.text.nilIfEmpty
        var provider = fields["provider"]?.object ?? [:]
        if let providerID = fields["provider_id"]?.text.nilIfEmpty {
            guard let raw = draftSettings["providers"]?.object[providerID]?.object else { throw ControlError(message: "Провайдер не найден.") }
            provider = raw
            provider["auth"] = .string(pendingKeys[providerID] != nil ? "key" : raw["api_key_env"] != nil ? "env" : raw["api_key_file"] != nil ? "file" : "none")
        }
        if provider["auth"]?.text == "key", request["api_key"]?.text.nilIfEmpty == nil {
            if let id, let key = pendingKeys[id] { request["api_key"] = .string(key) }
            else if provider["api_key_file"] != nil { provider["auth"] = .string("file") }
        }
        request.removeValue(forKey: "provider_id")
        request["provider"] = .object(provider)
        return request
    }

    private func savedConnectionUnchanged(_ id: String) -> Bool {
        guard pendingKeys[id] == nil, let saved = savedSettings["providers"]?.object[id]?.object,
              let draft = draftSettings["providers"]?.object[id]?.object else { return false }
        return ["base_url", "api_key_env", "api_key_file"].allSatisfy { saved[$0] == draft[$0] }
    }

    private func refreshSavedModels(_ providerID: String) async throws -> [String: SettingValue] {
        providerModelCounts.removeValue(forKey: providerID)
        providerModels.removeValue(forKey: providerID)
        let oldSaved = savedSettings
        let oldDraft = draftSettings
        let oldKeys = pendingKeys
        let hadDraft = hasChanges
        let result = try await backend("refresh_models", ["provider_id": .string(providerID), "revision": .string(savedRevision)])
        let freshSettings = result["settings"]?.object ?? [:]
        var expected = oldSaved
        var expectedProfiles = oldSaved["profiles"]?.object ?? [:]
        for (id, value) in freshSettings["profiles"]?.object ?? [:] where expectedProfiles[id] == nil {
            guard value.object["kind"]?.text == "chat", value.object["provider_id"]?.text == providerID else {
                throw ControlError(message: "Настройки изменились извне. Отмени черновик и обнови список.")
            }
            expectedProfiles[id] = value
        }
        expected["profiles"] = .object(expectedProfiles)
        guard expected == freshSettings else { throw ControlError(message: "Настройки изменились извне. Отмени черновик и обнови список.") }
        let models = try JSONDecoder().decode([RemoteModelRecord].self, from: JSONEncoder().encode(result["models"] ?? .array([])))
        if hadDraft {
            let merged = mergingDraft(base: oldSaved, draft: oldDraft, fresh: freshSettings)
            try acceptSaved(result, preservingDraft: true)
            draftSettings = merged
            _ = try await preview(merged, keys: oldKeys)
        } else {
            try acceptSaved(result)
        }
        providerModels[providerID] = models
        providerModelCounts[providerID] = models.count
        return result
    }

    private func remoteProfile(providerID: String, remoteModel: RemoteModelRecord, kind: String) -> [String: SettingValue] {
        var profile: [String: SettingValue] = ["kind": .string(kind), "backend": .string("openai"), "name": .string(remoteModel.name?.nilIfEmpty ?? remoteModel.id),
            "provider_id": .string(providerID), "model_id": .string(remoteModel.id), "enabled": .bool(true), "visible": .bool(true),
            "context": .number(Double(min(remoteModel.context ?? (kind == "chat" ? 65536 : 8192), kind == "chat" ? 2_147_483_647 : 32768)))]
        if kind == "embedding" { profile["dimensions"] = .number(1024) }
        return profile
    }

    private func assetOperation(_ operation: String, _ fields: [String: SettingValue]) async throws -> [String: SettingValue] {
        let id = fields["id"]?.text ?? ""
        if operation == "rebuild_index" {
            guard let saved = savedSettings["profiles"]?.object[id]?.object,
                  let draft = draftSettings["profiles"]?.object[id]?.object else {
                throw ControlError(message: "Сначала примени настройки нового энкодера.")
            }
            let presentation = Set(["name", "visible"])
            if saved.filter({ !presentation.contains($0.key) }) != draft.filter({ !presentation.contains($0.key) })
                || pendingKeys[draft["provider_id"]?.text ?? ""] != nil {
                throw ControlError(message: "Сначала примени изменения энкодера, затем пересобери индекс.")
            }
        }
        let current = try await backend("snapshot")
        guard current["revision"]?.text == savedRevision else { throw ControlError(message: "Настройки изменились извне. Отмени черновик и обнови список.") }
        let oldSaved = savedSettings
        let oldDraft = draftSettings
        let oldKeys = pendingKeys
        let hadDraft = hasChanges
        let result = try await backend(operation, fields)
        let fresh: [String: SettingValue]
        if result["settings"] != nil { fresh = result }
        else { fresh = try await backend("snapshot") }
        let freshSettings = fresh["settings"]?.object ?? [:]
        var expected = oldSaved
        var expectedProfiles = expected["profiles"]?.object ?? [:]
        let freshProfile = freshSettings["profiles"]?.object[id]?.object ?? [:]
        if expectedProfiles[id] == nil, operation == "download" {
            expectedProfiles[id] = .object(freshProfile)
        } else {
            var allowedProfile = expectedProfiles[id]?.object ?? [:]
            let allowedFields = operation == "download" ? ["model", "mmproj", "model_sha256", "mmproj_sha256", "model_bytes", "mmproj_bytes", "download"] : ["index_path"]
            for field in allowedFields { allowedProfile[field] = freshProfile[field] }
            expectedProfiles[id] = .object(allowedProfile)
        }
        expected["profiles"] = .object(expectedProfiles)
        guard expected == freshSettings else { throw ControlError(message: "Файлы обработаны, но настройки изменились извне. Отмени черновик и обнови список.") }
        let merged = mergingDraft(base: oldSaved, draft: oldDraft, fresh: freshSettings)
        // The asset is already saved, but refreshing its preview can still fail
        // or be cancelled. Adopt only that saved baseline until preview succeeds;
        // retain every pending edit and entered key in either outcome.
        try acceptSaved(fresh, preservingDraft: true)
        draftSettings = merged
        if !hadDraft { snapshot = savedSnapshot }
        _ = try await preview(merged, keys: oldKeys)
        notice = result["message"]?.text.nilIfEmpty ?? (operation == "download" ? "Модель скачана." : "Индекс собран.")
        return fresh
    }

    private func mergingDraft(base: [String: SettingValue], draft: [String: SettingValue], fresh: [String: SettingValue]) -> [String: SettingValue] {
        var result = fresh
        for key in Set(base.keys).union(draft.keys) where base[key] != draft[key] {
            if case .object(let old)? = base[key], case .object(let edited)? = draft[key], case .object(let updated)? = fresh[key] {
                result[key] = .object(mergingDraft(base: old, draft: edited, fresh: updated))
            } else { result[key] = draft[key] }
        }
        return result
    }
}

extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
