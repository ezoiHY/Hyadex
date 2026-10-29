import Darwin
import Foundation
import HyadexAPI
import Observation

/// Runs only an app-owned foreground instance. It never invokes an installer,
/// service manager or shell, and never reads credentials from installed servers.
@MainActor @Observable
public final class LocalServerController {
    public private(set) var state: LocalServerState = .idle
    public let configuration: LocalServerConfiguration
    public var isStarting: Bool { state == .starting }
    public var isRunning: Bool { state == .running }
    public var endpoint: ServerEndpoint? { connection?.endpoint }
    public var errorMessage: String? { if case .failed(let message) = state { message } else { nil } }
    public var statusText: String {
        switch state {
        case .idle: "本地服务未启动"
        case .starting: "正在启动本地服务…"
        case .running: "本地服务运行中"
        case .stopping: "正在停止本地服务…"
        case .failed: "本地服务启动失败"
        }
    }

    @ObservationIgnored private var process: Process?
    @ObservationIgnored private var output: LocalServerOutput?
    @ObservationIgnored private var connection: LocalServerConnection?
    @ObservationIgnored private var startup: Task<LocalServerConnection, Error>?
    @ObservationIgnored private var startupID: UUID?
    @ObservationIgnored private var shutdownTask: Task<Void, Never>?
    @ObservationIgnored private var cleanupTask: Task<Void, Never>?
    @ObservationIgnored private var lockDescriptor: Int32 = -1

    public init(configuration: LocalServerConfiguration = .current()) {
        self.configuration = configuration
    }

    /// Concurrent clicks share the same launch, and reconnecting keeps its
    /// endpoint, token and process. Readiness is authenticated and identity-bound.
    public func start() async throws -> LocalServerConnection {
        guard state != .stopping, shutdownTask == nil, cleanupTask == nil else { throw LocalServerError.stopping }
        if let startup {
            let result = try await startup.value
            guard shutdownTask == nil, state != .stopping, process?.isRunning == true,
                  connection?.endpoint == result.endpoint else { throw CancellationError() }
            return result
        }
        if let connection, process?.isRunning == true {
            try await verify(connection)
            return connection
        }
        state = .starting
        let launchID = UUID()
        let task = Task { try await launch() }
        startupID = launchID
        startup = task
        do {
            let result = try await task.value
            guard startupID == launchID, shutdownTask == nil, state == .starting else { throw CancellationError() }
            startup = nil
            startupID = nil
            state = .running
            return result
        } catch {
            // Shutdown may already have cleaned this launch and a subsequent
            // start may own the controller. A late waiter cannot clear it.
            guard startupID == launchID else { throw error }
            startup = nil
            startupID = nil
            if shutdownTask == nil {
                state = .stopping
                await stopProcess()
                if shutdownTask == nil, state == .stopping {
                    state = error is CancellationError ? .idle : .failed(error.localizedDescription)
                }
            }
            throw error
        }
    }

    /// Called by application termination, not when switching servers or closing
    /// a window. The long grace window leaves the backend time to stop providers.
    public func shutdown() async {
        if let shutdownTask { await shutdownTask.value; return }
        state = .stopping
        let task = Task {
            let pending = startup
            pending?.cancel()
            if let pending { _ = try? await pending.value }
            startup = nil
            startupID = nil
            await stopProcess()
            state = .idle
        }
        shutdownTask = task
        await task.value
        shutdownTask = nil
    }

