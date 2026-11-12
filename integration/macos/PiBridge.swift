import AppKit
import Foundation
import SwiftUI
import UserNotifications

struct PiRecord: Identifiable, Sendable {
    let values: [String: SettingValue]
    var id: String { values["id"]?.text ?? "" }
    func text(_ key: String) -> String { values[key]?.text ?? "" }
    var title: String { text("title") }
    var enabled: Bool { values["enabled"]?.flag ?? false }
}

/// Persist only submitted notifications. Permission and delivery failures remain retryable.
struct PiNotificationHistory {
    private(set) var identifiers: [String]?
    init(identifiers: [String]?) { self.identifiers = identifiers }

    mutating func pending(_ completed: [String], enabled: Bool) -> [String] {
        guard identifiers != nil else { record(completed); return [] }
        guard enabled else { record(completed); return [] }
        let known = Set(identifiers ?? [])
        return completed.filter { !known.contains($0) }
    }

    mutating func record(_ delivered: [String]) {
        let new = Set(delivered)
        identifiers = Array((delivered + (identifiers ?? []).filter { !new.contains($0) }).prefix(2000))
    }
}

extension Control {
    func pi(_ command: String, request: [String: SettingValue] = [:]) throws -> [String: SettingValue] {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appendingPathComponent("handy-pi-\(UUID().uuidString)")
        try files.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? files.removeItem(at: directory) }
        let input = directory.appendingPathComponent("input")
        let output = directory.appendingPathComponent("output")
        let errors = directory.appendingPathComponent("errors")
        try JSONEncoder().encode(request).write(to: input)
        try Data().write(to: output)
        try Data().write(to: errors)
        let stdin = try FileHandle(forReadingFrom: input)
        let stdout = try FileHandle(forWritingTo: output)
        let stderr = try FileHandle(forWritingTo: errors)
        defer { try? stdin.close(); try? stdout.close(); try? stderr.close() }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = checkout
        process.arguments = ["-m", "folder_memory.pi_control", "--state-dir", stateDirectory.path, command]
        var environment = ProcessInfo.processInfo.environment
        if !toolPath.isEmpty { environment["PATH"] = toolPath }
        process.environment = environment
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        try OwnedBridgeProcesses.shared.run(process)
        defer { OwnedBridgeProcesses.shared.release(process) }
        let deadline = Date().addingTimeInterval(command == "check-mcp" ? 40 : 30)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.025) }
        if process.isRunning {
            stopOwnedProcess(process)
            throw ControlError(message: "Handy не ответил. Проверь состояние сервиса.")
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let message = String(decoding: try Data(contentsOf: errors), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw ControlError(message: message.isEmpty ? "Не удалось выполнить действие Handy." : String(message.prefix(600)))
        }
        return try JSONDecoder().decode([String: SettingValue].self, from: Data(contentsOf: output))
    }
}

