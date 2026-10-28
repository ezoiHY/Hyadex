import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// Chat and group settings, job run outcomes, token totals and changed-file
/// paths, parsed from the shapes a real server returns.
struct SettingsAndOutcomeTests {
    static func json(_ text: String) throws -> JSON {
        try JSONDecoder().decode(JSON.self, from: Data(text.utf8))
    }

    @Test func chatSettingsParseAndSendOnlyChanges() throws {
        let settings = ChatSettings.parse(try Self.json("""
        {"system_prompt":"Answer in English.","subagent_limit":null,"auto_title_enabled":false,
         "provider_jobs_access":"read_only",
         "subagent_limit_control":{"supported":true,"applies_to":"next_idle_provider_start","message":"…"}}
        """))
        #expect(settings.systemPrompt == "Answer in English.")
        #expect(settings.subagentLimit == nil)
        #expect(settings.autoTitle == false)
        #expect(settings.jobsAccess == .readOnly)
        #expect(settings.subagentLimitAppliesTo == "next_idle_provider_start")

        var edited = settings
        #expect(settings.changes(to: edited).isEmpty)
        edited.systemPrompt = "  "
        edited.subagentLimit = 3
        edited.jobsAccess = .blocked
        let fields = settings.changes(to: edited)
        #expect(fields["system_prompt"] == .null)
        #expect(fields["subagent_limit"] == .number(3))
        #expect(fields["provider_jobs_access"] == .string("blocked"))
        #expect(fields["auto_title_enabled"] == nil)
    }

    @Test func groupSettingsParseAndResetToDefaults() throws {
        let settings = GroupSettings.parse(try Self.json("""
        {"settings":{"default_responder":"codex","max_agent_hops":2,"delta_char_limit":6000},
         "brief":{"text":"Keep the API stable.","version":3}}
        """))
        #expect(settings == GroupSettings(defaultResponder: "codex", maxAgentHops: 2, brief: "Keep the API stable."))
        var edited = settings
        edited.defaultResponder = nil
        edited.maxAgentHops = 0
        let fields = settings.changes(to: edited)
        #expect(fields["settings"] == .object(["default_responder": .null, "max_agent_hops": .number(0)]))
        #expect(fields["brief"] == nil)
        edited = settings
        edited.brief = "New brief\n"
        #expect(settings.changes(to: edited) == ["brief": .string("New brief")])
    }

    @Test func groupMapsItsStoredSettings() throws {
        let group = try #require(LiveMapping.group(try Self.json("""
        {"id":"grp_1","title":"g","cwd":"/tmp","participants":[{"handle":"claude","backend":"claude"},
         {"handle":"codex","backend":"codex"}],"settings":{"max_agent_hops":7},"brief":{"text":"","version":0}}
        """), previous: nil))
        #expect(group.settings.maxAgentHops == 7)
        #expect(group.settings.defaultResponder == nil)
        #expect(group.defaultResponder == "claude")
    }

    @Test func jobOutcomeFollowsTheRecordedRunStatus() throws {
        func outcome(_ text: String) throws -> (ActivityState, String, String?)? {
            LiveMapping.jobOutcome(try Self.json(text)).map { ($0.state, $0.text, $0.error) }
        }
        #expect(try outcome(#"{"last_run_at":"2026-10-28T01:00:00Z","last_run_status":"failed","last_run_error":"API overloaded"}"#)!
                == (.failed, "失败", "API overloaded"))
        #expect(try outcome(#"{"last_run_at":"2026-10-28T01:00:00Z","last_run_status":"completed"}"#)! == (.done, "成功", nil))
        #expect(try outcome(#"{"last_run_at":"2026-10-28T01:00:00Z","last_run_status":"running"}"#)! == (.running, "运行中", nil))
        #expect(try outcome(#"{"last_run_status":"stopped","last_run_finished_at":"2026-10-28T01:00:00Z"}"#)!
                == (.paused, "已停止", nil))
        #expect(try outcome(#"{"last_run_status":"dispatched"}"#)! == (.done, "已派发", nil))
        // A scheduled run that failed to start has no last_run_at, only its finish time.
        let failedStart = try #require(LiveMapping.jobOutcome(try Self.json(
            #"{"last_run_status":"failed","last_run_error":"chat busy","last_run_finished_at":"2026-10-28T02:00:00Z"}"#)))
        #expect(failedStart.at != nil)
        // Older servers only say a run started: not reported as success.
        #expect(try outcome(#"{"last_run_at":"2026-10-28T01:00:00Z"}"#)! == (.idle, "已运行", nil))
        #expect(try outcome(#"{"last_run_at":"2026-10-28T01:00:00Z","last_manual_run_error":"boom","last_manual_run_error_at":"2026-10-28T02:00:00Z"}"#)!
                == (.failed, "失败", "boom"))
        #expect(try outcome(#"{}"#) == nil)
    }

    @Test func chatTokenTotalsComeFromTheServer() throws {
        let chat = try #require(LiveMapping.chat(try Self.json("""
        {"id":"s1","backend":"claude","token_totals":{"total_tokens":1042,"input_tokens":1002,
         "cached_input_tokens":900,"output_tokens":40,"updated_at":"2026-10-28T01:00:00Z"}}
        """), previous: nil))
        #expect(chat.tokensUsed == 1042)
        #expect(chat.tokenTotals == TokenTotals(total: 1042, input: 1002, cachedInput: 900, output: 40))
        let fresh = try #require(LiveMapping.chat(try Self.json(#"{"id":"s2","backend":"codex"}"#), previous: nil))
        #expect(fresh.tokenTotals == nil)
    }

    @Test func changedFilesResolveAgainstTheirRoot() {
        let relative = FileChange(path: "app/main.py", added: 1, removed: 0, diff: "", root: "/repo")
        #expect(relative.absolutePath(fallbackRoot: "/elsewhere") == "/repo/app/main.py")
        let receipt = FileChange(path: "notes.md", added: 0, removed: 0, diff: "")
        #expect(receipt.absolutePath(fallbackRoot: "/group") == "/group/notes.md")
        let absolute = FileChange(path: "/tmp/x.txt", added: 0, removed: 0, diff: "", root: "/repo")
        #expect(absolute.absolutePath(fallbackRoot: nil) == "/tmp/x.txt")
    }
}
