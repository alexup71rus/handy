import AppKit
import SwiftUI

enum HandySettingsTab: Hashable {
    case models, terminal, mcp, schedules
}

@MainActor
final class HandySettingsNavigation: ObservableObject {
    @Published var tab: HandySettingsTab = .models
}

struct HandySettingsContent: View {
    @ObservedObject var models: SettingsModel
    @ObservedObject var pi: PiModel
    @ObservedObject var navigation: HandySettingsNavigation

    var body: some View {
        TabView(selection: $navigation.tab) {
            SettingsContent(model: models).tabItem { Label("Модели", systemImage: "cpu") }.tag(HandySettingsTab.models)
            PiSettingsContent(model: pi).tabItem { Label("Терминал", systemImage: "terminal") }.tag(HandySettingsTab.terminal)
            PiMCPContent(model: pi).tabItem { Label("MCP", systemImage: "puzzlepiece.extension") }.tag(HandySettingsTab.mcp)
            PiSchedulesContent(model: pi).tabItem { Label("Планировщик", systemImage: "calendar.badge.clock") }.tag(HandySettingsTab.schedules)
        }
        .padding(.top, 8)
        .frame(minWidth: 820, minHeight: 690)
    }
}

private struct PiMessages: View {
    @ObservedObject var model: PiModel
    var body: some View {
        if let error = model.error ?? model.refreshError { Text(error).foregroundStyle(.red).textSelection(.enabled) }
        if let notice = model.notice { Text(notice).foregroundStyle(.secondary).textSelection(.enabled) }
    }
}

struct PiSettingsContent: View {
    @ObservedObject var model: PiModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Form {
                Section("Терминал") {
                    Picker("Оформление", selection: model.text("theme")) {
                        Text("Как в системе").tag("system")
                        Text("Тёмное").tag("dark")
                        Text("Светлое").tag("light")
                    }
                    Picker("Рассуждения", selection: model.text("thinking_level")) {
                        Text("Выключены").tag("off")
                        Text("Минимальные").tag("minimal")
                        Text("Короткие").tag("low")
                        Text("Средние").tag("medium")
                        Text("Подробные").tag("high")
                        Text("Очень подробные").tag("xhigh")
                        Text("Максимальные").tag("max")
                    }
                    Text("Доступный уровень рассуждений зависит от модели.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Уведомления") {
                    Toggle("Уведомлять о новых ответах по расписанию", isOn: model.flag("notifications_enabled"))
                    Text("Нажатие открывает чат в терминале. Handy должен оставаться открытым.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !model.notificationStatus.isEmpty {
                        Text(model.notificationStatus).font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button("Настройки уведомлений macOS") { model.openNotificationSettings() }
                        Spacer()
                        Button("Проверить уведомление") { Task { await model.testNotifications() } }
                            .disabled(!model.notificationsAllowed)
                    }
                }
            }
            .formStyle(.grouped)
            .disabled(model.working)
            PiMessages(model: model).font(.caption)
            PiSettingsActions(model: model)
        }
        .padding(24)
        .task { await model.refresh() }
    }
}

private struct PiSettingsActions: View {
    @ObservedObject var model: PiModel
    var body: some View {
        HStack {
            Button("Отменить изменения") { model.settings = model.savedSettings }
                .disabled(!model.changed || model.working)
            Spacer()
            if model.working { ProgressView().controlSize(.small) }
            Button("Применить") { Task { await model.save() } }
                .buttonStyle(.borderedProminent).disabled(!model.changed || model.working)
        }
    }
}

