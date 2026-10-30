package dev.hyadex.android.core

import dev.hyadex.android.api.JsonCodec
import dev.hyadex.android.api.get
import dev.hyadex.android.api.string
import java.time.Instant
import kotlinx.serialization.json.JsonElement
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The same cases as the Mac app's projection tests, so both clients read a chat's log alike. */
class SessionProjectionTest {
  private fun at(second: Int) = "2030-01-01T00:%02d:%02dZ".format(second / 60, second % 60)

  private fun events(vararg json: String): List<JsonElement> = json.map(JsonCodec::parseToJsonElement)

  @Test
  fun eachRunOfWorkHasItsOwnDuration() {
    val projection = SessionProjection(AgentBackend.CLAUDE, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Draw a flowerbed","ts":"${at(0)}"}""",
        """{"seq":2,"type":"reasoning_summary","id":"t1","text":"Plan the picture.","ts":"${at(14)}"}""",
        """{"seq":3,"type":"tool_started","tool":{"id":"b1","name":"Bash","input":{"command":"python draw.py"}},"ts":"${at(20)}"}""",
        """{"seq":4,"type":"tool_finished","tool_id":"b1","tool":{"id":"b1","name":"Bash"},"ts":"${at(60)}"}""",
        """{"seq":5,"type":"artifact_created","id":"a1","artifact":{"id":"art_1","title":"flowerbed.png","content_type":"image/png"},"ts":"${at(68)}"}""",
        """{"seq":6,"type":"reasoning_summary","id":"t2","text":"Ready for the next question.","ts":"${at(72)}"}""",
        """{"seq":7,"type":"assistant_text","id":"m1","text":"Here it is.","ts":"${at(72)}"}""",
        // An older server kept the last thought again, as partial.
        """{"seq":8,"type":"reasoning_summary","id":"t3","text":"Ready for the next question.\n\n","partial":true,"ts":"${at(75)}"}""",
        """{"seq":9,"type":"turn_finished","ts":"${at(75)}"}""",
      )
    )
    // The image does not split the work: one run, from the turn's start to the answer.
    assertEquals(mapOf("t1" to 72), projection.workSeconds)
    assertEquals(2, projection.items.count { it is TimelineItem.Thinking })
    assertTrue(projection.items.last() is TimelineItem.Message)
    assertTrue(projection.items.any { it.floatsPastWork })
  }

  @Test
  fun workAfterTheAnswerIsTimedOnItsOwn() {
    val projection = SessionProjection(AgentBackend.CODEX, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Fix it","ts":"${at(0)}"}""",
        """{"seq":2,"type":"reasoning_summary","id":"t1","text":"Look first.","ts":"${at(5)}"}""",
        """{"seq":3,"type":"assistant_text","id":"m1","text":"Checking the tests now.","ts":"${at(10)}"}""",
        """{"seq":4,"type":"tool_started","tool":{"id":"c1","name":"shell","input":{"command":"make test"}},"ts":"${at(12)}"}""",
        """{"seq":5,"type":"tool_finished","tool_id":"c1","tool":{"id":"c1","name":"shell"},"ts":"${at(40)}"}""",
        """{"seq":6,"type":"turn_finished","ts":"${at(41)}"}""",
      )
    )
    assertEquals(mapOf("t1" to 10, "c1" to 29), projection.workSeconds)
  }

  @Test
  fun codexImageGenerationHasAName() {
    val tool = JsonCodec.parseToJsonElement("""{"name":"ImageGeneration","input":{}}""")
    assertEquals("生成图片", SessionProjection(AgentBackend.CODEX, "/w").describeTool(tool).second)
  }

  /** Codex records a stopped turn as finished with `stopped`; the interrupted command failed. */
  @Test
  fun aStoppedCodexTurnReadsAsStopped() {
    val projection = SessionProjection(AgentBackend.CODEX, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Run sleep 60","ts":"${at(0)}"}""",
        """{"seq":2,"type":"tool_started","tool":{"id":"c1","name":"shell","input":{"command":"/bin/zsh -lc 'sleep 60'"}},"ts":"${at(2)}"}""",
        """{"seq":3,"type":"turn_finished","id":"f1","stopped":true,"ts":"${at(12)}"}""",
        // The same stop recorded again shows once.
        """{"seq":4,"type":"turn_stopped","id":"s1","ts":"${at(12)}"}""",
      )
    )
    assertEquals(1, projection.items.count { it is TimelineItem.Divider })
    assertFalse(projection.running)
    val tool = projection.items.filterIsInstance<TimelineItem.Tool>().single()
    assertEquals(ToolStatus.FAILED, tool.status)
    assertEquals("sleep 60", tool.title)
    assertEquals("已停止", (projection.items.last() as TimelineItem.Divider).text)
  }

  @Test
  fun anApprovalWaitsThenSaysWhatWasChosen() {
    val projection = SessionProjection(AgentBackend.CODEX, "/Users/me/w", "/Users/me")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Delete build","ts":"${at(0)}"}""",
        """{"seq":2,"type":"codex_interaction_requested","interaction":{"id":"i1","method":"item/commandExecution/requestApproval",
          "params":{"command":"rm -rf /Users/me/w/build","cwd":"/Users/me/w","availableDecisions":["accept","decline"]}},"ts":"${at(1)}"}""",
      )
    )
    val pending = projection.items.pendingApproval()!!
    assertEquals("运行命令", pending.title)
    assertEquals("rm -rf ~/w/build", pending.command)
    assertEquals(listOf("允许", "拒绝"), pending.choices.map { it.label })
    assertTrue(projection.pending.containsKey("i1"))
    // The user's own answer names the resolved card.
    projection.noteDecision(ApprovalDecision.Denied, "i1")
    projection.apply(events("""{"seq":3,"type":"codex_interaction_resolved","interaction_id":"i1","ts":"${at(3)}"}"""))
    assertNull(projection.items.pendingApproval())
    assertEquals(ApprovalDecision.Denied, (projection.items.single { it.id == "i1" } as TimelineItem.Approval).decision)
    assertTrue(projection.items.single { it.id == "i1" }.isWorkStep)
  }

  @Test
  fun aSubagentsWorkFoldsIntoItsCard() {
    val projection = SessionProjection(AgentBackend.CLAUDE, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Research","ts":"${at(0)}"}""",
        """{"seq":2,"type":"tool_started","tool":{"id":"task1","name":"Agent","input":{"description":"Survey the code"}},"ts":"${at(1)}"}""",
        """{"seq":3,"type":"tool_started","parent_tool_id":"task1","tool":{"id":"r1","name":"Read","input":{"file_path":"/w/a.kt"}},"ts":"${at(2)}"}""",
        """{"seq":4,"type":"assistant_text","parent_tool_id":"task1","text":"Found it.","ts":"${at(3)}"}""",
        // As the server sends it: the finished event carries the tool again.
        """{"seq":5,"type":"tool_finished","tool_id":"task1","tool":{"id":"task1","name":"Agent","input":{"description":"Survey the code"}},"output":"Summary","ts":"${at(4)}"}""",
      )
    )
    // The subagent's steps never become the main agent's messages.
    assertEquals(listOf("u1", "task1"), projection.items.map { it.id })
    val card = projection.items.last() as TimelineItem.Tool
    assertEquals("Survey the code", card.title)
    assertTrue(card.output.startsWith("子代理用了 1 次工具\n· a.kt"))
    assertTrue(card.output.endsWith("Summary"))
  }

  /** An agent's question to another chat is its own card, with the answer from the helper's receipt. */
  @Test
  fun aQuestionToAnotherChatShowsItsAnswer() {
    val helper = "mcp__hyadex_internal_provider__helper"
    val projection = SessionProjection(AgentBackend.CLAUDE, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Ask the backend chat","ts":"${at(0)}"}""",
        """{"seq":2,"type":"tool_started","tool":{"id":"a1","name":"$helper","input":{"helper":"ask","arguments":["ask","Backend B","--deep"],"stdin":"Which port?"}},"ts":"${at(1)}"}""",
      )
    )
    val asking = projection.items.single { it.id == "a1" } as TimelineItem.Consult
    assertEquals(TimelineItem.Consult("a1", "Backend B", deep = true, question = "Which port?", running = true), asking)
    assertFalse(asking.isWorkStep)
    assertEquals("正在询问", liveActivity(AgentBackend.CLAUDE, true, projection.items, Instant.EPOCH, emptyList(), null)!!.label)
    projection.apply(
      events(
        """{"seq":3,"type":"tool_finished","tool_id":"a1","tool":{"id":"a1","name":"$helper","input":{"helper":"ask","arguments":["ask","Backend B","--deep"]}},"output":"{\"ok\":true,\"answer\":\"Port 7870.\"}","ts":"${at(9)}"}""",
      )
    )
    assertEquals("Port 7870.", (projection.items.single { it.id == "a1" } as TimelineItem.Consult).answer)
    // `ask list` stays an ordinary step.
    val list = JsonCodec.parseToJsonElement("""{"name":"$helper","input":{"helper":"ask","arguments":["list"]}}""")
    assertNull(SessionProjection.consultCall(list))
    assertEquals("查看可以询问的聊天", projection.describeTool(list).second)
    assertEquals(null to "Not allowed", SessionProjection.consultReceipt("""{"ok":false,"error":{"message":"Not allowed"}}""", failed = true))
    // Codex's form (seen live): the receipt is the text of an MCP result.
    val codex = """{"content":[{"type":"text","text":"{\"ok\":true,\"status\":\"completed\",\"answer\":\"One file: README.md\"}"}],"structuredContent":null}"""
    assertEquals("One file: README.md" to null, SessionProjection.consultReceipt(codex, failed = false))
  }

  @Test
  fun aQuestionStillRunningWhenTheTurnStopsIsSettled() {
    val projection = SessionProjection(AgentBackend.CODEX, "/w")
    projection.apply(
      events(
        """{"seq":1,"type":"turn_started","id":"u1","prompt":"Ask","ts":"${at(0)}"}""",
        """{"seq":2,"type":"tool_started","tool":{"id":"a1","name":"mcp__hyadex_internal_provider__helper","input":{"helper":"ask","arguments":["ask","B"],"stdin":"Hi?"}},"ts":"${at(1)}"}""",
        """{"seq":3,"type":"turn_stopped","id":"s1","ts":"${at(3)}"}""",
      )
    )
    val consult = projection.items.single { it.id == "a1" } as TimelineItem.Consult
    assertFalse(consult.running)
    assertEquals("已停止", consult.error)
  }

  @Test
  fun keptEventsAreTrimmed() {
    val long = "x".repeat(10_000)
    val event = JsonCodec.parseToJsonElement(
      """{"seq":1,"type":"tool_finished","tool_id":"w1","tool":{"id":"w1","name":"Write","input":{"file_path":"/w/a.txt","content":"$long"}},"output":"$long"}"""
    )
    val kept = dev.hyadex.android.app.AppViewModel.trimmed(event)
    assertEquals(4_000, kept["output"].string!!.length)
    assertEquals(4_000, kept["tool"]["input"]["content"].string!!.length)
    assertEquals("/w/a.txt", kept["tool"]["input"]["file_path"].string)
    // What the timeline reads is unchanged.
    val projection = SessionProjection(AgentBackend.CLAUDE, "/w")
    projection.apply(listOf(JsonCodec.parseToJsonElement("""{"seq":0,"type":"noop"}"""), kept))
    assertEquals("a.txt", (projection.items.single() as TimelineItem.Tool).title)
  }

  @Test
  fun eventsAlreadyAppliedAreIgnored() {
    val projection = SessionProjection(AgentBackend.CLAUDE, "/w")
    val first = events("""{"seq":1,"type":"turn_started","id":"u1","prompt":"Hi","ts":"${at(0)}"}""")
    projection.apply(first)
    projection.apply(first)
    assertEquals(1, projection.items.size)
    assertEquals(1, projection.lastSeq)
  }

  @Test
  fun liveAnswersGiveWayToTheirDurableText() {
    val live = LiveText()
    live.apply(JsonCodec.parseToJsonElement("""{"items":[{"item_id":"a","run_id":"r","text":"Hel","after_seq":4}]}"""))
    val author = Author.agent("claude", AgentBackend.CLAUDE)
    assertEquals("Hel", (live.messages(author).single() as TimelineItem.Message).text)
    live.settle(JsonCodec.parseToJsonElement("""{"type":"assistant_text","seq":6,"run_id":"r","text":"Hello"}"""))
    assertTrue(live.messages(author).isEmpty())
  }

  @Test
  fun theStatusLineShowsTheLatestThoughtOrTheRunningTool() {
    val user = TimelineItem.Message("u1", Author.user, "Go", Instant.EPOCH)
    val thinking = TimelineItem.Thinking("t1", 0, "**Reading the tests**\n\nThe suite is long.")
    val activity = liveActivity(AgentBackend.CODEX, true, listOf(user, thinking), Instant.EPOCH, emptyList(), null)!!
    assertEquals("正在思考", activity.label)
    assertEquals("Reading the tests", activity.detail)
    val tool = TimelineItem.Tool("c1", ToolKind.COMMAND, "make test", "", ToolStatus.RUNNING, 0.0)
    val running = liveActivity(AgentBackend.CODEX, true, listOf(user, thinking, tool), Instant.EPOCH, emptyList(), null)!!
    assertEquals("正在运行", running.label)
    assertEquals("make test", running.detail)
    assertNull(liveActivity(AgentBackend.CODEX, false, listOf(user), null, emptyList(), null))
  }

  @Test
  fun aQuestionFormRepliesInCodexsShape() {
    val form = InteractionForm.parse(
      "item/tool/requestUserInput",
      JsonCodec.parseToJsonElement("""{"questions":[{"id":"q1","header":"Color","question":"Which one?","options":[{"label":"Red"},{"label":"Blue"}]}]}"""),
    )!!
    assertFalse(form.isComplete(emptyMap()))
    val reply = form.reply(mapOf("q1" to listOf("Blue")))
    assertEquals("""{"answers":{"q1":{"answers":["Blue"]}}}""", reply.response.toString())
    assertEquals(ApprovalDecision.Answered("Color：Blue"), reply.outcome)
    assertEquals("""{"answers":{}}""", form.dismissal.response.toString())
  }
}
