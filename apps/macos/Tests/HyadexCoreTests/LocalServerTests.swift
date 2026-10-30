import Darwin
import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

@MainActor
struct LocalServerTests {
    @Test func missingSourceShowsActionableErrorWithoutCreatingState() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let controller = LocalServerController(configuration: .init(sourceDirectory: nil, homeDirectory: home))
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(controller.errorMessage?.contains("源码") == true)
        #expect(try FileManager.default.contentsOfDirectory(atPath: home.path).isEmpty)
    }

    @Test func missingDependenciesOfferPreparationAndDoNotInstall() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let source = try fixtureSource(in: home, withPython: false)
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home)
        let controller = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(controller.errorMessage?.contains("uv sync --locked --python 3.13") == true)
        #expect(!FileManager.default.fileExists(atPath: config.configDirectory.path))
    }

    @Test func installedNamedInstanceIsNeverTakenOver() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let config = LocalServerConfiguration(sourceDirectory: try fixtureSource(in: home), homeDirectory: home)
        try FileManager.default.createDirectory(at: config.configDirectory, withIntermediateDirectories: true)
        let file = config.configDirectory.appending(path: "env")
        let sentinel = Data("HYADEX_AGENT_TOKEN=preserve-this-fixture\n".utf8)
        try sentinel.write(to: file)
        let controller = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(try Data(contentsOf: file) == sentinel)
        #expect(!FileManager.default.fileExists(atPath: config.ownershipFile.path))
        #expect(controller.errorMessage?.contains("不会接管") == true)
    }

    @Test func unownedStateAndSymlinkedDirectoriesAreProtected() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let source = try fixtureSource(in: home)
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home)
        try FileManager.default.createDirectory(at: config.stateDirectory, withIntermediateDirectories: true)
        let sentinel = config.stateDirectory.appending(path: "sessions.json")
        try Data("{\"saved\":{}}".utf8).write(to: sentinel)
        let controller = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(try String(contentsOf: sentinel, encoding: .utf8) == "{\"saved\":{}}")

        let other = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home, instanceName: "other")
        try FileManager.default.createDirectory(at: other.configDirectory.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: other.configDirectory, withDestinationURL: config.stateDirectory)
        let linked = LocalServerController(configuration: other)
        await #expect(throws: LocalServerError.self) { try await linked.start() }
        #expect(linked.errorMessage?.contains("私有文件") == true)
    }

    @Test func busyPortIsLeftRunningAndRetryRemainsUsable() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let (descriptor, port) = try boundSocket()
        defer { close(descriptor) }
        let config = LocalServerConfiguration(sourceDirectory: try fixtureSource(in: home), homeDirectory: home, port: port)
        let controller = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(controller.errorMessage?.contains("已被占用") == true)
        // The app's failure path did not close or modify someone else's socket.
        #expect(fcntl(descriptor, F_GETFD) >= 0)
        #expect(FileManager.default.fileExists(atPath: config.ownershipFile.path))
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(controller.errorMessage?.contains("已被占用") == true)
    }

    @Test func timeoutTerminatesOnlyItsChildAndReleasesOwnership() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let source = try fixtureSource(in: home, sleeping: true)
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home, startupTimeout: 1.5)
        let first = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await first.start() }
        #expect(first.errorMessage?.contains("限定时间") == true)
        let pidText = try String(contentsOf: home.appending(path: "child-pid"), encoding: .utf8)
        let pid = try #require(Int32(pidText))
        #expect(kill(pid, 0) == -1 && errno == ESRCH)
        let second = LocalServerController(configuration: config)
        await #expect(throws: LocalServerError.self) { try await second.start() }
        #expect(second.errorMessage?.contains("限定时间") == true)
    }

    /// 允许手机连接: every interface at a fixed port, so a paired phone keeps its address.
    @Test func phoneAccessListensOnEveryInterfaceAtAFixedPort() {
        var config = LocalServerConfiguration(sourceDirectory: nil, homeDirectory: URL(fileURLWithPath: "/tmp"))
        #expect(config.bindAddress == "127.0.0.1")
        #expect(config.launchPort == 0)
        config.phoneAccess = true
        #expect(config.bindAddress == "0.0.0.0")
        #expect(config.launchPort == LocalServerConfiguration.phonePort)
        config.port = 17_870
        #expect(config.launchPort == 17_870)
    }

    /// A port another server answers on fails before anything is stopped or changed.
    @Test func phoneAccessLeavesEverythingAsItWasWhenItsPortIsTaken() async throws {
        let (descriptor, port) = try boundSocket()
        defer { close(descriptor) }
        #expect(listen(descriptor, 1) == 0)
        let controller = LocalServerController(configuration: .init(sourceDirectory: nil, homeDirectory: URL(fileURLWithPath: "/tmp"), port: port))
        do {
            _ = try await controller.setPhoneAccess(true)
            Issue.record("a taken port should fail")
        } catch LocalServerError.phonePortTaken(let taken) {
            #expect(taken == port)
        }
        #expect(!controller.configuration.phoneAccess)
        #expect(controller.state == .idle)
    }

    @Test func explicitEnvironmentOverridesDoNotFallBack() {
        let config = LocalServerConfiguration.current(environment: [
            "HYADEX_LOCAL_SERVER_SOURCE": "/does-not-exist/hyadex",
            "HYADEX_LOCAL_SERVER_HOME": "/tmp/isolated-home",
            "HYADEX_LOCAL_SERVER_INSTANCE": "acceptance",
            "HYADEX_LOCAL_SERVER_PORT": "17872",
        ])
        #expect(config.sourceDirectory?.path == "/does-not-exist/hyadex")
        #expect(config.instanceName == "acceptance")
        #expect(config.port == 17872)
        #expect(config.stateDirectory.lastPathComponent == "acceptance")
    }

    @Test func concurrentShutdownCancelsStartupWithoutReplacingItsChild() async throws {
        let home = try temporaryHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let source = try fixtureSource(in: home, sleeping: true, ignoresTermination: true)
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home,
                                              startupTimeout: 10, shutdownTimeout: 0.5)
        let controller = LocalServerController(configuration: config)
        let pending = Task { try await controller.start() }
        let pidFile = home.appending(path: "child-pid")
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while !FileManager.default.fileExists(atPath: pidFile.path), ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        guard let text = try? String(contentsOf: pidFile, encoding: .utf8), let pid = Int32(text) else {
            await controller.shutdown()
            _ = try? await pending.value
            Issue.record("fixture process did not start")
            return
        }
        let firstStop = Task { await controller.shutdown() }
        let stoppingDeadline = ContinuousClock.now.advanced(by: .seconds(1))
        while controller.state != .stopping, ContinuousClock.now < stoppingDeadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        guard controller.state == .stopping else {
            await firstStop.value
            _ = try? await pending.value
            Issue.record("shutdown did not enter its grace period")
            return
        }
        let secondStop = Task { await controller.shutdown() }
        await #expect(throws: LocalServerError.self) { try await controller.start() }
        #expect(controller.state == .stopping)
        await firstStop.value
        await secondStop.value
        await #expect(throws: CancellationError.self) { try await pending.value }
        #expect(controller.state == .idle)
        #expect(kill(pid, 0) == -1 && errno == ESRCH)
        #expect(try String(contentsOf: pidFile, encoding: .utf8) == text)

        // The exact same instance can start again after cancellation; no stale
        // app lock or late cleanup is allowed to affect the next process.
        let retry = Task { try await controller.start() }
        try await Task.sleep(for: .milliseconds(80))
        #expect(controller.state == .starting)
        await controller.shutdown()
        await #expect(throws: CancellationError.self) { try await retry.value }
        #expect(controller.state == .idle)
    }
}

