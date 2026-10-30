package dev.hyadex.android.app

import android.app.Application
import android.graphics.Bitmap
import android.graphics.ImageDecoder
import android.net.Uri
import android.provider.OpenableColumns
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import coil3.ImageLoader
import coil3.disk.DiskCache
import coil3.disk.directory
import coil3.memory.MemoryCache
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import dev.hyadex.android.api.EventFrame
import dev.hyadex.android.api.HyadexClient
import dev.hyadex.android.api.HyadexError
import dev.hyadex.android.api.array
import dev.hyadex.android.api.events
import dev.hyadex.android.api.get
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import dev.hyadex.android.core.ActivityState
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.ApprovalDecision
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.InteractionReply
import dev.hyadex.android.core.LiveActivity
import dev.hyadex.android.core.LiveText
import dev.hyadex.android.core.PairingLink
import dev.hyadex.android.core.PendingConsult
import dev.hyadex.android.core.PendingSend
import dev.hyadex.android.core.QueuedMessage
import dev.hyadex.android.core.RuntimeCatalog
import dev.hyadex.android.core.SessionProjection
import dev.hyadex.android.core.TimelineItem
import dev.hyadex.android.core.TimelineRow
import dev.hyadex.android.core.chatSummary
import dev.hyadex.android.core.liveActivity
import dev.hyadex.android.core.permissionLabel
import dev.hyadex.android.core.timelineRows
import dev.hyadex.android.ui.MarkdownCache
import java.io.ByteArrayOutputStream
import java.time.Instant
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Everything the chat screen shows for the open chat, rebuilt (at most once a frame) after events. */
data class ChatView(
  val id: String,
  val rows: List<TimelineRow>,
  val activity: LiveActivity?,
  val running: Boolean,
  val queued: List<QueuedMessage>,
  val olderHistory: Boolean,
  val loaded: Boolean,
  /** The live stream is down and reconnecting. */
  val reconnecting: Boolean = false,
)

/** A snackbar message, with an action such as undo. */
class Notice(val text: String, val action: String? = null, val run: () -> Unit = {})

sealed interface Connection {
  data object NotConfigured : Connection
  data object Connecting : Connection
  data class Connected(val address: String, val serverName: String?, val version: String?) : Connection
  data class Failed(val address: String, val message: String) : Connection
}

/** An image picked for the next message: uploading until it has a file id. */
data class Attachment(val key: String, val uri: Uri, val name: String, val fileID: String? = null, val failed: Boolean = false)

/**
 * The app's one state holder. Chats are polled while the app is in front; the
 * open chat streams its events over one socket, closed in the background and
 * resumed from the last applied `seq` when the app returns.
 */
class AppViewModel(application: Application) : AndroidViewModel(application) {
  private val store = ServerStore(application)

  var connection by mutableStateOf<Connection>(Connection.NotConfigured)
    private set
  var client by mutableStateOf<HyadexClient?>(null)
    private set
  var imageLoader by mutableStateOf<ImageLoader?>(null)
    private set
  var chats by mutableStateOf<List<ChatSummary>>(emptyList())
    private set
  /** The chat list has arrived at least once (so a chat missing from it is really gone). */
  var listLoaded by mutableStateOf(false)
    private set
  /** The last list poll could not reach the server. */
  var offline by mutableStateOf(false)
    private set
  val previews = mutableStateMapOf<String, String>()
  var openChatID by mutableStateOf<String?>(null)
    private set
  var chatView by mutableStateOf<ChatView?>(null)
    private set
  /** A message for the user (a failed request, or what can be undone); cleared once shown. */
  var notice by mutableStateOf<Notice?>(null)
  val drafts = mutableStateMapOf<String, String>()
  val attachments = mutableStateMapOf<String, List<Attachment>>()
  val catalogs = mutableStateMapOf<AgentBackend, RuntimeCatalog>()
  var refreshing by mutableStateOf(false)
    private set
  /** A chat to open, from a tapped notification. */
  var openRequest by mutableStateOf<String?>(null)
  /** A server from a scanned or opened pairing link, waiting for the user to connect. */
  var pairing by mutableStateOf<PairingLink?>(null)
  /** The open chat, closed by the server (deleted elsewhere): the screen leaves it. */
  var closedChat by mutableStateOf<String?>(null)
  /** The saved server, read once off the main thread (the token is decrypted with the Keystore). */
  var savedServer by mutableStateOf<ServerStore.Saved?>(null)
    private set

