import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// Each kind of native request is answered with the `response` body the
/// server validates for it (hyadex/providers/*/interactions.py).
struct InteractionTests {
    static func requested(_ method: String, _ params: JSON, backend: String = "claude", seq: Int = 2) -> JSON {
        ["seq": .number(Double(seq)), "type": .string("\(backend)_interaction_requested"),
         "interaction": ["id": "req-1", "method": .string(method), "params": params]]
    }

    static func project(_ event: JSON, backend: AgentBackend = .claude) -> SessionProjection {
        var projection = SessionProjection(backend: backend, cwd: "/tmp/work")
        projection.apply([["seq": 1, "type": "turn_started", "prompt": "hi", "client_request_id": "r1"], event])
        return projection
    }

    /// Claude's AskUserQuestion, as the server forwards it.
    static let claudeQuestions: JSON = ["questions": [
        ["question": "Which database?", "header": "Database", "multiSelect": false, "id": "question_1", "isOther": true,
         "options": [["label": "Postgres", "description": "Relational"], ["label": "SQLite", "description": "Embedded"]]],
        ["question": "Which checks?", "header": "Checks", "multiSelect": true, "id": "question_2", "isOther": true,
         "options": [["label": "Lint", "description": ""], ["label": "Tests", "description": ""]]],
    ]]

