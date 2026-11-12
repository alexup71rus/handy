import AppKit
import ServiceManagement
import SwiftUI

@MainActor
private final class MenuModel: ObservableObject {
    @Published private(set) var service: ServiceStatus?
    @Published private(set) var action: Command?
    @Published private(set) var serviceError: String?
    @Published private(set) var loginError: String?
    @Published private(set) var login = SMAppService.mainApp.status

    private let control: Control?

    init() {
        control = try? Control()
        let command: Command = login == .enabled ? .start : .status
        action = command
        Task {
            await execute(command)
        }
    }

    var loginRequested: Bool { login == .enabled || login == .requiresApproval }
    var logAvailable: Bool { control.map { FileManager.default.isReadableFile(atPath: $0.log.path) } ?? false }

    var serviceTitle: String {
        switch (action, service?.status) {
        case (.start, _), (_, .starting): return "Запускается…"
        case (.stop, _), (_, .stopping): return "Останавливается…"
        case (_, .running): return "Работает"
        case (_, .stopped): return "Остановлен"
        case (_, .conflict): return "Конфликт запуска"
        case (_, nil): return serviceError == nil ? "Проверка состояния…" : "Состояние недоступно"
        }
    }

    var indicatorColor: Color {
        switch service?.status {
        case .running: return .green
        case .starting, .stopping, .conflict: return .orange
        default: return .secondary
        }
    }

    var serviceDetail: String? {
        guard let service else { return nil }
        var parts: [String] = []
        if let port = service.port, service.status == .running { parts.append("Порт \(port)") }
        if service.launcher == "opencode" { parts.append("По запросу OpenCode") }
        if service.launcher == "managed" { parts.append("Отдельный сервис") }
        if service.watchesRunning == true { parts.append("Наблюдения активны") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    var actionAvailable: Bool {
        action == nil && (service?.status == .running || service?.status == .stopped)
    }

    var canPauseWatches: Bool {
        action == nil && service?.status == .stopped && service?.watchesRunning == true
    }

    func perform(_ command: Command) async {
        guard action == nil else { return }
        action = command
        await execute(command)
    }

    private func execute(_ command: Command) async {
        defer { action = nil }
        guard let control else {
            serviceError = "Пересобери меню из папки Handy."
            return
        }
        if command != .status { serviceError = nil }
        do {
            let status = try await Self.run(control, command)
            if command != .status || service == nil { serviceError = nil }
            service = status
        } catch {
            serviceError = error.localizedDescription
            if command == .status {
                service = nil
            } else {
                service = try? await Self.run(control, .status)
            }
        }
    }

    func refresh() async {
        login = SMAppService.mainApp.status
        await perform(.status)
    }

    func setLogin(_ enabled: Bool) {
        loginError = nil
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            loginError = error.localizedDescription
        }
        login = SMAppService.mainApp.status
    }

    func openLog() {
        guard let control else { return }
        if !NSWorkspace.shared.open(control.log) {
            serviceError = "Не удалось открыть журнал Handy."
        }
    }

    private static func run(_ control: Control, _ command: Command) async throws -> ServiceStatus {
        try await Task.detached(priority: .utility) { try control.run(command) }.value
    }
}

private struct HandyMark: View {
    let size: CGFloat
    var body: some View {
        if let path = Bundle.main.path(forResource: "Handy", ofType: "icns"), let icon = NSImage(contentsOfFile: path) {
            Image(nsImage: icon).resizable().frame(width: size, height: size).accessibilityHidden(true)
        } else {
            Image(systemName: "h.square.fill").font(.system(size: size)).accessibilityHidden(true)
        }
    }
}

private struct MenuContent: View {
    @ObservedObject var model: MenuModel
    @ObservedObject var settings: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                HandyMark(size: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Handy").font(.headline)
                    Text(model.serviceTitle).foregroundStyle(.secondary)
                    if let detail = model.serviceDetail {
                        Text(detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if let action = model.action, action != .status || model.service == nil {
                    ProgressView().controlSize(.small)
                } else {
                    Circle().fill(model.indicatorColor).frame(width: 8, height: 8).accessibilityHidden(true)
                }
                Button {
                    HandyWindows.showSettings()
                } label: {
                    Image(systemName: "gearshape").frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Настройки")
                .accessibilityLabel("Настройки")
                .keyboardShortcut(",", modifiers: .command)
            }
            if let error = model.serviceError ?? model.service?.message {
                Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            let running = model.service?.status == .running
            Button {
                Task { await model.perform(running ? .stop : .start) }
            } label: {
                Label(running ? "Остановить Handy" : "Запустить Handy", systemImage: running ? "stop.fill" : "play.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!model.actionAvailable || settings.working)
            if model.canPauseWatches {
                Button("Приостановить наблюдения") { Task { await model.perform(.stop) } }
            }

            Divider()
            Toggle("Запускать Handy при входе", isOn: Binding(get: { model.loginRequested }, set: model.setLogin))
            if model.login == .requiresApproval {
                Button("Разрешить в Системных настройках") {
                    SMAppService.openSystemSettingsLoginItems()
                }
                .buttonStyle(.link)
            } else if model.login == .enabled {
                Text("Handy запускается при открытии меню.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let error = model.loginError {
                Text(error).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Журнал", action: model.openLog).disabled(!model.logAvailable)
                Spacer()
                Button("Закрыть меню") { NSApp.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 306)
        .task {
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }
}

@MainActor
final class HandyApplicationDelegate: NSObject, NSApplicationDelegate {
    weak var settings: SettingsModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        settings = HandyState.settings
        if ProcessInfo.processInfo.arguments.contains("--settings") { HandyWindows.showSettings() }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        if urls.contains(where: { $0.scheme == "handy" && $0.host == "settings" }) {
            HandyWindows.showSettings()
        }
        if urls.contains(where: { $0.scheme == "handy" && $0.host == "schedules" }) {
            HandyWindows.showSettings(tab: .schedules)
        }
        if urls.contains(where: { $0.scheme == "handy" && $0.host == "notification-test" }) {
            Task { await HandyState.pi.testNotifications() }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let settings, settings.working else { return .terminateNow }
        // A cancelled download must remove its partial files. A configuration
        // transaction must finish or restore the previous running service.
        settings.cancel()
        Task {
            while settings.working { try? await Task.sleep(nanoseconds: 100_000_000) }
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        OwnedBridgeProcesses.shared.shutdown()
    }
}

@MainActor
private enum HandyState {
    static let settings = SettingsModel()
    static let pi = PiModel()
    static let navigation = HandySettingsNavigation()
}

@MainActor
private enum HandyWindows {
    static var settings: NSWindow?

    static func showSettings(tab: HandySettingsTab? = nil) {
        if let tab { HandyState.navigation.tab = tab }
        if settings == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 860, height: 800),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Настройки Handy"
            window.isReleasedWhenClosed = false
            window.contentViewController = NSHostingController(rootView: HandySettingsContent(models: HandyState.settings, pi: HandyState.pi, navigation: HandyState.navigation))
            window.center()
            settings = window
        }
        settings?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

@main
struct HandyMenuApp: App {
    @NSApplicationDelegateAdaptor(HandyApplicationDelegate.self) private var appDelegate
    @StateObject private var model = MenuModel()
    @StateObject private var settings = HandyState.settings
    @StateObject private var pi = HandyState.pi

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model, settings: settings)
                .onAppear { appDelegate.settings = settings }
        } label: {
            Text("h").font(.system(size: 18, weight: .bold, design: .rounded))
                .foregroundStyle(.primary).accessibilityLabel("Handy")
        }
        .menuBarExtraStyle(.window)
    }
}
