import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

@MainActor
struct AppModelConnectionTests {
    @Test func startsEmptyAndDisconnected() {
        let model = AppModel()
        #expect(model.connection == .disconnected)
        #expect(model.connection.label == "未连接")
        expectEmpty(model)
        #expect(model.selection == nil)
        #expect(!model.goalSupport(forChat: "missing").available)
        #expect(!model.serverIsLocal)
    }

    @Test func disconnectedCreationCannotFabricateServerObjects() {
        let model = AppModel()
        #expect(model.createChat(backend: .codex, model: "", cwd: "~", permission: "Default").isEmpty)
        model.createGroup(title: "new group", cwd: "~", participants: [])
        model.createRun(goal: "new goal", cwd: "~", approval: .plan, isolation: .worktree,
                        reviewer: .none, verifyCommand: "")
        model.createJob(title: "new job", prompt: "prompt", target: .chat("missing"),
                        schedule: .cron("0 0 9 * * *"), enabled: true)
        model.send("hello", toChat: "missing")
        model.send("hello", toGroup: "missing", mode: .sequential)
        model.stop("missing")
        model.askSide("question", inChat: "missing")
        expectEmpty(model)
        #expect(model.errorMessage?.contains("尚未连接服务器") == true)
    }

    @Test func disconnectedActionsDoNotAlterExistingFixtures() {
        let model = AppModel(snapshotFixturesAt: .now)
        let chats = model.chats, groups = model.groups, runs = model.runs, jobs = model.jobs
        let timelines = model.timelines, queues = model.queues, goals = model.goals, mcp = model.mcp
        model.send("hello", toChat: "c1")
        model.send("queued", toChat: "c3")
        model.send("@all hello", toGroup: "g1", mode: .sequential)
        model.stop("c3")
        model.decide("a1", .allowed, in: "c2")
        model.approve(run: "r2")
        model.pause(run: "r1")
        model.resume(run: "r1")
        model.abort(run: "r1")
        model.feedback(run: "r2", text: "change it")
        model.setModel("changed", forChat: "c1")
        model.setEffort("changed", forChat: "c1")
        model.setPermission("changed", forChat: "c1")
        model.togglePinned("c1")
        model.removeMember("claude", fromGroup: "g1")
        model.updateMember("codex", inGroup: "g1", model: "changed")
        model.controlMCP("disable", server: nil, forChat: "c1")
        model.setJob("j1", enabled: false)
        model.deleteJob("j1")
        if let first = model.queues["c3"]?.first {
            model.editQueued(first.id, in: "c3")
            model.withdrawQueued(first.id, in: "c3")
            model.runQueuedNow(first.id, in: "c3")
        }
        model.resumeQueue(in: "c3")
        #expect(model.chats == chats && model.groups == groups)
        #expect(model.runs == runs && model.jobs == jobs)
        #expect(model.timelines == timelines && model.queues == queues)
        #expect(model.goals == goals && model.mcp == mcp)
        #expect(model.drafts.isEmpty)
        #expect(model.errorMessage != nil)
    }

    @Test func disconnectClearsAllServerContent() {
        let model = AppModel(snapshotFixturesAt: .now)
        model.drafts["c1"] = "draft"
        model.sideChats["c1"] = SideChat(sideChatID: "old")
        model.pendingSends["c1"] = [PendingSend(id: "pending", text: "hello")]
        model.disconnect()
        expectEmpty(model)
        #expect(model.connection == .disconnected)
        #expect(model.selection == nil)
        #expect(model.drafts.isEmpty && model.pendingSends.isEmpty)
        #expect(model.goals.isEmpty && model.commands.isEmpty && model.mcp.isEmpty)
        #expect(model.errorMessage == nil)
    }

    @Test func failedConnectionStaysEmpty() async {
        let fixture = ConnectionFixture { _ in throw URLError(.cannotConnectToHost) }
        defer { fixture.close() }
        let model = AppModel(session: fixture.session)
        await model.connect(to: fixture.endpoint, token: nil, remember: false)
        expectEmpty(model)
        if case .offline = model.connection {} else { Issue.record("Expected the connection failure") }
        #expect(model.errorMessage?.contains("无法连接") == true)
        model.send("hello", toChat: "c1")
        expectEmpty(model)
    }

    @Test func disconnectInvalidatesAnInflightConnection() async {
        let gate = ConnectionGate()
        let fixture = ConnectionFixture { request in
            if request.url?.lastPathComponent == "health" { await gate.pause() }
            return ConnectionFixture.emptyResponse(request)
        }
        defer { fixture.close() }
        let model = AppModel(session: fixture.session)
        let attempt = Task { await model.connect(to: fixture.endpoint, token: nil, remember: false) }
        await gate.waitUntilPaused()
        model.disconnect()
        await gate.release()
        await attempt.value
        expectEmpty(model)
        #expect(model.connection == .disconnected)
        #expect(model.errorMessage == nil)
        #expect(model.live == nil)
    }