    @Test func claudeQuestionsBecomeAFormAndAnswerWithAnAnswersMap() throws {
        var projection = Self.project(Self.requested("item/tool/requestUserInput", Self.claudeQuestions))
        #expect(projection.needsAction)
        guard case .form(let id, _, let form, .pending)? = projection.items.last else {
            Issue.record("expected a pending form, got \(String(describing: projection.items.last))")
            return
        }
        #expect(form.kind == .questions)
        #expect(form.fields.map(\.header) == ["Database", "Checks"])
        #expect(form.fields[0].input == .choice(options: [FormOption(value: "Postgres", label: "Postgres", detail: "Relational"),
                                                          FormOption(value: "SQLite", label: "SQLite", detail: "Embedded")],
                                                multiple: false, allowsOther: true))
        guard case .choice(_, true, true) = form.fields[1].input else {
            Issue.record("expected a multiple choice with other")
            return
        }

        let partial = ["question_1": ["SQLite"]]
        #expect(!form.isComplete(partial))
        let answers = ["question_1": ["SQLite"], "question_2": ["Lint", "Tests", "typecheck"]]
        #expect(form.isComplete(answers))
        let reply = form.reply(answers)
        #expect(reply.response == ["answers": [
            "question_1": ["answers": ["SQLite"]],
            "question_2": ["answers": ["Lint", "Tests", "typecheck"]],
        ]])
        #expect(reply.outcome == .answered("Database：SQLite；Checks：Lint, Tests, typecheck"))
        #expect(form.dismissal.response == ["answers": [:]])
        #expect(form.dismissal.outcome == .skipped)

        projection.noteDecision(reply.outcome, for: id)
        projection.apply(["seq": 3, "type": "claude_interaction_resolved", "interaction_id": "req-1"])
        #expect(!projection.needsAction)
        guard case .form(_, _, _, .answered)? = projection.items.last else {
            Issue.record("expected the answered form")
            return
        }
    }

    @Test func codexQuestionsKeepSecretAndFreeTextFields() {
        let params: JSON = ["threadId": "t", "turnId": "u", "itemId": "i", "isBlocking": true, "autoResolutionMs": nil,
                            "questions": [
                                ["id": "token", "header": "Token", "question": "Paste the token", "isOther": false,
                                 "isSecret": true, "options": nil],
                                ["id": "env", "header": "Env", "question": "Which environment?", "isOther": true,
                                 "isSecret": false, "options": [["label": "staging", "description": "Safe"]]],
                            ]]
        let form = InteractionForm.parse(method: "item/tool/requestUserInput", params: params)
        #expect(form?.fields.map(\.input) == [
            .text(secret: true),
            .choice(options: [FormOption(value: "staging", label: "staging", detail: "Safe")], multiple: false,
                    allowsOther: true),
        ])
        let reply = form?.reply(["token": ["abc"], "env": ["prod"]])
        #expect(reply?.response == ["answers": ["token": ["answers": ["abc"]], "env": ["answers": ["prod"]]]])
        // A secret never shows in the answered card.
        #expect(reply?.outcome == .answered("Token：••••；Env：prod"))
    }

    @Test func mcpFormFieldsFollowTheSchemaAndSendTypedContent() throws {
        let params: JSON = ["threadId": "t", "turnId": nil, "serverName": "tickets", "mode": "form",
                            "message": "Create a ticket", "_meta": nil, "requestedSchema": [
                                "type": "object", "required": ["title", "priority"],
                                "properties": [
                                    "title": ["type": "string", "title": "Title"],
                                    "priority": ["type": "string", "oneOf": [["const": "p1", "title": "Urgent"],
                                                                             ["const": "p2", "title": "Normal"]]],
                                    "estimate": ["type": "integer", "title": "Estimate"],
                                    "notify": ["type": "boolean", "default": true],
                                    "labels": ["type": "array", "items": ["type": "string", "enum": ["bug", "ui"]]],
                                ],
                            ]]
        let form = try #require(InteractionForm.parse(method: "mcpServer/elicitation/request", params: params))
        #expect(form.title == "tickets 需要你填写")
        #expect(form.message == "Create a ticket")
        // Required fields first, then the rest by name.
        #expect(form.fields.map(\.id) == ["priority", "title", "estimate", "labels", "notify"])
        #expect(form.fields.map(\.required) == [true, true, false, false, false])
        #expect(form.fields[4].initial == ["true"])

        #expect(!form.isComplete(["title": ["Crash"]]))
        #expect(!form.isComplete(["title": ["Crash"], "priority": ["p1"], "estimate": ["soon"]]))
        let answers = ["title": ["Crash"], "priority": ["p1"], "estimate": ["3"], "labels": ["bug", "ui"],
                       "notify": ["false"]]
        #expect(form.isComplete(answers))
        let reply = form.reply(answers)
        #expect(reply.response == ["action": "accept", "content": [
            "title": "Crash", "priority": "p1", "estimate": 3, "labels": ["bug", "ui"], "notify": false,
        ]])
        #expect(reply.outcome == .answered("priority：Urgent；Title：Crash；Estimate：3；labels：bug, ui；notify：false"))
        #expect(form.dismissal.response == ["action": "decline"])
    }

    @Test func urlElicitationOpensThePageAndConfirms() {
        let params: JSON = ["threadId": "t", "turnId": "u", "serverName": "github", "mode": "url", "_meta": nil,
                            "message": "Authorize access", "url": "https://example.com/authorize", "elicitationId": "e1"]
        let form = InteractionForm.parse(method: "mcpServer/elicitation/request", params: params)
        #expect(form?.url == URL(string: "https://example.com/authorize"))
        #expect(form?.fields.isEmpty == true)
        #expect(form?.reply([:]).response == ["action": "accept", "content": [:]])
    }

    @Test func codexPermissionRequestGrantsWhatWasAsked() {
        let params: JSON = ["threadId": "t", "turnId": "u", "itemId": "i", "environmentId": nil, "startedAtMs": 1,
                            "cwd": "/tmp/work", "reason": "Needs the network",
                            "permissions": ["network": ["enabled": true],
                                            "fileSystem": ["read": nil, "write": ["/tmp/work/out"]]]]
        let projection = Self.project(Self.requested("item/permissions/requestApproval", params, backend: "codex"),
                                      backend: .codex)
        guard case .approval(_, _, let title, let detail, let reason, .pending, let choices)? = projection.items.last else {
            Issue.record("expected a pending approval")
            return
        }
        #expect(title == "更多权限")
        #expect(detail == "访问网络\n写入 out")
        #expect(reason == "Needs the network")
        let grant: JSON = ["network": ["enabled": true], "fileSystem": ["write": ["/tmp/work/out"]]]
        #expect(choices.map(\.reply.response) == [
            ["permissions": [:], "scope": "turn"],
            ["permissions": grant, "scope": "session"],
            ["permissions": grant, "scope": "turn"],
        ])
    }

    @Test func legacyCodexApprovalsUseReviewDecisions() {
        let params: JSON = ["conversationId": "c", "callId": "x", "approvalId": nil,
                            "command": ["/bin/zsh", "-lc", "ls -la"], "cwd": "/tmp/work", "reason": nil, "parsedCmd": []]
        let projection = Self.project(Self.requested("execCommandApproval", params, backend: "codex"), backend: .codex)
        guard case .approval(_, _, let title, let detail, _, .pending, let choices)? = projection.items.last else {
            Issue.record("expected a pending approval")
            return
        }
        #expect(title == "运行命令")
        #expect(detail == "ls -la")
        #expect(choices.map(\.reply.response) == [["decision": "denied"], ["decision": "approved_for_session"],
                                                  ["decision": "approved"]])
    }

    @Test func networkRulesAreOfferedAsChoices() {
        let params: JSON = ["availableDecisions": [
            "accept",
            ["applyNetworkPolicyAmendment": ["network_policy_amendment": ["host": "pypi.org", "action": "allow"]]],
            ["applyNetworkPolicyAmendment": ["network_policy_amendment": ["host": "pypi.org", "action": "deny"]]],
            "decline",
        ]]
        let choices = ApprovalChoice.choices(method: "item/commandExecution/requestApproval", params: params)
        #expect(choices.map(\.label) == ["允许", "始终允许访问 pypi.org", "始终阻止访问 pypi.org", "拒绝"])
        #expect(choices.map(\.reply.outcome) == [.allowed, .allowedAlways, .denied, .denied])
        #expect(choices[1].reply.response == ["decision": params["availableDecisions"][1]])
    }

    @Test func decisionsMapToTheOfferedChoice() {
        let approval = TimelineItem.approval(id: "a", author: .user, title: "运行命令", command: "ls", reason: "",
                                             decision: .pending,
                                             choices: ApprovalChoice.choices(method: "item/commandExecution/requestApproval",
                                                                             params: ["availableDecisions": ["accept", "cancel"]]))
        #expect(approval.reply(for: .allowed)?.response == ["decision": "accept"])
        #expect(approval.reply(for: .denied)?.response == ["decision": "cancel"])
        #expect(approval.reply(for: .allowedForSession) == nil)
    }

    @Test func pendingQuestionsOfAGroupMemberAreSurfaced() throws {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/work")
        projection.apply([
            ["seq": 1, "type": "turn_started", "prompt": "hi", "client_request_id": "dispatch-1"],
            Self.requested("item/tool/requestUserInput", Self.claudeQuestions),
        ])
        let slice = try #require(projection.turn(forRequest: "dispatch-1"))
        #expect(slice.pendingApprovals.map(\.id) == ["req-1"])
        let approval = GroupApproval(interactionID: "req-1", handle: "claude", backend: .claude, sessionID: "s",
                                     request: slice.pendingApprovals[0])
        #expect(approval.title == "需要你回答")
        #expect(approval.detail == "Which database?\nWhich checks?")
        #expect(approval.reply(for: .allowed) == nil)
    }
}

