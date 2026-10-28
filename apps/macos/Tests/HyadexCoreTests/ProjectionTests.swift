import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// Projections are checked against event logs recorded from a real server
/// running Claude Code and Codex (one turn each, with an approval).
struct ProjectionTests {
    static func events(_ name: String) throws -> [JSON] {
        let url = try #require(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode([JSON].self, from: Data(contentsOf: url))
    }

    @Test func claudeTurnWithApprovalProjectsInOrder() throws {
        let events = try Self.events("claude-turn")
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/hyadex-mac-e2e")

        // Up to the approval request: running, one pending approval.
        let requestIndex = try #require(events.firstIndex { $0["type"].string == "claude_interaction_requested" })
        projection.apply(Array(events[...requestIndex]))
        #expect(projection.running)
        #expect(projection.needsAction)
        guard case .approval(_, _, let title, let detail, _, .pending, let allowsSession)? = projection.items.last else {
            Issue.record("expected a pending approval, got \(String(describing: projection.items.last))")
            return
        }
        #expect(title == "修改文件")
        #expect(detail.hasPrefix("hello.txt"))
        #expect(allowsSession)

        // The rest of the turn: resolved, tools done, answer, diff, finished.
        projection.apply(Array(events[(requestIndex + 1)...]))
        #expect(!projection.running)
        #expect(!projection.needsAction)
        let kinds = projection.items.map { item -> String in
            switch item {
            case .message(_, let author, _, _, _, _, _): author.kind == .user ? "user" : "agent"
            case .tool(_, let kind, _, _, let status, _): "tool:\(kind.rawValue):\(status == .succeeded ? "ok" : "?")"
            case .approval(_, _, _, _, _, let decision, _): "approval:\(decision == .resolved ? "resolved" : "?")"
            case .changes: "changes"
            case .thinking: "thinking"
            case .divider: "divider"
            case .media: "media"
            }
        }
        #expect(kinds == ["user", "tool:edit:ok", "approval:resolved", "tool:command:ok", "agent", "changes"])
        #expect(projection.lastAssistantText?.contains("hello.txt") == true)
        #expect(projection.diffsToLoad.count == 1)
    }

    @Test func turnTimingAndAcceptedRequests() throws {
        let events = try Self.events("claude-turn")
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/hyadex-mac-e2e")
        let startIndex = try #require(events.firstIndex { $0["type"].string == "turn_started" })
        projection.apply(Array(events[...startIndex]))
        #expect(projection.turnStartedAt != nil)
        #expect(projection.acceptedRequests.contains("mac-e2e-1"))
        projection.apply(Array(events[(startIndex + 1)...]))
        #expect(projection.turnStartedAt == nil)
        let userID = try #require(events[startIndex]["id"].string)
        #expect((projection.turnSeconds[userID] ?? -1) >= 0)
    }

    @Test func homePathsAreAbbreviatedInCommands() {
        let tool: JSON = ["name": "Bash", "input": ["command": .string("cat \(NSHomeDirectory())/.codex/skills/a.md")]]
        #expect(SessionProjection.describe(tool: tool, cwd: "/tmp").title == "cat ~/.codex/skills/a.md")
    }

    @Test func sidebarPreviewDropsMarkdown() {
        #expect(LiveController.preview("I created [a.txt](a.txt), see `b` and $$x$$") == "I created a.txt, see b and x")
    }

    @Test func diffIsSplitPerFileAndAttached() throws {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp/hyadex-mac-e2e")
        projection.apply(try Self.events("claude-turn"))
        let itemID = try #require(projection.diffsToLoad.keys.first)
        let url = try #require(Bundle.module.url(forResource: "claude-diff", withExtension: "txt", subdirectory: "Fixtures"))
        projection.setDiff(try String(contentsOf: url, encoding: .utf8), forItem: itemID)
        #expect(projection.diffsToLoad.isEmpty)
        let changes = projection.items.allChanges
        #expect(changes.files.map(\.path) == ["hello.txt"])
        #expect(changes.files[0].diff.contains("+hi"))
        #expect(!changes.files[0].diff.contains("diff --git"))
    }

