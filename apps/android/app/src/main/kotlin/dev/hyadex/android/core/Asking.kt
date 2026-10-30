package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Instant
import kotlinx.serialization.json.JsonElement

// Asking: another chat (@ in the composer) or the agent itself on the side
// (side questions), ported from the Mac app's Consults and Extras.

/** A chat this chat may ask: same project, accepting questions. */
data class ConsultTarget(
  val id: String,
  val title: String,
  val backend: AgentBackend? = null,
  /** Whether it also answers in depth (its settings may allow only basic). */
  val allowsDeep: Boolean = true,
  /** Whether it is working right now (a deep copy may see files mid-change). */
  val working: Boolean = false,
) {
  companion object {
    fun parse(json: JsonElement?): ConsultTarget? {
      val id = json["id"].string ?: return null
      return ConsultTarget(
        id, json["title"].string ?: "聊天", AgentBackend.named(json["backend"].string),
        allowsDeep = json["modes"].array.any { it.string == "deep" }, working = json["working"].bool == true,
      )
    }
  }
}

/**
 * `@「title」` names another chat in a composer. Written at the very start of a
 * message, it asks that chat directly; anywhere else it is ordinary text the
 * chat's agent reads (and may ask with its `ask` helper).
 */
object ChatMention {
  fun token(title: String) = "@「$title」"

  /** The chat name being typed after the last "@" (`"问 @后端"` → `"后端"`), or null. */
  fun partial(text: String): String? {
    val at = text.lastIndexOf('@')
    if (at < 0) return null
    if (at > 0 && !text[at - 1].isWhitespace()) return null
    var tail = text.substring(at + 1)
    if ('」' in tail || '\n' in tail || tail.length > 60) return null
    if (tail.startsWith("「")) tail = tail.drop(1)
    return tail
  }

  /** The chats whose titles contain `partial`, prefix matches first. */
  fun matches(partial: String, targets: List<ConsultTarget>): List<ConsultTarget> {
    val wanted = partial.trim()
    if (wanted.isEmpty()) return targets.take(6)
    val found = targets.filter { it.title.contains(wanted, ignoreCase = true) }
    val (prefix, rest) = found.partition { it.title.lowercase().startsWith(wanted.lowercase()) }
    return (prefix + rest).take(6)
  }

  /** `text` with the "@partial" being typed replaced by the chosen chat's mention. */
  fun complete(text: String, target: ConsultTarget): String = text.substring(0, text.lastIndexOf('@')) + token(target.title) + " "

  /** A message that opens with a mention of one of `targets`: whom it asks, and the question. */
  fun direct(text: String, targets: List<ConsultTarget>): Pair<ConsultTarget, String>? {
    val trimmed = text.trimStart()
    if (!trimmed.startsWith("@「")) return null
    val close = trimmed.indexOf('」')
    if (close < 0) return null
    val title = trimmed.substring(2, close)
    val target = targets.firstOrNull { it.title == title } ?: return null
    return target to trimmed.substring(close + 1).trim()
  }
}

/** The side conversation of a chat: questions to its agent that do not touch the main conversation. */
data class SideChat(
  val sideChatID: String,
  val revision: Int = 0,
  val exchanges: List<Exchange> = emptyList(),
  val lastRequestID: String? = null,
  val contextNote: String? = null,
) {
  data class Exchange(
    val requestID: String,
    val question: String,
    val answer: String?,
    val status: Status,
    val error: String? = null,
    val time: Instant? = null,
  ) {
    enum class Status { RUNNING, COMPLETED, FAILED, CANCELLED, INTERRUPTED }
  }

  val isRunning: Boolean get() = exchanges.any { it.status == Exchange.Status.RUNNING }

  companion object {
    fun parse(json: JsonElement?): SideChat? {
      val id = json["side_chat_id"].string ?: return null
      val exchanges = json["exchanges"].array.mapNotNull { item ->
        val request = item["request_id"].string ?: return@mapNotNull null
        val status = Exchange.Status.entries.firstOrNull { it.name.lowercase() == item["status"].string } ?: Exchange.Status.FAILED
        Exchange(request, item["question"].text ?: "", item["answer"].text, status, item["error"].string, item["created_at"].instant)
      }
      val note = json["exchanges"].array.asReversed().firstNotNullOfOrNull { it["context_note"].text }
      return SideChat(id, json["revision"].int ?: 0, exchanges, json["last_request_id"].string, note)
    }
  }
}
