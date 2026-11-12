import Foundation
import AppKit

// Runs the actual settings state and Python bridge. The Python wrapper supplies
// an isolated registry, header-only GGUF fixtures and a local API fixture.
@main
struct SettingsSmoke {
    @MainActor
    static func main() async {
        do {
            try await checkSettings()
        } catch {
            FileHandle.standardError.write(Data(("Native settings smoke failed: " + error.localizedDescription + "\n").utf8))
            exit(1)
        }
    }

    @MainActor
    private static func checkSettings() async throws {
        let environment = ProcessInfo.processInfo.environment
        func path(_ key: String) throws -> URL {
            guard let value = environment[key] else { throw ControlError(message: "Missing smoke environment.") }
            return URL(fileURLWithPath: value)
        }
        let root = try path("HANDY_SETTINGS_TEST_ROOT")
        let config = try path("HANDY_CONFIG_HOME").appendingPathComponent("models.json")
        let state = try path("HANDY_SETTINGS_TEST_STATE")
        let control = Control(checkout: try path("HANDY_SETTINGS_TEST_CHECKOUT"),
                              python: try path("HANDY_SETTINGS_TEST_PYTHON"), stateDirectory: state,
                              toolPath: environment["PATH"] ?? "")
        let original = try Data(contentsOf: config)
        let model = SettingsModel(control: control)
        var checks = 0
        func expect(_ condition: Bool, _ label: String) throws {
            checks += 1
            if !condition { throw ControlError(message: label + (model.error.map { ": " + $0 } ?? "")) }
        }
        func profile(_ id: String) throws -> ModelRecord {
            guard let record = model.snapshot?.profiles.first(where: { $0.id == id }) else {
                throw ControlError(message: "Missing fixture profile.")
            }
            return record
        }
        func stageContext(_ context: Int) async throws {
            var value = try profile("lfm3-vl").profile
            value["context"] = .number(Double(context))
            let result = await model.run("save_profile", ["id": .string("lfm3-vl"), "original_id": .string("lfm3-vl"), "profile": .object(value)])
            try expect(result != nil, "Stage model context")
        }
        func providerRequest(_ id: String) -> [String: SettingValue] {
            ["id": .string(id), "provider": .object(["name": .string("Smoke API"),
                "base_url": .string(environment["HANDY_SETTINGS_TEST_URL"] ?? ""), "auth": .string("key")]),
             "api_key": .string("ONLY_A_TEST_SECRET")]
        }
        func installLegacyMask(_ owner: String, document: Data) throws {
            var settings = try JSONSerialization.jsonObject(with: document) as! [String: Any]
            if owner == "builtin-standard" { settings["standard_provider_visible"] = false }
            else {
                var providers = settings["providers"] as! [String: [String: Any]]
                providers[owner]!["visible"] = false
                settings["providers"] = providers
            }
            var profiles = settings["profiles"] as! [String: [String: Any]]
            for (id, value) in profiles where owner == "builtin-standard" ? (value["backend"] as? String ?? "local") == "local" : value["provider_id"] as? String == owner {
                profiles[id]!["visible"] = true
            }
            settings["profiles"] = profiles
            try JSONSerialization.data(withJSONObject: settings, options: [.sortedKeys]).write(to: config)
        }

        NSApplication.shared.setActivationPolicy(.prohibited)
        var clicks: [Bool] = []
        let checkbox = VisibilityCheckbox.Coordinator(state: .on) { clicks.append($0) }
        let button = checkbox.makeButton(label: "Provider visibility")
        try expect(button.allowsMixedState && button.state == .on, "Native checkbox renders On")
        button.performClick(nil)
        try expect(clicks == [false] && button.state == .off, "Native On click disables every child")
        button.performClick(nil)
        try expect(clicks == [false, true] && button.state == .on, "Native Off click enables every child")
        checkbox.update(button, state: .mixed, label: "Provider visibility", enabled: true) { clicks.append($0) }
        try expect(button.state == .mixed, "Native checkbox renders Mixed")
        button.performClick(nil)
        try expect(clicks == [false, true, true] && button.state == .on, "Native Mixed click enables every child")
        checkbox.update(button, state: .on, label: "Provider visibility", enabled: false) { clicks.append($0) }
        try expect(!button.isEnabled, "Native checkbox respects disabled settings")

        await model.refresh()
        try expect(model.snapshot != nil, "Load actual bridge snapshot")
        try expect(!model.hasChanges && !model.canApply, "Unchanged Apply is disabled")
        try expect(!model.canReset && !model.canResetProfile("lfm3-vl"), "Factory settings do not need Reset")
        try expect(try profile("lfm3-vl").context == 65536, "Factory context is 64k")

        await model.setVisibility("spark", provider: false, visible: false)
        try expect(model.hasChanges && model.canApply, "Visibility is staged")
        try expect(try Data(contentsOf: config) == original, "Visibility preview leaves registry untouched")
        await model.setVisibility("spark", provider: false, visible: true)
        try expect(!model.hasChanges && !model.canApply, "Implicit visible=true creates no dirty state")

        let initialSelection = (model.snapshot!.selectedChat, model.snapshot!.selectedEmbedding)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .on, "Standard provider aggregates all visible children")
        await model.setVisibility("builtin-standard", provider: true, visible: false)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .off && model.snapshot!.profiles.filter { $0.backend == "local" }.allSatisfy { !model.modelIsVisible($0.id) && !$0.visible }, "Disabling standard parent clears every child of both kinds")
        await model.setVisibility("lfm3-vl", provider: false, visible: true)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .mixed && model.modelIsVisible("lfm3-vl") && !model.modelIsVisible("spark"), "Enabling one child activates a Mixed parent")
        try expect(!model.modelIsVisible("qwen3-embedding"), "Enabling chat child preserves hidden embedding")
        await model.setVisibility("builtin-standard", provider: true, visible: true)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .on && model.modelIsVisible("qwen3-embedding"), "Enabling Mixed parent enables every kind")
        await model.setVisibility("spark", provider: false, visible: false)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .mixed, "Unchecking a child makes standard parent Mixed")
        await model.setVisibility("lfm3-vl", provider: false, visible: false)
        try expect(model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .off, "Unchecking last child makes standard parent Off")
        try expect(model.snapshot!.selectedChat == initialSelection.0 && model.snapshot!.selectedEmbedding == initialSelection.1, "Visibility does not change model selection")
        try expect(try Data(contentsOf: config) == original, "Hierarchical visibility changes remain staged")
        model.discardDraft()

        try installLegacyMask("builtin-standard", document: original)
        await model.refresh()
        try expect(try profile("lfm3-vl").visible && !model.modelIsVisible("lfm3-vl") && model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .off, "Legacy standard mask hides individually true children")
        await model.setVisibility("lfm3-vl", provider: false, visible: true)
        try expect(model.modelIsVisible("lfm3-vl") && !model.modelIsVisible("spark") && !model.modelIsVisible("qwen3-embedding") && model.providerVisibilityState(providerID: "builtin-standard", kind: "chat") == .mixed, "Legacy standard mask enables only requested child")
        model.discardDraft()
        try original.write(to: config)
        await model.refresh()

        try await stageContext(32768)
        try expect(try profile("lfm3-vl").context == 32768, "Draft context is visible")
        try expect(try Data(contentsOf: config) == original, "Editing context does not save")
        model.discardDraft()
        try expect(try profile("lfm3-vl").context == 65536 && !model.hasChanges, "Cancel restores saved 64k")

        try await stageContext(32768)
        let contextApplied = await model.run("apply")
        try expect(contextApplied != nil, "Apply saves 32k")
        try expect(try profile("lfm3-vl").context == 32768 && !model.hasChanges && !model.canApply, "Applied context is clean")
        try expect(model.canReset, "Nonfactory settings can reset")
        let saved32k = try Data(contentsOf: config)
        await model.resetDefaults()
        try expect(try profile("lfm3-vl").context == 65536 && model.hasChanges, "Reset stages factory 64k")
        try expect(try Data(contentsOf: config) == saved32k, "Reset does not save")
        model.discardDraft()
        try expect(try profile("lfm3-vl").context == 32768 && !model.hasChanges, "Cancel after Reset restores saved 32k")

        var encoder = try profile("qwen3-embedding").profile
        encoder["query_prefix"] = .string("Different query: ")
        let encoderStaged = await model.run("save_profile", ["id": .string("qwen3-embedding"), "original_id": .string("qwen3-embedding"), "profile": .object(encoder)])
        try expect(encoderStaged != nil, "Stage changed encoder")
        let build = await model.run("rebuild_index", ["id": .string("qwen3-embedding")])
        try expect(build == nil && model.error != nil, "Do not build saved encoder for changed draft")
        try expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent("model-runtime-executed").path), "Index rejection starts no runtime")
        model.discardDraft()

        let providerID = "provider-" + UUID().uuidString.lowercased()
        let providerStaged = await model.run("save_provider", providerRequest(providerID))
        try expect(providerStaged != nil && model.hasPendingKey(providerID), "Entered key remains pending")
        try expect(try Data(contentsOf: config) == saved32k, "Provider preview leaves registry untouched")
        let credentials = config.deletingLastPathComponent().appendingPathComponent("credentials")
        try expect(!FileManager.default.fileExists(atPath: credentials.path), "Draft key creates no credential file")
        await model.discoverModels(providerID: providerID)
        try expect(model.error == nil && model.providerModels[providerID]?.count == 2, "Discover actual local API models")
        try expect(model.snapshot?.profiles.filter { $0.providerID == providerID }.count == 2, "Discover stages model batch")
        try expect(try Data(contentsOf: config) == saved32k, "Discovered profiles are not saved early")
        model.discardDraft()
        try expect(!model.hasPendingKey(providerID) && !model.hasChanges, "Cancel drops pending key")
        try expect(model.snapshot?.providers.isEmpty == true, "Cancel drops new provider and models")

        let secondStage = await model.run("save_provider", providerRequest(providerID))
        try expect(secondStage != nil, "Restage provider")
        await model.discoverModels(providerID: providerID)
        let applied = await model.run("apply")
        try expect(applied != nil, "Apply pending provider and key")
        try expect(!model.hasChanges && !model.hasPendingKey(providerID) && !model.canApply, "Apply clears draft and pending key")
        var registered = try Data(contentsOf: config)
        try expect(!String(decoding: registered, as: UTF8.self).contains("ONLY_A_TEST_SECRET"), "Registry contains no plaintext key")
        let keyFiles = try FileManager.default.contentsOfDirectory(at: credentials, includingPropertiesForKeys: nil)
        try expect(keyFiles.count == 1, "Apply writes one credential file")
        let attributes = try FileManager.default.attributesOfItem(atPath: keyFiles[0].path)
        try expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "Credential file is private")

        let hiddenID = model.snapshot!.profiles.first { $0.providerID == providerID && $0.text("model_id") == "smoke-chat-one" }!.id
        await model.setVisibility(hiddenID, provider: false, visible: false)
        let hiddenSaved = await model.run("apply")
        try expect(hiddenSaved != nil, "Save an existing hidden API model")
        try Data("3".utf8).write(to: root.appendingPathComponent("api-inventory-size"))
        await model.discoverModels(providerID: providerID)
        try expect(model.error == nil && model.providerModels[providerID]?.count == 3, "Refresh saved provider inventory")
        try expect(!model.hasChanges && !model.canApply, "Reading new inventory does not dirty settings")
        try expect(model.snapshot!.profiles.first { $0.id == hiddenID }?.visible == false, "Inventory refresh preserves existing hidden flag")
        let inventoryDocument = try JSONSerialization.jsonObject(with: Data(contentsOf: config)) as! [String: Any]
        let inventoryProfiles = inventoryDocument["profiles"] as! [String: [String: Any]]
        try expect(inventoryProfiles.values.filter { $0["provider_id"] as? String == providerID }.count == 3, "Refreshed inventory is persisted for the CLI")

        try await stageContext(16384)
        try Data("4".utf8).write(to: root.appendingPathComponent("api-inventory-size"))
        await model.discoverModels(providerID: providerID)
        try expect(model.error == nil && model.hasChanges && (try profile("lfm3-vl").context) == 16384, "Inventory refresh preserves unrelated model draft")
        let mergedDocument = try JSONSerialization.jsonObject(with: Data(contentsOf: config)) as! [String: Any]
        let mergedProfiles = mergedDocument["profiles"] as! [String: [String: Any]]
        try expect(mergedProfiles["lfm3-vl"]!["context"] as? Int == 32768 && mergedProfiles.values.filter { $0["provider_id"] as? String == providerID }.count == 4, "Inventory refresh does not persist unrelated edits")
        model.discardDraft()
        try expect((try profile("lfm3-vl").context) == 32768 && !model.hasChanges, "Cancel retains refreshed inventory baseline")

        try await stageContext(16384)
        let inventoryExternal = Process()
        inventoryExternal.executableURL = control.python
        inventoryExternal.currentDirectoryURL = control.checkout
        inventoryExternal.standardOutput = FileHandle.nullDevice
        inventoryExternal.standardError = FileHandle.nullDevice
        inventoryExternal.arguments = ["-c", "import os,sys;from pathlib import Path;from folder_memory.model_registry import ModelRegistry;r=ModelRegistry(Path(os.environ['HANDY_SETTINGS_TEST_STATE']));p=r.providers()[sys.argv[1]];p['name']='Changed externally';r.save_provider(sys.argv[1],p,replace=True)", providerID]
        try inventoryExternal.run()
        inventoryExternal.waitUntilExit()
        try expect(inventoryExternal.terminationStatus == 0, "Concurrent provider edit succeeds")
        let inventoryConcurrent = try Data(contentsOf: config)
        await model.discoverModels(providerID: providerID)
        try expect(model.error != nil && model.hasChanges && (try profile("lfm3-vl").context) == 16384, "Stale inventory refresh rejects and retains draft")
        try expect(try Data(contentsOf: config) == inventoryConcurrent, "Stale inventory refresh preserves concurrent settings")
        model.discardDraft()
        await model.refresh()
        registered = try Data(contentsOf: config)

        let remoteIDs = model.snapshot!.profiles.filter { $0.providerID == providerID }.map(\.id)
        await model.setVisibility(providerID, provider: true, visible: true)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .on && remoteIDs.allSatisfy { model.modelIsVisible($0) }, "API parent enables every model")
        await model.setVisibility(providerID, provider: true, visible: false)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .off && remoteIDs.allSatisfy { !model.modelIsVisible($0) } && model.snapshot!.profiles.filter { $0.providerID == providerID }.allSatisfy { !$0.visible }, "API parent clears every child checkbox")
        await model.setVisibility(hiddenID, provider: false, visible: true)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .mixed && remoteIDs.filter { model.modelIsVisible($0) } == [hiddenID], "A single API child unmasks a Mixed parent")
        await model.setVisibility(providerID, provider: true, visible: true)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .on, "Mixed API parent becomes fully On")
        await model.setVisibility(hiddenID, provider: false, visible: false)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .mixed, "Unchecking API child makes parent Mixed")
        model.discardDraft()
        try installLegacyMask(providerID, document: registered)
        await model.refresh()
        try expect(model.snapshot!.profiles.filter { $0.providerID == providerID }.allSatisfy(\.visible) && remoteIDs.allSatisfy { !model.modelIsVisible($0) } && model.providerVisibilityState(providerID: providerID, kind: "chat") == .off, "Legacy API mask renders all children Off")
        await model.setVisibility(hiddenID, provider: false, visible: true)
        try expect(model.providerVisibilityState(providerID: providerID, kind: "chat") == .mixed && remoteIDs.filter { model.modelIsVisible($0) } == [hiddenID], "Legacy API unmask keeps sibling models Off")
        model.discardDraft()
        try registered.write(to: config)
        await model.refresh()

        let bulkProviderID = "provider-" + UUID().uuidString.lowercased()
        let bulkProvider = await model.run("save_provider", ["id": .string(bulkProviderID), "provider": .object([
            "name": .string("Second provider"), "base_url": .string(environment["HANDY_SETTINGS_TEST_URL"] ?? ""), "auth": .string("none")])])
        try expect(bulkProvider != nil, "Stage second provider for bulk visibility")
        let bulkModelID = await model.addRemoteModel(providerID: bulkProviderID, remoteModel: RemoteModelRecord(id: "bulk-model", name: nil, context: 4096), kind: "chat")
        try expect(bulkModelID != nil, "Stage model from second provider")
        let bulkEmbeddingID = await model.addRemoteModel(providerID: bulkProviderID, remoteModel: RemoteModelRecord(id: "bulk-embedding", name: nil, context: 4096), kind: "embedding")
        try expect(bulkEmbeddingID != nil, "Stage embedding from shared API provider")
        await model.setVisibility(bulkProviderID, provider: true, visible: false)
        try expect(!model.modelIsVisible(bulkModelID!) && !model.modelIsVisible(bulkEmbeddingID!) && !(try profile(bulkEmbeddingID!).visible), "API parent cascades across chat and embedding")
        await model.setVisibility(bulkProviderID, provider: true, visible: true)
        try expect(model.modelIsVisible(bulkModelID!) && model.modelIsVisible(bulkEmbeddingID!), "API parent reenables every kind")
        let missingStandard = await model.run("delete_profile", ["id": .string("spark")])
        try expect(missingStandard != nil && !model.snapshot!.profiles.contains { $0.id == "spark" }, "Prepare absent standard profile")
        await model.setVisibility("builtin-standard", provider: true, visible: false)
        try expect(model.snapshot!.profiles.contains { $0.id == "spark" && !$0.visible } && !model.modelIsVisible("spark"), "Standard parent includes missing catalogue model")
        await model.setVisibility("builtin-standard", provider: true, visible: true)
        try expect(model.modelIsVisible("spark"), "Standard parent enables missing catalogue model")
        _ = await model.run("delete_profile", ["id": .string("spark")])
        try expect(!model.snapshot!.profiles.contains { $0.id == "spark" }, "Keep catalogue absent before bulk action")
        let beforeBulk = model.snapshot!
        await model.setAllModelsVisibility(false, kind: "chat")
        try expect(model.error == nil && model.snapshot!.profiles.filter { $0.kind == "chat" }.allSatisfy { !$0.visible }, "Bulk hide spans standard and both API providers")
        try expect(model.snapshot!.profiles.contains { $0.id == "spark" && !$0.visible }, "Bulk hide includes absent catalogue model")
        try expect(model.snapshot!.profiles.filter { $0.kind == "embedding" }.map(\.visible) == beforeBulk.profiles.filter { $0.kind == "embedding" }.map(\.visible), "Chat bulk hide preserves embedding visibility")
        try expect(model.snapshot!.profiles.filter { $0.kind == "embedding" }.allSatisfy { model.modelIsVisible($0.id) }, "Chat bulk hide retains effective embedding visibility")
        let runtimePreserved = model.snapshot!.profiles.allSatisfy { current in
            guard let previous = beforeBulk.profiles.first(where: { $0.id == current.id }) ?? beforeBulk.catalogue.first(where: { $0.id == current.id }) else { return false }
            return current.profile.filter { $0.key != "visible" } == previous.profile.filter { $0.key != "visible" }
        }
        try expect(runtimePreserved, "Bulk visibility preserves model settings")
        await model.setVisibility(bulkModelID!, provider: false, visible: true)
        try expect(model.snapshot!.profiles.filter { $0.kind == "chat" && $0.visible }.map(\.id) == [bulkModelID!], "A single model can be reenabled after bulk hide")
        await model.setAllModelsVisibility(true, kind: "chat")
        try expect(model.snapshot!.profiles.filter { $0.kind == "chat" }.allSatisfy { model.modelIsVisible($0.id) }, "Bulk show restores effective chat visibility")
        try expect(try Data(contentsOf: config) == registered, "Bulk visibility stays unsaved before Apply")
        model.discardDraft()
        try expect(!model.hasChanges && model.snapshot!.providers.count == 1, "Cancel drops bulk edits and temporary second provider")
        let chatVisibility = Dictionary(uniqueKeysWithValues: model.snapshot!.profiles.filter { $0.kind == "chat" }.map { ($0.id, $0.visible) })
        await model.setAllModelsVisibility(false, kind: "embedding")
        try expect(model.snapshot!.profiles.filter { $0.kind == "embedding" }.allSatisfy { !$0.visible }, "Embedding bulk hide is supported")
        try expect(Dictionary(uniqueKeysWithValues: model.snapshot!.profiles.filter { $0.kind == "chat" }.map { ($0.id, $0.visible) }) == chatVisibility, "Embedding bulk hide preserves chat models")
        await model.setAllModelsVisibility(true, kind: "embedding")
        try expect(!model.hasChanges && !model.canApply, "Restoring implicit bulk visibility creates no dirty state")

        // Both masks emulate old registries: every raw child is true while its
        // parent hides it. Showing one tab must not reveal the other tab.
        try installLegacyMask("builtin-standard", document: registered)
        let maskedStandard = try Data(contentsOf: config)
        try installLegacyMask(providerID, document: maskedStandard)
        await model.refresh()
        try expect(!model.modelIsVisible("qwen3-embedding") && remoteIDs.allSatisfy { !model.modelIsVisible($0) }, "Bulk fixture starts with effective parent masks")
        let maskedEmbeddingID = await model.addRemoteModel(providerID: providerID, remoteModel: RemoteModelRecord(id: "masked-embedding", name: nil, context: 4096), kind: "embedding")
        try expect(maskedEmbeddingID != nil && !model.modelIsVisible(maskedEmbeddingID!), "New embedding inherits hidden API parent mask")
        await model.setAllModelsVisibility(true, kind: "chat")
        try expect(model.snapshot!.profiles.filter { $0.kind == "chat" }.allSatisfy { model.modelIsVisible($0.id) }, "Bulk show unmasks all chat providers")
        try expect(!model.modelIsVisible("qwen3-embedding") && !(try profile("qwen3-embedding").visible), "Bulk chat show clears masked embedding flags before unmask")
        try expect(!model.modelIsVisible(maskedEmbeddingID!) && !(try profile(maskedEmbeddingID!).visible), "Bulk chat show preserves shared API embedding effective visibility")
        await model.setAllModelsVisibility(false, kind: "embedding")
        try expect(model.snapshot!.profiles.filter { $0.kind == "chat" }.allSatisfy { model.modelIsVisible($0.id) }, "Embedding bulk hide retains effective chat visibility")
        model.discardDraft()
        try registered.write(to: config)
        await model.refresh()

        var firstKey = providerRequest(providerID)
        firstKey["original_id"] = .string(providerID)
        firstKey["api_key"] = .string("FIRST_PENDING_TEST_KEY")
        let secondProvider = "provider-" + UUID().uuidString.lowercased()
        var invalidKey = providerRequest(secondProvider)
        var invalidConnection = invalidKey["provider"]!.object
        invalidConnection["base_url"] = .string("invalid-url")
        invalidKey["provider"] = .object(invalidConnection)
        let firstEditor = await model.run("save_provider", firstKey)
        try expect(firstEditor != nil && model.hasPendingKey(providerID), "First provider editor stages its key")
        let rejectedEditor = await model.run("save_provider", invalidKey)
        try expect(rejectedEditor == nil && model.hasChanges && model.hasPendingKey(providerID) && !model.hasPendingKey(secondProvider), "Invalid second editor preserves first pending key")
        try expect(try Data(contentsOf: config) == registered, "Failed provider edit leaves registry untouched")
        let firstApply = await model.run("apply")
        try expect(firstApply != nil, "First provider remains applicable after rejected second edit")
        let firstPath = model.snapshot!.settings["providers"]!.object[providerID]!.object["api_key_file"]!.text
        try expect(try String(contentsOfFile: firstPath, encoding: .utf8) == "FIRST_PENDING_TEST_KEY", "Rejected editor did not alter first key value")
        let firstSaved = try Data(contentsOf: config)
        _ = await model.run("save_provider", firstKey)
        let secondEditor = await model.run("save_provider", providerRequest(secondProvider))
        try expect(secondEditor != nil && model.hasPendingKey(providerID) && model.hasPendingKey(secondProvider), "Normal provider editors share one draft")
        try expect(try Data(contentsOf: config) == firstSaved, "Several provider edits stay unsaved until Apply")
        model.discardDraft()
        try expect(!model.hasPendingKey(providerID) && !model.hasPendingKey(secondProvider) && !model.hasChanges, "Cancel drops both staged keys")
        _ = await model.run("save_provider", firstKey)
        _ = await model.run("save_provider", providerRequest(secondProvider))
        let providersApplied = await model.run("apply")
        try expect(providersApplied != nil && !model.hasChanges && !model.hasPendingKey(providerID) && !model.hasPendingKey(secondProvider), "General Apply saves several provider edits together")
        for id in [providerID, secondProvider] {
            let path = model.snapshot!.settings["providers"]!.object[id]!.object["api_key_file"]!.text
            let permissions = try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? NSNumber
            try expect(permissions?.intValue == 0o600, "General provider editor writes a private key file")
        }

        // Inject only an asset receipt and a failed preview. All other operations
        // still call the real backend; verified downloads have separate Python
        // tests. This exercises interruption without a network weight download.
        let faultControl = Control(checkout: control.checkout, python: try path("HANDY_SETTINGS_FAULT_BRIDGE"),
                                   stateDirectory: state, toolPath: control.toolPath)
        let assetModel = SettingsModel(control: faultControl)
        await assetModel.refresh()
        let assetProvider = "provider-" + UUID().uuidString.lowercased()
        let assetKey = await assetModel.run("save_provider", providerRequest(assetProvider))
        try expect(assetKey != nil && assetModel.hasPendingKey(assetProvider), "Prepare key before asset failure")
        let failedAsset = await assetModel.run("download", ["id": .string("spark")])
        try expect(failedAsset == nil && assetModel.error != nil, "Asset preview failure is reported")
        try expect(assetModel.hasChanges && assetModel.hasPendingKey(assetProvider), "Asset preview failure retains draft and key")
        try expect(assetModel.snapshot?.providers.contains(where: { $0.id == assetProvider }) == true, "Failed asset retains edited provider in UI")

        try Data("cancel".utf8).write(to: root.appendingPathComponent("asset-fault-mode"))
        let pendingAsset = Task { @MainActor in await assetModel.run("download", ["id": .string("spark")]) }
        let waiting = root.appendingPathComponent("asset-preview-waiting")
        for _ in 0..<100 {
            if FileManager.default.fileExists(atPath: waiting.path) { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try expect(FileManager.default.fileExists(atPath: waiting.path), "Asset preview reaches cancellable boundary")
        assetModel.cancel()
        let cancelledAsset = await pendingAsset.value
        try expect(cancelledAsset == nil && assetModel.hasPendingKey(assetProvider) && assetModel.hasChanges, "Asset cancellation retains draft and key")
        assetModel.discardDraft()
        try expect(!assetModel.hasChanges && !assetModel.hasPendingKey(assetProvider), "Cancel returns to newly saved asset baseline")
        let downloaded = assetModel.snapshot?.profiles.first { $0.id == "spark" }?.text("model") ?? ""
        try expect(downloaded.contains("downloaded-fixture") && FileManager.default.fileExists(atPath: downloaded), "Completed asset survives cancelling settings")

        await model.refresh()
        try expect(model.snapshot?.profiles.first { $0.id == "spark" }?.text("model") == downloaded, "Independent window refresh adopts completed asset")
        await model.setVisibility("spark", provider: false, visible: false)
        let external = Process()
        external.executableURL = control.python
        external.currentDirectoryURL = control.checkout
        external.standardOutput = FileHandle.nullDevice
        external.standardError = FileHandle.nullDevice
        external.arguments = ["-c", "import os;from pathlib import Path;from folder_memory.model_registry import ModelRegistry;r=ModelRegistry(Path(os.environ['HANDY_SETTINGS_TEST_STATE']));p=r.get('lfm3-vl');p['future_option']='external';r.update('lfm3-vl',p)"]
        try external.run()
        external.waitUntilExit()
        try expect(external.terminationStatus == 0, "Independent registry edit succeeds")
        let concurrent = try Data(contentsOf: config)
        let staleApply = await model.run("apply")
        try expect(staleApply == nil && model.hasChanges && model.error != nil, "Stale Apply rejects concurrent edit")
        try expect(try Data(contentsOf: config) == concurrent, "Stale Apply preserves external settings")
        model.discardDraft()
        await model.refresh()
        try expect(!model.hasChanges && model.error == nil && !model.canApply, "Discard and refresh recover after conflict")
        try expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent("model-runtime-executed").path), "No model runtime executed")
        print("Native settings smoke: \(checks) checks passed.")
    }
}
