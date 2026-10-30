import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// A chat's agent asking another chat: its `ask` call, the receipt, and the consult records.
struct ConsultTests {
    static let claudeTool = "mcp___hyadex_internal_provider_9f3a2c71__run"
    static let codexTool = "_hyadex_internal_provider_9f3a2c71/run"

    @Test func askCallsOfBothProvidersAreRecognized() {
        let claude: JSON = ["name": .string(Self.claudeTool), "input": [
            "helper": "ask", "arguments": ["ask", "Backend", "--deep"], "stdin": "  Which port?\n"]]
        let call = ConsultCall.parse(tool: claude)
        #expect(call == ConsultCall(target: "Backend", deep: true, question: "Which port?"))
        let codex: JSON = ["name": .string(Self.codexTool), "input": [
            "helper": "ask", "arguments": ["ask", "--question", "锁在哪里？", "后端重构"]]]
        #expect(ConsultCall.parse(tool: codex) == ConsultCall(target: "后端重构", deep: false, question: "锁在哪里？"))
        // Listing chats, other helpers and other tools stay ordinary tool rows.
        let list: JSON = ["name": .string(Self.codexTool), "input": ["helper": "ask", "arguments": ["list"]]]
        #expect(ConsultCall.parse(tool: list) == nil)
        let jobs: JSON = ["name": .string(Self.claudeTool), "input": ["helper": "jobs", "arguments": ["list"]]]
        #expect(ConsultCall.parse(tool: jobs) == nil)
        #expect(ConsultCall.parse(tool: ["name": "Bash", "input": ["command": "ls"]]) == nil)
        #expect(SessionProjection.describe(tool: list, cwd: "/w").title == "查看可以询问的聊天")
    }

    @Test func receiptsCarryTheAnswerOrWhyThereIsNone() {
        let answer = ConsultReceipt.parse(output: #"{"ok":true,"consult_id":"cns_1","chat":"Backend","chat_id":"b","mode":"basic","status":"completed","answer":"7872","confident":false,"suggest_deep":true}"#,
                                          failed: false)
        #expect(answer.ok && answer.answer == "7872" && answer.consultID == "cns_1")
        #expect(answer.confident == false && answer.suggestDeep && answer.status == .completed)
        let denied = ConsultReceipt.parse(output: #"{"ok":false,"chat":"Backend","status":"denied","error":"the user declined this deep consult"}"#,
                                          failed: true)
        #expect(!denied.ok && denied.status == .denied && denied.error == "the user declined this deep consult")
        let plain = ConsultReceipt.parse(output: "Hyadex helper timed out", failed: true)
        #expect(!plain.ok && plain.error == "Hyadex helper timed out")
        // Codex's form (seen live): the receipt is the text of an MCP result.
        let codex = ConsultReceipt.parse(
            output: #"{"content":[{"type":"text","text":"{\"ok\":true,\"consult_id\":\"cns_2\",\"status\":\"completed\",\"answer\":\"One file: README.md\"}"}],"structuredContent":null,"_meta":null}"#,
            failed: false)
        #expect(codex.ok && codex.answer == "One file: README.md" && codex.consultID == "cns_2")
    }

