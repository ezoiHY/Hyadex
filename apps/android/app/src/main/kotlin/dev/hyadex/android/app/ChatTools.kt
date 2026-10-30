package dev.hyadex.android.app

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import dev.hyadex.android.api.HyadexClient
import dev.hyadex.android.api.HyadexError
import dev.hyadex.android.api.array
import dev.hyadex.android.api.get
import dev.hyadex.android.api.string
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.ChatGoal
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.CommandInventory
import dev.hyadex.android.core.ConsultTarget
import dev.hyadex.android.core.ContextUsage
import dev.hyadex.android.core.GoalSupport
import dev.hyadex.android.core.McpServer
import dev.hyadex.android.core.McpStatus
import dev.hyadex.android.core.SideChat
import dev.hyadex.android.core.Subagent
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement

/**
 * What the open chat's agent offers beside its timeline, as the Mac app's
 * toolbar and composer drive it: its commands and skills, the chats it may ask,
 * how full its context is, its native goal, its MCP servers and subagents, and
 * the side conversation. Everything is read for one chat at a time and dropped
 * when another opens.
 */
class ChatTools(private val scope: CoroutineScope, private val tell: (String?) -> Unit) {
  private var client: HyadexClient? = null
  var chatID: String? = null
    private set
  var backend: AgentBackend = AgentBackend.CLAUDE
    private set

  var commands by mutableStateOf<CommandInventory?>(null)
    private set
  var targets by mutableStateOf<List<ConsultTarget>?>(null)
    private set
  var context by mutableStateOf<ContextUsage?>(null)
    private set
  var goal by mutableStateOf<ChatGoal?>(null)
    private set
  var goalSupport by mutableStateOf<GoalSupport?>(null)
    private set
  /** A goal change is on its way (Claude runs `/goal` as a turn). */
  var goalBusy by mutableStateOf(false)
    private set
  var mcp by mutableStateOf<McpStatus?>(null)
    private set
  var mcpBusy by mutableStateOf(false)
    private set
  var subagents by mutableStateOf<List<Subagent>>(emptyList())
    private set
  var side by mutableStateOf<SideChat?>(null)
    private set
  var sideError by mutableStateOf<String?>(null)
  /** The goal sheet and the side panel are open (the composer's `/goal` and `/btw` open them too). */
  var goalSheetOpen by mutableStateOf(false)
  var sideOpen by mutableStateOf(false)

  fun open(client: HyadexClient, chat: ChatSummary) {
    close()
    this.client = client
    chatID = chat.id
    backend = chat.backend
    refreshRuntime()
  }

  fun close() {
    client = null
    chatID = null
    commands = null
    targets = null
    context = null
    goal = null
    goalSupport = null
    goalBusy = false
    mcp = null
    mcpBusy = false
    subagents = emptyList()
    side = null
    sideError = null
    goalSheetOpen = false
    sideOpen = false
  }

  /** Runs `block` for the open chat. Its results are applied only while that chat is still open (`current`). */
  private fun run(block: suspend (client: HyadexClient, id: String, current: () -> Boolean) -> Unit): Job? {
    val client = client ?: return null
    val id = chatID ?: return null
    val current = { chatID == id }
    return scope.launch {
      try {
        block(client, id, current)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        if (current()) tell(error.message)
      }
    }
  }

  // Context and goal

  /** Rereads the provider runtime: context fill and native goal. */
  fun refreshRuntime() {
    run { client, id, current ->
      val runtime = client.runtime(id, backend.raw)
      if (current()) applyRuntime(runtime)
    }
  }

  private fun applyRuntime(runtime: JsonElement) {
    applyContext(runtime)
    goal = if (backend == AgentBackend.CLAUDE) ChatGoal.claude(runtime) else ChatGoal.codex(runtime)
    goalSupport = if (backend == AgentBackend.CLAUDE) GoalSupport.claude(runtime) else GoalSupport.codex(runtime)
  }

  private fun applyContext(json: JsonElement?) {
    when (val result = ContextUsage.from(json)) {
      is ContextUsage.Result.Known -> if (context != result.usage) context = result.usage
      ContextUsage.Result.None -> context = null
      ContextUsage.Result.Unchanged -> Unit
    }
  }

  /** A control frame from the open chat's stream. */
  fun onControl(frame: JsonElement) {
    when (frame["type"].string) {
      "provider_runtime_changed" -> if (frame["runtime"].string == "context_usage") applyContext(frame)
      "side_chat_updated" -> refreshSide()
    }
  }

  /** An event from the open chat's log that changes what the runtime reports. */
  fun onEvent(type: String?) {
    when (type) {
      "claude_goal_changed", "codex_goal_updated", "codex_goal_cleared", "turn_finished",
      "claude_compaction_completed", "codex_compaction_completed" -> refreshRuntime()
    }
  }

  /** Sets the goal: Claude's completion condition, or Codex's objective with optional budgets. */
  fun setGoal(text: String, tokenBudget: Int? = null, timeLimitMinutes: Int? = null) {
    goalBusy = true
    run { client, id, current ->
      try {
        if (backend == AgentBackend.CLAUDE) {
          // Claude Code runs `/goal <condition>` as a turn and keeps going until it holds.
          val runtime = client.setClaudeGoal(id, text)
          if (current()) applyRuntime(runtime)
        } else {
          val result = client.setCodexGoal(id, mapOf("objective" to text, "status" to "active", "token_budget" to tokenBudget, "time_budget_seconds" to timeLimitMinutes?.let { it * 60 }))
          if (current()) goal = ChatGoal.codex(result)
        }
      } finally {
        if (current()) goalBusy = false
      }
      if (current()) refreshRuntime()
    }
  }

