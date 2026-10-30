package dev.hyadex.android.api

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

sealed class HyadexError(message: String) : Exception(message) {
  class Unauthorized : HyadexError("令牌无效或缺失")
  class Http(val code: Int, val detail: String?) : HyadexError(detail?.let { "服务器返回 $code：$it" } ?: "服务器返回 HTTP $code")
  class Unreachable(reason: String) : HyadexError("无法连接：$reason")
  class InvalidAddress : HyadexError("服务器地址无效")
}

/** REST client for a Hyadex server. The token travels only in the Authorization header. */
class HyadexClient(val baseUrl: HttpUrl, private val token: String) {
  val http: OkHttpClient =
    OkHttpClient.Builder()
      .connectTimeout(10, TimeUnit.SECONDS)
      .readTimeout(30, TimeUnit.SECONDS)
      // The server answers within its own 30-second deadline; live events come over WebSockets.
      .pingInterval(20, TimeUnit.SECONDS)
      .addInterceptor { chain ->
        val request = chain.request()
        // Only this server gets the token (images and sockets share the client).
        if (token.isNotEmpty() && request.url.host == baseUrl.host && request.url.port == baseUrl.port) {
          // Native controls (plan usage, side questions) take it in their own header, and only there.
          val native = request.tag(NativeToken::class.java) != null
          chain.proceed(request.newBuilder().header(if (native) "X-Hyadex-Token" else "Authorization", if (native) token else "Bearer $token").build())
        } else {
          chain.proceed(request)
        }
      }
      .build()

  /** Marks a request for a native control. */
  private object NativeToken

  fun url(path: String, query: Map<String, String> = emptyMap()): HttpUrl =
    baseUrl.newBuilder().addPathSegments(path).apply { query.toSortedMap().forEach { (key, value) -> addQueryParameter(key, value) } }.build()

  /** For the few requests the server may take minutes over (a fork copies the provider's session). */
  private val slowHttp: OkHttpClient by lazy { http.newBuilder().readTimeout(3, TimeUnit.MINUTES).build() }

  suspend fun request(
    method: String,
    path: String,
    query: Map<String, String> = emptyMap(),
    body: JsonElement? = null,
    native: Boolean = false,
    slow: Boolean = false,
  ): JsonElement {
    val payload = body?.toString()?.toRequestBody(JSON_TYPE) ?: if (method == "POST") "{}".toRequestBody(JSON_TYPE) else null
    val request = Request.Builder().url(url(path, query)).method(method, payload).apply { if (native) tag(NativeToken::class.java, NativeToken) }.build()
    val response = try {
      (if (slow) slowHttp else http).newCall(request).await()
    } catch (error: IOException) {
      throw HyadexError.Unreachable(error.message ?: error.javaClass.simpleName)
    }
    return withContext(Dispatchers.IO) {
      response.use {
        val text = it.body.string()
        val json = runCatching { JsonCodec.parseToJsonElement(text) }.getOrDefault(JsonNull)
        if (it.code == 401 || it.code == 403) throw HyadexError.Unauthorized()
        if (it.code !in 200..299) throw HyadexError.Http(it.code, json["detail"].string ?: json["detail"]["message"].string)
        json
      }
    }
  }

  suspend fun get(path: String, query: Map<String, String> = emptyMap()) = request("GET", path, query)

  suspend fun post(path: String, body: JsonElement = JsonObject(emptyMap())) = request("POST", path, body = body)

  suspend fun health(): JsonElement = get("api/health")

  /** The chat list; `summary` leaves out prompts, goals and permission settings (about half the size). */
  suspend fun sessions(summary: Boolean = false): List<JsonElement> =
    get("api/sessions", if (summary) mapOf("summary" to "true") else emptyMap())["sessions"].array

  /** The most recent events of a chat, its record and queued turns. */
  suspend fun sessionTail(id: String, limit: Int = 400): JsonElement = get("api/sessions/$id", mapOf("limit" to limit.toString()))

  /** Events before `before` (exclusive seq), newest last. */
  suspend fun sessionPage(id: String, before: Int, limit: Int = 400): JsonElement =
    get("api/sessions/$id", mapOf("before" to before.toString(), "limit" to limit.toString()))

  /** Sends a message; `skill` is the opaque id and inventory revision of the command or skill it starts with. */
  suspend fun sendTurn(
    id: String,
    prompt: String,
    requestID: String,
    capabilities: List<String>,
    fileIDs: List<String> = emptyList(),
    skill: Pair<String, String>? = null,
  ): JsonElement {
    val body = mutableMapOf<String, Any?>("prompt" to prompt, "client_request_id" to requestID, "client_capabilities" to capabilities)
    if (fileIDs.isNotEmpty()) body["file_ids"] = fileIDs
    if (skill != null) body["skill_selection"] = mapOf("id" to skill.first, "revision" to skill.second)
    return post("api/sessions/$id/turns", toJson(body))
  }

  suspend fun stop(id: String) {
    post("api/sessions/$id/stop")
  }

  suspend fun markRead(id: String) {
    post("api/sessions/$id/read")
  }

