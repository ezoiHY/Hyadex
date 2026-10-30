package dev.hyadex.android.core

import dev.hyadex.android.api.JsonCodec
import kotlinx.serialization.json.JsonElement
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The Mac app's capability tests, on the shapes a real server returns, so both clients read them alike. */
class CapabilitiesTest {
  private fun json(text: String): JsonElement = JsonCodec.parseToJsonElement(text)

  @Test
  fun parsesClaudeCommandsAndCodexSkills() {
    val claude = CommandInventory.parse(
      json(
        """{"backend":"claude","revision":"pcmdrev_e89f","support":{"available":true,"mode":"native"},
         "commands":[{"id":"pcmd_1","name":"api-design-principles","label":"api-design-principles",
           "description":"Master REST and GraphQL API design. (user)","scope":null,"source":"claude",
           "kind":"command","invocation":"/api-design-principles"},
          {"id":"pcmd_2","name":"goal","label":"goal","description":"Set a goal","kind":"command","invocation":"/goal"},
          {"id":"pcmd_bad","name":"x","kind":"command","invocation":"x"}]}"""
      )
    )!!
    assertTrue(claude.available)
    assertEquals("pcmdrev_e89f", claude.revision)
    assertEquals(listOf("/api-design-principles", "/goal"), claude.commands.map { it.invocation })
    assertEquals("Master REST and GraphQL API design.", claude.commands[0].detail)

    val codex = CommandInventory.parse(
      json(
        """{"backend":"codex","revision":"pcmdrev_7eb9","support":{"available":true,"mode":"native"},
         "commands":[{"id":"pcmd_3","name":"academic-research-suite","label":"ARS-Codex",
           "description":"Codex-native research workflows","scope":"user","source":"codex","kind":"skill",
           "invocation":"/academic-research-suite"}]}"""
      )
    )!!
    assertTrue(codex.commands[0].skill)
    assertEquals("ARS-Codex", codex.commands[0].label)
    assertEquals("user", codex.commands[0].scope)
  }

  @Test
  fun matchesAndResolvesCommands() {
    val inventory = CommandInventory(
      "r", true, null,
      listOf(
        ProviderCommand("1", "review", "review", "", "/review", skill = false),
        ProviderCommand("2", "code-review", "code-review", "", "/code-review", skill = false),
        ProviderCommand("3", "compact", "compact", "", "/compact", skill = false),
      ),
    )
    assertEquals(listOf("1", "2"), inventory.matches("rev").map { it.id })
    assertEquals(3, inventory.matches("").size)
    assertEquals("1", inventory.command("/review the diff")?.id)
    assertEquals("1", inventory.command("/review\nplease")?.id)
    assertNull(inventory.command("/reviewer"))
    assertNull(inventory.command("review"))
  }

  @Test
  fun slashParsing() {
    assertEquals("rev", SlashParser.partialCommand("/rev"))
    assertEquals("", SlashParser.partialCommand("/"))
    assertNull(SlashParser.partialCommand("/review now"))
    assertNull(SlashParser.partialCommand("hi /x"))
    assertEquals("", SlashParser.goalArgument("/goal"))
    assertEquals("all tests pass", SlashParser.goalArgument("/goal  all tests pass "))
    assertNull(SlashParser.goalArgument("/goals"))
    assertNull(SlashParser.goalArgument("set /goal"))
    assertEquals("why this?", SlashParser.sideQuestion("/btw why this?"))
    assertNull(SlashParser.sideQuestion("/btw"))
  }