@MainActor
final class PiModel: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    @Published var settings: [String: SettingValue] = [:]
    @Published private(set) var savedSettings: [String: SettingValue] = [:]
    @Published private(set) var mcpServers: [PiRecord] = []
    @Published private(set) var mcpChecks: [PiRecord] = []
    @Published private(set) var mcpErrors: [String] = []
    @Published private(set) var scheduler: [String: SettingValue] = [:]
    @Published private(set) var jobs: [PiRecord] = []
    @Published private(set) var runs: [PiRecord] = []
    @Published private(set) var working = false
    @Published var error: String?
    @Published private(set) var refreshError: String?
    @Published var notice: String?
    @Published private(set) var notificationStatus = ""
    @Published private(set) var notificationsAllowed = false
    private let control: Control?
    private var polling: Task<Void, Never>?
    private var refreshing = false
    private var changeRevision = 0
    private var startedScheduler = false
    private var notificationHistory = PiNotificationHistory(identifiers: nil)
    private var seenKey: String { "handy.pi.seen-runs." + (control?.stateDirectory.path ?? "default") }

    init(control: Control? = try? Control(), poll: Bool = true) {
        self.control = control
        super.init()
        if isAppBundle { UNUserNotificationCenter.current().delegate = self }
        notificationHistory = PiNotificationHistory(identifiers: UserDefaults.standard.stringArray(forKey: seenKey))
        if poll {
            polling = Task { [weak self] in
                while !Task.isCancelled {
                    await self?.refresh()
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                }
            }
        }
    }

    private var isAppBundle: Bool { Bundle.main.bundleURL.pathExtension == "app" }
    var changed: Bool { settings != savedSettings }
    func flag(_ key: String) -> Binding<Bool> {
        Binding(get: { self.settings[key]?.flag ?? false }, set: { self.settings[key] = .bool($0) })
    }
    func text(_ key: String) -> Binding<String> {
        Binding(get: { self.settings[key]?.text ?? "" }, set: { self.settings[key] = .string($0) })
    }

    func refresh() async {
        guard let control, !refreshing, !working else { return }
        refreshing = true
        let revision = changeRevision
        defer { refreshing = false }
        do {
            let data = try await Task.detached { try control.pi("status") }.value
            guard revision == changeRevision else { return }
            let dirty = changed
            savedSettings = data["settings"]?.object ?? [:]
            if !dirty { settings = savedSettings }
            let servers = (data["mcp_servers"]?.array ?? []).map { PiRecord(values: $0.object) }
            if servers.map(\.values) != mcpServers.map(\.values) { clearMCPCheck() }
            mcpServers = servers
            scheduler = data["scheduler"]?.object ?? [:]
            jobs = (data["jobs"]?.array ?? []).map { PiRecord(values: $0.object) }
            runs = (data["runs"]?.array ?? []).map { PiRecord(values: $0.object) }
            if !startedScheduler {
                if savedSettings["scheduler_enabled"]?.flag == true {
                    _ = try await Task.detached { try control.pi("start-scheduler") }.value
                }
                startedScheduler = true
            }
            refreshError = nil
            await publishNewRuns()
        } catch {
            if revision == changeRevision { refreshError = error.localizedDescription }
        }
    }

    @discardableResult
    func perform(_ command: String, request: [String: SettingValue] = [:]) async -> Bool {
        guard let control, !working else { return false }
        changeRevision &+= 1
        working = true
        error = nil
        notice = nil
        do {
            _ = try await Task.detached { try control.pi(command, request: request) }.value
            working = false
            await refresh()
            return true
        } catch {
            working = false
            self.error = error.localizedDescription
            return false
        }
    }

    func mcpProbe(for server: PiRecord) -> PiRecord? {
        mcpChecks.first { $0.text("name") == server.id || $0.text("name") == server.text("name") }
    }

    private func clearMCPCheck() {
        mcpChecks = []
        mcpErrors = []
    }

    func setMCPEnabled(_ server: PiRecord, enabled: Bool) async {
        if await perform("set-mcp-enabled", request: ["id": .string(server.id), "enabled": .bool(enabled)]) {
            // The compatibility browser flag and the server switch represent one setting.
            if server.id == "browser" { settings["browser_enabled"] = savedSettings["browser_enabled"] }
            clearMCPCheck()
        }
    }

    func checkMCP() async {
        guard let control, !working, !changed else { return }
        working = true
        error = nil
        notice = nil
        defer { working = false }
        do {
            let data = try await Task.detached { try control.pi("check-mcp") }.value
            mcpChecks = (data["servers"]?.array ?? []).map { PiRecord(values: $0.object) }
            mcpErrors = (data["errors"]?.array ?? []).map(\.text)
        } catch { self.error = error.localizedDescription }
    }

    func save() async {
        var permissionNotice: String?
        if isAppBundle, settings["notifications_enabled"]?.flag == true && savedSettings["notifications_enabled"]?.flag != true {
            do {
                let allowed = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
                if !allowed { permissionNotice = "Разреши уведомления Handy в Системных настройках macOS." }
            } catch {
                let failure = error as NSError
                if failure.domain == UNErrorDomain && failure.code == UNError.notificationsNotAllowed.rawValue {
                    permissionNotice = "Разреши уведомления Handy в Системных настройках macOS."
                } else { self.error = error.localizedDescription; return }
            }
        }
        let candidate = settings
        if await perform("save-settings", request: candidate) {
            savedSettings = candidate
            settings = candidate
            clearMCPCheck()
            notice = permissionNotice ?? "Настройки сохранены. Новые чаты используют их при запуске."
        }
    }

    func openNotificationSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
    }

    private func updateNotificationStatus(_ authorization: UNNotificationSettings) {
        notificationsAllowed = authorization.authorizationStatus == .authorized
        switch authorization.authorizationStatus {
        case .authorized:
            notificationStatus = authorization.alertSetting == .enabled ? "macOS разрешает уведомления Handy." : "Уведомления разрешены, но баннеры выключены в macOS."
        case .denied: notificationStatus = "Уведомления Handy запрещены в macOS. Включи их в Системных настройках."
        case .notDetermined: notificationStatus = "macOS запросит разрешение при включении уведомлений."
        default: notificationStatus = "Проверь разрешение уведомлений в macOS."
        }
    }

    func testNotifications() async {
        guard isAppBundle else { return }
        let center = UNUserNotificationCenter.current()
        let authorization = await center.notificationSettings()
        updateNotificationStatus(authorization)
        var diagnostic: [String: Any] = ["authorization": authorization.authorizationStatus.rawValue,
            "alerts_enabled": authorization.alertSetting == .enabled,
            "bundle_id": Bundle.main.bundleIdentifier ?? "", "test_submitted": false,
            "test_delivered": false, "updated_at": Date().timeIntervalSince1970]
        if notificationsAllowed {
            let identifier = "handy-notification-test"
            let content = UNMutableNotificationContent()
            content.title = "Handy"
            content.body = "Проверка уведомлений. Новые ответы будут появляться здесь."
            content.sound = .default
            do {
                try await center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: nil))
                diagnostic["test_submitted"] = true
                try? await Task.sleep(nanoseconds: 700_000_000)
                let delivered = await center.deliveredNotifications()
                let visible = delivered.contains { $0.request.identifier == identifier }
                diagnostic["test_delivered"] = visible
                diagnostic["delivered_run_ids"] = delivered.map { $0.request.identifier }.filter { $0.hasPrefix("handy-run-") }
                notice = visible ? "Тестовое уведомление доставлено в Центр уведомлений macOS." : "Запрос отправлен. macOS пока не подтвердила доставку."
            } catch { self.error = error.localizedDescription; diagnostic["error"] = error.localizedDescription }
        } else { notice = "Разреши уведомления Handy в Системных настройках macOS." }
        if let directory = control?.stateDirectory {
            do {
                let data = try JSONSerialization.data(withJSONObject: diagnostic, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: directory.appendingPathComponent("pi-notification-status.json"), options: .atomic)
            } catch { self.error = error.localizedDescription }
        }
    }

    func openChat(_ run: PiRecord) async {
        _ = await perform("open-chat", request: ["session_path": .string(run.text("session_path")), "cwd": .string(run.text("cwd"))])
    }

    private func publishNewRuns() async {
        let completed = runs.filter { ["succeeded", "failed"].contains($0.text("status")) }
        let pending = Set(notificationHistory.pending(completed.map(\.id), enabled: savedSettings["notifications_enabled"]?.flag == true))
        persistNotificationHistory()
        guard isAppBundle else { return }
        let center = UNUserNotificationCenter.current()
        let authorization = await center.notificationSettings()
        updateNotificationStatus(authorization)
        guard notificationsAllowed else { return }
        for run in completed where pending.contains(run.id) {
            let content = UNMutableNotificationContent()
            content.title = run.title.isEmpty ? "Handy" : run.title
            content.subtitle = run.text("status") == "succeeded" ? "Новый ответ в чате" : "Не удалось выполнить задачу"
            content.body = String((run.text("status") == "succeeded" ? run.text("answer") : run.text("error")).prefix(240))
            content.sound = .default
            content.userInfo = ["session_path": run.text("session_path"), "cwd": run.text("cwd")]
            do {
                try await center.add(UNNotificationRequest(identifier: "handy-run-" + run.id, content: content, trigger: nil))
                notificationHistory.record([run.id])
                persistNotificationHistory()
            } catch { self.error = error.localizedDescription }
        }
    }

    private func persistNotificationHistory() {
        if let identifiers = notificationHistory.identifiers {
            UserDefaults.standard.set(identifiers, forKey: seenKey)
        }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                           withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                           withCompletionHandler completionHandler: @escaping () -> Void) {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else { completionHandler(); return }
        let info = response.notification.request.content.userInfo
        guard let session = info["session_path"] as? String, !session.isEmpty,
              let cwd = info["cwd"] as? String, !cwd.isEmpty else { completionHandler(); return }
        Task { @MainActor in
            // Notification clicks must not disappear while another settings action runs.
            if let control = self.control {
                do {
                    _ = try await Task.detached { try control.pi("open-chat", request: ["session_path": .string(session), "cwd": .string(cwd)]) }.value
                } catch { self.error = error.localizedDescription }
            }
            completionHandler()
        }
    }
}
