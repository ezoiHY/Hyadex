package dev.hyadex.android.core

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** How items fold into rows: one "已处理" row per run of work, as on the Mac. */
class TimelineRowsTest {
  private val agent = Author.agent("claude", AgentBackend.CLAUDE)
  private val user = TimelineItem.Message("u1", Author.user, "Draw", Instant.EPOCH)
  private val thought = TimelineItem.Thinking("t1", 0, "Plan")
  private val tool = TimelineItem.Tool("b1", ToolKind.COMMAND, "python draw.py", "", ToolStatus.SUCCEEDED, 3.0)
  private val image = TimelineItem.Media("a1", agent, listOf(MediaFile("f1", "out.png", "image/png")))
  private val answer = TimelineItem.Message("m1", agent, "Here it is.", Instant.EPOCH)

  @Test
  fun anImageInsideTheWorkShowsAfterTheRun() {
    val rows = timelineRows(listOf(user, thought, image, tool, answer), null, mapOf("t1" to 72), emptyMap(), emptyList())
    assertEquals(listOf("u1", "process-t1", "a1", "m1"), rows.map { it.key })
    val process = rows[1] as TimelineRow.Process
    assertEquals(listOf("t1", "b1"), process.steps.map { it.id })
    assertEquals("已处理 1:12 · 运行 1 条命令", processSummary(process.steps, process.seconds, process.inProgress))
  }

  @Test
  fun aRunningTurnIsHeadedByTheLiveStatus() {
    val running = tool.copy(status = ToolStatus.RUNNING)
    val activity = LiveActivity(LiveActivity.Kind.TOOL, "正在运行", "python draw.py", Instant.EPOCH)
    val rows = timelineRows(listOf(user, thought, running), activity, emptyMap(), emptyMap(), emptyList())
    val process = rows.last() as TimelineRow.Process
    assertTrue(process.live)
    // A sent message the server has not accepted yet shows at once, followed by the status.
    val pending = timelineRows(listOf(user), LiveActivity(LiveActivity.Kind.STARTING, "正在启动 Claude", since = Instant.EPOCH), emptyMap(), emptyMap(), listOf(PendingSend("p1", "More")))
    assertEquals(listOf("u1", "p1", "live-activity"), pending.map { it.key })
  }

  @Test
  fun aPendingApprovalStaysOutOfTheWork() {
    val approval = TimelineItem.Approval("i1", agent, "修改文件", "a.txt", "", ApprovalDecision.Pending, ApprovalChoice.standard)
    val waiting = LiveActivity(LiveActivity.Kind.WAITING, "等待你批准", since = Instant.EPOCH)
    val rows = timelineRows(listOf(user, tool, approval), waiting, emptyMap(), emptyMap(), emptyList())
    assertEquals(listOf("u1", "process-b1", "i1"), rows.map { it.key })
  }

  @Test
  fun previewsAndTimesReadLikeTheMac() {
    assertEquals("Read the file and run tests", dev.hyadex.android.app.AppViewModel.previewLine("**Read** the [file](a.kt) and `run`\n tests"))
    assertEquals("1:05", formatElapsed(65))
    assertEquals("12 秒", formatElapsed(12))
  }
}

/** The Mac's pairing code: its link fills the connect screen. */
class PairingLinkTest {
  @Test
  fun theMacsLinkGivesAddressAndToken() {
    val link = PairingLink.parse("hyadex://connect?address=http%3A%2F%2F100.64.0.1%3A7870&token=abc123")
    assertEquals(PairingLink("http://100.64.0.1:7870", "abc123"), link)
    // The Mac's URLComponents leaves ":" and "/" in the address as they are (decoded from its QR code).
    val fromMac = PairingLink.parse("hyadex://connect?address=http://192.168.1.20:7870&token=0123456789abcdef")
    assertEquals(PairingLink("http://192.168.1.20:7870", "0123456789abcdef"), fromMac)
    assertEquals(null, PairingLink.parse("https://example.com/?address=x&token=y"))
    assertEquals(null, PairingLink.parse("hyadex://connect?address=http://a:1"))
    assertEquals(null, PairingLink.parse("not a link"))
  }
}
