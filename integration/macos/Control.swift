import Foundation
import Darwin

@discardableResult
func stopOwnedProcess(_ process: Process, grace: TimeInterval = 7) -> Bool {
    if process.isRunning {
        process.terminate()
        let deadline = ProcessInfo.processInfo.systemUptime + grace
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.025)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            let killDeadline = ProcessInfo.processInfo.systemUptime + 2
            while process.isRunning && ProcessInfo.processInfo.systemUptime < killDeadline {
                Thread.sleep(forTimeInterval: 0.025)
            }
        }
    }
    guard !process.isRunning else { return false }
    process.waitUntilExit()
    return true
}

final class OwnedBridgeProcesses: @unchecked Sendable {
    static let shared = OwnedBridgeProcesses()
    private let lock = NSLock()
    private var processes: [ObjectIdentifier: Process] = [:]
    private var shuttingDown = false

    func run(_ process: Process) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !shuttingDown else { throw CancellationError() }
        try process.run()
        processes[ObjectIdentifier(process)] = process
    }

    func release(_ process: Process) {
        lock.lock()
        processes.removeValue(forKey: ObjectIdentifier(process))
        lock.unlock()
    }

    @MainActor
    func shutdown(grace: TimeInterval = 7) {
        lock.lock()
        shuttingDown = true
        let children = Array(processes.values)
        processes.removeAll()
        lock.unlock()
        for child in children where child.isRunning { child.terminate() }
        let deadline = ProcessInfo.processInfo.systemUptime + grace
        // Foundation delivers child termination through the main run loop.
        while children.contains(where: \.isRunning) && ProcessInfo.processInfo.systemUptime < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.025))
        }
        for child in children where child.isRunning { kill(child.processIdentifier, SIGKILL) }
        let killDeadline = ProcessInfo.processInfo.systemUptime + 2
        while children.contains(where: \.isRunning) && ProcessInfo.processInfo.systemUptime < killDeadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.025))
        }
        for child in children where !child.isRunning { child.waitUntilExit() }
    }
}

enum Command: String, Sendable {
    case status, start, stop
}

struct ServiceStatus: Decodable, Sendable {
    enum State: String, Decodable, Sendable {
        case stopped, starting, running, stopping, conflict
    }

    let status: State
    let port: Int?
    let launcher: String?
    let watchesRunning: Bool?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case status, port, launcher, message
        case watchesRunning = "watches_running"
    }
}

struct ControlError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct Control: Sendable {
    let checkout: URL
    let python: URL
    let stateDirectory: URL
    let toolPath: String

    init(checkout: URL, python: URL, stateDirectory: URL, toolPath: String = "") {
        self.checkout = checkout
        self.python = python
        self.stateDirectory = stateDirectory
        self.toolPath = toolPath
    }

    init(bundle: Bundle = .main) throws {
        func path(_ key: String) throws -> URL {
            guard let value = bundle.object(forInfoDictionaryKey: key) as? String, value.hasPrefix("/") else {
                throw ControlError(message: "Пересобери меню из папки Handy.")
            }
            return URL(fileURLWithPath: value)
        }
        checkout = try path("HandyCheckout")
        python = try path("HandyPython")
        stateDirectory = try path("HandyStateDirectory")
        toolPath = bundle.object(forInfoDictionaryKey: "HandyToolPath") as? String ?? ""
    }

    var log: URL { stateDirectory.appendingPathComponent("service.log") }

    func run(_ command: Command) throws -> ServiceStatus {
        if command == .start {
            _ = try SettingsJob().run(control: self, request: ["operation": .string("start_service")], progress: { _ in })
            let configuration = try pi("status")
            if configuration["settings"]?.object["scheduler_enabled"]?.flag == true {
                _ = try pi("start-scheduler")
            }
        } else if command == .stop {
            _ = try invoke(command)
        }
        let data = try invoke(.status)
        guard let status = try? JSONDecoder().decode(ServiceStatus.self, from: data) else {
            throw ControlError(message: "Не удалось прочитать состояние Handy.")
        }
        return status
    }

    private func invoke(_ command: Command) throws -> Data {
        let files = FileManager.default
        let temporary = files.temporaryDirectory.appendingPathComponent("handy-menu-\(UUID().uuidString)")
        try files.createDirectory(at: temporary, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? files.removeItem(at: temporary) }
        let output = temporary.appendingPathComponent("stdout")
        let errors = temporary.appendingPathComponent("stderr")
        for url in [output, errors] {
            try Data().write(to: url)
        }
        let stdout = try FileHandle(forWritingTo: output)
        defer { try? stdout.close() }
        let stderr = try FileHandle(forWritingTo: errors)
        defer { try? stderr.close() }
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = checkout
        var arguments = ["-m", "folder_memory.control", "--state-dir", stateDirectory.path, command.rawValue]
        if command == .status { arguments.append("--json") }
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        process.standardError = stderr
        if !toolPath.isEmpty {
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = toolPath
            process.environment = environment
        }
        try OwnedBridgeProcesses.shared.run(process)
        defer { OwnedBridgeProcesses.shared.release(process) }
        let deadline = ProcessInfo.processInfo.systemUptime + (command == .status ? 30 : 60)
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.025)
        }
        if process.isRunning {
            stopOwnedProcess(process)
            throw ControlError(message: "Handy не ответил. Проверь состояние сервиса и доступ к папке проекта.")
        }
        process.waitUntilExit()

        if process.terminationStatus != 0 {
            let errorFile = try FileHandle(forReadingFrom: errors)
            defer { try? errorFile.close() }
            let details = String(decoding: try errorFile.read(upToCount: 4096) ?? Data(), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw ControlError(message: details.isEmpty ? "Команда Handy завершилась с ошибкой." : String(details.prefix(400)))
        }
        guard command == .status else { return Data() }
        let result = try FileHandle(forReadingFrom: output)
        defer { try? result.close() }
        return try result.read(upToCount: 16_384) ?? Data()
    }
}