  @Test
  fun parsesClaudeGoalFromRuntime() {
    assertNull(ChatGoal.claude(json("""{"goal":null,"features":{"goals":true}}""")))
    val runtime = json(
      """{"goal":{"condition":"tests pass","status":"active","iterations":2,"set_at":1761500000000,
                 "last_reason":"2 failing","duration_ms":125000,"tokens":5000},
         "goal_starting":false,"features":{"goals":true}}"""
    )
    val goal = ChatGoal.claude(runtime)!!
    assertEquals("tests pass", goal.text)
    assertEquals(ChatGoal.Status.ACTIVE, goal.status)
    assertEquals(2, goal.iterations)
    assertEquals("2 failing", goal.reason)
    assertEquals(125, goal.secondsUsed)
    assertTrue(goal.isOpen)
    assertFalse(goal.canPause)
    assertTrue(GoalSupport.claude(runtime).available)
    assertEquals(ChatGoal.Status.STARTING, ChatGoal.claude(json("""{"goal":{"condition":"x","status":"achieved"},"goal_starting":true}"""))?.status)
    assertEquals(false, ChatGoal.claude(json("""{"goal":{"condition":"x","status":"achieved"}}"""))?.isOpen)
    assertFalse(GoalSupport.claude(json("""{"features":{"goals":false}}""")).available)
  }

  @Test
  fun parsesCodexGoal() {
    val goal = ChatGoal.codex(
      json(
        """{"enabled":true,"goal":{"threadId":"t","objective":"ship it","status":"paused","tokenBudget":500000,
          "tokensUsed":182400,"timeUsedSeconds":1260,"createdAt":1,"updatedAt":2},
         "time_budget_seconds":3600,"time_budget_exhausted":false}"""
      )
    )!!
    assertEquals(ChatGoal.Status.PAUSED, goal.status)
    assertEquals(500_000, goal.tokenBudget)
    assertEquals(182_400, goal.tokensUsed)
    assertEquals(3600, goal.timeBudgetSeconds)
    assertTrue(goal.canResume)
    assertFalse(goal.canPause)
    assertNull(ChatGoal.codex(json("""{"enabled":true,"goal":null}""")))
    assertTrue(GoalSupport.codex(json("""{"goals_enabled":true,"goal":null}""")).available)
    assertFalse(GoalSupport.codex(json("""{"enabled":false}""")).available)
    assertEquals(false, ChatGoal.codex(json("""{"goal":{"objective":"x","status":"paused"},"time_budget_exhausted":true}"""))?.canResume)
  }

  @Test
  fun parsesMcpStatus() {
    val claude = McpStatus.parse(
      json(
        """{"version":1,"available":true,"transport":"agent_sdk","generation":"g7","session_loaded":true,
         "servers":[{"name":"github","status":"connected","enabled":true,"error":null,"scope":"user",
                     "server_info":{"name":"github-mcp-server","version":"1.4.0"},"tool_count":46},
                    {"name":"figma","status":"needs-auth","enabled":true,"error":"This MCP server needs authentication.",
                     "scope":"claudeai","server_info":null,"tool_count":null},
                    {"name":"off","status":"disabled","enabled":false,"error":null,"scope":"project",
                     "server_info":null,"tool_count":0}],
         "truncated":false,"reason":null,"action":null}"""
      ),
      AgentBackend.CLAUDE,
    )!!
    assertTrue(claude.controllable)
    assertEquals(listOf(McpServer.Status.CONNECTED, McpServer.Status.NEEDS_AUTH, McpServer.Status.DISABLED), claude.servers.map { it.status })
    assertEquals("github-mcp-server 1.4.0", claude.servers[0].serverInfo)
    assertEquals(46, claude.servers[0].toolCount)
    assertFalse(claude.servers[2].enabled)

    val codex = McpStatus.parse(
      json(
        """{"version":1,"available":true,"transport":"app-server","generation":"codexmcp_ab","session_loaded":false,
         "servers":[{"name":"github","status":"pending","enabled":true,"error":null,"scope":null,
                     "server_info":null,"tool_count":12}],"truncated":false,"reason":null,"action":null}"""
      ),
      AgentBackend.CODEX,
    )!!
    assertFalse(codex.controllable)
    assertEquals(McpServer.Status.PENDING, codex.servers[0].status)

    val unavailable = McpStatus.parse(
      json(
        """{"version":1,"available":false,"transport":"print","generation":null,"session_loaded":false,"servers":[],
         "truncated":false,"reason":{"code":"claude_mcp_print_transport","message":"needs SDK","retryable":false},
         "action":null}"""
      ),
      AgentBackend.CLAUDE,
    )!!
    assertFalse(unavailable.available)
    assertEquals("needs SDK", unavailable.reason)
    assertFalse(unavailable.controllable)
    assertNull(McpStatus.parse(json("""{"version":2}"""), AgentBackend.CLAUDE))
  }

