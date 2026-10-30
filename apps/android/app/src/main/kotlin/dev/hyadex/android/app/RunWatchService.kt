package dev.hyadex.android.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import dev.hyadex.android.MainActivity
import dev.hyadex.android.R
import dev.hyadex.android.api.HyadexClient
import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import dev.hyadex.android.core.ActivityState
import dev.hyadex.android.core.chatSummary
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * While the app is out of sight and a chat is still working, watches the chat
 * list and says when a reply is done or an approval is waiting. It runs only
 * then: it stops once nothing is busy, when the app comes back, or after two hours.
 */
class RunWatchService : Service() {
  private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
  private var watch: Job? = null

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    createChannels(this)
    // "id|STATE|seq": the state when the app left, and the chat's seq then.
    val entries = intent?.getStringArrayListExtra(EXTRA_BUSY).orEmpty().map { it.split('|') }
    val busy = entries.associate { it[0] to ActivityState.valueOf(it[1]) }
    val baselines = entries.associate { it[0] to (it.getOrNull(2)?.toIntOrNull() ?: 0) }
    val ongoing = ongoing(busy.size.coerceAtLeast(1))
    startForeground(ONGOING_ID, ongoing, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    if (watch == null) watch = scope.launch { watch(busy, baselines) }
    return START_NOT_STICKY
  }

  override fun onTimeout(startId: Int, fgsType: Int) {
    stopSelf()
  }

  override fun onDestroy() {
    scope.cancel()
    super.onDestroy()
  }

  private suspend fun watch(initial: Map<String, ActivityState>, baselines: Map<String, Int>) {
    val saved = ServerStore(this).load()
    val url = saved?.let { HyadexClient.parseAddress(it.address) }
    if (saved == null || url == null) return stopSelf()
    val client = HyadexClient(url, saved.token)
    // Read once: the settings cannot change while the app is out of sight.
    val settings = AppSettings(this)
    val notifyApproval = settings.notifyApproval
    val notifyFinished = settings.notifyFinished
    var previous = initial
    var shownCount = -1
    var interval = POLL_MILLIS
    val started = System.currentTimeMillis()
    while (System.currentTimeMillis() < started + MAX_WATCH_MILLIS) {
      // The summary list carries every state this reads, at about half the size.
      val chats = runCatching { client.sessions(summary = true).mapNotNull(::chatSummary) }.getOrNull()
      if (chats != null) {
        var changed = false
        val next = mutableMapOf<String, ActivityState>()
        for (chat in chats) {
          val before = previous[chat.id] ?: continue
          // A message just sent: the server has not started its turn yet. Wait for it
          // (for a while), so the previous answer is never reported as this one.
          if (before == ActivityState.QUEUED && chat.state == ActivityState.IDLE) {
            // Unless its agent already acted: the turn was quick and is done.
            if (chat.agentSeq > (baselines[chat.id] ?: Int.MAX_VALUE)) {
              if (notifyFinished) notify(chat.id, chat.title, outcome(client, chat.id), important = false)
            } else if (System.currentTimeMillis() - started < START_GRACE_MILLIS) {
              next[chat.id] = ActivityState.QUEUED
            }
            continue
          }
          if (before != chat.state) changed = true
          when {
            chat.state == ActivityState.NEEDS_ACTION && before != ActivityState.NEEDS_ACTION ->
              if (notifyApproval) notify(chat.id, chat.title, "等待你处理", important = true)
            chat.state == ActivityState.IDLE -> if (notifyFinished) notify(chat.id, chat.title, outcome(client, chat.id), important = false)
          }
          if (chat.state == ActivityState.RUNNING || chat.state == ActivityState.NEEDS_ACTION) next[chat.id] = chat.state
        }
        previous = next
        // Waiting for the user needs the user, not this watch.
        if (previous.values.none { it == ActivityState.RUNNING || it == ActivityState.QUEUED }) break
        val count = previous.size
        if (count != shownCount) {
          shownCount = count
          getSystemService(NotificationManager::class.java).notify(ONGOING_ID, ongoing(count))
        }
        // Long turns need no 5-second checks: back off while nothing changes.
        interval = if (changed) POLL_MILLIS else (interval * 2).coerceAtMost(MAX_POLL_MILLIS)
      }
      delay(interval)
    }
    stopSelf()
  }

