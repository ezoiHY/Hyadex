package dev.hyadex.android.core

/** What the timeline shows: items, with each run of work folded into one "已处理" row. */
sealed interface TimelineRow {
  val key: String

  data class Item(val item: TimelineItem) : TimelineRow {
    override val key: String get() = item.id
  }

  /** A run of work; `live` while the turn is still running (headed by the live status). */
  data class Process(
    override val key: String,
    val steps: List<TimelineItem>,
    val seconds: Int?,
    val live: Boolean,
    val inProgress: Boolean = false,
  ) : TimelineRow

  /** A deep question to another chat waiting for the user's approval. */
  data class ConsultApproval(val consult: PendingConsult) : TimelineRow {
    override val key: String get() = "consult-approval-" + consult.id
  }

  /** The live status line when no run of work is showing. */
  data object Activity : TimelineRow {
    override val key: String = "live-activity"
  }
}

/** Groups items into rows the way the Mac app's `TimelineView.rows` does. */
fun timelineRows(
  items: List<TimelineItem>,
  activity: LiveActivity?,
  workSeconds: Map<String, Int>,
  turnSeconds: Map<String, Int>,
  pending: List<PendingSend>,
  consultApprovals: List<PendingConsult> = emptyList(),
): List<TimelineRow> {
  val result = mutableListOf<TimelineRow>()
  var steps = mutableListOf<TimelineItem>()
  // An agent's images met inside a run of work: shown after it.
  var floating = mutableListOf<TimelineItem>()
  var turn: String? = null
  var firstRunOfTurn = true
  val lastUser = items.indexOfLast { it is TimelineItem.Message && it.author.kind == Author.Kind.USER }
  var index = 0
  fun flush() {
    val first = steps.firstOrNull()
    if (first != null) {
      val current = activity != null && (lastUser < 0 || index > lastUser)
      // A run's own duration; without one only the turn's first run shows the turn's.
      val seconds = workSeconds[first.id] ?: if (firstRunOfTurn) turn?.let { turnSeconds[it] } else null
      result += TimelineRow.Process("process-" + first.id, steps, seconds, live = false, inProgress = current)
      firstRunOfTurn = false
      steps = mutableListOf()
    }
    floating.mapTo(result) { TimelineRow.Item(it) }
    floating = mutableListOf()
  }
  for ((position, item) in items.withIndex()) {
    index = position
    if (item.isWorkStep) {
      steps += item
      continue
    }
    if (item.floatsPastWork && steps.isNotEmpty()) {
      floating += item
      continue
    }
    flush()
    if (item is TimelineItem.Message && item.author.kind == Author.Kind.USER) {
      turn = item.id
      firstRunOfTurn = true
    }
    result += TimelineRow.Item(item)
  }
  index = items.size
  val first = steps.firstOrNull()
  if (activity != null && activity.kind != LiveActivity.Kind.WAITING && first != null) {
    result += TimelineRow.Process("process-" + first.id, steps, null, live = true)
    floating.mapTo(result) { TimelineRow.Item(it) }
  } else {
    flush()
  }
  for (send in pending) result += TimelineRow.Item(TimelineItem.Message(send.id, Author.user, send.text, send.time))
  consultApprovals.mapTo(result) { TimelineRow.ConsultApproval(it) }
  val streaming = (items.lastOrNull() as? TimelineItem.Message)?.streaming == true
  // While an approval card is showing, the card itself is the status.
  if (activity != null && activity.kind != LiveActivity.Kind.WAITING && !streaming && result.none { it is TimelineRow.Process && it.live }) {
    result += TimelineRow.Activity
  }
  return result
}

/** "已处理 12 秒 · 运行 2 条命令" for a run of work. */
fun processSummary(steps: List<TimelineItem>, seconds: Int?, inProgress: Boolean): String {
  val counts = mutableMapOf<ToolKind, Int>()
  var failed = 0
  for (step in steps) {
    if (step !is TimelineItem.Tool) continue
    counts[step.kind] = (counts[step.kind] ?: 0) + 1
    if (step.status == ToolStatus.FAILED) failed++
  }
  val parts = mutableListOf(if (inProgress) "处理中" else seconds?.let { "已处理 ${formatElapsed(it.toLong())}" } ?: "已处理")
  counts[ToolKind.COMMAND]?.let { parts += "运行 $it 条命令" }
  counts[ToolKind.EDIT]?.let { parts += "编辑 $it 次" }
  counts[ToolKind.READ]?.let { parts += "读取 $it 个文件" }
  counts[ToolKind.SEARCH]?.let { parts += "搜索 $it 次" }
  counts[ToolKind.WEB]?.let { parts += "访问 $it 次网页" }
  counts[ToolKind.MCP]?.let { parts += "调用 $it 个工具" }
  if (counts.isEmpty()) parts += "思考"
  if (failed > 0) parts += "$failed 个失败"
  return parts.joinToString(" · ")
}

/** "12 秒", "1:05". */
fun formatElapsed(seconds: Long): String {
  val total = seconds.coerceAtLeast(0)
  return if (total < 60) "$total 秒" else "%d:%02d".format(total / 60, total % 60)
}

/** An agent's deep question to another chat, waiting for approval (`GET /api/sessions/{id}/consults`). */
data class PendingConsult(val id: String, val target: String, val question: String)