  /** Answers a pending approval, question or form; `backend` is "claude" or "codex". */
  suspend fun resolveInteraction(id: String, backend: String, interactionID: String, response: JsonElement) {
    post("api/sessions/$id/$backend/interactions/$interactionID/resolve", jsonOf("response" to response))
  }

  /** Sends the messages held after a Stop. */
  suspend fun resumeQueue(id: String) {
    post("api/sessions/$id/queue/resume")
  }

  /** What this chat asked other chats (`outgoing`) and was asked (`incoming`). */
  suspend fun consults(id: String): JsonElement = get("api/sessions/$id/consults")

  /** Answers a deep question waiting for approval: "allow", "always" or "deny". */
  suspend fun decideConsult(id: String, consultID: String, decision: String) {
    post("api/sessions/$id/consults/$consultID/decision", jsonOf("decision" to decision))
  }

  suspend fun deleteQueued(id: String, queuedID: String) {
    request("DELETE", "api/sessions/$id/queue/$queuedID")
  }

  /** Changes a chat's settings (title, pinned, archived, model, effort, permissions…); returns its record. */
  suspend fun updateSession(id: String, fields: Map<String, Any?>): JsonElement = request("PATCH", "api/sessions/$id", body = toJson(fields))["session"] ?: JsonNull

  suspend fun createSession(fields: Map<String, Any?>): JsonElement = post("api/sessions", toJson(fields))["session"] ?: JsonNull

  suspend fun markUnread(id: String) {
    post("api/sessions/$id/unread")
  }

  /** Acknowledges a chat's emergency alerts up to and including `alertID`. */
  suspend fun acknowledgeEmergency(id: String, alertID: String) {
    post("api/sessions/$id/emergency/acknowledge", jsonOf("through_alert_id" to alertID))
  }

  /** A copy of the chat that remembers its conversation so far; returns the copy's record. */
  suspend fun fork(id: String): JsonElement = request("POST", "api/sessions/$id/fork", slow = true)["session"] ?: JsonNull

  /** Deletes a chat and its history on the server. */
  suspend fun deleteSession(id: String) {
    request("DELETE", "api/sessions/$id")
  }

  /** Chats whose history matches `query` (at least two characters), newest match first, one per chat. */
  suspend fun search(query: String, limit: Int = 40): JsonElement = get("api/search", mapOf("q" to query, "limit" to limit.toString()))

  /** A provider's plan usage ("claude" or "codex"); `refresh` asks the provider again instead of the server's last reading. */
  suspend fun usage(backend: String, refresh: Boolean = false): JsonElement =
    request("GET", "api/runtime/usage", mapOf("backend" to backend) + (if (refresh) mapOf("refresh" to "true") else emptyMap()), native = true)

  /** Native models and reasoning efforts per backend (probes the CLIs; may take seconds). */
  suspend fun runtimeCatalog(): JsonElement = get("api/runtime/catalog")

  /** Uploads one file to the chat; returns its record (`id`, `filename`, `content_type`). */
  suspend fun uploadFile(id: String, name: String, contentType: String, bytes: ByteArray): JsonElement {
    val body = MultipartBody.Builder()
      .setType(MultipartBody.FORM)
      .addFormDataPart("file", name.replace("\"", "_"), bytes.toRequestBody(contentType.toMediaTypeOrNull()))
      .build()
    val request = Request.Builder().url(url("api/sessions/$id/files")).post(body).build()
    val response = try {
      http.newBuilder().writeTimeout(10, TimeUnit.MINUTES).build().newCall(request).await()
    } catch (error: IOException) {
      throw HyadexError.Unreachable(error.message ?: error.javaClass.simpleName)
    }
    return withContext(Dispatchers.IO) {
      response.use {
        val json = runCatching { JsonCodec.parseToJsonElement(it.body.string()) }.getOrDefault(JsonNull)
        if (it.code == 401 || it.code == 403) throw HyadexError.Unauthorized()
        if (it.code !in 200..299) throw HyadexError.Http(it.code, json["detail"].string)
        json["file"] ?: JsonNull
      }
    }
  }

  // The agent's own tools, as the Mac app drives them.

  /** Context fill and the native goal (`backend` is "claude" or "codex"). */
  suspend fun runtime(id: String, backend: String): JsonElement = get("api/sessions/$id/$backend/runtime")

  /** Commands and skills the chat's agent offers in its folder. */
  suspend fun providerCommands(id: String, refresh: Boolean = false): JsonElement =
    request("GET", "api/sessions/$id/provider-commands", if (refresh) mapOf("refresh" to "true") else emptyMap(), slow = true)

  /** Sets Claude Code's `/goal` completion condition; returns the Claude runtime. */
  suspend fun setClaudeGoal(id: String, condition: String): JsonElement = request("PUT", "api/sessions/$id/claude/goal", body = jsonOf("condition" to condition), slow = true)

  suspend fun clearClaudeGoal(id: String): JsonElement = request("DELETE", "api/sessions/$id/claude/goal", slow = true)

  /** Creates or changes Codex's persistent goal; only the given fields change (null removes a budget). */
  suspend fun setCodexGoal(id: String, fields: Map<String, Any?>): JsonElement = request("PUT", "api/sessions/$id/codex/goal", body = toJson(fields), slow = true)

