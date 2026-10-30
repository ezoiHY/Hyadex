package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.get
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import java.time.Instant
import kotlinx.serialization.json.JsonElement

/**
 * Answers a provider is still writing, from the server's non-durable
 * `assistant_text_stream` lane. Each shows as a streaming message until its
 * durable `assistant_text` (or, for Codex commentary, `reasoning_summary`) arrives.
 */
class LiveText {
  data class Item(val id: String, val runID: String, val text: String, val afterSeq: Int, val started: Instant)

  var items: List<Item> = emptyList()
    private set

  /** Replaces the live answers with a server snapshot (a complete list). */
  fun apply(snapshot: JsonElement) {
    val previous = items.associate { it.id to it.started }
    items = snapshot["items"].array.mapNotNull { item ->
      val id = item["item_id"].string ?: return@mapNotNull null
      val text = item["text"].string?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
      Item(id, item["run_id"].string ?: "", text, item["after_seq"].int ?: 0, previous[id] ?: Instant.now())
    }
  }

  /** A durable event arrived: drop the live answers it now records. */
  fun settle(event: JsonElement) {
    when (event["type"].string) {
      "assistant_text" -> {
        val seq = event["seq"].int ?: 0
        val run = event["run_id"].string
        val itemID = event["item_id"].string
        // Codex names the item; Claude's finished block is the run's answer that began before it.
        items = items.filterNot { it.id == itemID || (it.runID == run && it.afterSeq < seq) }
      }
      "reasoning_summary" -> event["item_id"].string?.let { itemID -> items = items.filterNot { it.id == itemID } }
      "turn_started", "turn_finished", "turn_stopped", "turn_aborted", "turn_failed" -> items = emptyList()
    }
  }

  fun clear() {
    items = emptyList()
  }

  /** Streaming messages for the live answers, in the order they began. */
  fun messages(author: Author): List<TimelineItem> =
    items.sortedWith(compareBy({ it.afterSeq }, { it.started })).map {
      TimelineItem.Message("live-" + it.id, author, it.text, it.started, streaming = true)
    }
}

/** The one line a live status shows for a provider's thinking. */
object ThoughtSummary {
  fun line(text: String): String? {
    val trimmed = text.trim()
    if (trimmed.isEmpty()) return null
    val lines = trimmed.lines().map { it.trim() }
    // Codex's newest bold step heading.
    lines.lastOrNull { it.length > 4 && it.startsWith("**") && it.endsWith("**") }?.let { return plain(it.drop(2).dropLast(2)) }
    val paragraph = lines.lastOrNull { it.isNotEmpty() } ?: trimmed
    val sentences = mutableListOf<String>()
    val current = StringBuilder()
    var previous: Char? = null
    for (character in paragraph) {
      // A full stop ends a sentence only before a space ("3.5" and "file.swift" do not).
      if (previous == '.' && character == ' ') {
        sentences += current.toString()
        current.clear()
        previous = character
        continue
      }
      current.append(character)
      if (character in "。！？!?；") {
        sentences += current.toString()
        current.clear()
      }
      previous = character
    }
    sentences += current.toString()
    val parts = sentences.map { it.trim() }.filter { it.isNotEmpty() }
    var last = parts.lastOrNull() ?: return null
    // A sentence just begun says too little on its own.
    if (last.length < 8 && parts.size > 1) last = parts[parts.size - 2] + " " + last
    return plain(last)
  }

  private fun plain(text: String) = text.replace("**", "").replace("`", "").trim()

  /** The turn's latest thinking, unless the answer has started since. */
  fun latest(items: List<TimelineItem>): String? {
    for (item in items.asReversed()) {
      when (item) {
        is TimelineItem.Thinking -> return item.text
        is TimelineItem.Message -> return null
        else -> continue
      }
    }
    return null
  }
}

/** The status line for a busy chat, as the Mac app's `AppModel.activity(forChat:)`. */
fun liveActivity(
  backend: AgentBackend,
  busy: Boolean,
  items: List<TimelineItem>,
  turnStartedAt: Instant?,
  pending: List<PendingSend>,
  liveReasoning: String?,
  consultApprovals: Int = 0,
): LiveActivity? {
  if (!busy && pending.isEmpty()) return null
  val since = turnStartedAt ?: pending.firstOrNull()?.time ?: Instant.now()
  if (items.pendingApproval() != null) return LiveActivity(LiveActivity.Kind.WAITING, "等待你批准", since = since)
  if (consultApprovals > 0) return LiveActivity(LiveActivity.Kind.WAITING, "等待你批准深度询问", since = since)
  if (items.pendingForm() != null) return LiveActivity(LiveActivity.Kind.WAITING, "等待你回答", since = since)
  if (turnStartedAt == null && pending.isNotEmpty()) {
    return LiveActivity(LiveActivity.Kind.STARTING, "正在启动 ${backend.displayName}", since = since, backend = backend)
  }
  var turn = items
  for (index in items.indices.reversed()) {
    val item = items[index]
    if (item is TimelineItem.Tool && item.status == ToolStatus.RUNNING) {
      return LiveActivity(LiveActivity.Kind.TOOL, item.kind.activeVerb, item.title, since)
    }
    if (item is TimelineItem.Consult && item.running) return LiveActivity(LiveActivity.Kind.TOOL, "正在询问", "「${item.target}」", since)
    if (item is TimelineItem.Message && item.author.kind == Author.Kind.USER) {
      turn = items.subList(index + 1, items.size)
      break
    }
  }
  // Claude's summarized thinking arrives in bursts, so the turn's last thought stays in view until the next one.
  val thought = liveReasoning?.let(ThoughtSummary::line) ?: ThoughtSummary.latest(turn)?.let(ThoughtSummary::line)
  return LiveActivity(LiveActivity.Kind.THINKING, "正在思考", thought, since, backend)
}
