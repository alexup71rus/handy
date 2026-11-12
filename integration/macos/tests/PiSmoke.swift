import AppKit
import Foundation
import SwiftUI

@main
struct PiSmoke {
    @MainActor
    static func main() async {
        do {
            let environment = ProcessInfo.processInfo.environment
            guard let root = environment["HANDY_PI_SMOKE_ROOT"], let python = environment["HANDY_PI_SMOKE_PYTHON"],
                  let checkout = environment["HANDY_PI_SMOKE_CHECKOUT"] else { throw ControlError(message: "Missing smoke paths") }
            let control = Control(checkout: URL(fileURLWithPath: checkout), python: URL(fileURLWithPath: python),
                                  stateDirectory: URL(fileURLWithPath: root).appendingPathComponent("state"), toolPath: environment["PATH"] ?? "")
            var checks = 0
            func expect(_ value: Bool, _ message: String) throws { checks += 1; if !value { throw ControlError(message: message) } }
            _ = try control.pi("save-settings", request: ["scheduler_enabled": .bool(false), "browser_enabled": .bool(false)])
            var snapshot = try control.pi("status")
            try expect(snapshot["settings"]?.object["scheduler_enabled"]?.flag == false, "Persist scheduler setting")
            try expect(snapshot["settings"]?.object["theme"]?.text == "system", "Default theme follows system")
            let model = PiModel(control: control, poll: false)
            await model.refresh()
            try expect(Set(model.mcpServers.map(\.id)) == Set(["handy", "browser", "custom-fixture"]), "List real managed and custom servers")
            try expect(model.mcpChecks.isEmpty, "Refreshing settings does not connect MCP servers")
            let settingsFile = control.stateDirectory.appendingPathComponent("pi-settings.json")
            let savedSettingsFile = try Data(contentsOf: settingsFile)
            try Data("invalid settings JSON".utf8).write(to: settingsFile)
            await model.refresh()
            try expect(model.refreshError != nil, "Surface passive refresh failure")
            try savedSettingsFile.write(to: settingsFile)
            _ = await model.perform("delete-job", request: ["job_id": .string("missing")])
            await model.refresh()
            try expect(model.refreshError == nil && model.error?.contains("не найдена") == true, "Recovered refresh clears only its own error and preserves action error")
            guard let handy = model.mcpServers.first(where: { $0.id == "handy" }),
                  let browser = model.mcpServers.first(where: { $0.id == "browser" }),
                  let custom = model.mcpServers.first(where: { $0.id == "custom-fixture" }) else { throw ControlError(message: "Missing server rows") }
            await model.setMCPEnabled(handy, enabled: false)
            try expect(model.mcpServers.first { $0.id == "handy" }?.enabled == false, "Disable Handy server")
            try expect(model.savedSettings["artifacts_enabled"]?.flag == true && model.savedSettings["memory_enabled"]?.flag == true && model.savedSettings["rag_enabled"]?.flag == true, "Master switch preserves Handy feature settings")
            model.settings["theme"] = .string("light")
            await model.setMCPEnabled(browser, enabled: true)
            try expect(model.settings["theme"]?.text == "light" && model.savedSettings["theme"]?.text == "system", "Server action preserves unsaved terminal settings")
            try expect(model.settings["browser_enabled"]?.flag == true && model.savedSettings["browser_enabled"]?.flag == true, "Browser switch synchronizes compatibility setting")
            await model.setMCPEnabled(browser, enabled: false)
            await model.setMCPEnabled(custom, enabled: true)
            try expect(model.mcpServers.first { $0.id == "custom-fixture" }?.enabled == true, "Enable custom server")
            await model.setMCPEnabled(custom, enabled: false)
            model.settings = model.savedSettings
            await model.checkMCP()
            try expect(model.error == nil && model.mcpErrors.isEmpty, "Explicit official pi MCP probe succeeds")
            try expect(model.mcpChecks.count == 3, "Probe reports every configured server")
            try expect(model.mcpChecks.allSatisfy { $0.text("state") == "disabled" && ($0.values["tools"]?.array ?? []).isEmpty }, "Disabled servers remain disconnected during check")
            try expect(model.mcpProbe(for: custom)?.text("name") == "custom-fixture", "Probe maps to custom server details")
            await model.refresh()
            try expect(model.mcpChecks.count == 3, "Passive refresh retains explicit check results")
            model.settings["thinking_level"] = .string("max")
            await model.save()
            try expect(model.savedSettings["thinking_level"]?.text == "max" && model.error == nil, "Persist supported maximum thinking level")
            let prompt = "Проверь 'кавычки', $HOME и `backticks`\nв сообщении"
            let job = try control.pi("create-job", request: ["title": .string("Проверка Swift → Python"), "prompt": .string(prompt),
                "cwd": .string(root), "run_at": .number(Date().addingTimeInterval(3600).timeIntervalSince1970), "interval_seconds": .null])
            guard let id = job["id"]?.text, !id.isEmpty else { throw ControlError(message: "Missing created job") }
            try expect(job["prompt"]?.text == prompt, "JSON round trip preserves text")
            let updated = try control.pi("update-job", request: ["job_id": .string(id), "enabled": .bool(false)])
            try expect(updated["enabled"]?.flag == false, "Disable actual job")
            snapshot = try control.pi("status")
            try expect(snapshot["jobs"]?.array.count == 1, "Status contains created job")
            if let output = environment["HANDY_PI_LAYOUT_OUTPUT"] {
                NSApplication.shared.setActivationPolicy(.prohibited)
                try render(PiSettingsContent(model: model), to: URL(fileURLWithPath: output).appendingPathComponent("pi-settings.png"))
                try render(PiMCPContent(model: model), to: URL(fileURLWithPath: output).appendingPathComponent("pi-mcp.png"))
                try render(PiSchedulesContent(model: model), to: URL(fileURLWithPath: output).appendingPathComponent("pi-schedules.png"))
                let shortInterval = PiRecord(values: ["id": .string("render-only"), "title": .string("Проверка интервала"), "prompt": .string("Проверь обновления"), "cwd": .string(root), "interval_seconds": .number(90)])
                try render(PiJobEditor(model: model, job: shortInterval), to: URL(fileURLWithPath: output).appendingPathComponent("pi-schedule-editor.png"))
            }
            _ = try control.pi("delete-job", request: ["job_id": .string(id)])
            snapshot = try control.pi("status")
            try expect(snapshot["jobs"]?.array.isEmpty == true, "Delete actual job")
            try expect(snapshot["runs"]?.array.first?.object["id"]?.text == "finished-fixture", "Persisted run is visible")
            _ = try control.pi("delete-run", request: ["run_id": .string("finished-fixture")])
            snapshot = try control.pi("status")
            try expect(snapshot["runs"]?.array.isEmpty == true, "Delete actual finished result")
            try expect(FileManager.default.fileExists(atPath: environment["HANDY_PI_SMOKE_SESSION"] ?? ""), "History removal retains saved pi chat")
            do {
                _ = try control.pi("delete-run", request: ["run_id": .string("missing")])
                throw ControlError(message: "Missing result must fail")
            } catch let failure as ControlError { try expect(failure.message.contains("не найден"), "Surface missing history result") }
            do {
                _ = try control.pi("update-job", request: ["job_id": .string("missing"), "enabled": .bool(false)])
                throw ControlError(message: "Missing job must fail")
            } catch let failure as ControlError { try expect(failure.message.contains("не найдена"), "Surface backend error") }
            var history = PiNotificationHistory(identifiers: nil)
            try expect(history.pending(["old"], enabled: true).isEmpty, "First launch ignores historic results")
            try expect(history.pending(["new", "old"], enabled: true) == ["new"], "New run is pending")
            try expect(history.pending(["new", "old"], enabled: true) == ["new"], "Permission/delivery failure retries")
            history.record(["new"])
            try expect(history.pending(["new", "old"], enabled: true).isEmpty, "Successful notification is not duplicated")
            try expect(history.pending(["disabled", "new"], enabled: false).isEmpty, "Disabled notifications consume existing results")
            try expect(history.pending(["disabled", "new"], enabled: true).isEmpty, "Enabling does not announce old disabled results")
            history.record((0..<2200).map { "run-\($0)" })
            try expect(history.identifiers?.count == 2000, "Notification history is bounded")
            try expect(history.identifiers?.first == "run-0", "Recent result survives history cap")
            let delayedState = URL(fileURLWithPath: root).appendingPathComponent("delayed-state")
            try FileManager.default.createDirectory(at: delayedState, withIntermediateDirectories: true)
            let fixture = URL(fileURLWithPath: root).appendingPathComponent("delayed-bridge")
            try Data("""
            #!/usr/bin/env python3
            import json,os,signal,sys,time
            from pathlib import Path
            if sys.argv[-1]=='ignore-term':
                signal.signal(signal.SIGTERM,signal.SIG_IGN)
                Path(__file__).with_name('ignored-ready-'+str(os.getpid())).touch()
                time.sleep(15);sys.exit()
            state=Path(sys.argv[sys.argv.index('--state-dir')+1])
            request=json.load(sys.stdin)
            if request.get('operation')=='stderr-test':
                sys.stderr.write('x'*8192+'\\nFatal Python error: fixture startup failed\\n')
                sys.exit(3)
            if request.get('operation')=='snapshot':
                (state/'snapshot-started').touch()
                time.sleep(15);sys.exit()
            settings=state/'settings.json'
            if sys.argv[-1]=='status':
                snapshot=json.loads(settings.read_text()) if settings.exists() else {'theme':'system','scheduler_enabled':False}
                if (state/'delay').exists():
                    (state/'started').touch()
                    deadline=time.monotonic()+10
                    while not (state/'release').exists() and time.monotonic()<deadline:time.sleep(.01)
                print(json.dumps({'settings':snapshot}))
            elif sys.argv[-1]=='save-settings':
                settings.write_text(json.dumps(request));print(json.dumps(request))
            """.utf8).write(to: fixture)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: fixture.path)
            let delayed = PiModel(control: Control(checkout: control.checkout, python: fixture, stateDirectory: delayedState, toolPath: control.toolPath), poll: false)
            await delayed.refresh()
            try Data().write(to: delayedState.appendingPathComponent("delay"))
            let staleRefresh = Task { await delayed.refresh() }
            let deadline = Date().addingTimeInterval(5)
            while !FileManager.default.fileExists(atPath: delayedState.appendingPathComponent("started").path) && Date() < deadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            try expect(FileManager.default.fileExists(atPath: delayedState.appendingPathComponent("started").path), "Old status request is in flight")
            delayed.settings["theme"] = .string("light")
            await delayed.save()
            try Data().write(to: delayedState.appendingPathComponent("release"))
            await staleRefresh.value
            try expect(delayed.settings["theme"]?.text == "light" && delayed.savedSettings["theme"]?.text == "light", "Old status response cannot undo a completed settings save")
            let stubborn = Process()
            stubborn.executableURL = fixture
            stubborn.arguments = ["ignore-term"]
            stubborn.standardInput = FileHandle.nullDevice
            stubborn.standardOutput = FileHandle.nullDevice
            stubborn.standardError = FileHandle.nullDevice
            try stubborn.run()
            defer { stopOwnedProcess(stubborn, grace: 0.05) }
            let readyDeadline = Date().addingTimeInterval(5)
            while !FileManager.default.fileExists(atPath: fixture.deletingLastPathComponent().appendingPathComponent("ignored-ready-\(stubborn.processIdentifier)").path) && Date() < readyDeadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            stopOwnedProcess(stubborn, grace: 0.05)
            try expect(!stubborn.isRunning && stubborn.terminationReason == .uncaughtSignal && stubborn.terminationStatus == 9, "Timed-out bridge is killed and reaped even when SIGTERM is ignored")
            let snapshotModel = SettingsModel(control: Control(checkout: control.checkout, python: fixture, stateDirectory: delayedState, toolPath: control.toolPath))
            let blockedSnapshot = Task { await snapshotModel.refresh() }
            let snapshotDeadline = Date().addingTimeInterval(5)
            while !FileManager.default.fileExists(atPath: delayedState.appendingPathComponent("snapshot-started").path) && Date() < snapshotDeadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            try expect(snapshotModel.canCancel, "Initial settings lookup can be cancelled while Python startup is blocked")
            snapshotModel.cancel()
            await blockedSnapshot.value
            try expect(!snapshotModel.working && snapshotModel.notice == "Операция отменена.", "Cancelling the initial lookup releases shutdown")
            let cadences: [Double] = [1, 1.5, 5, 60, 90, 3600, 366 * 86400]
            try expect(cadences.allSatisfy { PiScheduleInterval(seconds: $0).seconds == $0 && PiScheduleInterval(seconds: $0).isValid }, "Editing a task preserves seconds, fractions and minute/hour intervals")
            try expect([0, -1, Double(Int.max), Double.infinity, Double.nan, Double(366 * 86400 + 1)].allSatisfy { !PiScheduleInterval(seconds: $0).isValid }, "Oversized intervals are rejected without integer overflow")
            let lifecycle = OwnedBridgeProcesses()
            defer { lifecycle.shutdown(grace: 0.05) }
            var children: [Process] = []
            for _ in 0..<2 {
                let child = Process()
                child.executableURL = fixture
                child.arguments = ["ignore-term"]
                child.standardInput = FileHandle.nullDevice
                child.standardOutput = FileHandle.nullDevice
                child.standardError = FileHandle.nullDevice
                try lifecycle.run(child)
                children.append(child)
            }
            let childrenDeadline = Date().addingTimeInterval(5)
            while children.contains(where: { !FileManager.default.fileExists(atPath: fixture.deletingLastPathComponent().appendingPathComponent("ignored-ready-\($0.processIdentifier)").path) }) && Date() < childrenDeadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            let shutdownStart = ProcessInfo.processInfo.systemUptime
            lifecycle.shutdown(grace: 0.1)
            try expect(children.allSatisfy { !$0.isRunning && $0.terminationReason == .uncaughtSignal && $0.terminationStatus == 9 }, "App shutdown kills and reaps all owned bridges")
            try expect(ProcessInfo.processInfo.systemUptime - shutdownStart < 3, "Owned bridge shutdown has a bounded grace period")
            do {
                try lifecycle.run(Process())
                throw ControlError(message: "Late bridge launch must be refused")
            } catch is CancellationError { try expect(true, "App shutdown refuses new bridge launches") }
            do {
                _ = try SettingsJob().run(control: Control(checkout: control.checkout, python: fixture, stateDirectory: delayedState, toolPath: control.toolPath), request: ["operation": .string("stderr-test")], progress: { _ in })
                throw ControlError(message: "Startup failure must be reported")
            } catch let failure as ControlError {
                try expect(failure.message.hasSuffix("Fatal Python error: fixture startup failed") && failure.message.utf8.count <= 4096, "Startup diagnostics preserve the bounded stderr tail")
            }
            print("Native pi bridge smoke: \(checks) checks passed.")

        } catch {
            FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
            exit(1)
        }
    }

    @MainActor
    static func render<Content: View>(_ content: Content, to destination: URL) throws {
        let view = NSHostingView(rootView: content.environment(\.colorScheme, .light).background(Color(nsColor: .windowBackgroundColor)))
        view.frame = NSRect(x: 0, y: 0, width: 860, height: 800)
        view.appearance = NSAppearance(named: .aqua)
        let window = NSWindow(contentRect: view.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = view
        window.appearance = NSAppearance(named: .aqua)
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
        view.layoutSubtreeIfNeeded()
        view.displayIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw ControlError(message: "Cannot render SwiftUI") }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let image = bitmap.representation(using: .png, properties: [:]) else { throw ControlError(message: "Cannot encode SwiftUI") }
        try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try image.write(to: destination)
    }
}