    private func launch() async throws -> LocalServerConnection {
        try Task.checkCancellation()
        let config = configuration
        guard config.instanceName != "default",
              config.instanceName.range(of: "^[a-z][a-z0-9-]{0,31}$", options: .regularExpression) != nil,
              (0...65535).contains(config.port), config.startupTimeout > 0, config.shutdownTimeout >= 0 else {
            throw LocalServerError.invalidConfiguration
        }
        let fm = FileManager.default
        guard let source = config.sourceDirectory,
              fm.fileExists(atPath: source.appending(path: "scripts/dev_server.py").path),
              fm.fileExists(atPath: source.appending(path: "hyadex/main.py").path) else {
            throw LocalServerError.missingSource
        }
        let python = source.appending(path: ".venv/bin/python")
        guard fm.isExecutableFile(atPath: python.path) else { throw LocalServerError.missingPython(source.path) }

        let endpointID = try prepareOwnership()
        let port = try availablePort(config.port)
        let endpoint = ServerEndpoint(id: endpointID, name: "这台 Mac", baseURL: URL(string: "http://127.0.0.1:\(port)")!)
        let child = Process()
        child.executableURL = python
        // --exit-with-parent: the backend shuts itself down if this app crashes
        // or is force-quit, so it never holds the instance for the next launch.
        child.arguments = [source.appending(path: "scripts/dev_server.py").path,
                           "--instance", config.instanceName, "--port", String(port), "--bind", "127.0.0.1",
                           "--exit-with-parent"]
        child.currentDirectoryURL = source
        var environment = ProcessInfo.processInfo.environment
        // The runner also scrubs server variables. Keep the native process
        // boundary explicit and prevent Python injection inherited from a host.
        let prefixes = ["HYADEX_", "AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHDOCK_", "ZENITHBOT_", "CLAUDE_CODE_", "CLAUDECODE", "PYTHON"]
        environment = environment.filter { key, _ in !prefixes.contains(where: key.hasPrefix) }
        environment["HOME"] = config.homeDirectory.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        let binPaths = [config.homeDirectory.appending(path: ".local/bin").path, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        environment["PATH"] = (binPaths + [environment["PATH"] ?? ""]).joined(separator: ":")
        child.environment = environment
        child.standardInput = FileHandle.nullDevice
        let capture = LocalServerOutput()
        child.standardOutput = capture.pipe
        child.standardError = capture.pipe
        child.terminationHandler = { [weak self] child in
            Task { @MainActor [weak self] in
                guard let self, self.process === child, self.state == .running else { return }
                self.connection = nil
                self.state = .failed("本地服务已停止（退出码 \(child.terminationStatus)），可以重新启动。")
                self.releaseLock()
            }
        }
        process = child
        output = capture
        do { try child.run() } catch { throw LocalServerError.launchFailed }

        let deadline = ContinuousClock.now.advanced(by: .seconds(config.startupTimeout))
        while ContinuousClock.now < deadline {
            try Task.checkCancellation()
            guard child.isRunning else { throw LocalServerError.exited(child.terminationStatus, capture.summary()) }
            if fm.fileExists(atPath: config.tokenFile.path) {
                let token = String(decoding: try readPrivateFile(config.tokenFile, maximum: 512), as: UTF8.self)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard !token.isEmpty, !token.contains(where: { $0.isWhitespace }) else { throw LocalServerError.unsafeFile }
                let candidate = LocalServerConnection(endpoint: endpoint, token: token)
                do {
                    try await verify(candidate)
                    guard child.isRunning else { throw LocalServerError.exited(child.terminationStatus, capture.summary()) }
                    connection = candidate
                    return candidate
                } catch let error as LocalServerError { throw error }
                catch let error as HyadexClientError {
                    if case .unauthorized = error { throw LocalServerError.wrongServer }
                    // A bounded readiness wait tolerates connection refused while
                    // imports and startup recovery finish; no idle polling.
                }
            }
            try await Task.sleep(for: .milliseconds(150))
        }
        throw LocalServerError.timeout
    }

    private func verify(_ connection: LocalServerConnection) async throws {
        let health = try await HyadexClient(endpoint: connection.endpoint, token: connection.token)
            .request("GET", "api/health", timeout: 1)
        let identityFile = configuration.stateDirectory.appending(path: "server-identity")
        let identity = String(decoding: try readPrivateFile(identityFile, maximum: 256), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard health["ok"].bool == true, health["auth_required"].bool == true,
              health["state_dir"].string == configuration.stateDirectory.path,
              health["server_identity"].string == identity, !identity.isEmpty,
              health["server_instance_id"].string?.isEmpty == false else {
            throw LocalServerError.wrongServer
        }
    }

    private struct Ownership: Codable { let version: Int; let endpointID: UUID }

    private func prepareOwnership() throws -> UUID {
        let config = configuration, fm = FileManager.default
        // No service is queried, started or stopped. Even a stopped installation
        // is protected from being launched through a different execution model.
        let installedPaths = [config.configDirectory.appending(path: "env"),
                              config.runtimeDirectory.appending(path: "current"),
                              config.runtimeDirectory.appending(path: "execution-layout.json"),
                              config.homeDirectory.appending(path: "Library/LaunchAgents/com.hyadex.server.\(config.instanceName).plist"),
                              config.homeDirectory.appending(path: "Library/LaunchAgents/com.hyadex.gateway.\(config.instanceName).plist")]
        guard !installedPaths.contains(where: {
            var metadata = stat()
            return lstat($0.path, &metadata) == 0
        }) else { throw LocalServerError.existingInstance }
        for directory in [config.configDirectory, config.stateDirectory, config.runtimeDirectory] {
            try validateManagedPath(directory)
        }
        if !fm.fileExists(atPath: config.ownershipFile.path) {
            for directory in [config.configDirectory, config.stateDirectory, config.runtimeDirectory] {
                if fm.fileExists(atPath: directory.path), try !fm.contentsOfDirectory(atPath: directory.path).isEmpty {
                    throw LocalServerError.existingInstance
                }
            }
        }
        try fm.createDirectory(at: config.configDirectory, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let descriptor = open(config.configDirectory.appending(path: ".mac-app.lock").path,
                              O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw LocalServerError.unsafeFile }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o077 == 0,
              metadata.st_nlink == 1 else { close(descriptor); throw LocalServerError.unsafeFile }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { close(descriptor); throw LocalServerError.alreadyOwned }
        lockDescriptor = descriptor
        if fm.fileExists(atPath: config.ownershipFile.path) {
            let owner = try JSONDecoder().decode(Ownership.self, from: readPrivateFile(config.ownershipFile, maximum: 1024))
            guard owner.version == 1 else { throw LocalServerError.existingInstance }
            return owner.endpointID
        }
        let owner = Ownership(version: 1, endpointID: UUID())
        let data = try JSONEncoder().encode(owner)
        guard fm.createFile(atPath: config.ownershipFile.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            throw LocalServerError.unsafeFile
        }
        return owner.endpointID
    }

    private func validateManagedPath(_ target: URL) throws {
        var directory = target
        while directory.path != configuration.homeDirectory.path {
            var metadata = stat()
            if lstat(directory.path, &metadata) == 0 {
                guard metadata.st_mode & S_IFMT == S_IFDIR, metadata.st_uid == getuid(), metadata.st_mode & 0o022 == 0 else {
                    throw LocalServerError.unsafeFile
                }
            } else if errno != ENOENT { throw LocalServerError.unsafeFile }
            let parent = directory.deletingLastPathComponent()
            guard parent != directory else { throw LocalServerError.unsafeFile }
            directory = parent
        }
    }

    private func readPrivateFile(_ file: URL, maximum: Int) throws -> Data {
        let descriptor = open(file.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        guard descriptor >= 0 else { throw LocalServerError.unsafeFile }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? handle.close() }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o077 == 0,
              metadata.st_nlink == 1, metadata.st_size <= maximum else { throw LocalServerError.unsafeFile }
        return try handle.read(upToCount: maximum + 1) ?? Data()
    }

    private func availablePort(_ requested: Int) throws -> Int {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw LocalServerError.portUnavailable(requested) }
        defer { close(descriptor) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(requested).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard result == 0 else { throw LocalServerError.portUnavailable(requested) }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(descriptor, $0, &length) }
        }
        guard named == 0 else { throw LocalServerError.portUnavailable(requested) }
        return Int(in_port_t(bigEndian: address.sin_port))
    }

    private func stopProcess() async {
        if let cleanupTask { await cleanupTask.value; return }
        let cleanup = Task {
            let child = process
            if let child, child.isRunning {
                child.terminate()
                // Startup recovery can already own provider work before the
                // first healthy response. Give it the same full shutdown grace.
                let deadline = ContinuousClock.now.advanced(by: .seconds(configuration.shutdownTimeout))
                while child.isRunning, ContinuousClock.now < deadline {
                    // Cleanup must complete even when startup was cancelled.
                    await Task.detached { try? await Task.sleep(for: .milliseconds(100)) }.value
                }
                if child.isRunning {
                    kill(child.processIdentifier, SIGKILL)
                    await Task.detached { child.waitUntilExit() }.value
                }
            }
            process = nil
            connection = nil
            output?.finish()
            output = nil
            releaseLock()
        }
        cleanupTask = cleanup
        await cleanup.value
        cleanupTask = nil
    }

    private func releaseLock() {
        if lockDescriptor >= 0 { close(lockDescriptor); lockDescriptor = -1 }
    }
}

/// Drain stdout continuously without a full pipe blocking the server. Retain
/// only a short diagnostic tail, never credentials or unbounded server logs.
private final class LocalServerOutput: @unchecked Sendable {
    let pipe = Pipe()
    private let lock = NSLock()
    private var tail = Data()

    init() {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let self else { return }
            self.lock.lock()
            self.tail.append(data)
            if self.tail.count > 16_384 { self.tail = Data(self.tail.suffix(16_384)) }
            self.lock.unlock()
        }
    }

    func summary() -> String {
        lock.lock()
        let text = String(decoding: tail, as: UTF8.self)
        lock.unlock()
        // Diagnose common preparation failures without presenting traceback
        // environment, private paths or arbitrary child output in a UI alert.
        if text.contains("ModuleNotFoundError") || text.contains("No module named") {
            return "后端依赖不完整，请在源码目录运行 uv sync --locked --python 3.13 后重试。"
        }
        if text.contains("Another process owns") { return "已有进程正在使用这个实例；未停止该进程。" }
        if text.contains("Address already in use") || text.contains("address already in use") { return "端口已被占用；未停止其他服务。" }
        return "请检查源码目录中的后端环境，然后重试。"
    }

    func finish() { pipe.fileHandleForReading.readabilityHandler = nil }
}
