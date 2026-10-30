package dev.hyadex.android.app

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** How the app looks and behaves on this phone; kept here, not on the server. Each change is saved at once. */
class AppSettings(context: Context) {
  private val preferences = context.getSharedPreferences("ui", Context.MODE_PRIVATE)

  enum class Theme(val label: String) { SYSTEM("跟随系统"), LIGHT("浅色"), DARK("深色") }

  /** The Mac app's standard and larger text; both follow the phone's own font size on top. */
  enum class TextSize(val label: String, val scale: Float) { STANDARD("标准", 1f), LARGE("较大", 1.15f) }

  private var themeState by mutableStateOf(read(THEME, Theme.SYSTEM))
  private var textSizeState by mutableStateOf(read(TEXT_SIZE, TextSize.STANDARD))
  private var watchState by mutableStateOf(preferences.getBoolean(WATCH, true))
  private var expandWorkState by mutableStateOf(preferences.getBoolean(EXPAND_WORK, false))
  private var byProjectState by mutableStateOf(preferences.getBoolean(BY_PROJECT, true))
  private var notifyApprovalState by mutableStateOf(preferences.getBoolean(NOTIFY_APPROVAL, true))
  private var notifyFinishedState by mutableStateOf(preferences.getBoolean(NOTIFY_FINISHED, true))
  private var showUsageState by mutableStateOf(preferences.getBoolean(SHOW_USAGE, true))

  var theme: Theme
    get() = themeState
    set(value) {
      themeState = value
      preferences.edit().putString(THEME, value.name).apply()
    }

  var textSize: TextSize
    get() = textSizeState
    set(value) {
      textSizeState = value
      preferences.edit().putString(TEXT_SIZE, value.name).apply()
    }

  /** Keep an eye on running chats for a while after leaving the app, and notify. */
  var watchInBackground: Boolean
    get() = watchState
    set(value) {
      watchState = value
      preferences.edit().putBoolean(WATCH, value).apply()
    }

  /** Each turn's "已处理" row starts open, as the Mac's 默认展开处理过程. */
  var expandWork: Boolean
    get() = expandWorkState
    set(value) {
      expandWorkState = value
      preferences.edit().putBoolean(EXPAND_WORK, value).apply()
    }

  /** Chats grouped under their projects; off, one list by recent activity. */
  var groupByProject: Boolean
    get() = byProjectState
    set(value) {
      byProjectState = value
      preferences.edit().putBoolean(BY_PROJECT, value).apply()
    }

  var notifyApproval: Boolean
    get() = notifyApprovalState
    set(value) {
      notifyApprovalState = value
      preferences.edit().putBoolean(NOTIFY_APPROVAL, value).apply()
    }

  var notifyFinished: Boolean
    get() = notifyFinishedState
    set(value) {
      notifyFinishedState = value
      preferences.edit().putBoolean(NOTIFY_FINISHED, value).apply()
    }

  /** The providers' plan usage in the settings, as the Mac's 显示套餐用量. */
  var showUsage: Boolean
    get() = showUsageState
    set(value) {
      showUsageState = value
      preferences.edit().putBoolean(SHOW_USAGE, value).apply()
    }

  private inline fun <reified T : Enum<T>> read(key: String, fallback: T): T =
    preferences.getString(key, null)?.let { name -> enumValues<T>().firstOrNull { it.name == name } } ?: fallback

  private companion object {
    const val THEME = "theme"
    const val TEXT_SIZE = "text_size"
    const val WATCH = "watch_in_background"
    const val EXPAND_WORK = "expand_work"
    const val BY_PROJECT = "group_by_project"
    const val NOTIFY_APPROVAL = "notify_approval"
    const val NOTIFY_FINISHED = "notify_finished"
    const val SHOW_USAGE = "show_usage"
  }
}