/// Opt in only with a prepared checkout. This exercises the production runner,
/// auth boundary, state persistence and process lifecycle, with no provider turns
/// and no installed service access. UI acceptance is performed separately.
@MainActor
struct LocalServerLiveTests {
    @Test(.enabled(if: ProcessInfo.processInfo.environment["HYADEX_LOCAL_SERVER_LIVE_TEST"] == "1"))
    func realRunnerAuthenticatesReusesAndRestartsAnIsolatedInstance() async throws {
        let home = try temporaryHome()
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home, instanceName: "mac-test")
        let first = LocalServerController(configuration: config)
        let competitor = LocalServerController(configuration: config)
        do {
            async let one = first.start()
            async let two = first.start()
            let (connection, repeated) = try await (one, two)
            #expect(connection.endpoint == repeated.endpoint)
            #expect(connection.token == repeated.token)
            #expect(first.isRunning)
            let client = HyadexClient(endpoint: connection.endpoint, token: connection.token)
            let health = try await client.health()
            #expect(health.raw["state_dir"].string == config.stateDirectory.path)
            #expect(health.raw["auth_required"].bool == true)
            let savedID = try #require(health.raw["server_identity"].string)
            let processID = try #require(health.raw["server_instance_id"].string)
            let noAuth = HyadexClient(endpoint: connection.endpoint, token: nil)
            await #expect(throws: HyadexClientError.self) { try await noAuth.health() }
            let wrongAuth = HyadexClient(endpoint: connection.endpoint, token: "wrong-fixture-token")
            await #expect(throws: HyadexClientError.self) { try await wrongAuth.health() }

            // A second app does not launch, restart or stop the first app's server.
            await #expect(throws: LocalServerError.self) { try await competitor.start() }
            #expect(competitor.errorMessage?.contains("另一个 Hyadex") == true)
            #expect(try await client.health().raw["server_instance_id"].string == processID)
            #expect(try await first.start().endpoint == connection.endpoint)

            // Persist a real server object, then restart and read it back.
            let created = try await client.createSession(["backend": "codex", "title": "Local lifecycle fixture", "cwd": .string(home.path)])
            let sessionID = try #require(created["id"].string)
            await first.shutdown()
            #expect(first.state == .idle)
            await #expect(throws: HyadexClientError.self) {
                try await client.request("GET", "api/health", timeout: 1)
            }
            let restarted = try await first.start()
            #expect(restarted.endpoint.id == connection.endpoint.id)
            #expect(restarted.token == connection.token)
            let restored = HyadexClient(endpoint: restarted.endpoint, token: restarted.token)
            let restartedHealth = try await restored.health()
            #expect(restartedHealth.raw["server_identity"].string == savedID)
            #expect(restartedHealth.raw["server_instance_id"].string != processID)
            #expect(try await restored.sessions().contains { $0["id"].string == sessionID })
        } catch {
            await first.shutdown()
            await competitor.shutdown()
            try? FileManager.default.removeItem(at: home)
            throw error
        }
        await first.shutdown()
        await competitor.shutdown()
        try FileManager.default.removeItem(at: home)
    }
}