  private var foreground = true
  private var pollJob: Job? = null
  private var polls = 0
  private var streamJob: Job? = null
  private var publishJob: Job? = null
  private var consultJob: Job? = null
  private var projection: SessionProjection? = null
  /** The open chat's events, oldest first and trimmed, so older history can be paged in front of them. */
  private val events = mutableListOf<JsonElement>()
  private var olderHistory = false
  private val liveText = LiveText()
  private var liveReasoning: String? = null
  private var reconnecting = false
  private var consultApprovals = emptyList<PendingConsult>()
  private val pendingSends = mutableMapOf<String, MutableList<PendingSend>>()
  /** Interactions being answered: a second tap waits for the first. */
  private val answering = mutableSetOf<String>()
  /** Permission presets by chat, from full records (the summary list leaves them out). */
  private val permissions = mutableMapOf<String, String>()
  private val uploads = Semaphore(2)

  init {
    viewModelScope.launch {
      val saved = withContext(Dispatchers.IO) { store.load() }
      savedServer = saved
      saved?.let { connect(it.address, it.token, save = false) }
    }
  }

  // Connection

  fun connect(address: String, token: String, save: Boolean = true) {
    val url = HyadexClient.parseAddress(address)
    if (url == null) {
      connection = Connection.Failed(address, "服务器地址无效")
      return
    }
    connection = Connection.Connecting
    val candidate = HyadexClient(url, token.trim())
    viewModelScope.launch {
      try {
        val health = candidate.health()
        val sessions = candidate.sessions()
        if (save) {
          withContext(Dispatchers.IO) { store.save(address.trim(), token.trim()) }
          savedServer = ServerStore.Saved(address.trim(), token.trim())
        }
        client = candidate
        imageLoader = ImageLoader.Builder(getApplication())
          .components { add(OkHttpNetworkFetcherFactory(callFactory = { candidate.http })) }
          .memoryCache { MemoryCache.Builder().maxSizePercent(getApplication(), 0.15).build() }
          .diskCache { DiskCache.Builder().directory(getApplication<Application>().cacheDir.resolve("images")).maxSizeBytes(64L shl 20).build() }
          .build()
        val port = if (url.port != 80 && url.port != 443) ":${url.port}" else ""
        connection = Connection.Connected(url.host + port, health["server_name"].text, health["server_version"].string)
        offline = false
        applyChats(sessions, full = true)
        startPolling()
        loadCatalog()
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        connection = Connection.Failed(address, error.message ?: "无法连接")
      }
    }
  }

  /** Connects again with the saved address and token (after the server was down or the token changed). */
  fun reconnect() {
    val saved = savedServer ?: return
    connect(saved.address, saved.token, save = false)
  }

  fun disconnect() {
    pollJob?.cancel()
    closeChat()
    store.clear()
    savedServer = null
    client = null
    imageLoader = null
    chats = emptyList()
    listLoaded = false
    previews.clear()
    catalogs.clear()
    permissions.clear()
    connection = Connection.NotConfigured
  }

  /** In the background nothing polls and no socket stays open; returning catches up from the last `seq`. */
  fun setForeground(value: Boolean) {
    if (foreground == value) return
    foreground = value
    if (value) {
      if (client != null) startPolling(immediately = true)
      val id = openChatID
      if (id != null && streamJob == null) {
        // Left while the history was still loading: load it again.
        if (projection == null) {
          openChatID = null
          openChat(id)
        } else {
          startStream()
        }
      }
    } else {
      pollJob?.cancel()
      pollJob = null
      streamJob?.cancel()
      streamJob = null
      consultJob?.cancel()
      consultJob = null
    }
  }