    @Test func codexTurnUnwrapsShellAndKeepsCommentary() throws {
        var projection = SessionProjection(backend: .codex, cwd: "/tmp/hyadex-mac-e2e")
        projection.apply(try Self.events("codex-turn"))
        #expect(!projection.running)
        let tool = projection.items.compactMap { item -> (String, String)? in
            if case .tool(_, .command, let title, let output, .succeeded, _) = item { return (title, output) }
            return nil
        }.first
        #expect(tool?.0.hasPrefix("printf") == true)
        #expect(tool?.1 == "hi\nbye\n")
        #expect(projection.items.contains { if case .thinking = $0 { true } else { false } })
        #expect(projection.items.contains {
            if case .approval(_, _, "运行命令", _, _, .resolved, false) = $0 { true } else { false }
        })
    }

    @Test func replayedEventsAreIgnored() throws {
        let events = try Self.events("claude-turn")
        var projection = SessionProjection(backend: .claude, cwd: "/tmp")
        projection.apply(events)
        let count = projection.items.count
        projection.apply(events)
        #expect(projection.items.count == count)
    }

    @Test func userDecisionIsShownAfterResolution() throws {
        let events = try Self.events("claude-turn")
        var projection = SessionProjection(backend: .claude, cwd: "/tmp")
        let requestIndex = try #require(events.firstIndex { $0["type"].string == "claude_interaction_requested" })
        projection.apply(Array(events[...requestIndex]))
        let id = try #require(events[requestIndex]["interaction"]["id"].string)
        projection.noteDecision(.allowedForSession, for: id)
        projection.apply(Array(events[(requestIndex + 1)...]))
        #expect(projection.items.contains {
            if case .approval(id, _, _, _, _, .allowedForSession, _) = $0 { true } else { false }
        })
    }

    @Test func groupMessagesReceiptsAndMemberStates() {
        var projection = GroupProjection(backends: ["claude": .claude, "codex": .codex])
        projection.apply([
            ["seq": 1, "type": "group_created", "ts": 1_792_997_032],
            ["seq": 2, "type": "message", "author": "user", "author_kind": "user", "text": "@codex hi", "mentions": ["codex"]],
            ["seq": 3, "type": "member_status", "handle": "codex", "state": "running"],
            ["seq": 4, "type": "message", "author": "codex", "author_kind": "agent", "text": "done",
             "receipt": ["changed_files": ["a.txt"]]],
            ["seq": 5, "type": "member_status", "handle": "codex", "state": "idle"],
            ["seq": 6, "type": "round_finished", "status": "done"],
            ["seq": 7, "type": "message", "author": "autopilot", "author_kind": "orchestrator", "text": "note"],
        ])
        #expect(projection.items.count == 5)
        #expect(projection.memberStates["codex"] == .idle)
        #expect(projection.items.allChanges.files.map(\.path) == ["a.txt"])
        #expect(projection.notes.count == 1)
    }

    @Test func sessionListMapping() {
        let running: JSON = ["id": "s1", "backend": "codex", "title": "t", "cwd": "/tmp/x", "latest_event_type": "tool_started",
                             "codex_sandbox_mode": "workspace-write", "codex_approval_policy": "never",
                             "latest_agent_event_seq": 5, "last_read_agent_event_seq": 3]
        let chat = LiveMapping.chat(running, previous: nil)
        #expect(chat?.state == .running)
        #expect(chat?.unread == true)
        #expect(chat?.permission == "Custom (workspace-write, never)")
        let waiting: JSON = ["id": "s2", "backend": "claude", "latest_event_type": "claude_interaction_requested",
                             "claude_needs_user_action": true]
        #expect(LiveMapping.chat(waiting, previous: nil)?.state == .needsAction)
        #expect(LiveMapping.chat(["id": "s3", "backend": "gemini"], previous: nil) == nil)
        // Archived chats stay in the list, flagged, for the sidebar's 已归档 section.
        #expect(LiveMapping.chat(["id": "s4", "backend": "claude", "archived": true], previous: nil)?.archived == true)
    }
}

struct AutopilotMappingTests {
    @Test func agentAddressedOrchestratorMessagesAreNotNotes() {
        var projection = GroupProjection(backends: [:])
        projection.apply([
            ["seq": 1, "type": "message", "author": "autopilot", "author_kind": "orchestrator", "text": "plan it",
             "audience": ["planner"]],
            ["seq": 2, "type": "message", "author": "autopilot", "author_kind": "orchestrator", "text": "Task 1 approved",
             "audience": ["user"]],
        ])
        #expect(projection.notes.count == 1)
    }

    @Test func runMapsPlanStatusesCommitsAndAcceptanceLists() {
        let json: JSON = [
            "id": "run_1", "status": "verifying", "approval": "step", "isolation": "in_place", "reviewer": "fresh",
            "task_index": 1, "goal": "g",
            "plan": ["summary": "s", "tasks": [
                ["title": "a", "status": "done", "acceptance": ["x", "y"]],
                ["title": "b", "status": "in_progress", "attempts": 2],
                ["title": "c", "status": "pending"],
            ]],
            "commits": [["task_index": 0, "commit": "0fd76129b85f"]],
            "last_step": ["task_index": 1, "verification": ["passed": false, "output_tail": "1 failed", "seconds": .number(3.2)]],
        ]
        let run = LiveMapping.run(json, previous: nil)
        #expect(run?.status == .verifying)
        #expect(run?.approval == .step)
        #expect(run?.isolation == .inPlace)
        #expect(run?.tasks.map(\.state) == [.approved, .verifying, .pending])
        #expect(run?.tasks[0].commit == "0fd7612")
        #expect(run?.tasks[0].acceptance == "x\ny")
        #expect(run?.tasks[1].revisions == 1)
        #expect(run?.tasks[1].verification?.passed == false)
    }
}

