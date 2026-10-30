package dev.hyadex.android.core

import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import java.time.Instant
import kotlinx.serialization.json.JsonElement

/** A chat whose history matches the search box: its newest matching message, as the server's history search finds it. */
data class SearchHit(val chatID: String, val seq: Int, val snippet: String, val matches: Int, val time: Instant?) {
  companion object {
    fun parse(json: JsonElement): SearchHit? {
      val id = json["session_id"].string ?: return null
      val snippet = json["snippet"].string?.replace(Regex("""\s+"""), " ")?.trim().orEmpty()
      if (snippet.isEmpty()) return null
      val time = json["ts"].instant
      return SearchHit(id, json["seq"].int ?: 0, snippet, json["match_count"].int ?: 1, time)
    }
  }
}

/** Where each word of the query appears in `text` (ignoring case), for highlighting. */
fun searchMatches(text: String, query: String): List<IntRange> {
  val words = query.trim().split(Regex("""\s+""")).filter { it.isNotEmpty() }
  val found = mutableListOf<IntRange>()
  for (word in words) {
    var start = text.indexOf(word, ignoreCase = true)
    while (start >= 0) {
      found += start until start + word.length
      start = text.indexOf(word, start + word.length, ignoreCase = true)
    }
  }
  return found.sortedBy { it.first }
}