  private fun startPolling(immediately: Boolean = false) {
    pollJob?.cancel()
    pollJob = viewModelScope.launch {
      if (immediately) refreshChats()
      while (true) {
        // The open chat has its own stream; the list matters less then.
        delay(if (openChatID != null) OPEN_CHAT_POLL_MILLIS else POLL_MILLIS)
        refreshChats()
      }
    }
  }

  fun refresh() {
    viewModelScope.launch {
      refreshing = true
      refreshChats(full = true)
      refreshing = false
    }
  }

  /** The chat list; the summary form most of the time, a full one now and then for permission presets. */
  suspend fun refreshChats(full: Boolean = polls++ % FULL_LIST_EVERY == 0) {
    val client = client ?: return
    try {
      applyChats(client.sessions(summary = !full), full)
      offline = false
    } catch (cancelled: CancellationException) {
      throw cancelled
    } catch (error: HyadexError.Unauthorized) {
      connection = Connection.Failed((connection as? Connection.Connected)?.address ?: "", error.message ?: "")
    } catch (_: Exception) {
      // Retried on the next tick; the footer says the server is out of reach meanwhile.
      offline = true
    }
  }

  private suspend fun applyChats(sessions: List<JsonElement>, full: Boolean) {
    val open = projection
    var unknown = false
    val mapped = sessions.mapNotNull { json ->
      val chat = chatSummary(json) ?: return@mapNotNull null
      if (full) permissions[chat.id] = chat.permission
      val permission = permissions[chat.id] ?: chat.permission.also { unknown = true }
      chat.copy(permission = permission)
    }.filter { !it.archived }.map { chat ->
      if (chat.id == openChatID && open != null) chat.copy(state = stateOf(open), unread = false) else chat
    }
    // Keep unchanged chats as they were, so their rows skip recomposition.
    val previous = chats.associateBy { it.id }
    val merged = mapped.map { chat -> previous[chat.id]?.takeIf { it == chat } ?: chat }
    if (merged != chats) chats = merged
    listLoaded = true
    if (unknown && !full) refreshChats(full = true)
    loadMissingPreviews()
  }

  /** The list has no message text, so fetch a short tail for chats without a preview. */
  private suspend fun loadMissingPreviews() {
    val client = client ?: return
    val missing = chats.filter { it.id !in previews }.take(12)
    if (missing.isEmpty()) return
    val results = missing.map { chat ->
      viewModelScope.async {
        val tail = runCatching { client.sessionTail(chat.id, 40) }.getOrNull()
        val event = tail["events"].array.asReversed().firstOrNull { it["type"].string in setOf("assistant_text", "turn_started") }
        chat.id to (event?.let { it["text"].text ?: it["display_prompt"].text ?: it["prompt"].text })
      }
    }.awaitAll()
    for ((id, text) in results) previews[id] = text?.let(::previewLine) ?: "还没有消息"
  }

  private fun loadCatalog() {
    val client = client ?: return
    viewModelScope.launch {
      val json = runCatching { client.runtimeCatalog() }.getOrNull() ?: return@launch
      for (backend in AgentBackend.entries) RuntimeCatalog.parse(json["backends"][backend.raw])?.let { catalogs[backend] = it }
    }
  }

  // Chat list actions

  fun updateChat(id: String, fields: Map<String, Any?>) {
    val client = client ?: return
    viewModelScope.launch {
      try {
        val record = client.updateSession(id, fields)
        chatSummary(record)?.let { updated ->
          permissions[id] = updated.permission
          chats = if (updated.archived) chats.filter { it.id != id } else chats.map { if (it.id == id) updated.copy(state = it.state, unread = it.unread) else it }
        }
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        tell(error.message)
      }
    }
  }

  fun tell(text: String?) {
    notice = Notice(text ?: "出错了")
  }