/// The native catalog recorded from a real server (`GET /api/runtime/catalog`).
struct CatalogTests {
    static func catalog(_ backend: AgentBackend) throws -> RuntimeCatalog {
        let url = try #require(Bundle.module.url(forResource: "runtime-catalog", withExtension: "json",
                                                 subdirectory: "Fixtures"))
        let json = try JSONDecoder().decode(JSON.self, from: Data(contentsOf: url))
        return try #require(RuntimeCatalog.parse(json["backends"][backend.rawValue]))
    }

    @Test func codexUsesNativeModelsAndPerModelEfforts() throws {
        let codex = try Self.catalog(.codex)
        #expect(codex.models.first?.value == "")
        #expect(codex.models.first?.label.hasPrefix("Server default") == true)
        #expect(codex.models.contains { $0.value == "gpt-6-astra" && $0.label == "GPT-6-Astra" })
        #expect(codex.modelName("") == "GPT-6.1-Sol")
        #expect(codex.effortName("", model: "") == "XHigh")
        let efforts = codex.effortOptions(model: "gpt-6-astra", current: "").map(\.label)
        #expect(efforts.first?.hasPrefix("Server default") == true)
        #expect(efforts.contains("XHigh"))
        #expect(!codex.supports(effort: "ultracode", model: "gpt-6-astra"))
    }

    @Test func claudeDefaultIsLabelledAsDefault() throws {
        let claude = try Self.catalog(.claude)
        #expect(claude.models.first?.label == "Server default (Sonnet)")
        #expect(claude.efforts.first?.label == "Server default (High)")
        #expect(claude.modelName("haiku") == "Haiku")
        #expect(claude.effortName("max", model: "") == "Max")
        #expect(claude.modelOptions(current: "custom-model").last?.value == "custom-model")
    }
}

struct UsageAndPermissionTests {
    @Test func providerUsageKeepsNativeWindowNames() {
        let claude: JSON = ["status": "available", "plan": "max", "observed_at": "2030-01-01T00:00:00Z", "windows": [
            ["id": "five_hour", "label": "Current session", "used_percent": 77, "resets_at": 1893474000, "window_minutes": 300],
            ["id": "seven_day", "label": "Current week (all models)", "used_percent": 21, "window_minutes": 10080],
        ]]
        let usage = ProviderUsage.parse(claude, backend: .claude)
        #expect(usage.available)
        #expect(usage.windows.map(\.label) == ["Current session", "Current week (all models)"])
        #expect(usage.tightest?.id == "five_hour")
        #expect(usage.windows[0].resetsAt == Date(timeIntervalSince1970: 1893474000))
        // Codex leaves windows unnamed; they get Codex's own `/status` names.
        let codex: JSON = ["status": "available", "windows": [
            ["id": "codex:primary", "label": .null, "used_percent": 4, "window_minutes": 10080]]]
        #expect(ProviderUsage.parse(codex, backend: .codex).windows.first?.label == "Weekly limit")
        let missing = ProviderUsage.parse(["status": "unavailable", "reason": "account_usage_unavailable"], backend: .claude)
        #expect(!missing.available)
        #expect(missing.unavailableText.contains("API key"))
    }

    @Test func contextUsageFromBothProviders() throws {
        let codex: JSON = ["context_tokens": 38164, "context_window": 475000, "effective_context_window": 463000,
                           "context_percent": .number(5.65)]
        let parsed = try #require(ContextUsage.parse(codex))
        #expect(parsed.window == 463000)
        #expect(parsed.percent == 5.65)
        let claude: JSON = ["totalTokens": 28286, "maxTokens": 1000000, "percentage": .number(3.0)]
        #expect(ContextUsage.parse(claude)?.tokens == 28286)
        #expect(ContextUsage.parse([:]) == nil)
    }