/// Answers being written arrive on the non-durable text lane and give way to
/// their durable messages.
struct LiveTextTests {
    static func snapshot(_ items: [JSON]) -> JSON {
        ["type": "assistant_text_stream", "revision": 3, "items": .array(items)]
    }

    @Test func snapshotsBecomeStreamingMessagesInOrder() {
        var live = LiveText()
        live.apply(snapshot: Self.snapshot([
            ["item_id": "b", "run_id": "r1", "text": "Second", "after_seq": 9],
            ["item_id": "a", "run_id": "r1", "text": "First", "after_seq": 4],
            ["item_id": "empty", "run_id": "r1", "text": "", "after_seq": 2],
        ]))
        let messages = live.messages(author: .agent("codex", .codex))
        #expect(messages.map(\.id) == ["live-a", "live-b"])
        guard case .message(_, _, "First", _, _, _, true)? = messages.first else {
            Issue.record("expected a streaming message")
            return
        }
        live.apply(snapshot: Self.snapshot([]))
        #expect(live.items.isEmpty)
    }

    @Test func codexAnswersSettleByItemAndClaudeBlocksByRun() {
        var live = LiveText()
        live.apply(snapshot: Self.snapshot([
            ["item_id": "note", "run_id": "codex-run", "text": "Looking", "after_seq": 4],
            ["item_id": "msg_1:1", "run_id": "claude-run", "text": "All gr", "after_seq": 6],
            ["item_id": "msg_1:3", "run_id": "claude-run", "text": "Next", "after_seq": 8],
        ]))
        // Codex commentary settles into a trace item with the same id.
        live.settle(with: ["seq": 7, "type": "reasoning_summary", "item_id": "note", "run_id": "codex-run"])
        #expect(live.items.map(\.id) == ["msg_1:1", "msg_1:3"])
        // Claude's finished block carries no item id: the run's answer that began before it.
        live.settle(with: ["seq": 7, "type": "assistant_text", "run_id": "claude-run", "text": "All green."])
        #expect(live.items.map(\.id) == ["msg_1:3"])
        live.settle(with: ["seq": 9, "type": "turn_stopped", "run_id": "claude-run"])
        #expect(live.items.isEmpty)
    }
}