  /** Archives a chat, with a way back. */
  fun archive(chat: ChatSummary) {
    updateChat(chat.id, mapOf("archived" to true))
    notice = Notice("已归档「${chat.title}」", "撤销") {
      val client = client ?: return@Notice
      viewModelScope.launch {
        runCatching { client.updateSession(chat.id, mapOf("archived" to false)) }.onFailure { tell(it.message) }
        refreshChats()
      }
    }
  }

  fun markUnread(id: String) {
    val client = client ?: return
    viewModelScope.launch {
      runCatching { client.markUnread(id) }.onSuccess { chats = chats.map { if (it.id == id) it.copy(unread = true) else it } }.onFailure { tell(it.message) }
    }
  }

  /** Creates a chat and opens it. */
  fun createChat(backend: AgentBackend, cwd: String, folder: String?, onCreated: (String) -> Unit) {
    val client = client ?: return
    viewModelScope.launch {
      try {
        val fields = mutableMapOf<String, Any?>("backend" to backend.raw, "cwd" to cwd.trim())
        if (!folder.isNullOrBlank()) fields["folder"] = folder.trim()
        val chat = chatSummary(client.createSession(fields)) ?: return@launch
        permissions[chat.id] = chat.permission
        chats = listOf(chat.copy(lastActivity = Instant.now())) + chats
        previews[chat.id] = "还没有消息"
        onCreated(chat.id)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        tell(error.message)
      }
    }
  }

  /**
   * Chats still working or waiting for the user, for the watch that runs while
   * the app is away. A message sent but not yet started reads as QUEUED.
   */
  fun busyChats(): Map<String, ActivityState> {
    val busy = chats.filter { it.state == ActivityState.RUNNING || it.state == ActivityState.NEEDS_ACTION }.associate { it.id to it.state }.toMutableMap()
    val id = openChatID
    val open = projection
    if (id != null && open != null) {
      if (open.running || open.pending.isNotEmpty()) busy[id] = stateOf(open) else busy.remove(id)
      if (!open.running && !pendingSends[id].isNullOrEmpty()) busy[id] = ActivityState.QUEUED
    }
    return busy
  }

  /** Where each busy chat's log stands now: anything after this is new. */
  fun busyBaselines(): Map<String, Int> = openChatID?.let { id -> projection?.let { mapOf(id to it.lastSeq) } } ?: emptyMap()

  /** Folders chats already use, most recent first: the choices for a new chat. */
  val knownFolders: List<String> get() = chats.sortedByDescending { it.lastActivity }.map { it.cwd }.distinct()

  // Open chat

