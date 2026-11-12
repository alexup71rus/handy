import AppKit
import SwiftUI
import UniformTypeIdentifiers

private struct EditorError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private func trimmed(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespacesAndNewlines)
}

private func checkedID(_ value: String) throws -> String {
    let result = trimmed(value)
    guard result.range(of: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$", options: .regularExpression) != nil else {
        throw EditorError(message: "Для ID используй до 80 латинских букв, цифр, точек, дефисов или подчёркиваний.")
    }
    return result
}

private func integer(_ text: String, name: String, minimum: Int, maximum: Int) throws -> Int {
    guard let result = Int(trimmed(text)), result >= minimum, result <= maximum else {
        throw EditorError(message: "\(name): нужно целое число от \(minimum) до \(maximum).")
    }
    return result
}

private func decimal(_ text: String, name: String, minimum: Double, maximum: Double) throws -> Double {
    guard let result = Double(trimmed(text).replacingOccurrences(of: ",", with: ".")),
          result.isFinite, result >= minimum, result <= maximum else {
        throw EditorError(message: "\(name): нужно число от \(minimum) до \(maximum).")
    }
    return result
}

private struct FileSetting: View {
    let title: String
    @Binding var path: String
    var gguf = false
    var onChoose: ((String) -> Void)? = nil

    var body: some View {
        HStack {
            TextField(title, text: $path)
                .help(path)
            Button("Выбрать…") {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = false
                panel.canChooseFiles = true
                panel.allowsMultipleSelection = false
                panel.message = title
                if gguf { panel.allowedContentTypes = [UTType(filenameExtension: "gguf") ?? .data] }
                if !path.isEmpty {
                    panel.directoryURL = URL(fileURLWithPath: NSString(string: path).expandingTildeInPath).deletingLastPathComponent()
                }
                if panel.runModal() == .OK, let selected = panel.url {
                    path = selected.path
                    onChoose?(selected.path)
                }
            }
        }
    }
}