/// Opt in like the test above: phones reach the backend only while 允许手机连接 is on.
@MainActor
struct LocalServerPhoneAccessLiveTests {
    @Test(.enabled(if: ProcessInfo.processInfo.environment["HYADEX_LOCAL_SERVER_LIVE_TEST"] == "1"))
    func phonesReachTheBackendOnlyWhileAllowed() async throws {
        let home = try temporaryHome()
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let config = LocalServerConfiguration(sourceDirectory: source, homeDirectory: home, instanceName: "mac-phone-test",
                                              port: 17_871, phoneAccess: true)
        let controller = LocalServerController(configuration: config)
        do {
            let open = try await controller.start()
            let local = HyadexClient(endpoint: open.endpoint, token: open.token)
            // The pairing addresses come from the server, as the settings code shows them.
            let addresses = try await local.request("GET", "api/connection-addresses", nativeToken: true)["urls"].array
                .compactMap(\.string)
            let lan = try #require(addresses.first, "this Mac has no LAN or Tailscale address")
            let remote = HyadexClient(endpoint: ServerEndpoint(name: "phone", baseURL: try #require(URL(string: lan))), token: open.token)
            #expect(try await remote.health().raw["auth_required"].bool == true)
            // Back to this Mac only: the same token, no longer reachable from the LAN.
            let closed = try #require(try await controller.setPhoneAccess(false))
            #expect(closed.token == open.token)
            #expect(try await HyadexClient(endpoint: closed.endpoint, token: closed.token).health().raw["auth_required"].bool == true)
            await #expect(throws: HyadexClientError.self) { try await remote.request("GET", "api/health", timeout: 2) }
        } catch {
            await controller.shutdown()
            try? FileManager.default.removeItem(at: home)
            throw error
        }
        await controller.shutdown()
        try FileManager.default.removeItem(at: home)
    }
}

private func temporaryHome() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appending(path: "hyadex-local-test-\(UUID().uuidString)").resolvingSymlinksInPath()
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    return url
}

private func fixtureSource(in home: URL, withPython: Bool = true, sleeping: Bool = false, ignoresTermination: Bool = false) throws -> URL {
    let source = home.appending(path: "source")
    for path in ["scripts", "hyadex", ".venv/bin"] {
        try FileManager.default.createDirectory(at: source.appending(path: path), withIntermediateDirectories: true)
    }
    let signal = ignoresTermination ? "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" : ""
    let script = sleeping ? "import os, pathlib, time\n" + signal + "pathlib.Path(os.environ['HOME'], 'child-pid').write_text(str(os.getpid()))\ntime.sleep(60)\n" : ""
    try Data(script.utf8).write(to: source.appending(path: "scripts/dev_server.py"))
    try Data().write(to: source.appending(path: "hyadex/main.py"))
    if withPython {
        // /usr/bin/python3 is an Apple developer-tool shim. Invoking a symlink
        // named `python` makes it search for Python 2 instead. Resolve the real
        // interpreter before constructing this deliberately minimal fixture.
        let interpreter = sleeping ? try fixturePython() : URL(fileURLWithPath: "/usr/bin/false")
        try FileManager.default.createSymbolicLink(at: source.appending(path: ".venv/bin/python"),
                                                  withDestinationURL: interpreter)
    }
    return source
}

private func fixturePython() throws -> URL {
    let command = Process()
    command.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    command.arguments = ["--find", "python3"]
    let output = Pipe()
    command.standardOutput = output
    command.standardError = FileHandle.nullDevice
    try command.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    command.waitUntilExit()
    let path = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    guard command.terminationStatus == 0, FileManager.default.isExecutableFile(atPath: path) else {
        throw LocalServerError.launchFailed
    }
    return URL(fileURLWithPath: path)
}

private func boundSocket() throws -> (Int32, Int) {
    let descriptor = socket(AF_INET, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw LocalServerError.portUnavailable(0) }
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let bound = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    guard bound == 0 else { close(descriptor); throw LocalServerError.portUnavailable(0) }
    var length = socklen_t(MemoryLayout<sockaddr_in>.size)
    let result = withUnsafeMutablePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(descriptor, $0, &length) }
    }
    guard result == 0 else { close(descriptor); throw LocalServerError.portUnavailable(0) }
    return (descriptor, Int(in_port_t(bigEndian: address.sin_port)))
}
