package dev.hyadex.android.core

import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Instant
import kotlinx.serialization.json.JsonElement

/** An agent's emergency alert the user has not acknowledged: the newest one of its chat. */
data class EmergencyAlert(val chatID: String, val title: String, val alertID: String, val message: String, val raisedAt: Instant?, val count: Int) {
  companion object {
    /** From a chat as the emergency stream sends it (`emergency_alert` and its unacknowledged count). */
    fun parse(session: JsonElement?): EmergencyAlert? {
      val id = session["id"].string ?: return null
      val alert = session["emergency_alert"]
      val alertID = alert["id"].string ?: return null
      return EmergencyAlert(
        chatID = id,
        title = session["title"].text ?: "未命名聊天",
        alertID = alertID,
        message = alert["message"].text ?: "",
        raisedAt = alert["raised_at"].instant ?: alert["raised_at"].string?.toDoubleOrNull()?.let { Instant.ofEpochMilli((it * 1000).toLong()) },
        count = session["unacknowledged_emergency_count"].int ?: 1,
      )
    }
  }
}