private struct EditorFeedback: View {
    @ObservedObject var model: SettingsModel
    var validation: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let message = validation ?? model.error {
                Label(message, systemImage: "exclamationmark.circle")
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            } else if let message = model.notice {
                if message == "Подключено" {
                    Label(message, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                } else {
                    Text(message).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            if let busy = model.busy {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(busy)
                    Spacer()
                    if model.canCancel { Button("Отменить", action: model.cancel) }
                }
            }
        }
        .font(.callout)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ProviderEditor: View {
    @ObservedObject var model: SettingsModel
    let provider: ProviderRecord?
    @Environment(\.dismiss) private var dismiss
    @State private var identifier: String
    @State private var name: String
    @State private var baseURL: String
    @State private var auth: String
    @State private var key = ""
    @State private var keyEnvironment: String
    @State private var keyFile: String
    @State private var template = "custom"
    @State private var validation: String?

    init(model: SettingsModel, provider: ProviderRecord?) {
        self.model = model
        self.provider = provider
        _identifier = State(initialValue: provider?.id ?? "provider-" + UUID().uuidString.lowercased())
        _name = State(initialValue: provider?.name ?? "")
        _baseURL = State(initialValue: provider?.baseURL ?? "")
        _auth = State(initialValue: provider?.auth ?? "key")
        _keyEnvironment = State(initialValue: provider?.values["api_key_env"]?.text ?? "")
        _keyFile = State(initialValue: provider?.values["api_key_file"]?.text ?? "")
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(provider == nil ? "Новый провайдер" : "Настройки провайдера").font(.title2.bold())
                Spacer()
            }.padding(20)
            Form {
                if provider == nil {
                    Section {
                        Picker("Шаблон", selection: $template) {
                            Text("Другой OpenAI-совместимый API").tag("custom")
                            Text("OpenAI").tag("openai")
                            Text("Z.AI Coding Plan").tag("zai-coding")
                            Text("Z.AI API").tag("zai-api")
                            Text("Ollama").tag("ollama")
                            Text("LM Studio").tag("lmstudio")
                        }
                        .onChange(of: template) { value in
                            switch value {
                            case "openai": name = "OpenAI"; baseURL = "https://api.openai.com/v1"; auth = "key"
                            case "zai-coding": name = "Z.AI Coding Plan"; baseURL = "https://api.z.ai/api/coding/paas/v4"; auth = "key"
                            case "zai-api": name = "Z.AI"; baseURL = "https://api.z.ai/api/paas/v4"; auth = "key"
                            case "ollama": name = "Ollama"; baseURL = "http://127.0.0.1:11434/v1"; auth = "none"
                            case "lmstudio": name = "LM Studio"; baseURL = "http://127.0.0.1:1234/v1"; auth = "none"
                            default: break
                            }
                        }
                    }
                }
                Section("Подключение") {
                    TextField("Название (необязательно)", text: $name)
                    TextField("Base URL", text: $baseURL)
                        .help("Корень OpenAI-совместимого API, например https://host/v1")
                    Picker("Авторизация", selection: $auth) {
                        Text("Без ключа").tag("none")
                        Text("API-ключ").tag("key")
                        Text("Переменная окружения").tag("env")
                        Text("Приватный файл ключа").tag("file")
                    }
                    if auth == "key" {
                        SecureField(provider == nil || keyFile.isEmpty ? "API-ключ" : "Новый ключ (пусто — сохранить прежний)", text: $key)
                        Text("Ключ хранится в отдельном файле с доступом только для твоего пользователя.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else if auth == "env" {
                        TextField("Имя переменной", text: $keyEnvironment)
                        Text("Приложение должно получить эту переменную при запуске. Для запуска из Finder удобнее сохранить ключ.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else if auth == "file" {
                        FileSetting(title: "Файл ключа", path: $keyFile)
                        Text("Обычный файл текущего пользователя с правами 600.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
            .disabled(model.working)
            Divider()
            VStack(spacing: 12) {
                EditorFeedback(model: model, validation: validation)
                HStack {
                    Button("Проверить доступ") { Task { await test() } }.disabled(model.working)
                    Spacer()
                    Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction).disabled(model.working)
                    Button("Сохранить") { Task { await save() } }
                        .keyboardShortcut(.defaultAction).disabled(model.working)
                }
            }.padding(20)
        }
        .frame(width: 650, height: 560)
        .onAppear { model.clearMessages() }
        .onDisappear { key = "" }
    }

    private func fields() throws -> [String: SettingValue] {
        guard !trimmed(baseURL).isEmpty else {
            throw EditorError(message: "Укажи адрес API.")
        }
        let displayName = trimmed(name).nilIfEmpty ?? URL(string: trimmed(baseURL))?.host ?? "API"
        var value: [String: SettingValue] = ["name": .string(displayName), "base_url": .string(trimmed(baseURL)), "auth": .string(auth)]
        if auth == "env" {
            guard !trimmed(keyEnvironment).isEmpty else { throw EditorError(message: "Укажи имя переменной с ключом.") }
            value["api_key_env"] = .string(trimmed(keyEnvironment))
        } else if auth == "file" {
            guard !trimmed(keyFile).isEmpty else { throw EditorError(message: "Выбери файл с ключом.") }
            value["api_key_file"] = .string(NSString(string: trimmed(keyFile)).expandingTildeInPath)
        } else if auth == "key" {
            guard !trimmed(key).isEmpty || !keyFile.isEmpty || model.hasPendingKey(identifier) else { throw EditorError(message: "Введи API-ключ.") }
            if !keyFile.isEmpty { value["api_key_file"] = .string(keyFile) }
        }
        var fields: [String: SettingValue] = ["provider": .object(value)]
        if auth == "key", !trimmed(key).isEmpty { fields["api_key"] = .string(trimmed(key)) }
        if let provider { fields["original_id"] = .string(provider.id) }
        return fields
    }

    private func test() async {
        validation = nil
        do {
            _ = await model.run("test_provider", try fields(), title: "Проверяю доступ…")
        } catch { validation = error.localizedDescription }
    }

    private func save() async {
        validation = nil
        do {
            var request = try fields()
            request["id"] = .string(try checkedID(identifier))
            if let provider { request["original_id"] = .string(provider.id) }
            if await model.run("save_provider", request) != nil {
                key = ""
                dismiss()
            }
        } catch { validation = error.localizedDescription }
    }
}

struct ModelEditor: View {
    @ObservedObject var model: SettingsModel
    let record: ModelRecord?
    private let backend: String
    @Environment(\.dismiss) private var dismiss
    @State private var kind: String
    @State private var identifier: String
    @State private var name: String
    @State private var modelPath: String
    @State private var binaryPath: String
    @State private var projectorPath: String
    @State private var providerID: String
    @State private var modelID: String
    @State private var baseURL: String
    @State private var auth: String
    @State private var keyEnvironment: String
    @State private var keyFile: String
    @State private var context: String
    @State private var thinking: Bool
    @State private var dimensions: String
    @State private var queryPrefix: String
    @State private var passagePrefix: String
    @State private var pooling: String
    @State private var sendDimensions: Bool
    @State private var temperature: String
    @State private var topP: String
    @State private var topK: String
    @State private var repeatPenalty: String
    @State private var maxOutput: String
    @State private var idleSleep: String
    @State private var idleTerminate: String
    @State private var advanced = false
    @State private var validation: String?

    init(model: SettingsModel, record: ModelRecord?, kind: String, backend: String, providerID: String? = nil, remoteModel: RemoteModelRecord? = nil) {
        self.model = model
        self.record = record
        self.backend = record?.backend ?? backend
        let values = record?.profile ?? [:]
        let actualKind = record?.kind ?? kind
        let isLocal = (record?.backend ?? backend) == "local"
        let remoteName = remoteModel?.name ?? remoteModel?.id ?? ""
        _kind = State(initialValue: actualKind)
        _identifier = State(initialValue: record?.id ?? (isLocal ? "local-" : "api-") + UUID().uuidString.lowercased())
        _name = State(initialValue: record?.name ?? remoteName)
        _modelPath = State(initialValue: values["model"]?.text ?? "")
        _binaryPath = State(initialValue: values["binary"]?.text ?? model.snapshot?.binary ?? "")
        _projectorPath = State(initialValue: values["mmproj"]?.text ?? "")
        _providerID = State(initialValue: record?.providerID ?? providerID ?? model.snapshot?.providers.first?.id ?? "")
        _modelID = State(initialValue: values["model_id"]?.text ?? remoteModel?.id ?? "")
        _baseURL = State(initialValue: values["base_url"]?.text ?? "")
        _auth = State(initialValue: values["api_key_env"] == nil ? (values["api_key_file"] == nil ? "none" : "file") : "env")
        _keyEnvironment = State(initialValue: values["api_key_env"]?.text ?? "")
        _keyFile = State(initialValue: values["api_key_file"]?.text ?? "")
        _context = State(initialValue: String(values["context"]?.integer ?? remoteModel?.context ?? (actualKind == "chat" ? 65536 : (isLocal ? 512 : 8192))))
        _thinking = State(initialValue: values["enable_thinking"]?.flag ?? false)
        _dimensions = State(initialValue: String(values["dimensions"]?.integer ?? 1024))
        _queryPrefix = State(initialValue: values["query_prefix"]?.text ?? "")
        _passagePrefix = State(initialValue: values["passage_prefix"]?.text ?? "")
        _pooling = State(initialValue: values["pooling"]?.text ?? "mean")
        _sendDimensions = State(initialValue: values["send_dimensions"]?.flag ?? false)
        let sampling = values["sampling"]?.object ?? [:]
        func numberString(_ value: SettingValue?, fallback: String) -> String {
            if case .number(let number) = value {
                if number.rounded() == number, let integer = value?.integer { return String(integer) }
                return String(number)
            }
            return fallback
        }
        _temperature = State(initialValue: numberString(sampling["temperature"], fallback: "0.3"))
        _topP = State(initialValue: numberString(sampling["top_p"], fallback: "0.95"))
        _topK = State(initialValue: numberString(sampling["top_k"], fallback: "40"))
        _repeatPenalty = State(initialValue: numberString(sampling["repeat_penalty"], fallback: "1.1"))
        _maxOutput = State(initialValue: String(values["max_output_tokens"]?.integer ?? 1536))
        _idleSleep = State(initialValue: String(values["idle_sleep_seconds"]?.integer ?? 60))
        _idleTerminate = State(initialValue: String(values["idle_terminate_seconds"]?.integer ?? (actualKind == "chat" ? 120 : 30)))
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(record == nil ? "Добавить модель" : "Настройки модели").font(.title2.bold())
                Spacer()
                Text(backend == "local" ? "GGUF · llama.cpp" : "OpenAI-совместимый API").foregroundStyle(.secondary)
            }.padding(20)
            Form {
                Section("Модель") {
                    TextField("Название (необязательно)", text: $name)
                    Picker("Назначение", selection: $kind) {
                        Text("Основная модель").tag("chat")
                        Text("Эмбеддинги").tag("embedding")
                    }
                    .disabled(record != nil)
                    .onChange(of: kind) { value in
                        guard record == nil else { return }
                        context = value == "chat" ? "65536" : (backend == "local" ? "512" : "8192")
                    }
                    if backend == "local" {
                        FileSetting(title: "Файл модели GGUF", path: $modelPath, gguf: true, onChoose: modelChosen)
                        FileSetting(title: "llama-server", path: $binaryPath)
                        if kind == "chat" { FileSetting(title: "Vision-проектор (необязательно)", path: $projectorPath, gguf: true) }
                    } else {
                        Picker("Провайдер", selection: $providerID) {
                            Text("Отдельный профиль").tag("")
                            ForEach(model.snapshot?.providers ?? []) { Text($0.name).tag($0.id) }
                        }
                        if providerID.isEmpty {
                            TextField("Base URL", text: $baseURL)
                            Picker("Авторизация", selection: $auth) {
                                Text("Без ключа").tag("none")
                                Text("Переменная окружения").tag("env")
                                Text("Приватный файл ключа").tag("file")
                            }
                            if auth == "env" { TextField("Имя переменной с ключом", text: $keyEnvironment) }
                            if auth == "file" { FileSetting(title: "Файл ключа", path: $keyFile) }
                            Text("Для общего ключа и списка моделей добавь подключение в разделе «Провайдеры».")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        TextField("Модель (ID)", text: $modelID)
                    }
                    TextField("Контекст, токенов", text: $context)
                        .help("Общий предел сообщений, инструментов и ответа. Например, 32768 для 32k или 16384 для 16k.")
                    if backend == "local", kind == "chat" {
                        Toggle("Рассуждения, если поддерживаются моделью", isOn: $thinking)
                    }
                }
                if kind == "embedding" {
                    Section("Векторный поиск") {
                        TextField("Размерность вектора", text: $dimensions)
                        TextField("Префикс запроса", text: $queryPrefix, axis: .vertical).lineLimit(1...4)
                        TextField("Префикс документа", text: $passagePrefix, axis: .vertical).lineLimit(1...4)
                        if backend == "local" {
                            Picker("Pooling", selection: $pooling) {
                                Text("mean").tag("mean"); Text("last").tag("last"); Text("cls").tag("cls")
                            }
                        } else {
                            Toggle("Передавать размерность в API", isOn: $sendDimensions)
                        }
                        Text("После смены энкодера или его префиксов пересобери индекс. Чужие векторы использоваться не будут.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                DisclosureGroup("Дополнительно", isExpanded: $advanced) {
                    if kind == "chat" {
                        TextField("Temperature", text: $temperature)
                        TextField("Top P", text: $topP)
                        if backend == "local" {
                            TextField("Top K (−1 — без ограничения)", text: $topK)
                            TextField("Repeat penalty", text: $repeatPenalty)
                        }
                        TextField("Максимум токенов ответа", text: $maxOutput)
                    }
                    if backend == "local" {
                        TextField("Сон после простоя, секунд (−1 — выключить)", text: $idleSleep)
                        TextField("Выгрузка после простоя, секунд (0 — выключить)", text: $idleTerminate)
                    }
                }
            }
            .formStyle(.grouped)
            .disabled(model.working)
            Divider()
            VStack(spacing: 12) {
                EditorFeedback(model: model, validation: validation)
                HStack {
                    Spacer()
                    Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction).disabled(model.working)
                    Button("Сохранить") { Task { await save() } }.keyboardShortcut(.defaultAction).disabled(model.working)
                }
            }.padding(20)
        }
        .frame(width: 700, height: 710)
        .onAppear { model.clearMessages() }
    }

    private func profile() throws -> [String: SettingValue] {
        let sourceName = backend == "local" ? URL(fileURLWithPath: trimmed(modelPath)).deletingPathExtension().lastPathComponent : trimmed(modelID)
        let displayName = trimmed(name).nilIfEmpty ?? sourceName.nilIfEmpty ?? "Модель"
        let contextValue = try integer(context, name: "Контекст", minimum: kind == "chat" ? 512 : 32,
                                       maximum: backend == "local" ? (kind == "chat" ? 65536 : 4096) : (kind == "chat" ? 2_147_483_647 : 32768))
        var value = record?.profile ?? [:]
        value["name"] = .string(displayName)
        value["kind"] = .string(kind)
        value["backend"] = .string(backend)
        value["enabled"] = value["enabled"] ?? .bool(true)
        value["context"] = .number(Double(contextValue))
        if backend == "local" {
            for (field, path, label) in [("model", modelPath, "GGUF-модель"), ("binary", binaryPath, "llama-server")] {
                let expanded = NSString(string: trimmed(path)).expandingTildeInPath
                var directory = ObjCBool(false)
                guard expanded.hasPrefix("/"), FileManager.default.fileExists(atPath: expanded, isDirectory: &directory), !directory.boolValue else {
                    throw EditorError(message: "\(label): выбери существующий файл по абсолютному пути.")
                }
                if field == "binary", !FileManager.default.isExecutableFile(atPath: expanded) {
                    throw EditorError(message: "У выбранного llama-server нет разрешения на запуск.")
                }
                value[field] = .string(expanded)
            }
            guard trimmed(modelPath).lowercased().hasSuffix(".gguf") else { throw EditorError(message: "Модель должна быть файлом .gguf.") }
            if kind == "chat" {
                if trimmed(projectorPath).isEmpty { value.removeValue(forKey: "mmproj") }
                else {
                    let path = NSString(string: trimmed(projectorPath)).expandingTildeInPath
                    guard path.hasPrefix("/"), FileManager.default.isReadableFile(atPath: path) else { throw EditorError(message: "Файл vision-проектора не найден.") }
                    value["mmproj"] = .string(path)
                }
                if record == nil || value["enable_thinking"] != nil || thinking {
                    value["enable_thinking"] = .bool(thinking)
                }
            }
            if record == nil { value["port"] = value["port"] ?? .number(kind == "chat" ? 18747 : 18749) }
            let sleep = try integer(idleSleep, name: "Простой до сна", minimum: -1, maximum: 86400)
            guard sleep != 0 else { throw EditorError(message: "Для сна укажи число секунд больше нуля или −1, чтобы выключить его.") }
            if record == nil || value["idle_sleep_seconds"] != nil || sleep != 60 {
                value["idle_sleep_seconds"] = .number(Double(sleep))
            }
            let terminate = try integer(idleTerminate, name: "Простой до выгрузки", minimum: 0, maximum: 86400)
            if record == nil || value["idle_terminate_seconds"] != nil || terminate != (kind == "chat" ? 120 : 30) {
                value["idle_terminate_seconds"] = .number(Double(terminate))
            }
        } else {
            guard !trimmed(modelID).isEmpty else { throw EditorError(message: "Укажи точный ID модели у провайдера.") }
            value["model_id"] = .string(trimmed(modelID))
            if !providerID.isEmpty {
                value["provider_id"] = .string(providerID)
            } else {
                guard !trimmed(baseURL).isEmpty else { throw EditorError(message: "Выбери провайдера или укажи адрес API.") }
                value.removeValue(forKey: "provider_id")
                value["base_url"] = .string(trimmed(baseURL))
                value.removeValue(forKey: "api_key_env")
                value.removeValue(forKey: "api_key_file")
                if auth == "env" { value["api_key_env"] = .string(trimmed(keyEnvironment)) }
                if auth == "file" { value["api_key_file"] = .string(NSString(string: trimmed(keyFile)).expandingTildeInPath) }
            }
        }
        if kind == "embedding" {
            guard queryPrefix.unicodeScalars.count <= 1000, passagePrefix.unicodeScalars.count <= 1000 else {
                throw EditorError(message: "Каждый префикс может содержать не больше 1000 символов.")
            }
            value["embedding"] = .bool(true)
            value["dimensions"] = .number(Double(try integer(dimensions, name: "Размерность", minimum: 1, maximum: 65536)))
            value["query_prefix"] = .string(queryPrefix)
            value["passage_prefix"] = .string(passagePrefix)
            if record == nil { value["max_input_chars"] = value["max_input_chars"] ?? .number(1200) }
            if backend == "local" {
                value["pooling"] = .string(pooling)
                if record == nil {
                    value["batch_tokens"] = value["batch_tokens"] ?? .number(512)
                    value["micro_batch_tokens"] = value["micro_batch_tokens"] ?? .number(512)
                }
            } else { value["send_dimensions"] = .bool(sendDimensions) }
        } else {
            var sampling = value["sampling"]?.object ?? [:]
            func setSampling(_ key: String, _ number: Double, defaultValue: Double) {
                if record == nil || sampling[key] != nil || number != defaultValue { sampling[key] = .number(number) }
            }
            setSampling("temperature", try decimal(temperature, name: "Temperature", minimum: 0, maximum: 2), defaultValue: 0.3)
            setSampling("top_p", try decimal(topP, name: "Top P", minimum: 0.0001, maximum: 1), defaultValue: 0.95)
            if backend == "local" {
                let k = try integer(topK, name: "Top K", minimum: -1, maximum: 1_000_000)
                guard k != 0 else { throw EditorError(message: "Для Top K укажи положительное число или −1.") }
                setSampling("top_k", Double(k), defaultValue: 40)
                setSampling("repeat_penalty", try decimal(repeatPenalty, name: "Repeat penalty", minimum: 0.01, maximum: 3), defaultValue: 1.1)
            }
            if record == nil || value["sampling"] != nil || !sampling.isEmpty { value["sampling"] = .object(sampling) }
            let output = try integer(maxOutput, name: "Максимум токенов ответа", minimum: 1, maximum: 2_147_483_647)
            if record == nil || value["max_output_tokens"] != nil || output != 1536 {
                guard output <= contextValue else { throw EditorError(message: "Максимум токенов ответа должен помещаться в контекст модели.") }
                value["max_output_tokens"] = .number(Double(output))
            }
        }
        return value
    }

    private func modelChosen(_ path: String) {
        guard record == nil else { return }
        let filename = URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
        if trimmed(name).isEmpty { name = filename }
    }

    private func save() async {
        validation = nil
        do {
            var request: [String: SettingValue] = ["id": .string(try checkedID(identifier)), "profile": .object(try profile())]
            if let record { request["original_id"] = .string(record.id) }
            if await model.run("save_profile", request) != nil { dismiss() }
        } catch { validation = error.localizedDescription }
    }
}
