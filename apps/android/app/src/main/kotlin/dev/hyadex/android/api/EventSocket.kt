package dev.hyadex.android.api

import kotlin.random.Random
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.channelFlow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.isActive
import kotlinx.serialization.json.JsonElement
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/** One frame of an event stream. */
sealed interface EventFrame {
  /** A durable event (it has a `seq`). */
  data class Event(val json: JsonElement) : EventFrame

  /** A control frame: live text and reasoning lanes, consult updates… */
  data class Control(val json: JsonElement) : EventFrame

  /** The stream is connected, or lost and reconnecting. */
  data class Link(val up: Boolean) : EventFrame

  /** The server refused the stream for good: 4401 (token), 4404 (chat gone). The flow ends. */
  data class Closed(val code: Int) : EventFrame
}

private object Opened

/**
 * A chat's event stream. Replays everything after the last applied `seq`, then
 * delivers live events; on disconnect it reconnects with backoff and resumes
 * after the last `seq` it delivered, so events are never repeated or skipped.
 */
fun HyadexClient.events(path: String, after: Int, query: Map<String, String> = emptyMap()): Flow<EventFrame> = channelFlow {
  var lastSeq = after
  var delaySeconds = 0.5
  while (isActive) {
    val url = url(path, query + ("after" to lastSeq.toString()))
    val request = Request.Builder().url(url).header("Sec-WebSocket-Protocol", PROTOCOL).build()
    val signals = Channel<Any>(Channel.UNLIMITED)
    val socket = http.newWebSocket(
      request,
      object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
          signals.trySend(Opened)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
          signals.trySend(text)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
          signals.trySend(code)
          webSocket.close(1000, null)
          signals.close()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
          signals.close()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
          signals.close()
        }
      },
    )
    var openedAt = 0L
    var closeCode = 0
    try {
      for (signal in signals) {
        when (signal) {
          Opened -> {
            openedAt = System.currentTimeMillis()
            send(EventFrame.Link(up = true))
          }
          is Int -> closeCode = signal
          is String -> {
            val json = runCatching { JsonCodec.parseToJsonElement(signal) }.getOrNull() ?: continue
            val seq = json["seq"].int
            if (seq == null) {
              send(EventFrame.Control(json))
            } else if (seq > lastSeq) {
              lastSeq = seq
              send(EventFrame.Event(json))
            }
          }
        }
      }
    } finally {
      socket.cancel()
    }
    if (closeCode == 4401 || closeCode == 4404) {
      send(EventFrame.Closed(closeCode))
      return@channelFlow
    }
    send(EventFrame.Link(up = false))
    // Only a connection that held for a while resets the backoff: a server that
    // accepts and drops at once is not retried in a tight loop.
    val held = openedAt != 0L && System.currentTimeMillis() - openedAt > STABLE_MILLIS
    delaySeconds = if (held) 0.5 else (delaySeconds * 2).coerceAtMost(30.0)
    delay((Random.nextDouble(delaySeconds / 2, delaySeconds) * 1000).toLong())
  }
}.flowOn(Dispatchers.IO) // Frames (live snapshots can be large) are parsed off the main thread.

private const val PROTOCOL = "hyadex-events-v1"
private const val STABLE_MILLIS = 10_000L