  /** How the turn ended, in a line: its answer, or that it stopped or failed. */
  private suspend fun outcome(client: HyadexClient, id: String): String {
    val events = runCatching { client.sessionTail(id, 20) }.getOrNull()["events"].array
    for (event in events.asReversed()) {
      when (event["type"].string) {
        "assistant_text" -> return event["text"].text?.let(AppViewModel::previewLine) ?: "回复完成"
        "turn_stopped", "turn_aborted" -> return "已停止"
        "turn_failed", "error" -> return "运行出错"
        "turn_finished" -> if (event["stopped"].bool == true) return "已停止"
        "turn_started" -> return "回复完成"
      }
    }
    return "回复完成"
  }

  private fun ongoing(count: Int): Notification =
    NotificationCompat.Builder(this, CHANNEL_RUNNING)
      .setSmallIcon(R.drawable.ic_stat_hyadex)
      .setContentTitle(if (count > 1) "$count 个对话正在运行" else "对话正在运行")
      .setContentText("完成或需要你批准时会通知你")
      .setContentIntent(openApp(null))
      .setOngoing(true)
      .setSilent(true)
      .build()

  private fun notify(chatID: String, title: String, text: String, important: Boolean) {
    if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
    val notification = NotificationCompat.Builder(this, if (important) CHANNEL_APPROVALS else CHANNEL_REPLIES)
      .setSmallIcon(R.drawable.ic_stat_hyadex)
      .setContentTitle(title)
      .setContentText(text)
      .setStyle(NotificationCompat.BigTextStyle().bigText(text))
      .setContentIntent(openApp(chatID))
      .setAutoCancel(true)
      .setCategory(if (important) NotificationCompat.CATEGORY_REMINDER else NotificationCompat.CATEGORY_MESSAGE)
      .build()
    getSystemService(NotificationManager::class.java).notify(chatID.hashCode(), notification)
  }

  private fun openApp(chatID: String?): PendingIntent {
    val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
    chatID?.let { intent.putExtra(EXTRA_CHAT, it) }
    return PendingIntent.getActivity(this, chatID?.hashCode() ?: 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
  }

  companion object {
    const val EXTRA_CHAT = "chat_id"
    private const val EXTRA_BUSY = "busy"
    private const val ONGOING_ID = 1
    private const val CHANNEL_RUNNING = "running"
    private const val CHANNEL_REPLIES = "replies"
    private const val CHANNEL_APPROVALS = "approvals"
    private const val POLL_MILLIS = 5_000L
    private const val MAX_POLL_MILLIS = 30_000L
    private const val START_GRACE_MILLIS = 90_000L
    private const val MAX_WATCH_MILLIS = 2 * 60 * 60 * 1000L

    /**
     * Starts watching when the app leaves the screen with chats still busy
     * (QUEUED: a message sent whose turn the server has not started yet).
     */
    fun startIfBusy(context: Context, busy: Map<String, ActivityState>, baselines: Map<String, Int> = emptyMap()) {
      if (busy.values.none { it == ActivityState.RUNNING || it == ActivityState.QUEUED }) return
      // Nothing it could say: no watch.
      if (!NotificationManagerCompat.from(context).areNotificationsEnabled()) return
      val intent = Intent(context, RunWatchService::class.java)
        .putStringArrayListExtra(EXTRA_BUSY, ArrayList(busy.map { (id, state) -> "$id|${state.name}|${baselines[id] ?: 0}" }))
      runCatching { ContextCompat.startForegroundService(context, intent) }
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, RunWatchService::class.java))
    }

    private fun createChannels(context: Context) {
      val manager = context.getSystemService(NotificationManager::class.java)
      manager.createNotificationChannels(
        listOf(
          NotificationChannel(CHANNEL_RUNNING, "运行中", NotificationManager.IMPORTANCE_LOW),
          NotificationChannel(CHANNEL_REPLIES, "回复完成", NotificationManager.IMPORTANCE_DEFAULT),
          NotificationChannel(CHANNEL_APPROVALS, "等待批准", NotificationManager.IMPORTANCE_HIGH),
        )
      )
    }
  }
}