  /** Pauses or resumes Codex's goal. */
  fun setGoalPaused(paused: Boolean) {
    goalBusy = true
    run { client, id, current ->
      try {
        val result = client.setCodexGoal(id, mapOf("status" to if (paused) "paused" else "active"))
        if (current()) goal = ChatGoal.codex(result)
      } finally {
        if (current()) goalBusy = false
      }
    }
  }

  fun clearGoal() {
    goalBusy = true
    run { client, id, current ->
      try {
        if (backend == AgentBackend.CLAUDE) {
          val runtime = client.clearClaudeGoal(id)
          if (current()) applyRuntime(runtime)
        } else {
          val result = client.clearCodexGoal(id)
          if (current()) goal = ChatGoal.codex(result)
        }
      } finally {
        if (current()) goalBusy = false
      }
    }
  }

  /** Codex compacts natively; Claude runs `/compact` as a turn (the composer sends it). */
  fun compactCodex() {
    run { client, id, _ -> client.compactCodex(id) }
  }

  // Commands, skills and chats to ask (loaded the first time the composer needs them)

  fun loadCommands(refresh: Boolean = false) {
    if (commands != null && !refresh) return
    run { client, id, current ->
      val inventory = CommandInventory.parse(client.providerCommands(id, refresh))
      if (current()) commands = inventory
    }
  }

  /** Reads the commands again now (a send named a command the server no longer knows by that revision). */
  suspend fun reloadCommands(client: HyadexClient, id: String): CommandInventory? {
    val inventory = CommandInventory.parse(client.providerCommands(id, refresh = true))
    if (chatID == id) commands = inventory
    return inventory
  }

  fun loadTargets() {
    if (targets != null) return
    run { client, id, current ->
      // Older servers have none: the composer just offers no chats.
      val json = runCatching { client.consultTargets(id) }.getOrNull() ?: return@run
      if (current()) targets = json["chats"].array.mapNotNull(ConsultTarget::parse)
    }
  }

  /** The user asks another chat; the answer shows as a card in this chat and is not sent to its agent. */
  fun ask(target: ConsultTarget, question: String, deep: Boolean, onFailed: () -> Unit) {
    run { client, id, current ->
      try {
        client.askFromChat(id, target.id, question, deep && target.allowsDeep)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        if (current()) {
          tell(error.message)
          onFailed()
        }
      }
    }
  }

  fun cancelConsult(consultID: String) {
    run { client, id, _ -> client.cancelConsult(id, consultID) }
  }

  // MCP servers and subagents (read while the details sheet is open)

  fun refreshMcp() {
    mcpBusy = true
    run { client, id, current ->
      try {
        val json = client.mcp(id, backend.raw)
        if (current()) mcp = McpStatus.parse(json, backend) ?: McpStatus(false, null, emptyList(), false, "服务器返回了无法识别的 MCP 状态。", false)
      } finally {
        if (current()) mcpBusy = false
      }
    }
  }

  /** Claude only: "reconnect", "reconnect_all", "enable" or "disable". */
  fun controlMcp(action: String, server: McpServer?) {
    val generation = mcp?.generation ?: return
    mcpBusy = true
    run { client, id, current ->
      try {
        val json = client.controlClaudeMcp(id, action, server?.name, generation)
        if (current()) McpStatus.parse(json["status"] ?: json, backend)?.let { mcp = it }
      } finally {
        if (current()) mcpBusy = false
      }
      if (current()) refreshMcp()
    }
  }

  fun refreshSubagents() {
    run { client, id, current ->
      val agents = Subagent.parse(client.subagents(id))
      if (current() && agents != subagents) subagents = agents
    }
  }

  // Side questions: Claude Code's /btw, or a Codex fork; the main conversation is untouched.

  fun refreshSide() {
    run { client, id, current ->
      val json = runCatching { client.sideChat(id) }.getOrElse {
        if (current()) sideError = sideMessage(it)
        return@run
      }
      if (current()) SideChat.parse(json)?.let { side = it }
    }
  }

  fun askSide(question: String) {
    sideError = null
    run { client, id, current ->
      val known = side ?: SideChat.parse(client.sideChat(id)) ?: return@run
      if (!current()) return@run
      try {
        val json = client.askSideChat(id, "android" + UUID.randomUUID().toString().replace("-", ""), question, known.sideChatID, known.lastRequestID)
        if (current()) SideChat.parse(json)?.let { side = it }
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        if (!current()) return@run
        // Another device asked meanwhile: catch up and let the user ask again.
        sideError = if (error is HyadexError.Http && error.code == 409 && error.detail?.contains("refresh") == true) "侧问刚被更新，请再发一次。" else sideMessage(error)
      }
      if (current()) refreshSide()
    }
  }

  fun cancelSide(requestID: String) {
    run { client, id, current ->
      try {
        val json = client.cancelSideQuestion(id, requestID)
        if (current()) SideChat.parse(json)?.let { side = it }
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        if (current()) sideError = sideMessage(error)
      }
    }
  }

  fun clearSide() {
    val sideChatID = side?.sideChatID ?: return
    run { client, id, current ->
      try {
        val json = client.clearSideChat(id, sideChatID)
        if (current()) SideChat.parse(json)?.let { side = it }
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (error: Exception) {
        if (current()) sideError = sideMessage(error)
      }
    }
  }

  private fun sideMessage(error: Throwable): String =
    when {
      error is HyadexError.Http && error.code == 409 && error.detail?.contains("not started") == true -> "先和代理说一句话，侧问才有对话内容可以参考。"
      error is HyadexError.Http && error.code == 410 -> "主对话已经变了：点“清空”开始新的侧问。"
      else -> error.message ?: "侧问出错了"
    }
}
