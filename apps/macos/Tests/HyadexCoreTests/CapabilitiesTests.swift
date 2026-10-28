import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// Goals, slash commands and MCP servers, parsed from the shapes a real
/// server returns (`provider-commands`, `…/runtime`, `…/codex/goal`, `…/mcp`).
struct CapabilitiesTests {
    static func json(_ text: String) throws -> JSON {
        try JSONDecoder().decode(JSON.self, from: Data(text.utf8))
    }

    @Test func parsesClaudeCommandsAndCodexSkills() throws {
        let claude = try #require(CommandInventory.parse(Self.json("""
        {"backend":"claude","revision":"pcmdrev_e89f","support":{"available":true,"mode":"native"},
         "commands":[{"id":"pcmd_1","name":"api-design-principles","label":"api-design-principles",
           "description":"Master REST and GraphQL API design. (user)","scope":null,"source":"claude",
           "kind":"command","invocation":"/api-design-principles"},
          {"id":"pcmd_2","name":"goal","label":"goal","description":"Set a goal","kind":"command","invocation":"/goal"},
          {"id":"pcmd_bad","name":"x","kind":"command","invocation":"x"}]}
        """)))
        #expect(claude.available)
        #expect(claude.revision == "pcmdrev_e89f")
        #expect(claude.commands.map(\.invocation) == ["/api-design-principles", "/goal"])
        #expect(claude.commands[0].detail == "Master REST and GraphQL API design.")

        let codex = try #require(CommandInventory.parse(Self.json("""
        {"backend":"codex","revision":"pcmdrev_7eb9","support":{"available":true,"mode":"native"},
         "commands":[{"id":"pcmd_3","name":"academic-research-suite","label":"ARS-Codex",
           "description":"Codex-native research workflows","scope":"user","source":"codex","kind":"skill",
           "invocation":"/academic-research-suite"}]}
        """)))
        #expect(codex.commands[0].kind == .skill)
        #expect(codex.commands[0].label == "ARS-Codex")
        #expect(codex.commands[0].scope == "user")
    }

    @Test func matchesAndResolvesCommands() {
        let inventory = CommandInventory(revision: "r", available: true, commands: [
            ProviderCommand(id: "1", name: "review", label: "review", detail: "", invocation: "/review", kind: .command),
            ProviderCommand(id: "2", name: "code-review", label: "code-review", detail: "", invocation: "/code-review",
                            kind: .command),
            ProviderCommand(id: "3", name: "compact", label: "compact", detail: "", invocation: "/compact", kind: .command),
        ])
        #expect(inventory.matches("rev").map(\.id) == ["1", "2"])
        #expect(inventory.matches("").count == 3)
        #expect(inventory.command(for: "/review the diff")?.id == "1")
        #expect(inventory.command(for: "/review\nplease")?.id == "1")
        #expect(inventory.command(for: "/reviewer") == nil)
        #expect(inventory.command(for: "review") == nil)
    }

    @Test func slashParsing() {
        #expect(SlashParser.partialCommand(in: "/rev") == "rev")
        #expect(SlashParser.partialCommand(in: "/") == "")
        #expect(SlashParser.partialCommand(in: "/review now") == nil)
        #expect(SlashParser.partialCommand(in: "hi /x") == nil)
        #expect(SlashParser.goalArgument("/goal") == "")
        #expect(SlashParser.goalArgument("/goal  all tests pass ") == "all tests pass")
        #expect(SlashParser.goalArgument("/goals") == nil)
        #expect(SlashParser.goalArgument("set /goal") == nil)
    }

    @Test func parsesClaudeGoalFromRuntime() throws {
        #expect(ChatGoal.claude(runtime: try Self.json(#"{"goal":null,"features":{"goals":true}}"#)) == nil)
        let runtime = try Self.json("""
        {"goal":{"condition":"tests pass","status":"active","iterations":2,"set_at":1761500000000,
                 "last_reason":"2 failing","duration_ms":125000,"tokens":5000},
         "goal_starting":false,"features":{"goals":true}}
        """)
        let goal = try #require(ChatGoal.claude(runtime: runtime))
        #expect(goal.text == "tests pass")
        #expect(goal.status == .active)
        #expect(goal.iterations == 2)
        #expect(goal.reason == "2 failing")
        #expect(goal.secondsUsed == 125)
        #expect(goal.isOpen)
        #expect(!goal.canPause)
        #expect(GoalSupport.claude(runtime: runtime).available)

        let starting = try Self.json(#"{"goal":{"condition":"x","status":"achieved"},"goal_starting":true}"#)
        #expect(ChatGoal.claude(runtime: starting)?.status == .starting)
        #expect(ChatGoal.claude(runtime: try Self.json(#"{"goal":{"condition":"x","status":"achieved"}}"#))?.isOpen == false)
        #expect(!GoalSupport.claude(runtime: try Self.json(#"{"features":{"goals":false}}"#)).available)
    }

    @Test func parsesCodexGoal() throws {
        let snapshot = try Self.json("""
        {"enabled":true,"goal":{"threadId":"t","objective":"ship it","status":"paused","tokenBudget":500000,
          "tokensUsed":182400,"timeUsedSeconds":1260,"createdAt":1,"updatedAt":2},
         "time_budget_seconds":3600,"time_budget_exhausted":false}
        """)
        let goal = try #require(ChatGoal.codex(snapshot))
        #expect(goal.status == .paused)
        #expect(goal.tokenBudget == 500_000)
        #expect(goal.tokensUsed == 182_400)
        #expect(goal.timeBudgetSeconds == 3600)
        #expect(goal.canResume)
        #expect(!goal.canPause)
        #expect(ChatGoal.codex(try Self.json(#"{"enabled":true,"goal":null}"#)) == nil)
        #expect(GoalSupport.codex(runtime: try Self.json(#"{"goals_enabled":true,"goal":null}"#)).available)
        #expect(!GoalSupport.codex(runtime: try Self.json(#"{"enabled":false}"#)).available)

        let exhausted = try Self.json(#"{"goal":{"objective":"x","status":"paused"},"time_budget_exhausted":true}"#)
        #expect(ChatGoal.codex(exhausted)?.canResume == false)
    }

    @Test func parsesMCPStatus() throws {
        let claude = try #require(MCPStatus.parse(Self.json("""
        {"version":1,"available":true,"transport":"agent_sdk","generation":"g7","session_loaded":true,
         "servers":[{"name":"github","status":"connected","enabled":true,"error":null,"scope":"user",
                     "server_info":{"name":"github-mcp-server","version":"1.4.0"},"tool_count":46},
                    {"name":"figma","status":"needs-auth","enabled":true,"error":"This MCP server needs authentication.",
                     "scope":"claudeai","server_info":null,"tool_count":null},
                    {"name":"off","status":"disabled","enabled":false,"error":null,"scope":"project",
                     "server_info":null,"tool_count":0}],
         "truncated":false,"reason":null,"action":null}
        """), backend: .claude))
        #expect(claude.controllable)
        #expect(claude.servers.map(\.status) == [.connected, .needsAuth, .disabled])
        #expect(claude.servers[0].serverInfo == "github-mcp-server 1.4.0")
        #expect(claude.servers[0].toolCount == 46)
        #expect(claude.servers[2].enabled == false)

        let codex = try #require(MCPStatus.parse(Self.json("""
        {"version":1,"available":true,"transport":"app-server","generation":"codexmcp_ab","session_loaded":false,
         "servers":[{"name":"github","status":"pending","enabled":true,"error":null,"scope":null,
                     "server_info":null,"tool_count":12}],"truncated":false,"reason":null,"action":null}
        """), backend: .codex))
        #expect(!codex.controllable)
        #expect(codex.servers[0].status == .pending)

        let unavailable = try #require(MCPStatus.parse(Self.json("""
        {"version":1,"available":false,"transport":"print","generation":null,"session_loaded":false,"servers":[],
         "truncated":false,"reason":{"code":"claude_mcp_print_transport","message":"needs SDK","retryable":false},
         "action":null}
        """), backend: .claude))
        #expect(!unavailable.available)
        #expect(unavailable.reason == "needs SDK")
        #expect(!unavailable.controllable)
        #expect(MCPStatus.parse(try Self.json(#"{"version":2}"#), backend: .claude) == nil)
    }

    @Test func parsesGroupGoal() throws {
        let group = LiveMapping.group(try Self.json("""
        {"id":"grp_1","title":"t","cwd":"/tmp","participants":[{"handle":"codex","backend":"codex","session_id":"s1"}],
         "settings":{},"stats":{},"goal":{"handle":"codex","objective":"all green","set_at":1761500000}}
        """), previous: nil)
        #expect(group?.goal?.handle == "codex")
        #expect(group?.goal?.objective == "all green")
    }

    @MainActor @Test func disconnectedGoalsRequireAServer() {
        let model = AppModel(snapshotFixturesAt: .now)
        let goals = model.goals
        model.send("/goal 所有测试通过", toChat: "c1")
        model.send("/goal clear", toChat: "c1")
        model.setGoal("new goal", forChat: "c1")
        model.clearGoal(forChat: "c1")
        model.pauseGoal(forChat: "c2")
        #expect(model.goals == goals)
        #expect(model.errorMessage != nil)
        #expect(!AppModel().goalSupport(forChat: "unknown").available)
    }
}

/// Jobs, queue order, emergency alerts and archived items.
struct ManagementTests {
    static func json(_ text: String) throws -> JSON { try CapabilitiesTests.json(text) }

    @Test func jobScheduleFieldsMatchTheServer() {
        let daily = JobSchedule.cron("0 30 8 * * *").fields
        #expect(daily["schedule_kind"] == "cron")
        #expect(daily["cron_expression"] == "0 30 8 * * *")
        #expect(daily["interval_seconds"] == .null)
        let hourly = JobSchedule.interval(seconds: 3600, repeats: true, firstRun: nil).fields
        #expect(hourly["interval_seconds"] == 3600)
        #expect(hourly["loop"] == true)
        #expect(hourly["max_runs"] == nil)
        let once = JobSchedule.interval(seconds: 600, repeats: false,
                                        firstRun: Date(timeIntervalSince1970: 1_800_000_000)).fields
        #expect(once["loop"] == false)
        #expect(once["max_runs"] == 1)
        #expect(once["first_run_at"] == "2027-01-15T08:00:00Z")
    }

    @Test func jobMappingKeepsTargetsAndSchedule() throws {
        let chatJob = try #require(LiveMapping.job(Self.json("""
        {"id":"job_1","session_id":"s1","title":"t","prompt":"p","schedule_kind":"cron","cron_expression":"0 0 9 * * 1-5",
         "enabled":true,"next_run_at":1800000000}
        """), chatTitles: ["s1": "聊天一"]))
        #expect(chatJob.sessionID == "s1")
        #expect(chatJob.groupID == nil)
        #expect(chatJob.spec == .cron("0 0 9 * * 1-5"))
        let groupJob = try #require(LiveMapping.job(Self.json("""
        {"id":"job_2","session_id":"m1","group_id":"grp_1","title":"t","prompt":"p","schedule_kind":"interval",
         "interval_seconds":7200,"loop":true}
        """), chatTitles: ["grp_1": "组"]))
        #expect(groupJob.groupID == "grp_1")
        #expect(groupJob.target == "讨论组 · 组")
        if case .interval(7200, true, _)? = groupJob.spec {} else { Issue.record("expected a repeating interval") }
    }

    @Test func queueEventsReorderUpdateAndForceSend() throws {
        var projection = SessionProjection(backend: .codex, cwd: "/tmp")
        projection.apply(try Self.json("""
        [{"seq":1,"type":"turn_started","prompt":"go"},
         {"seq":2,"type":"turn_queued","queued_id":"q1","prompt":"one"},
         {"seq":3,"type":"turn_queued","queued_id":"q2","prompt":"two"},
         {"seq":4,"type":"turn_queue_reordered","queued_id":"q2","direction":"up",
          "positions":[{"queued_id":"q2","position":1},{"queued_id":"q1","position":2}]},
         {"seq":5,"type":"turn_queue_updated","queued_id":"q1","prompt":"one, edited"}]
        """).array)
        #expect(projection.queued.map(\.id) == ["q2", "q1"])
        #expect(projection.queued.last?.text == "one, edited")
        projection.apply(try Self.json("""
        [{"seq":6,"type":"turn_steered","queued_id":"q2","prompt":"two"},
         {"seq":7,"type":"turn_queue_run_now","queued_id":"q1"},
         {"seq":8,"type":"codex_compaction_completed","status":"completed"},
         {"seq":9,"type":"agent_notification","message":"build done","notification_level":"important"}]
        """).array)
        #expect(projection.queued.isEmpty)
        #expect(projection.items.contains { if case .divider(_, "上下文已压缩") = $0 { true } else { false } })
        #expect(projection.items.contains { if case .divider(_, "重要通知：build done") = $0 { true } else { false } })
    }

    @Test func emergencyAlertsParse() throws {
        let alert = try #require(EmergencyAlert.parse(Self.json("""
        {"id":"s1","title":"部署","unacknowledged_emergency_count":2,
         "emergency_alert":{"id":"emergency_0123","status":"active","severity":"critical","message":"磁盘将满",
                            "raised_at":1800000000}}
        """)))
        #expect(alert.alertID == "emergency_0123")
        #expect(alert.count == 2)
        #expect(alert.message == "磁盘将满")
        #expect(EmergencyAlert.parse(try Self.json(#"{"id":"s1","emergency_alert":null}"#)) == nil)
    }

    @MainActor @Test func disconnectedManagementLeavesServerStateUnchanged() {
        let model = AppModel(snapshotFixturesAt: .now)
        let chats = model.chats
        let queue = model.queues
        model.setArchived(.chat("c4"), true)
        model.rename(.chat("c4"), to: "新名字")
        model.delete(.chat("c4"))
        model.moveQueued("missing", up: true, in: "c3")
        model.attach([URL(fileURLWithPath: "/tmp/x.txt")], toChat: "c1")
        #expect(model.chats == chats)
        #expect(model.queues == queue)
        #expect(model.attachments.isEmpty)
        #expect(model.errorMessage != nil)
    }
}

/// Search hits, side chats, subagents and held queues.
struct ExtrasTests {
    static func json(_ text: String) throws -> JSON { try CapabilitiesTests.json(text) }

    @Test func searchHitsAndTerms() throws {
        let hits = SearchHit.parse(try Self.json("""
        {"query":"deploy","results":[{"session_id":"s1","event_id":"evt_1","seq":42,"ts":1800000000,"role":"assistant",
          "snippet":"…the deploy finished…","match_count":3},{"session_id":"s2","seq":1}]}
        """))
        #expect(hits.count == 1)
        #expect(hits[0].seq == 42 && hits[0].matches == 3 && hits[0].roleLabel == "代理")
        #expect(SearchHit.terms(in: #"deploy "rolling restart" now"#) == ["rolling restart", "deploy", "now"])
    }

    @Test func sideChatParses() throws {
        let side = try #require(SideChat.parse(Self.json("""
        {"session_id":"s1","side_chat_id":"sc_1","revision":3,"last_request_id":"r2","exchanges":[
          {"request_id":"r1","question":"why?","status":"completed","answer":"because","context_note":"sees all"},
          {"request_id":"r2","question":"and?","status":"running"}]}
        """)))
        #expect(side.sideChatID == "sc_1" && side.revision == 3 && side.lastRequestID == "r2")
        #expect(side.isRunning)
        #expect(side.exchanges.first?.answer == "because")
        #expect(side.contextNote == "sees all")
    }

    @Test func subagentsParse() throws {
        let agents = Subagent.parse(try Self.json("""
        {"session_id":"s1","subagents":[{"id":"a1","subagent_name":"explorer","subagent_status":"running",
          "subagent_activity":"Reading files","subagent_log":[{"ts":1,"text":"grep"}]},
          {"id":"a2","subagent_title":"reviewer","subagent_status":"completed","subagent_summary":"LGTM"}],"count":2}
        """))
        #expect(agents.map(\.name) == ["explorer", "reviewer"])
        #expect(agents[0].state == .running && agents[0].log == ["grep"])
        #expect(agents[1].state == .done && agents[1].summary == "LGTM")
    }

    @Test func heldQueueFollowsStopAndResume() throws {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp")
        projection.apply(try Self.json("""
        [{"seq":1,"type":"turn_queued","queued_id":"q1","prompt":"one"},
         {"seq":2,"type":"turn_queued","queued_id":"q2","prompt":"two"},
         {"seq":3,"type":"turn_queue_paused","queued_ids":["q1","q2"]},
         {"seq":4,"type":"turn_queue_delivery_fenced","queued_id":"q2"}]
        """).array)
        #expect(projection.queued.map(\.hold) == [.stopped, .deliveryUncertain])
        projection.apply(try Self.json(#"[{"seq":5,"type":"turn_queue_resumed","queued_ids":["q1","q2"]}]"#).array)
        // A delivery-uncertain message stays held until the user decides.
        #expect(projection.queued.map(\.hold) == [nil, .deliveryUncertain])
        projection.setQueue(try Self.json("""
        [{"queued_id":"q3","prompt":"x","paused":true,"pause_reason":"stopped"}]
        """).array)
        #expect(projection.queued.first?.hold == .stopped)
    }

    @MainActor @Test func disconnectedForkAndSideChatDoNotCreateData() {
        let model = AppModel(snapshotFixturesAt: .now)
        let chats = model.chats
        model.markUnread("c1")
        model.fork("c1")
        model.send("/btw 为什么这么改？", toChat: "c4")
        model.askSide("为什么这么改？", inChat: "c4")
        model.refreshSide("c4")
        #expect(model.chats == chats)
        #expect(model.sideChats.isEmpty)
        #expect(model.sideChatOpen == nil)
        #expect(model.errorMessage != nil)
    }
}

struct ClaudeLocalCommandTests {
    @Test func compactShowsOneLandmark() throws {
        var projection = SessionProjection(backend: .claude, cwd: "/tmp")
        projection.apply(try CapabilitiesTests.json("""
        [{"seq":1,"type":"turn_started","prompt":"/compact"},
         {"seq":2,"type":"claude_compaction_completed","trigger":"manual"},
         {"seq":3,"type":"assistant_text","text":"Goal cleared: x","local_command":"goal","local_command_detached":true},
         {"seq":4,"type":"assistant_text","text":"Compacted","local_command":"compact"},
         {"seq":5,"type":"turn_finished"},
         {"seq":6,"type":"turn_started","prompt":"/context"},
         {"seq":7,"type":"assistant_text","text":"## Context Usage","local_command":"context"}]
        """).array)
        let texts = projection.items.compactMap { item -> String? in
            switch item {
            case .divider(_, let text): "divider:" + text
            case .message(_, _, let text, _, _, _, _): text
            default: nil
            }
        }
        #expect(texts == ["/compact", "divider:上下文已压缩", "/context", "## Context Usage"])
    }
}