    @Test func lateFailureCannotClearANewerConnection() async {
        let gate = ConnectionGate()
        let fixture = ConnectionFixture { request in
            if request.url?.path.contains("/old/") == true {
                await gate.pause()
                throw URLError(.cannotConnectToHost)
            }
            return ConnectionFixture.emptyResponse(request)
        }
        defer { fixture.close() }
        let model = AppModel(session: fixture.session)
        defer { model.disconnect() }
        let old = ServerEndpoint(name: "old", baseURL: fixture.endpoint.baseURL.appending(path: "old"))
        let attempt = Task { await model.connect(to: old, token: nil, remember: false) }
        await gate.waitUntilPaused()
        await model.connect(to: fixture.endpoint, token: nil, remember: false)
        #expect(model.connection == .connected(version: "test"))
        await gate.release()
        await attempt.value
        #expect(model.isLive)
        #expect(model.currentServerID == fixture.endpoint.id)
        #expect(model.connection == .connected(version: "test"))
        #expect(model.errorMessage == nil)
    }

    @Test func staleListsCannotPopulateANewerConnection() async {
        let gate = ConnectionGate()
        let fixture = ConnectionFixture { request in
            if request.url?.path.contains("/old/") == true, request.url?.lastPathComponent == "sessions" {
                await gate.pause()
                return "{\"sessions\":[{\"id\":\"stale\",\"title\":\"stale\",\"backend\":\"codex\",\"cwd\":\"/tmp\"}]}"
            }
            return ConnectionFixture.emptyResponse(request)
        }
        defer { fixture.close() }
        let model = AppModel(session: fixture.session)
        defer { model.disconnect() }
        let old = ServerEndpoint(name: "old", baseURL: fixture.endpoint.baseURL.appending(path: "old"))
        let attempt = Task { await model.connect(to: old, token: nil, remember: false) }
        await gate.waitUntilPaused()
        await model.connect(to: fixture.endpoint, token: nil, remember: false)
        await gate.release()
        await attempt.value
        #expect(model.isLive && model.chats.isEmpty)
        #expect(model.currentServerID == fixture.endpoint.id)
        #expect(model.connection == .connected(version: "test"))
        #expect(model.errorMessage == nil)
    }

    private func expectEmpty(_ model: AppModel, sourceLocation: SourceLocation = #_sourceLocation) {
        #expect(!model.isLive, sourceLocation: sourceLocation)
        #expect(model.chats.isEmpty && model.groups.isEmpty && model.runs.isEmpty && model.jobs.isEmpty,
                sourceLocation: sourceLocation)
        #expect(model.timelines.isEmpty && model.queues.isEmpty && model.sideChats.isEmpty,
                sourceLocation: sourceLocation)
        #expect(model.selectedChanges.files.isEmpty, sourceLocation: sourceLocation)
    }
}

/// Transport-only fixtures exercise real model/controller ownership, not app acceptance.
private struct ConnectionFixture {
    let endpoint: ServerEndpoint
    let session: URLSession

    init(handler: @escaping ConnectionProtocol.Handler) {
        let id = UUID().uuidString.lowercased()
        endpoint = ServerEndpoint(name: "Test", baseURL: URL(string: "http://127.0.0.1:1/\(id)")!)
        ConnectionProtocol.registry.set(handler, for: id)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConnectionProtocol.self]
        session = URLSession(configuration: configuration)
    }

    func close() {
        session.invalidateAndCancel()
        ConnectionProtocol.registry.remove(endpoint.baseURL.lastPathComponent)
    }

    static func emptyResponse(_ request: URLRequest) -> String {
        switch request.url?.lastPathComponent {
        case "health": "{\"server_version\":\"test\"}"
        case "sessions": "{\"sessions\":[]}"
        case "groups": "{\"groups\":[]}"
        case "runs": "{\"runs\":[]}"
        case "jobs": "{\"jobs\":[]}"
        default: "{}"
        }
    }
}

private final class ConnectionProtocol: URLProtocol, @unchecked Sendable {
    typealias Handler = @Sendable (URLRequest) async throws -> String
    static let registry = Registry()
    private var loadingTask: Task<Void, Never>?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let request = request
        let key = request.url?.pathComponents.dropFirst().first ?? ""
        let handler = Self.registry.handler(for: key)
        loadingTask = Task { @Sendable [self, request, handler] in
            do {
                guard let handler, let url = request.url else { throw URLError(.unsupportedURL) }
                let body = try await handler(request)
                guard !Task.isCancelled else { return }
                let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil,
                                               headerFields: ["Content-Type": "application/json"])!
                self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                self.client?.urlProtocol(self, didLoad: Data(body.utf8))
                self.client?.urlProtocolDidFinishLoading(self)
            } catch {
                guard !Task.isCancelled else { return }
                self.client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }

    override func stopLoading() { loadingTask?.cancel() }

    final class Registry: @unchecked Sendable {
        private let lock = NSLock()
        private var handlers: [String: Handler] = [:]
        func set(_ handler: @escaping Handler, for key: String) { lock.withLock { handlers[key] = handler } }
        func remove(_ key: String) { _ = lock.withLock { handlers.removeValue(forKey: key) } }
        func handler(for key: String) -> Handler? { lock.withLock { handlers[key] } }
    }
}

private actor ConnectionGate {
    private var paused = false
    private var opened = false
    private var pauseContinuation: CheckedContinuation<Void, Never>?
    private var observer: CheckedContinuation<Void, Never>?

    func pause() async {
        paused = true
        observer?.resume()
        observer = nil
        if opened { return }
        await withCheckedContinuation { pauseContinuation = $0 }
    }

    func waitUntilPaused() async {
        if paused { return }
        await withCheckedContinuation { observer = $0 }
    }

    func release() {
        opened = true
        pauseContinuation?.resume()
        pauseContinuation = nil
    }
}