    @Test func theTimelineShowsAConsultCardThatFinishesWithItsReceipt() {
        var projection = SessionProjection(backend: .claude, cwd: "/w")
        let input: JSON = ["helper": "ask", "arguments": ["ask", "Backend"], "stdin": "Which port?"]
        projection.apply([
            ["seq": 1, "type": "turn_started", "id": "t1", "prompt": "Ask the backend chat", "ts": "2030-01-01T00:00:00Z"],
            ["seq": 2, "type": "tool_started", "tool": ["id": "c1", "name": .string(Self.claudeTool), "input": input],
             "ts": "2030-01-01T00:00:01Z"],
        ])
        guard case .consult("c1", let running)? = projection.items.last else {
            Issue.record("expected a consult card, got \(String(describing: projection.items.last))")
            return
        }
        #expect(running.running && running.target == "Backend" && running.question == "Which port?")
        // MCP results arrive as content parts.
        projection.apply([
            ["seq": 3, "type": "tool_finished", "tool_id": "c1",
             "tool": ["id": "c1", "name": .string(Self.claudeTool), "input": input],
             "output": ["content": [["type": "text", "text": #"{"ok":true,"consult_id":"cns_9","chat":"Backend","answer":"7872","status":"completed"}"#]]],
             "ts": "2030-01-01T00:00:09Z"],
        ])
        guard case .consult("c1", let finished)? = projection.items.last else {
            Issue.record("the consult card disappeared")
            return
        }
        #expect(!finished.running && finished.receipt?.answer == "7872" && finished.receipt?.consultID == "cns_9")
    }

    @MainActor @Test func consultRecordsAndTheCardsTheyBelongTo() {
        let json: JSON = ["outgoing": [[
            "id": "cns_2", "source": ["kind": "chat", "id": "a", "title": "Mac app", "backend": "claude"],
            "target": ["id": "b", "title": "Backend", "backend": "codex"], "mode": "deep", "question": "Which port?",
            "status": "running", "steps": [["kind": "command", "title": "rg port"]], "created_at": .number(1_900_000_000),
            "usage": ["input_tokens": .number(1200), "output_tokens": .number(40)],
        ]], "incoming": [[
            "id": "cns_3", "source": ["kind": "user"], "target": ["id": "a", "title": "Mac app"], "mode": "deep",
            "question": "What changed?", "status": "completed", "answer": "Two files.", "duration_seconds": .number(42.5),
        ]]]
        let consults = ChatConsults.parse(json)
        #expect(consults.outgoing.first?.targetBackend == .codex)
        #expect(consults.outgoing.first?.steps == [ConsultStep(kind: "command", title: "rg port")])
        #expect(consults.outgoing.first?.inputTokens == 1200)
        #expect(consults.incoming.first?.sourceID == nil && consults.incoming.first?.duration == 42.5)

        let model = AppModel(snapshotFixturesAt: .now)
        model.consults["a"] = consults
        let running = ConsultCall(target: "backend", deep: true, question: "Which port?")
        #expect(model.consultRecord(for: running, inChat: "a")?.id == "cns_2")
        #expect(model.consultRecord(for: ConsultCall(target: "Backend", deep: false, question: "Which port?"), inChat: "a") == nil)
        var finished = running
        finished.receipt = ConsultReceipt(ok: true, consultID: "cns_2")
        #expect(model.consultRecord(for: finished, inChat: "a")?.id == "cns_2")
    }

    @Test func chatMentionsInTheComposer() {
        let targets = [ConsultTarget(id: "b", title: "Backend B", backend: .codex),
                       ConsultTarget(id: "c", title: "后端重构", backend: .claude, allowsDeep: false),
                       ConsultTarget(id: "d", title: "Notes about backend")]
        #expect(ChatMention.partial(in: "问一下 @后端") == "后端")
        #expect(ChatMention.partial(in: "@「Backend") == "Backend")
        #expect(ChatMention.partial(in: "mail me at a@b") == nil)
        #expect(ChatMention.partial(in: "@「后端重构」 端口？") == nil)
        #expect(ChatMention.matches("back", in: targets).map(\.id) == ["b", "d"])
        #expect(ChatMention.matches("", in: targets).count == 3)
        // Only a mention that opens the message asks that chat directly.
        let direct = ChatMention.direct(in: "  @「后端重构」 端口是多少？", targets: targets)
        #expect(direct?.target.id == "c" && direct?.question == "端口是多少？")
        #expect(ChatMention.direct(in: "请参考 @「后端重构」 的结论", targets: targets) == nil)
        #expect(ChatMention.direct(in: "@「Unknown」 hi", targets: targets) == nil)
        #expect(ChatMention.token("Backend B") == "@「Backend B」")
        let parsed = ConsultTarget.parse(["id": "b", "title": "Backend B", "backend": "codex", "modes": ["basic"], "working": true])
        #expect(parsed == ConsultTarget(id: "b", title: "Backend B", backend: .codex, allowsDeep: false, working: true))
    }

    @Test func aQuestionTheUserAskedShowsAsItsCard() {
        var projection = SessionProjection(backend: .claude, cwd: "/w")
        projection.apply([
            ["seq": 1, "type": "chat_consult_asked", "consult_id": "cns_u1",
             "target": ["id": "b", "title": "Backend B", "backend": "codex"], "mode": "deep", "question": "Which port?",
             "ts": "2030-01-01T00:00:00Z"],
        ])
        guard case .consult("consult-cns_u1", let call)? = projection.items.last else {
            Issue.record("expected the user's consult card")
            return
        }
        #expect(call.byUser && call.deep && call.consultID == "cns_u1" && call.target == "Backend B")
    }

    @Test func consultSettingsRoundTrip() {
        let loaded = ChatSettings.parse(["consult_inbound": "basic", "consult_deep_approval": false,
                                         "consult_deep_allowed": ["b", "c"]])
        #expect(loaded.consultInbound == .basic && !loaded.consultDeepApproval && loaded.consultDeepAllowed == ["b", "c"])
        #expect(ChatSettings.parse([:]).consultInbound == .deep && ChatSettings.parse([:]).consultDeepApproval)
        var edited = loaded
        edited.consultInbound = .off
        edited.consultDeepAllowed = []
        let changes = loaded.changes(to: edited)
        #expect(changes["consult_inbound"] == "off")
        #expect(changes["consult_deep_allowed"] == .array([]))
        #expect(changes["consult_deep_approval"] == nil)
    }
}
