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
          chain.proceed(request.newBuilder().header("Authorization", "Bearer $token").build())
        } else {
          chain.proceed(request)
        }
      }
      .build()

  fun url(path: String, query: Map<String, String> = emptyMap()): HttpUrl =
    baseUrl.newBuilder().addPathSegments(path).apply { query.toSortedMap().forEach { (key, value) -> addQueryParameter(key, value) } }.build()

  suspend fun request(method: String, path: String, query: Map<String, String> = emptyMap(), body: JsonElement? = null): JsonElement {
    val payload = body?.toString()?.toRequestBody(JSON_TYPE) ?: if (method == "POST") "{}".toRequestBody(JSON_TYPE) else null
    val request = Request.Builder().url(url(path, query)).method(method, payload).build()
    val response = try {
      http.newCall(request).await()
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

  suspend fun sendTurn(id: String, prompt: String, requestID: String, capabilities: List<String>, fileIDs: List<String> = emptyList()): JsonElement {
    val body = mutableMapOf<String, Any?>("prompt" to prompt, "client_request_id" to requestID, "client_capabilities" to capabilities)
    if (fileIDs.isNotEmpty()) body["file_ids"] = fileIDs
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