  fun openChat(id: String) {
    if (openChatID == id) return
    closeChat()
    val client = client ?: return
    val chat = chats.firstOrNull { it.id == id } ?: return
    openChatID = id
    chatView = ChatView(id, emptyList(), null, false, emptyList(), olderHistory = false, loaded = false)
    streamJob = viewModelScope.launch {
      val self = coroutineContext[Job]
      val page = try {
        client.sessionTail(id, HISTORY_PAGE)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        tell(error.message)
        if (streamJob === self) streamJob = null
        return@launch
      }
      // Hundreds of events: the timeline is built off the main thread.
      val loaded = page["events"].array.map(::trimmed)
      val built = withContext(Dispatchers.Default) {
        SessionProjection(chat.backend, chat.cwd, SessionProjection.homeOf(chat.cwd)).also {
          it.apply(loaded)
          it.setQueue(page["queued_turns"].array)
        }
      }
      if (openChatID != id) return@launch
      page["session"].takeIf { it is JsonObject }?.let { permissions[id] = permissionLabel(it, chat.backend) }
      events.clear()
      events += loaded
      olderHistory = (page["events_omitted_before"].int ?: 0) > 0
      projection = built
      publish()
      if (built.items.any { it is TimelineItem.Consult && it.running }) watchConsults()
      try {
        client.markRead(id)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (_: Exception) {
        // Unread stays on the server; nothing else depends on it.
      }
      if (streamJob === self) streamJob = null
      if (openChatID == id && foreground) startStream()
    }
  }

  /** The open chat's live stream, from the last applied event. */
  private fun startStream() {
    val id = openChatID ?: return
    val client = client ?: return
    val projection = projection ?: return
    streamJob?.cancel()
    streamJob = viewModelScope.launch {
      // The reasoning and text lanes carry the provider's live reasoning summary
      // and the answer being written (neither is durable).
      client.events(
        "api/sessions/$id/events",
        projection.lastSeq,
        mapOf("reasoning_stream" to "true", "reasoning_text" to "true", "text_stream" to "true"),
      ).collect { frame ->
        when (frame) {
          is EventFrame.Event -> {
            // A reconnect replays after the projection's seq; keep each event once.
            if ((frame.json["seq"].int ?: 0) > (events.lastOrNull()?.get("seq").int ?: 0)) events += trimmed(frame.json)
            projection.apply(frame.json)
            liveText.settle(frame.json)
            when (frame.json["type"].string) {
              "turn_finished" -> launch {
                try {
                  client.markRead(id)
                } catch (cancelled: CancellationException) {
                  throw cancelled
                } catch (_: Exception) {
                }
              }
              "assistant_text" -> frame.json["text"].text?.let {
                previews[id] = previewLine(it)
                launch(Dispatchers.Default) { MarkdownCache.warm(it) }
              }
              "tool_started" -> if (SessionProjection.consultCall(frame.json["tool"]) != null) watchConsults()
            }
            schedulePublish()
          }
          is EventFrame.Control ->
            when (frame.json["type"].string) {
              "reasoning_summary_stream" -> {
                liveReasoning = frame.json["items"].array.asReversed().firstNotNullOfOrNull { it["text"].text }?.takeLast(400)
                schedulePublish(LIVE_FRAME_MILLIS)
              }
              "assistant_text_stream" -> {
                liveText.apply(frame.json)
                schedulePublish(LIVE_FRAME_MILLIS)
              }
              "chat_consult_updated" -> watchConsults()
            }
          is EventFrame.Link -> {
            if (reconnecting == frame.up) {
              reconnecting = !frame.up
              schedulePublish()
            }
          }
          is EventFrame.Closed -> {
            if (frame.code == 4401) {
              connection = Connection.Failed((connection as? Connection.Connected)?.address ?: "", "令牌无效或已更换")
            } else {
              tell("这个聊天已不存在")
              closedChat = id
              closeChat()
            }
          }
        }
      }
    }
  }

  /**
   * An agent's question to another chat may wait for the user's approval (deep
   * questions do by default). While one runs, its approvals are polled.
   */
  private fun watchConsults() {
    val id = openChatID ?: return
    val client = client ?: return
    if (consultJob?.isActive == true) return
    consultJob = viewModelScope.launch {
      while (openChatID == id) {
        val waiting = try {
          client.consults(id)["outgoing"].array
            .filter { it["status"].string == "awaiting_approval" }
            .mapNotNull { record ->
              val consultID = record["id"].string ?: return@mapNotNull null
              PendingConsult(consultID, record["target"]["title"].string ?: "聊天", record["question"].string ?: "")
            }
        } catch (cancelled: CancellationException) {
          throw cancelled
        } catch (_: Exception) {
          consultApprovals
        }
        if (waiting != consultApprovals) {
          consultApprovals = waiting
          schedulePublish()
        }
        val asking = projection?.items?.any { it is TimelineItem.Consult && it.running } == true
        if (!asking && waiting.isEmpty()) break
        delay(CONSULT_POLL_MILLIS)
      }
    }
  }

  /** "allow", "always" (this chat from now on) or "deny". */
  fun decideConsult(consultID: String, decision: String) {
    val id = openChatID ?: return
    val client = client ?: return
    consultApprovals = consultApprovals.filter { it.id != consultID }
    publish()
    viewModelScope.launch {
      runCatching { client.decideConsult(id, consultID, decision) }.onFailure {
        tell(it.message)
        watchConsults()
      }
    }
  }

  /** Loads the page of events before the oldest one shown and rebuilds the timeline. */
  fun loadOlder() {
    val id = openChatID ?: return
    val client = client ?: return
    val current = projection ?: return
    val first = events.firstOrNull()?.get("seq").int ?: return
    if (!olderHistory) return
    olderHistory = false
    viewModelScope.launch {
      val page = try {
        client.sessionPage(id, first, HISTORY_PAGE)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        olderHistory = true
        tell(error.message)
        return@launch
      }
      if (openChatID != id || projection !== current) return@launch
      val older = page["events"].array.map(::trimmed)
      val all = older + events
      val queue = current.queued.toList()
      val rebuilt = withContext(Dispatchers.Default) {
        SessionProjection(current.backend, current.cwd, SessionProjection.homeOf(current.cwd)).also {
          it.apply(all)
          // The queue as it stands now, not as old events left it.
          it.queued.clear()
          it.queued.addAll(queue)
        }
      }
      if (openChatID != id || projection !== current) return@launch
      // Events the stream applied while this was rebuilt.
      rebuilt.apply(events.filter { (it["seq"].int ?: 0) > rebuilt.lastSeq })
      events.addAll(0, older)
      olderHistory = (page["events_omitted_before"].int ?: 0) > 0
      projection = rebuilt
      // The stream keeps applying to the projection it holds; restart it on the rebuilt one.
      if (streamJob != null) startStream()
      publish()
    }
  }

  fun closeChat() {
    // Cleared first, so a job finishing as it is cancelled sees the chat closed.
    openChatID = null
    streamJob?.cancel()
    streamJob = null
    publishJob?.cancel()
    publishJob = null
    consultJob?.cancel()
    consultJob = null
    projection = null
    events.clear()
    liveText.clear()
    liveReasoning = null
    reconnecting = false
    consultApprovals = emptyList()
    answering.clear()
    chatView = null
  }

  /** Coalesces a burst of events (a replay, a fast stream) into one rebuild per frame; live text a little less often. */
  private fun schedulePublish(after: Long = FRAME_MILLIS) {
    if (publishJob?.isActive == true) return
    publishJob = viewModelScope.launch {
      delay(after)
      publish()
    }
  }

  private fun publish() {
    val id = openChatID ?: return
    val projection = projection ?: return
    if (!projection.running) {
      liveText.clear()
      liveReasoning = null
    }
    val pending = pendingSends[id]?.apply { removeAll { it.id in projection.acceptedRequests } }?.toList() ?: emptyList()
    val items = projection.items.toList() + liveText.messages(projection.agent)
    val busy = projection.running || projection.pending.isNotEmpty()
    val activity = liveActivity(projection.backend, busy, items, projection.turnStartedAt, pending, liveReasoning, consultApprovals.size)
    chatView = ChatView(
      id = id,
      rows = timelineRows(items, activity, projection.workSeconds, projection.turnSeconds, pending, consultApprovals),
      activity = activity,
      running = busy || pending.isNotEmpty(),
      queued = projection.queued.toList(),
      olderHistory = olderHistory,
      loaded = true,
      reconnecting = reconnecting,
    )
    val index = chats.indexOfFirst { it.id == id }
    if (index >= 0 && (chats[index].state != stateOf(projection) || chats[index].unread)) {
      chats = chats.toMutableList().also { it[index] = it[index].copy(state = stateOf(projection), unread = false) }
    }
  }

  private fun stateOf(projection: SessionProjection) =
    when {
      projection.pending.isNotEmpty() || consultApprovals.isNotEmpty() -> ActivityState.NEEDS_ACTION
      projection.running -> ActivityState.RUNNING
      else -> ActivityState.IDLE
    }

  // Composer

  /** Sends the message; false when it cannot go yet (an image still uploading or failed), and nothing changes. */
  fun send(text: String): Boolean {
    val id = openChatID ?: return false
    val client = client ?: return false
    val message = text.trim()
    val files = attachments[id].orEmpty()
    if (files.any { it.fileID == null && !it.failed }) {
      tell("图片还在上传")
      return false
    }
    if (files.any { it.failed }) {
      tell("有图片没传上去，先移除它再发送")
      return false
    }
    val fileIDs = files.mapNotNull { it.fileID }
    if (message.isEmpty() && fileIDs.isEmpty()) return false
    // Show the message and a "starting" status at once; the server's turn_started
    // (matched by client_request_id) replaces it.
    val requestID = "android-" + UUID.randomUUID()
    val busy = projection?.let { it.running || it.pending.isNotEmpty() } ?: false
    if (!busy) pendingSends.getOrPut(id) { mutableListOf() } += PendingSend(requestID, message)
    drafts.remove(id)
    attachments.remove(id)
    publish()
    viewModelScope.launch {
      try {
        client.sendTurn(id, message, requestID, CAPABILITIES, fileIDs)
        if (message.isNotEmpty()) previews[id] = previewLine(message)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        pendingSends[id]?.removeAll { it.id == requestID }
        drafts[id] = message
        attachments[id] = files
        tell(error.message)
        publish()
      }
    }
    return true
  }

  fun stop() {
    val id = openChatID ?: return
    val client = client ?: return
    viewModelScope.launch { runCatching { client.stop(id) }.onFailure { tell(it.message) } }
  }

  /** Answers an approval, question or form. The card settles at once; a failed answer opens it again. */
  fun reply(interactionID: String, reply: InteractionReply) {
    val id = openChatID ?: return
    val client = client ?: return
    val projection = projection ?: return
    if (!answering.add(interactionID)) return
    projection.noteDecision(reply.outcome, interactionID)
    publish()
    viewModelScope.launch {
      try {
        client.resolveInteraction(id, projection.backend.raw, interactionID, reply.response)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        projection.noteDecision(ApprovalDecision.Pending, interactionID)
        tell(error.message)
        publish()
      } finally {
        answering.remove(interactionID)
      }
    }
  }

  fun removeQueued(queuedID: String) {
    val id = openChatID ?: return
    val client = client ?: return
    viewModelScope.launch { runCatching { client.deleteQueued(id, queuedID) }.onFailure { tell(it.message) } }
  }

  /** Sends the messages held after a Stop. */
  fun resumeQueue() {
    val id = openChatID ?: return
    val client = client ?: return
    viewModelScope.launch { runCatching { client.resumeQueue(id) }.onFailure { tell(it.message) } }
  }

  /** Uploads picked images at once (two at a time), so sending only names their file ids. */
  fun attach(uris: List<Uri>) {
    val id = openChatID ?: return
    val client = client ?: return
    for (uri in uris) {
      val key = UUID.randomUUID().toString()
      attachments[id] = attachments[id].orEmpty() + Attachment(key, uri, "image")
      viewModelScope.launch {
        val result = runCatching {
          uploads.withPermit {
            val (name, type, bytes) = withContext(Dispatchers.IO) { readForUpload(uri) }
            client.uploadFile(id, name, type, bytes)["id"].string ?: error("上传没有返回文件")
          }
        }
        attachments[id] = attachments[id].orEmpty().map {
          if (it.key != key) it else result.fold({ fileID -> it.copy(fileID = fileID) }, { _ -> it.copy(failed = true) })
        }
        result.onFailure { if (it !is CancellationException) tell("上传失败：" + it.message) }
      }
    }
  }

  fun removeAttachment(key: String) {
    val id = openChatID ?: return
    attachments[id] = attachments[id].orEmpty().filter { it.key != key }
  }

  /**
   * A phone photo as the agent should get it: JPEG no larger than 2560 px on its
   * long side, decoded straight from the file at that size. Camera HEIC is not
   * something every provider reads, and full resolution only costs upload time and
   * tokens. GIFs and small PNGs go as they are.
   */
  private fun readForUpload(uri: Uri): Triple<String, String, ByteArray> {
    val resolver = getApplication<Application>().contentResolver
    val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
      if (it.moveToFirst()) it.getString(0) else null
    } ?: "image"
    val type = resolver.getType(uri) ?: "application/octet-stream"
    val size = resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { if (it.moveToFirst()) it.getLong(0) else -1L } ?: -1L
    if (type == "image/gif" || (type == "image/png" && size in 0 until 1_500_000) || !type.startsWith("image/")) {
      return Triple(name, type, resolver.openInputStream(uri)?.use { it.readBytes() } ?: error("无法读取图片"))
    }
    val bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(resolver, uri)) { decoder, info, _ ->
      val long = maxOf(info.size.width, info.size.height)
      if (long > MAX_IMAGE_SIDE) {
        val scale = MAX_IMAGE_SIDE.toFloat() / long
        decoder.setTargetSize((info.size.width * scale).toInt(), (info.size.height * scale).toInt())
      }
      decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
    }
    val out = ByteArrayOutputStream()
    bitmap.compress(Bitmap.CompressFormat.JPEG, 88, out)
    bitmap.recycle()
    return Triple(name.substringBeforeLast('.') + ".jpg", "image/jpeg", out.toByteArray())
  }

  /** Changes the open chat's model, effort or permission preset. */
  fun updateOpenChat(fields: Map<String, Any?>) {
    openChatID?.let { updateChat(it, fields) }
  }

  fun fileUrl(fileID: String): String? {
    val id = openChatID ?: return null
    return client?.fileUrl(id, fileID)
  }

  companion object {
    /** This client answers approvals, questions and forms, so turns may ask for them. */
    val CAPABILITIES = listOf("claude_sdk_interactive_v1", "codex_interactive_v1")
    private const val POLL_MILLIS = 4_000L
    private const val OPEN_CHAT_POLL_MILLIS = 12_000L
    private const val FULL_LIST_EVERY = 15
    private const val CONSULT_POLL_MILLIS = 3_000L
    private const val FRAME_MILLIS = 32L
    private const val LIVE_FRAME_MILLIS = 80L
    private const val HISTORY_PAGE = 400
    private const val MAX_IMAGE_SIDE = 2560
    /** What a kept event's tool input and output are cut to (the timeline shows less). */
    private const val KEPT_TEXT = 4_000

    /** One plain line for the list: Markdown links, code marks, emphasis and math delimiters removed. */
    fun previewLine(text: String): String {
      var plain = text.replace(Regex("""\[([^\]]*)\]\([^)]*\)"""), "$1")
      for (mark in listOf("**", "__", "`", "$$", "$", "#")) plain = plain.replace(mark, "")
      return plain.replace(Regex("""\s+"""), " ").trim().take(120)
    }

    /**
     * An event as kept for paging: a tool's long input values (a whole file being
     * written) and output cut to what the timeline can show.
     */
    fun trimmed(event: JsonElement): JsonElement {
      val type = event["type"].string
      if (type != "tool_started" && type != "tool_finished") return event
      val record = event as? JsonObject ?: return event
      fun cut(value: JsonElement?): JsonElement? =
        (value as? JsonPrimitive)?.takeIf { it.isString && it.content.length > KEPT_TEXT }?.let { JsonPrimitive(it.content.take(KEPT_TEXT)) } ?: value
      val tool = record["tool"] as? JsonObject
      val input = tool?.get("input") as? JsonObject
      val keptTool = tool?.let { JsonObject(it + ("input" to (input?.let { JsonObject(it.mapValues { (_, v) -> cut(v) ?: v }) } ?: it["input"] ?: JsonObject(emptyMap())))) }
      return JsonObject(record + listOfNotNull(keptTool?.let { "tool" to it }, record["output"]?.let { "output" to (cut(it) ?: it) }))
    }
  }
}
