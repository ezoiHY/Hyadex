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
            }
        }
        #expect(kinds == ["user", "tool:edit:ok", "approval:resolved", "tool:command:ok", "agent", "changes"])
        #expect(projection.lastAssistantText?.contains("hello.txt") == true)
        #expect(projection.diffsToLoad.count == 1)
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
        #expect(chat?.permission == "工作区可写 · 免批准")
        let waiting: JSON = ["id": "s2", "backend": "claude", "latest_event_type": "claude_interaction_requested",
                             "claude_needs_user_action": true]
        #expect(LiveMapping.chat(waiting, previous: nil)?.state == .needsAction)
        #expect(LiveMapping.chat(["id": "s3", "backend": "gemini"], previous: nil) == nil)
        #expect(LiveMapping.chat(["id": "s4", "backend": "claude", "archived": true], previous: nil) == nil)
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