/// A Claude subagent's work folds into the Agent/Task tool that runs it.
struct SubagentFoldTests {
    @Test func subagentStepsAndTextStayInsideTheirTool() {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/work")
        projection.apply([
            ["seq": 1, "type": "turn_started", "prompt": "Find the bug"],
            ["seq": 2, "type": "tool_started", "tool": ["id": "agent-1", "name": "Task",
                                                        "input": ["description": "Search the handlers"]]],
            ["seq": 3, "type": "tool_started", "parent_tool_id": "agent-1",
             "tool": ["id": "child-read", "name": "Read", "input": ["file_path": "/tmp/work/a.py"]]],
            ["seq": 4, "type": "tool_finished", "parent_tool_id": "agent-1", "tool_id": "child-read", "output": "code"],
            ["seq": 5, "type": "assistant_text", "parent_tool_id": "agent-1", "text": "The handler drops errors."],
        ])
        guard case .tool("agent-1", .mcp, "Search the handlers", let running, .running, _)? = projection.items.last else {
            Issue.record("expected the running Task tool last, got \(projection.items)")
            return
        }
        #expect(running == "子代理用了 1 次工具\n· a.py\nThe handler drops errors.")
        #expect(projection.lastAssistantText == nil)

        projection.apply([
            ["seq": 6, "type": "tool_finished", "tool_id": "agent-1", "output": "Report: the handler drops errors."],
            ["seq": 7, "type": "assistant_text", "text": "Found it: errors are dropped."],
        ])
        #expect(projection.items.map(\.id) == ["seq-1", "agent-1", "seq-7"])
        guard case .tool(_, _, _, let finished, .succeeded, _) = projection.items[1] else {
            Issue.record("expected the finished Task tool")
            return
        }
        #expect(finished == "子代理用了 1 次工具\n· a.py\n\n— 结果 —\nReport: the handler drops errors.")
        #expect(projection.lastAssistantText == "Found it: errors are dropped.")
    }

    @Test func subagentEventsWithoutTheirToolStayInTheFoldedWork() {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/work")
        projection.apply([
            ["seq": 10, "type": "assistant_text", "parent_tool_id": "agent-9", "text": "Child narration"],
            ["seq": 11, "type": "tool_started", "parent_tool_id": "agent-9",
             "tool": ["id": "child-grep", "name": "Grep", "input": ["pattern": "TODO"]]],
        ])
        #expect(projection.lastAssistantText == nil)
        #expect(projection.items.allSatisfy { if case .message = $0 { false } else { true } })
    }
}