  suspend fun clearCodexGoal(id: String): JsonElement = request("DELETE", "api/sessions/$id/codex/goal", slow = true)

  /** Starts Codex's native context compaction (idle chats only). */
  suspend fun compactCodex(id: String): JsonElement = post("api/sessions/$id/codex/compact")

  /** The MCP servers the chat's agent loaded. */
  suspend fun mcp(id: String, backend: String): JsonElement = request("GET", "api/sessions/$id/$backend/mcp", slow = true)

  /** `action` is reconnect, reconnect_all, enable or disable (Claude only). */
  suspend fun controlClaudeMcp(id: String, action: String, server: String?, generation: String): JsonElement {
    val body = mutableMapOf<String, Any?>("version" to 1, "action" to action, "expected_generation" to generation)
    if (server != null) body["server_name"] = server
    return request("POST", "api/sessions/$id/claude/mcp", body = toJson(body), slow = true)
  }

  suspend fun subagents(id: String): JsonElement = get("api/sessions/$id/subagents")

  /** The chats this chat may ask (same project, accepting questions), most recent first. */
  suspend fun consultTargets(id: String): JsonElement = get("api/sessions/$id/consult-targets")

  /** The user asks `chat` a question from this chat (@ in its composer); the answer stays in this chat's view. */
  suspend fun askFromChat(id: String, chat: String, question: String, deep: Boolean): JsonElement =
    post("api/sessions/$id/ask", jsonOf("chat" to chat, "mode" to if (deep) "deep" else "basic", "question" to question))

  suspend fun cancelConsult(id: String, consultID: String): JsonElement = post("api/sessions/$id/consults/$consultID/cancel")

  /** Force Send: steers the running turn natively, or stops it and runs this message next. */
  suspend fun runQueuedNow(id: String, queuedID: String): JsonElement =
    request("POST", "api/sessions/$id/queue/$queuedID/run-now", body = jsonOf("accept_deferred_queue_response" to true), slow = true)

  /** Moves a queued message one place "up" or "down". */
  suspend fun moveQueued(id: String, queuedID: String, direction: String, expectedAdjacent: String?) {
    val body = mutableMapOf<String, Any?>("direction" to direction)
    if (expectedAdjacent != null) body["expected_adjacent_queued_id"] = expectedAdjacent
    post("api/sessions/$id/queue/$queuedID/move", toJson(body))
  }

  /** The chat's side conversation (Claude Code's /btw, a Codex fork). */
  suspend fun sideChat(id: String): JsonElement = request("GET", "api/sessions/$id/side-chat", native = true)

  suspend fun askSideChat(id: String, requestID: String, question: String, sideChatID: String, after: String?): JsonElement =
    request(
      "POST", "api/sessions/$id/side-chat",
      body = jsonOf("request_id" to requestID, "question" to question, "side_chat_id" to sideChatID, "after_request_id" to after),
      native = true, slow = true,
    )

  suspend fun cancelSideQuestion(id: String, requestID: String): JsonElement = request("DELETE", "api/sessions/$id/side-chat/requests/$requestID", native = true)

  suspend fun clearSideChat(id: String, sideChatID: String): JsonElement = request("DELETE", "api/sessions/$id/side-chat/$sideChatID", native = true)

  /** A run's changes as one unified diff (plain text). */
  suspend fun diff(id: String, runID: String): String {
    val request = Request.Builder().url(url("api/sessions/$id/diffs/$runID")).build()
    val response = try {
      http.newCall(request).await()
    } catch (error: IOException) {
      throw HyadexError.Unreachable(error.message ?: error.javaClass.simpleName)
    }
    return withContext(Dispatchers.IO) {
      response.use {
        if (it.code == 401 || it.code == 403) throw HyadexError.Unauthorized()
        if (it.code !in 200..299) throw HyadexError.Http(it.code, null)
        it.body.string()
      }
    }
  }

  /** Where a file the chat uploaded or published is served (loaded with the token by the shared client). */
  fun fileUrl(id: String, fileID: String): String = url("api/sessions/$id/files/$fileID").toString()

  companion object {
    private val JSON_TYPE = "application/json".toMediaType()

    /** Accepts "host:port", "http://host:port" or "https://host". */
    fun parseAddress(address: String): HttpUrl? {
      val trimmed = address.trim().trimEnd('/')
      if (trimmed.isEmpty()) return null
      val withScheme = if ("://" in trimmed) trimmed else "http://$trimmed"
      return withScheme.toHttpUrlOrNull()?.takeIf { it.scheme == "http" || it.scheme == "https" }
    }
  }
}

suspend fun Call.await(): Response =
  suspendCancellableCoroutine { continuation ->
    enqueue(
      object : Callback {
        override fun onResponse(call: Call, response: Response) = continuation.resume(response)

        override fun onFailure(call: Call, e: IOException) {
          if (!continuation.isCancelled) continuation.resumeWithException(e)
        }
      }
    )
    continuation.invokeOnCancellation { runCatching { cancel() } }
  }