  @Test
  fun queueEventsReorderUpdateAndForceSend() {
    val projection = SessionProjection(AgentBackend.CODEX, "/tmp")
    projection.apply(
      listOf(
        """{"seq":1,"type":"turn_started","prompt":"go"}""",
        """{"seq":2,"type":"turn_queued","queued_id":"q1","prompt":"one"}""",
        """{"seq":3,"type":"turn_queued","queued_id":"q2","prompt":"two"}""",
        """{"seq":4,"type":"turn_queue_reordered","queued_id":"q2","direction":"up","positions":[{"queued_id":"q2","position":1},{"queued_id":"q1","position":2}]}""",
        """{"seq":5,"type":"turn_queue_updated","queued_id":"q1","prompt":"one, edited"}""",
      ).map(::json)
    )
    assertEquals(listOf("q2", "q1"), projection.queued.map { it.id })
    assertEquals("one, edited", projection.queued.last().text)
    projection.apply(
      listOf(
        """{"seq":6,"type":"turn_steered","queued_id":"q2","prompt":"two"}""",
        """{"seq":7,"type":"turn_queue_run_now","queued_id":"q1"}""",
      ).map(::json)
    )
    assertTrue(projection.queued.isEmpty())
  }

  @Test
  fun heldQueueFollowsStopAndResume() {
    val projection = SessionProjection(AgentBackend.CLAUDE, "/tmp")
    projection.apply(
      listOf(
        """{"seq":1,"type":"turn_queued","queued_id":"q1","prompt":"one"}""",
        """{"seq":2,"type":"turn_queued","queued_id":"q2","prompt":"two"}""",
        """{"seq":3,"type":"turn_queue_paused","queued_ids":["q1","q2"]}""",
        """{"seq":4,"type":"turn_queue_delivery_fenced","queued_id":"q2"}""",
      ).map(::json)
    )
    assertEquals(listOf(QueuedMessage.Hold.STOPPED, QueuedMessage.Hold.DELIVERY_UNCERTAIN), projection.queued.map { it.hold })
    // Resuming releases what Stop held, not a message whose delivery is uncertain.
    projection.apply(listOf(json("""{"seq":5,"type":"turn_queue_resumed","queued_ids":["q1","q2"]}""")))
    assertEquals(listOf(null, QueuedMessage.Hold.DELIVERY_UNCERTAIN), projection.queued.map { it.hold })
  }

  @Test
  fun emergencyAlertsParse() {
    val alert = EmergencyAlert.parse(
      json(
        """{"id":"s1","title":"部署","unacknowledged_emergency_count":2,
         "emergency_alert":{"id":"emergency_0123","status":"active","severity":"critical","message":"磁盘将满","raised_at":1800000000}}"""
      )
    )!!
    assertEquals("emergency_0123", alert.alertID)
    assertEquals(2, alert.count)
    assertEquals("磁盘将满", alert.message)
    assertEquals(1_800_000_000L, alert.raisedAt?.epochSecond)
    assertNull(EmergencyAlert.parse(json("""{"id":"s1","emergency_alert":null}""")))
  }

  @Test
  fun searchHitsParse() {
    val hit = SearchHit.parse(json("""{"session_id":"s1","event_id":"evt_1","seq":42,"ts":1800000000,"role":"assistant","snippet":"…the deploy finished…","match_count":3}"""))!!
    assertEquals(42, hit.seq)
    assertEquals(3, hit.matches)
    assertEquals(1_800_000_000L, hit.time?.epochSecond)
    assertEquals(listOf(4 until 10), searchMatches("the deploy", "DEPLOY"))
  }

