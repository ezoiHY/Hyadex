package dev.hyadex.android.app

import android.content.Context

/** Unsent text by chat, kept on this phone so it outlives the app being closed. */
class DraftStore(context: Context) {
  private val preferences = context.getSharedPreferences("drafts", Context.MODE_PRIVATE)

  fun load(): Map<String, String> = preferences.all.mapNotNull { (id, text) -> (text as? String)?.takeIf { it.isNotBlank() }?.let { id to it } }.toMap()

  fun save(drafts: Map<String, String>) {
    preferences.edit().clear().apply { drafts.forEach { (id, text) -> if (text.isNotBlank()) putString(id, text) } }.apply()
  }
}