    @Test func permissionLabelsAreTheProvidersOwn() {
        func chat(_ fields: [String: JSON], _ backend: String) -> ChatSummary? {
            var json: [String: JSON] = ["id": "s", "backend": .string(backend), "title": "t", "cwd": "/tmp"]
            json.merge(fields) { $1 }
            return LiveMapping.chat(.object(json), previous: nil)
        }
        #expect(chat(["claude_permission_mode": "acceptEdits"], "claude")?.permission == "Accept edits")
        #expect(chat(["claude_permission_mode": "dontAsk"], "claude")?.permission == "Don't Ask")
        #expect(chat([:], "claude")?.permission == "Manual")
        #expect(PermissionPreset.presets(for: .claude).count == 6)
        #expect(chat(["codex_sandbox_mode": "workspace-write", "codex_approval_policy": "on-request"], "codex")?.permission == "Default")
        #expect(chat(["codex_sandbox_mode": "danger-full-access", "codex_approval_policy": "never"], "codex")?.permission == "Full Access")
        #expect(chat(["codex_sandbox_mode": "workspace-write", "codex_approval_policy": "untrusted"], "codex")?.permission
                == "Custom (workspace-write, untrusted)")
    }

    @Test func attachmentsAndPublishedFilesBecomeMedia() {
        var projection = SessionProjection(backend: .codex, cwd: "/tmp")
        projection.apply([
            ["seq": 1, "type": "file_uploaded", "file": ["id": "f1", "filename": "shot.png", "content_type": "image/png"]],
            ["seq": 2, "type": "turn_started", "id": "u1", "prompt": "看看这张图", "file_ids": ["f1"]],
            ["seq": 3, "type": "artifact_created", "id": "a1", "publication_id": "p",
             "artifact": ["id": "f2", "filename": "plot.png", "content_type": "image/png"]],
            ["seq": 4, "type": "artifact_created", "id": "a2", "publication_id": "p",
             "artifact": ["id": "f3", "filename": "report.pdf", "content_type": "application/pdf"]],
        ])
        let media = projection.items.compactMap { item -> (Author, [MediaFile])? in
            if case .media(_, let author, let files) = item { return (author, files) }
            return nil
        }
        #expect(media.count == 2)
        #expect(media[0].0.kind == .user)
        #expect(media[0].1.first?.name == "shot.png")
        #expect(media[0].1.first?.isImage == true)
        #expect(media[1].0.kind == .agent)
        #expect(media[1].1.map(\.name) == ["plot.png", "report.pdf"])
        #expect(media[1].1.last?.isImage == false)
    }
}

struct GroupMemberWorkTests {
    @Test func memberTurnIsFoundByDispatchID() throws {
        var member = SessionProjection(backend: .codex, cwd: "/tmp")
        member.apply([
            ["seq": 1, "type": "turn_started", "id": "t1", "prompt": "group context", "client_request_id": "gd_1",
             "ts": "2030-01-01T00:00:00Z"],
            ["seq": 2, "type": "tool_started", "tool": ["id": "x1", "name": "exec_command", "input": ["cmd": "ls"]],
             "ts": "2030-01-01T00:00:01Z"],
            ["seq": 3, "type": "tool_finished", "tool": ["id": "x1", "name": "exec_command", "output": "a"],
             "ts": "2030-01-01T00:00:02Z"],
            ["seq": 4, "type": "assistant_text", "text": "done", "ts": "2030-01-01T00:00:03Z"],
            ["seq": 5, "type": "turn_finished", "ts": "2030-01-01T00:00:09Z"],
            ["seq": 6, "type": "turn_started", "id": "t2", "prompt": "next", "client_request_id": "gd_2",
             "ts": "2030-01-01T00:01:00Z"],
        ])
        let first = try #require(member.turn(forRequest: "gd_1"))
        #expect(first.steps.count == 1)
        #expect(first.seconds == 9)
        #expect(!first.running)
        let second = try #require(member.turn(forRequest: "gd_2"))
        #expect(second.steps.isEmpty)
        #expect(second.running)
        #expect(member.turn(forRequest: "gd_9") == nil)
    }

    @Test func activeDispatchesFollowTheRound() {
        var group = GroupProjection(backends: ["codex": .codex])
        group.apply([
            ["seq": 1, "type": "dispatch", "dispatch_id": "gd_1", "handle": "codex", "state": "running", "session_id": "s1"],
            ["seq": 2, "type": "member_status", "handle": "codex", "state": "running", "dispatch_id": "gd_1"],
        ])
        #expect(group.activeDispatches.map(\.id) == ["gd_1"])
        #expect(group.activeDispatches.first?.sessionID == "s1")
        group.apply(["seq": 3, "type": "message", "author": "codex", "author_kind": "agent", "text": "ok", "dispatch_id": "gd_1"])
        #expect(group.activeDispatches.isEmpty)
        #expect(group.dispatchOfMessage["g3"] == "gd_1")
    }
}