  @Test
  fun sideChatParses() {
    val side = SideChat.parse(
      json(
        """{"session_id":"s1","side_chat_id":"sc_1","revision":3,"last_request_id":"r2","exchanges":[
          {"request_id":"r1","question":"why?","status":"completed","answer":"because","context_note":"sees all"},
          {"request_id":"r2","question":"and?","status":"running"}]}"""
      )
    )!!
    assertEquals("sc_1", side.sideChatID)
    assertEquals(3, side.revision)
    assertEquals("r2", side.lastRequestID)
    assertTrue(side.isRunning)
    assertEquals("because", side.exchanges.first().answer)
    assertEquals("sees all", side.contextNote)
  }

  @Test
  fun subagentsParse() {
    val agents = Subagent.parse(
      json(
        """{"session_id":"s1","subagents":[{"id":"a1","subagent_name":"explorer","subagent_status":"running",
          "subagent_activity":"Reading files","subagent_log":[{"ts":1,"text":"grep"}]},
          {"id":"a2","subagent_title":"reviewer","subagent_status":"completed","subagent_summary":"LGTM"}],"count":2}"""
      )
    )
    assertEquals(listOf("explorer", "reviewer"), agents.map { it.name })
    assertEquals(ActivityState.RUNNING, agents[0].state)
    assertEquals(listOf("grep"), agents[0].log)
    assertEquals(ActivityState.DONE, agents[1].state)
    assertEquals("LGTM", agents[1].summary)
  }

  @Test
  fun mentionsAskAnotherChat() {
    val targets = listOf(ConsultTarget("c1", "后端"), ConsultTarget("c2", "前端 Web"), ConsultTarget("c3", "Web 后端"))
    assertEquals("后", ChatMention.partial("问 @后"))
    assertEquals("", ChatMention.partial("@"))
    assertNull(ChatMention.partial("mail@x"))
    assertEquals(listOf("c1", "c3"), ChatMention.matches("后端", targets).map { it.id })
    assertEquals("问 @「后端」 ", ChatMention.complete("问 @后", targets[0]))
    val (target, question) = ChatMention.direct("@「后端」 接口改了吗？", targets)!!
    assertEquals("c1", target.id)
    assertEquals("接口改了吗？", question)
    assertNull(ChatMention.direct("看看 @「后端」", targets))
  }

  @Test
  fun contextUsageFromRuntime() {
    val known = ContextUsage.from(json("""{"context_usage":{"context_tokens":45000,"effective_context_window":200000}}"""))
    assertEquals(ContextUsage.Result.Known(ContextUsage(45000, 200000, 22.5)), known)
    assertEquals(ContextUsage.Result.None, ContextUsage.from(json("""{"context_usage":null,"context_usage_state":"unavailable"}""")))
    assertEquals(ContextUsage.Result.Unchanged, ContextUsage.from(json("""{"runtime":"context_usage"}""")))
    assertEquals("182.4k", compactCount(182_400))
    assertEquals("2M", compactCount(2_000_000))
  }
}

/** A run's unified diff, split by file as the change panel shows it. */
class DiffSplitTest {
  @Test
  fun splitsByFileWithoutHeaders() {
    val unified = listOf(
      "diff --git a/src/app.py b/src/app.py", "index 1..2 100644", "--- a/src/app.py", "+++ b/src/app.py",
      "@@ -1,2 +1,2 @@", " keep", "-old", "+new",
      "diff --git a/README.md b/README.md", "new file mode 100644", "--- /dev/null", "+++ b/README.md", "@@ -0,0 +1 @@", "+hello",
    ).joinToString("\n")
    val files = splitDiff(unified)
    assertEquals(setOf("src/app.py", "README.md"), files.keys)
    assertEquals("@@ -1,2 +1,2 @@\n keep\n-old\n+new", files["src/app.py"])
    assertEquals("@@ -0,0 +1 @@\n+hello", files["README.md"])
  }
}
