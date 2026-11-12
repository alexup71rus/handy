import AppKit
import SwiftUI

private struct ProviderChoice: Identifiable {
    let id: String
    let name: String
    let address: String
    let record: ProviderRecord?
    let legacy: ModelRecord?
}

private enum SettingsSheet: Identifiable {
    case model(ModelRecord?, String, String, String?, RemoteModelRecord?)
    case provider(ProviderRecord?)
    var id: String {
        switch self {
        case .model(let record, let kind, let backend, let provider, let remote): return "model-\(record?.id ?? remote?.id ?? provider ?? "new")-\(kind)-\(backend)"
        case .provider(let record): return "provider-\(record?.id ?? "new")"
        }
    }
}

struct SettingsContent: View {
    @ObservedObject var model: SettingsModel
    @State private var kind = "chat"
    @State private var browsedProvider: String?
    @State private var browsedModel: String?
    @State private var expandedProviders: Set<String> = ["builtin-standard"]
    @State private var sheet: SettingsSheet?
    @State private var deletingModel: ModelRecord?
    @State private var deletingProvider: ProviderRecord?
    @State private var downloading: ModelRecord?
    @State private var rebuilding: ModelRecord?
    @State private var knownProviders: Set<String>?

    private var profiles: [ModelRecord] {
        let saved = model.snapshot?.profiles ?? []
        let identifiers = Set(saved.map(\.id))
        return saved + (model.snapshot?.catalogue ?? []).filter { !identifiers.contains($0.id) }
    }
    private var selectedID: String? { kind == "chat" ? model.snapshot?.selectedChat : model.snapshot?.selectedEmbedding }
    private var selected: ModelRecord? { profiles.first { $0.id == selectedID } }
    private func providerID(_ record: ModelRecord) -> String {
        record.backend == "local" ? "builtin-standard" : record.providerID.nilIfEmpty ?? "legacy:" + record.id
    }
    private var providerChoices: [ProviderChoice] {
        var choices = [ProviderChoice(id: "builtin-standard", name: "Стандартные", address: "GGUF · llama.cpp", record: nil, legacy: nil)]
        choices += (model.snapshot?.providers ?? []).sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            .map { ProviderChoice(id: $0.id, name: $0.name, address: $0.baseURL, record: $0, legacy: nil) }
        choices += profiles.filter { $0.backend == "openai" && $0.providerID.isEmpty && $0.kind == kind }.map {
            ProviderChoice(id: "legacy:" + $0.id, name: URL(string: $0.text("base_url"))?.host ?? $0.name,
                           address: $0.text("base_url"), record: nil, legacy: $0)
        }
        return choices
    }
    private var focusedProviderID: String {
        if let browsedProvider, providerChoices.contains(where: { $0.id == browsedProvider }) { return browsedProvider }
        return selected.map(providerID) ?? "builtin-standard"
    }
    private var providerModels: [ModelRecord] { profiles.filter { $0.kind == kind && providerID($0) == focusedProviderID } }
    private var focusedModel: ModelRecord? {
        providerModels.first { $0.id == browsedModel } ?? providerModels.first { $0.id == selectedID } ?? providerModels.first
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Раздел", selection: $kind) {
                Text("Модели").tag("chat")
                Text("Эмбеддинги").tag("embedding")
            }
            .pickerStyle(.segmented).labelsHidden().frame(width: 300)
            .padding(.top, 18).padding(.bottom, 14)
            VStack(alignment: .leading, spacing: 16) {
                selectionHeader
                if model.snapshot == nil {
                    Spacer()
                    if model.working { ProgressView().frame(maxWidth: .infinity) }
                    Spacer()
                } else {
                    providerTree
                    if let record = focusedModel { modelDetails(record) }
                    Spacer(minLength: 0)
                }
            }
            .padding(.horizontal, 22).padding(.bottom, 16)
            Divider()
            footer
        }
        .frame(minWidth: 760, minHeight: 760)
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            if model.snapshot == nil { await model.refresh() }
        }
        .task(id: kind + ":" + (model.snapshot?.revision ?? "") + ":" + (sheet == nil ? "open" : "modal")) {
            guard sheet == nil else { return }
            if let selected { expandedProviders.insert(providerID(selected)) }
            for choice in providerChoices where choice.record != nil && expandedProviders.contains(choice.id) && needsInventory(choice) {
                await model.discoverModels(providerID: choice.id, importForChat: kind == "chat")
            }
        }
        .onChange(of: kind) { _ in browsedProvider = nil; browsedModel = nil }
        .onChange(of: model.snapshot?.providers.map(\.id) ?? []) { identifiers in
            let current = Set(identifiers)
            if let previous = knownProviders, let added = identifiers.first(where: { !previous.contains($0) }) {
                browsedProvider = added
                browsedModel = nil
                expandedProviders.insert(added)
            }
            knownProviders = current
        }
        .sheet(item: $sheet) { item in
            switch item {
            case .model(let record, let kind, let backend, let providerID, let remote):
                ModelEditor(model: model, record: record, kind: kind, backend: backend, providerID: providerID, remoteModel: remote)
            case .provider(let provider): ProviderEditor(model: model, provider: provider)
            }
        }
        .confirmationDialog("Удалить модель?", isPresented: Binding(get: { deletingModel != nil }, set: { if !$0 { deletingModel = nil } }), titleVisibility: .visible, presenting: deletingModel) { record in
            Button("Удалить", role: .destructive) {
                Task { await model.run("delete_profile", ["id": .string(record.id)]) }
                deletingModel = nil
            }
        } message: { _ in Text("Файл модели останется на диске.") }
        .confirmationDialog("Удалить провайдера и его модели?", isPresented: Binding(get: { deletingProvider != nil }, set: { if !$0 { deletingProvider = nil } }), titleVisibility: .visible, presenting: deletingProvider) { provider in
            Button("Удалить", role: .destructive) {
                Task { await model.run("delete_provider", ["id": .string(provider.id)]) }
                deletingProvider = nil
            }
        }
        .confirmationDialog("Скачать модель?", isPresented: Binding(get: { downloading != nil }, set: { if !$0 { downloading = nil } }), titleVisibility: .visible, presenting: downloading) { record in
            Button("Скачать") {
                Task { await model.run("download", ["id": .string(record.id)], title: "Скачиваю \(record.name)…") }
                downloading = nil
            }
        } message: { record in
            Text("\(record.name) · \(byteSize(record.downloadBytes))")
        }
        .confirmationDialog("Пересобрать индекс?", isPresented: Binding(get: { rebuilding != nil }, set: { if !$0 { rebuilding = nil } }), titleVisibility: .visible, presenting: rebuilding) { record in
            Button("Пересобрать") {
                Task { await model.run("rebuild_index", ["id": .string(record.id)], title: "Собираю индекс…") }
                rebuilding = nil
            }
        } message: { record in Text(record.backend == "openai" ? "Технические карточки будут отправлены выбранному API." : "Энкодер обработает технические карточки.") }
    }

    private var selectionHeader: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(selected?.name ?? (kind == "embedding" ? "Без эмбеддингов" : "Модель не выбрана"))
                    .font(.title3.bold())
                if let selected {
                    Text(providerChoices.first { $0.id == providerID(selected) }?.name ?? "API")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if kind == "embedding" {
                Button("Отключить") { Task { await model.selectDraft(nil, kind: "embedding") } }
                    .disabled(selectedID == nil || model.working)
            }
            Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                .help("Обновить").disabled(model.working || model.hasChanges)
        }
    }

    private var treeSelection: Binding<String?> {
        Binding(get: {
            if let record = focusedModel { return "model:" + record.id }
            return "provider:" + focusedProviderID
        }, set: { value in
            guard let value else { return }
            if value.hasPrefix("model:"), let record = profiles.first(where: { $0.id == String(value.dropFirst(6)) }) {
                browsedModel = record.id
                browsedProvider = providerID(record)
            } else if value.hasPrefix("provider:") {
                browsedProvider = String(value.dropFirst(9))
                browsedModel = nil
            }
        })
    }

    private var providerTree: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { present(.provider(nil)) } label: { Label("Добавить провайдера…", systemImage: "plus") }
                Spacer()
                let anyVisible = profiles.contains { $0.kind == kind && model.modelIsVisible($0.id) }
                Button(anyVisible ? "Скрыть все" : "Показать все") {
                    Task { await model.setAllModelsVisibility(!anyVisible, kind: kind) }
                }.help("Все модели выбранной вкладки, во всех провайдерах")
            }.disabled(model.working)
            List(selection: treeSelection) {
                ForEach(providerChoices) { choice in
                    DisclosureGroup(isExpanded: expansion(choice)) {
                        ForEach(models(for: choice)) { record in modelRow(record) }
                        ForEach(candidates(for: choice)) { remote in
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(remote.name ?? remote.id)
                                    if remote.name != nil { Text(remote.id).font(.caption).foregroundStyle(.secondary) }
                                }
                                Spacer()
                                Button("Добавить…") { present(.model(nil, kind, "openai", choice.record?.id, remote)) }
                            }.padding(.vertical, 4)
                        }
                        HStack {
                            Button(choice.id == "builtin-standard" ? "Добавить GGUF…" : "Добавить по ID…") {
                                present(.model(nil, kind, choice.id == "builtin-standard" ? "local" : "openai", choice.record?.id, nil))
                            }
                            .buttonStyle(.link)
                            Spacer()
                            if choice.record != nil {
                                Button("Обновить список") {
                                    Task { await model.discoverModels(providerID: choice.id, importForChat: kind == "chat") }
                                }.buttonStyle(.link)
                            }
                        }.padding(.vertical, 6)
                    } label: {
                        HStack(spacing: 10) {
                            VisibilityCheckbox(state: model.providerVisibilityState(providerID: choice.id, kind: kind),
                                               label: "Показывать \(choice.name) в /models") { value in
                                Task {
                                    if let legacy = choice.legacy { await model.setVisibility(legacy.id, provider: false, visible: value) }
                                    else { await model.setVisibility(choice.id, provider: true, visible: value) }
                                }
                            }.frame(width: 18, height: 18).help("Показывать провайдера в /models")
                            VStack(alignment: .leading, spacing: 3) {
                                Text(choice.name).font(.headline).lineLimit(1)
                                Text(choice.address).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            }
                            Spacer()
                            if let selected, providerID(selected) == choice.id { selectionChip("Выбран") }
                            Text("\(model.providerModelCounts[choice.id] ?? models(for: choice).count)")
                                .font(.caption).foregroundStyle(.secondary)
                            if model.providerModelCounts[choice.id] != nil {
                                Circle().fill(.green).frame(width: 7, height: 7).help("Подключено")
                            }
                            if let provider = choice.record {
                                Button { present(.provider(provider)) } label: { Image(systemName: "gearshape") }
                                    .buttonStyle(.borderless).help("Настройки провайдера")
                                Button { deletingProvider = provider } label: { Image(systemName: "trash") }
                                    .buttonStyle(.borderless).help("Удалить провайдера")
                            }
                        }.padding(.vertical, 7)
                    }
                    .tag("provider:" + choice.id)
                }
            }
            .listStyle(.inset(alternatesRowBackgrounds: false))
            .frame(minHeight: 250, maxHeight: .infinity)
            .disabled(model.working)
        }
    }

    private func models(for choice: ProviderChoice) -> [ModelRecord] {
        profiles.filter { $0.kind == kind && providerID($0) == choice.id }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    private func candidates(for choice: ProviderChoice) -> [RemoteModelRecord] {
        let identifiers = Set(models(for: choice).map { $0.text("model_id") })
        return (model.providerModels[choice.id] ?? []).filter { !identifiers.contains($0.id) }
    }

    private func needsInventory(_ choice: ProviderChoice) -> Bool {
        guard let available = model.providerModels[choice.id] else { return true }
        guard kind == "chat" else { return false }
        let known = Set(models(for: choice).map { $0.text("model_id") })
        return available.contains { !known.contains($0.id) }
    }

    private func expansion(_ choice: ProviderChoice) -> Binding<Bool> {
        Binding(get: { expandedProviders.contains(choice.id) }, set: { expanded in
            if expanded {
                expandedProviders.insert(choice.id)
                browsedProvider = choice.id
                browsedModel = nil
                if choice.record != nil, needsInventory(choice) {
                    Task { await model.discoverModels(providerID: choice.id, importForChat: kind == "chat") }
                }
            } else { expandedProviders.remove(choice.id) }
        })
    }

    private func modelRow(_ record: ModelRecord) -> some View {
        HStack(spacing: 10) {
            VisibilityCheckbox(state: model.modelIsVisible(record.id) ? .on : .off,
                               label: "Показывать \(record.name) в /models") { value in
                Task { await model.setVisibility(record.id, provider: false, visible: value) }
            }.frame(width: 18, height: 18).help("Показывать модель в /models")
            VStack(alignment: .leading, spacing: 3) {
                Text(record.name).lineLimit(1)
                if modelIdentifier(record) != record.name {
                    Text(modelIdentifier(record)).font(.caption).foregroundStyle(.secondary).lineLimit(1).textSelection(.enabled)
                }
            }
            Spacer()
            if selectedID == record.id { selectionChip("Выбрана") }
            if record.downloadable && !record.ready {
                Button("Скачать…") { downloading = record }.controlSize(.small)
            } else if selectedID != record.id {
                Button("Выбрать") {
                    browsedModel = record.id
                    browsedProvider = providerID(record)
                    Task { await model.selectDraft(record.id, kind: kind) }
                }.controlSize(.small).disabled(!record.ready)
            }
            Text(tokenSize(record.context)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.vertical, 5).padding(.leading, 6)
        .tag("model:" + record.id)
    }

    private func modelDetails(_ record: ModelRecord) -> some View {
        HStack {
            Button("Настроить…") { present(.model(record, record.kind, record.backend, record.providerID.nilIfEmpty, nil)) }
            Button(record.backend == "local" ? "Проверить файлы" : "Проверить ответ") {
                Task { await model.run("test_profile", ["id": .string(record.id)], title: "Проверяю…") }
            }.disabled(!record.ready)
            Spacer()
            Menu {
                if record.backend == "local" { Button("Показать файл") { NSWorkspace.shared.selectFile(record.text("model"), inFileViewerRootedAtPath: "") } }
                if record.kind == "embedding" { Button("Пересобрать индекс…") { rebuilding = record }.disabled(!record.ready) }
                if record.category != "standard" && (record.backend == "local" || record.providerID.isEmpty) {
                    Divider()
                    Button("Удалить…", role: .destructive) { deletingModel = record }.disabled(selectedID == record.id)
                }
            } label: { Image(systemName: "ellipsis") }.help("Дополнительно")
        }
        .disabled(model.working)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let busy = model.busy {
                HStack(spacing: 10) {
                    if let progress = model.progress, let total = progress["total"]?.integer, total > 0 {
                        ProgressView(value: min(Double(progress["done"]?.integer ?? 0), Double(total)), total: Double(total)).frame(width: 130)
                        Text("\(byteSize(progress["done"]?.integer ?? 0)) / \(byteSize(total))").font(.caption.monospacedDigit())
                    } else { ProgressView().controlSize(.small) }
                    Text(model.progress?["file"]?.text.nilIfEmpty ?? busy).font(.caption).lineLimit(1)
                    Spacer()
                    if model.canCancel { Button("Отменить операцию", action: model.cancel) }
                }
            }
            if let error = model.error {
                Label(error, systemImage: "exclamationmark.circle").font(.callout).foregroundStyle(.red).textSelection(.enabled)
            } else if let notice = model.notice {
                if notice == "Подключено" { Label(notice, systemImage: "checkmark.circle.fill").foregroundStyle(.green) }
                else { Text(notice).font(.callout).foregroundStyle(.secondary) }
            }
            HStack {
                Button {
                    if let root = model.snapshot?.configRoot { NSWorkspace.shared.open(URL(fileURLWithPath: root, isDirectory: true)) }
                } label: { Image(systemName: "folder") }.help("Открыть конфигурацию")
                Button("Сброс") { Task { await model.resetDefaults() } }
                    .help("Стандартные параметры моделей и видимость")
                    .disabled(model.working || !model.canReset)
                Spacer()
                Button("Отмена", action: model.discardDraft).disabled(model.working || !model.hasChanges)
                Button("Применить") { Task { await model.run("apply", title: "Применяю…") } }
                    .buttonStyle(.borderedProminent).disabled(model.working || !model.canApply)
            }
        }.padding(.horizontal, 20).padding(.vertical, 12)
    }

    private func selectionChip(_ text: String) -> some View {
        Text(text).font(.caption.weight(.medium)).foregroundStyle(.green)
            .padding(.horizontal, 8).padding(.vertical, 3).background(Color.green.opacity(0.14), in: Capsule())
    }
    private func modelIdentifier(_ record: ModelRecord) -> String {
        record.backend == "openai" ? record.text("model_id") : URL(fileURLWithPath: record.text("model")).lastPathComponent
    }
    private func present(_ item: SettingsSheet) { model.clearMessages(); sheet = item }
}

private func tokenSize(_ count: Int) -> String { count > 0 ? "\(count.formatted()) токенов" : "" }
private func byteSize(_ count: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(count), countStyle: .file) }