struct PiMCPContent: View {
    @ObservedObject var model: PiModel
    @State private var selectedID: String?
    private var selected: PiRecord? { model.mcpServers.first { $0.id == selectedID } ?? model.mcpServers.first }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("MCP-серверы").font(.title2.bold())
                Spacer()
                Button("Проверить подключение") { Task { await model.checkMCP() } }
                    .disabled(model.working || model.changed)
            }
            HStack(alignment: .top, spacing: 18) {
                List(selection: $selectedID) {
                    ForEach(model.mcpServers) { server in
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(server.text("name")).font(.headline)
                                Text(server.enabled ? "Включён" : "Выключен")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Toggle("Включить \(server.text("name"))", isOn: Binding(get: { server.enabled }, set: { value in
                                Task { await model.setMCPEnabled(server, enabled: value) }
                            })).labelsHidden().toggleStyle(.switch).controlSize(.small).disabled(model.working)
                        }
                        .padding(.vertical, 6).tag(server.id)
                    }
                }.frame(width: 255)
                if let server = selected {
                    Form {
                        Section(server.text("name")) {
                            LabeledContent("Подключение", value: server.text("transport").uppercased())
                            if !server.text("detail").isEmpty {
                                Text(server.text("detail")).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                            if let probe = model.mcpProbe(for: server) {
                                LabeledContent("Последняя проверка", value: probeLabel(probe))
                                LabeledContent("Инструментов", value: String(probe.values["tools"]?.array.count ?? 0))
                                if !probe.text("exposure").isEmpty {
                                    LabeledContent("Доступ модели", value: exposureLabel(probe.text("exposure")))
                                }
                                if !probe.text("error").isEmpty {
                                    Text(probe.text("error")).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                                }
                            } else {
                                Text("Подключение ещё не проверено").foregroundStyle(.secondary)
                            }
                        }
                        if server.id == "handy" {
                            Section("Инструменты Handy") {
                                Toggle("Артефакты", isOn: model.flag("artifacts_enabled"))
                                Toggle("Память", isOn: model.flag("memory_enabled"))
                                Toggle("Справка и RAG", isOn: model.flag("rag_enabled"))
                                Toggle("Планировщик", isOn: model.flag("scheduler_enabled"))
                            }
                        } else if server.id == "browser" {
                            Section("Chrome") {
                                TextField("Адрес отладки", text: model.text("browser_cdp_url"))
                                Text("Пустой адрес — отдельный Chrome для Handy.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }.formStyle(.grouped).disabled(model.working)
                } else {
                    Text("Нет настроенных MCP-серверов").foregroundStyle(.secondary).frame(maxWidth: .infinity)
                }
            }
            Text("Переключатели серверов сохраняются сразу. Параметры применяются к новым чатам.")
                .font(.caption).foregroundStyle(.secondary)
            if !model.mcpErrors.isEmpty {
                Text(model.mcpErrors.joined(separator: "\n")).font(.caption).foregroundStyle(.red).textSelection(.enabled)
            }
            PiMessages(model: model).font(.caption)
            PiSettingsActions(model: model)
        }.padding(24)
        .onAppear { if selectedID == nil { selectedID = model.mcpServers.first?.id } }
        .onChange(of: model.mcpServers.map(\.id)) { ids in
            if selectedID == nil || !ids.contains(selectedID ?? "") { selectedID = ids.first }
        }
        .task { await model.refresh() }
    }

    private func exposureLabel(_ value: String) -> String {
        switch value {
        case "direct": return "Напрямую"
        case "deferred": return "По запросу"
        case "codemode": return "Через код"
        case "hidden": return "Скрыт"
        default: return value
        }
    }

    private func probeLabel(_ probe: PiRecord) -> String {
        switch probe.text("state") {
        case "connected", "ready": return "Доступен"
        case "disabled": return "Выключен"
        case "error", "failed": return "Ошибка подключения"
        case "needs-auth": return "Нужна авторизация"
        case "connecting": return "Подключается"
        case "disconnected", "closed": return "Отключён"
        default: return probe.text("state")
        }
    }
}

private enum PiScheduleDeletion {
    case job(PiRecord), result(PiRecord)

    var record: PiRecord { switch self { case .job(let value), .result(let value): return value } }
    var title: String { switch self { case .job: return "Удалить задачу?"; case .result: return "Удалить результат из истории?" } }
    var message: String {
        switch self {
        case .job: return "«\(record.title)» больше не будет запускаться. Созданные чаты и результаты останутся."
        case .result: return "«\(record.title)» исчезнет из истории планировщика. Сам чат pi останется."
        }
    }
    var command: String { switch self { case .job: return "delete-job"; case .result: return "delete-run" } }
    var request: [String: SettingValue] {
        switch self {
        case .job: return ["job_id": .string(record.id)]
        case .result: return ["run_id": .string(record.id)]
        }
    }
}

struct PiSchedulesContent: View {
    @ObservedObject var model: PiModel
    @State private var adding = false
    @State private var editing: PiRecord?
    @State private var deleting: PiScheduleDeletion?
    @State private var selectedRun: PiRecord?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Планировщик").font(.title2.bold())
                Spacer()
                Circle().fill(model.scheduler["running"]?.flag == true ? .green : .secondary).frame(width: 7, height: 7)
                Text(model.scheduler["running"]?.flag == true ? "Работает" : "Остановлен").font(.caption).foregroundStyle(.secondary)
                Button(model.scheduler["running"]?.flag == true ? "Остановить" : "Запустить") {
                    Task { _ = await model.perform(model.scheduler["running"]?.flag == true ? "stop-scheduler" : "start-scheduler") }
                }.disabled(model.working)
                Button("Новая задача", systemImage: "plus") { adding = true }
            }
            Text("Запуск по дате или через интервал. Каждый раз создаётся отдельный чат pi; задачи выполняются, пока Handy и компьютер работают.")
                .foregroundStyle(.secondary)
            List {
                Section("Задачи") {
                    if model.jobs.isEmpty { Text("Пока нет задач").foregroundStyle(.secondary) }
                    ForEach(model.jobs) { job in
                        HStack(spacing: 12) {
                            Toggle("Включена", isOn: Binding(get: { job.enabled }, set: { enabled in
                                Task { _ = await model.perform("update-job", request: ["job_id": .string(job.id), "enabled": .bool(enabled)]) }
                            })).labelsHidden()
                            VStack(alignment: .leading, spacing: 4) {
                                Text(job.title).font(.headline)
                                Text(job.text("prompt")).lineLimit(2).foregroundStyle(.secondary)
                                Text(scheduleLabel(job)).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Изменить") { editing = job }
                            Button { deleting = .job(job) } label: { Image(systemName: "trash") }.help("Удалить задачу")
                        }.padding(.vertical, 6)
                    }
                }
                Section("Результаты") {
                    if model.runs.isEmpty { Text("Здесь появятся ответы на выполненные задачи").foregroundStyle(.secondary) }
                    ForEach(model.runs) { run in
                        HStack(spacing: 12) {
                            Image(systemName: run.text("status") == "succeeded" ? "checkmark.circle.fill" : run.text("status") == "failed" ? "exclamationmark.circle.fill" : "clock")
                                .foregroundStyle(run.text("status") == "failed" ? Color.orange : Color.secondary)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(run.title).font(.headline)
                                Text(run.text("status") == "failed" ? run.text("error") : run.text("answer"))
                                    .lineLimit(2).foregroundStyle(.secondary)
                                if let timestamp = run.values["completed_at"]?.integer {
                                    Text(Date(timeIntervalSince1970: Double(timestamp)), style: .date).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Button("Подробнее") { selectedRun = run }
                            Button("Открыть чат") { Task { await model.openChat(run) } }
                                .disabled(run.text("session_path").isEmpty || run.text("status") == "running")
                            Button { deleting = .result(run) } label: { Image(systemName: "trash") }
                                .help("Удалить из истории").disabled(run.text("status") == "running")
                        }.padding(.vertical, 6)
                    }
                }
            }
            .disabled(model.working)
            PiMessages(model: model).font(.caption)
            HStack {
                Text("Для уведомлений приложение должно оставаться открытым.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Обновить") { Task { await model.refresh() } }.disabled(model.working)
            }
        }
        .padding(24)
        .sheet(isPresented: $adding) { PiJobEditor(model: model, job: nil) }
        .sheet(item: $editing) { job in PiJobEditor(model: model, job: job) }
        .sheet(item: $selectedRun) { run in
            VStack(alignment: .leading, spacing: 16) {
                Text(run.title).font(.title2)
                ScrollView { Text(run.text("status") == "failed" ? run.text("error") : run.text("answer")).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                HStack {
                    Button("Закрыть") { selectedRun = nil }
                    Spacer()
                    Button("Открыть чат") { Task { await model.openChat(run) } }.disabled(run.text("session_path").isEmpty)
                }
            }.padding(24).frame(width: 650, height: 450)
        }
        .confirmationDialog(deleting?.title ?? "Удалить?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible, presenting: deleting) { target in
            Button("Удалить", role: .destructive) {
                Task { _ = await model.perform(target.command, request: target.request) }
                deleting = nil
            }
        } message: { target in Text(target.message) }
    }

    private func scheduleLabel(_ job: PiRecord) -> String {
        if case .number(let seconds) = job.values["interval_seconds"], seconds > 0 {
            let interval = PiScheduleInterval(seconds: seconds)
            let suffix = interval.unit == .hours ? "ч" : interval.unit == .minutes ? "мин" : "с"
            let cadence = "Каждые \(interval.count.formatted()) \(suffix)"
            return cadence + " · " + job.text("cwd")
        }
        if let next = (job.values["next_run"] ?? job.values["run_at"])?.integer {
            return Date(timeIntervalSince1970: Double(next)).formatted(date: .abbreviated, time: .shortened) + " · " + job.text("cwd")
        }
        return job.text("cwd")
    }
}

struct PiScheduleInterval {
    enum Unit: Int { case seconds = 1, minutes = 60, hours = 3600 }
    var count: Double
    var unit: Unit

    init(seconds: Double = 3600) {
        if seconds.truncatingRemainder(dividingBy: 3600) == 0 { unit = .hours }
        else if seconds.truncatingRemainder(dividingBy: 60) == 0 { unit = .minutes }
        else { unit = .seconds }
        count = seconds / Double(unit.rawValue)
    }

    var seconds: Double { count * Double(unit.rawValue) }
    var isValid: Bool { seconds.isFinite && (1...Double(366 * 86400)).contains(seconds) }
}

struct PiJobEditor: View {
    @ObservedObject var model: PiModel
    let job: PiRecord?
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var prompt = ""
    @State private var cwd = NSHomeDirectory()
    @State private var repeated = false
    @State private var interval = PiScheduleInterval()
    @State private var runAt = Date().addingTimeInterval(300)

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(job == nil ? "Новая задача" : "Изменить задачу").font(.title2.bold())
            TextField("Название", text: $title)
            Text("Задание для модели").font(.headline)
            TextEditor(text: $prompt).font(.body).frame(minHeight: 120).border(.quaternary)
            HStack {
                TextField("Рабочая папка", text: $cwd)
                Button("Выбрать…") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.allowsMultipleSelection = false
                    if panel.runModal() == .OK, let url = panel.url { cwd = url.path }
                }
            }
            Toggle("Повторять", isOn: $repeated)
            if repeated {
                HStack {
                    TextField("Интервал", value: $interval.count, format: .number)
                    Picker("Единицы", selection: $interval.unit) {
                        Text("Секунды").tag(PiScheduleInterval.Unit.seconds)
                        Text("Минуты").tag(PiScheduleInterval.Unit.minutes)
                        Text("Часы").tag(PiScheduleInterval.Unit.hours)
                    }.labelsHidden()
                }
            } else { DatePicker("Запустить", selection: $runAt, in: Date()..., displayedComponents: [.date, .hourAndMinute]) }
            if let error = model.error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Сохранить") { Task { await save() } }.buttonStyle(.borderedProminent)
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !cwd.hasPrefix("/") || model.working || (repeated && !interval.isValid))
            }
        }
        .padding(24).frame(width: 600)
        .onAppear {
            if let job {
                title = job.title; prompt = job.text("prompt"); cwd = job.text("cwd")
                if case .number(let seconds) = job.values["interval_seconds"], seconds > 0 {
                    repeated = true; interval = PiScheduleInterval(seconds: seconds)
                }
                if let date = (job.values["next_run"] ?? job.values["run_at"])?.integer { runAt = max(Date(), Date(timeIntervalSince1970: Double(date))) }
            }
        }
    }

    private func save() async {
        var request: [String: SettingValue] = ["title": .string(title), "prompt": .string(prompt), "cwd": .string(cwd)]
        request["run_at"] = repeated ? .null : .number(runAt.timeIntervalSince1970)
        guard !repeated || interval.isValid else { return }
        request["interval_seconds"] = repeated ? .number(interval.seconds) : .null
        if let job { request["job_id"] = .string(job.id) }
        if await model.perform(job == nil ? "create-job" : "update-job", request: request) { dismiss() }
    }
}
