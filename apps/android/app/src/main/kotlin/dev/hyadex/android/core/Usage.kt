package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.get
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Instant
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull

/** "2 小时后重置", "3 天后重置": when a limit starts over, from now. */
fun resetLabel(at: Instant, now: Instant = Instant.now()): String {
  val minutes = java.time.Duration.between(now, at).toMinutes()
  return when {
    minutes <= 0 -> "即将重置"
    minutes < 60 -> "$minutes 分钟后重置"
    minutes < 48 * 60 -> "${(minutes + 30) / 60} 小时后重置"
    else -> "${(minutes + 12 * 60) / (24 * 60)} 天后重置"
  }
}

/** One limit of a provider's plan: how much of it is used and when it starts over. */
data class UsageWindow(val id: String, val label: String, val usedPercent: Double?, val resetsAt: Instant?)

/** A provider's plan usage as its own account reports it, or why it cannot be shown. */
data class ProviderUsage(val backend: AgentBackend, val available: Boolean, val windows: List<UsageWindow>, val reason: String?) {
  /** The window closest to its limit: what the list's footer shows. */
  val tightest: UsageWindow? get() = windows.filter { it.usedPercent != null }.maxByOrNull { it.usedPercent!! }

  companion object {
    fun parse(backend: AgentBackend, json: JsonElement?): ProviderUsage {
      val windows = json["windows"].array.mapNotNull { window ->
        val id = window["id"].string ?: return@mapNotNull null
        val used = (window["used_percent"] as? JsonPrimitive)?.doubleOrNull
        val resets = (window["resets_at"] as? JsonPrimitive)?.doubleOrNull?.let { Instant.ofEpochMilli((it * 1000).toLong()) }
        val minutes = (window["window_minutes"] as? JsonPrimitive)?.doubleOrNull?.toInt()
        UsageWindow(id, window["label"].text ?: nativeLabel(backend, minutes), used, resets)
      }
      return ProviderUsage(backend, json["status"].string == "available", windows, json["reason"].string)
    }

    /** The provider's own name for a window the server sent without one, as the Mac names it (Codex's `/status`). */
    fun nativeLabel(backend: AgentBackend, minutes: Int?): String =
      when {
        minutes == 300 -> if (backend == AgentBackend.CODEX) "5h limit" else "Current session"
        minutes == 10080 -> if (backend == AgentBackend.CODEX) "Weekly limit" else "Current week"
        minutes == null -> "Limit"
        minutes < 1440 -> "${minutes / 60}h limit"
        else -> "${minutes / 1440}d limit"
      }
  }
}
