package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.double
import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.isNull
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Instant
import kotlinx.serialization.json.JsonElement

// Goals, slash commands, MCP servers, context and subagents come from each
// provider's own runtime (ported from the Mac app's Capabilities and Extras).
// The app only shows and drives them.

/** A chat's native goal: Claude Code's `/goal` completion condition or Codex's persistent thread objective. */
data class ChatGoal(
  val backend: AgentBackend,
  val text: String,
  val status: Status,
  /** Claude: how many times the goal check ran. */
  val iterations: Int? = null,
  /** Claude: the evaluator's last explanation. */
  val reason: String? = null,
  val tokensUsed: Int? = null,
  val tokenBudget: Int? = null,
  val secondsUsed: Int? = null,
  /** Codex: the server-enforced wall-clock limit. */
  val timeBudgetSeconds: Int? = null,
  val timeBudgetExhausted: Boolean = false,
) {
  enum class Status(val raw: String) {
    ACHIEVED("achieved"), CLEARED("cleared"), FAILED("failed"),
    PAUSED("paused"), BLOCKED("blocked"), USAGE_LIMITED("usageLimited"), BUDGET_LIMITED("budgetLimited"), COMPLETE("complete"),
    ACTIVE("active"),
    /** Accepted by the server; the provider has not confirmed it yet. */
    STARTING("starting");

    companion object {
      fun of(raw: String?): Status = entries.firstOrNull { it.raw == raw } ?: ACTIVE
    }
  }

  /** The goal still drives the agent (or can be resumed). */
  val isOpen: Boolean get() = status in setOf(Status.ACTIVE, Status.STARTING, Status.PAUSED, Status.BLOCKED, Status.USAGE_LIMITED, Status.BUDGET_LIMITED)
  val canPause: Boolean get() = backend == AgentBackend.CODEX && status == Status.ACTIVE
  val canResume: Boolean
    get() = backend == AgentBackend.CODEX && status in setOf(Status.PAUSED, Status.BLOCKED, Status.USAGE_LIMITED, Status.BUDGET_LIMITED) && !timeBudgetExhausted

  val statusLabel: String
    get() = when (status) {
      Status.ACTIVE -> "进行中"
      Status.STARTING -> "正在设定"
      Status.ACHIEVED, Status.COMPLETE -> "已达成"
      Status.CLEARED -> "已清除"
      Status.FAILED -> "未达成"
      Status.PAUSED -> if (timeBudgetExhausted) "时间用完" else "已暂停"
      Status.BLOCKED -> "受阻"
      Status.USAGE_LIMITED -> "额度受限"
      Status.BUDGET_LIMITED -> "token 用完"
    }

  val activity: ActivityState
    get() = when (status) {
      Status.ACTIVE, Status.STARTING -> ActivityState.RUNNING
      Status.PAUSED -> ActivityState.PAUSED
      Status.BLOCKED, Status.USAGE_LIMITED, Status.BUDGET_LIMITED -> ActivityState.NEEDS_ACTION
      Status.ACHIEVED, Status.COMPLETE -> ActivityState.DONE
      Status.CLEARED -> ActivityState.IDLE
      Status.FAILED -> ActivityState.FAILED
    }

  companion object {
    /** Claude's goal from `GET …/claude/runtime` (`goal`, `goal_starting`). */
    fun claude(runtime: JsonElement?): ChatGoal? {
      val goal = runtime["goal"]
      val condition = goal["condition"].text ?: return null
      return ChatGoal(
        AgentBackend.CLAUDE, condition,
        if (runtime["goal_starting"].bool == true) Status.STARTING else Status.of(goal["status"].string),
        iterations = goal["iterations"].int, reason = goal["last_reason"].text,
        tokensUsed = goal["tokens"].int, secondsUsed = goal["duration_ms"].int?.let { it / 1000 },
      )
    }

    /** Codex's goal from `GET …/codex/goal` or `…/codex/runtime`. */
    fun codex(json: JsonElement?): ChatGoal? {
      val goal = json["goal"]
      val objective = goal["objective"].text ?: return null
      return ChatGoal(
        AgentBackend.CODEX, objective, Status.of(goal["status"].string),
        tokensUsed = goal["tokensUsed"].int, tokenBudget = goal["tokenBudget"].int, secondsUsed = goal["timeUsedSeconds"].int,
        timeBudgetSeconds = json["time_budget_seconds"].int, timeBudgetExhausted = json["time_budget_exhausted"].bool ?: false,
      )
    }
  }
}

/** Whether the goal controls work for a chat right now, and why not. */
data class GoalSupport(val available: Boolean, val reason: String? = null) {
  companion object {
    fun claude(runtime: JsonElement?): GoalSupport =
      if (runtime["features"]["goals"].bool == true) GoalSupport(true)
      else GoalSupport(false, "这台服务器上的 Claude Code 不支持 /goal（需要 Agent SDK 连接）。")

    fun codex(runtime: JsonElement?): GoalSupport =
      if (runtime["goals_enabled"].bool ?: runtime["enabled"].bool ?: false) GoalSupport(true)
      else GoalSupport(false, "服务器关闭了 Codex 持久目标。")
  }
}

/** One command or skill the chat's agent offers, as the server discovered it. */
data class ProviderCommand(
  /** Opaque, server-issued; sent back with the turn. */
  val id: String,
  val name: String,
  val label: String,
  val detail: String,
  /** What the message starts with, e.g. "/review". */
  val invocation: String,
  val skill: Boolean,
  /** Where it was defined (user, project …), when the provider says. */
  val scope: String? = null,
)

/** `GET /api/sessions/{id}/provider-commands`. */
data class CommandInventory(val revision: String, val available: Boolean, val reason: String?, val commands: List<ProviderCommand>) {
  /** Commands for a partial "/name" the user is typing, best matches first. */
  fun matches(partial: String, limit: Int = 8): List<ProviderCommand> {
    val query = partial.lowercase()
    if (query.isEmpty()) return commands.take(limit)
    fun rank(command: ProviderCommand): Int? {
      val name = command.invocation.drop(1).lowercase()
      val label = command.label.lowercase()
      return when {
        name.startsWith(query) -> 0
        label.startsWith(query) -> 1
        query in name || query in label -> 2
        else -> null
      }
    }
    return commands.mapNotNull { command -> rank(command)?.let { it to command } }.sortedBy { it.first }.take(limit).map { it.second }
  }

  /** The command a message invokes: it starts with the invocation, then a space, a newline or nothing. */
  fun command(message: String): ProviderCommand? {
    if (!message.startsWith("/")) return null
    val token = message.takeWhile { !it.isWhitespace() }
    return commands.firstOrNull { it.invocation == token }
  }

  companion object {
    fun parse(json: JsonElement?): CommandInventory? {
      val revision = json["revision"].string ?: return null
      val commands = json["commands"].array.mapNotNull { item ->
        val id = item["id"].string ?: return@mapNotNull null
        val invocation = item["invocation"].string?.takeIf { it.startsWith("/") } ?: return@mapNotNull null
        val name = item["name"].text ?: invocation.drop(1)
        ProviderCommand(
          id, name, item["label"].text ?: name, trimScopeSuffix(item["description"].text ?: ""), invocation,
          skill = item["kind"].string == "skill", scope = item["scope"].string,
        )
      }
      return CommandInventory(revision, json["support"]["available"].bool ?: false, json["support"]["reason"].text, commands)
    }

    /** Claude appends "(user)" or "(project)" to descriptions; the scope is shown separately. */
    private fun trimScopeSuffix(text: String): String = text.replace(Regex("""\s*\((user|project|local|plugin[^)]*)\)$"""), "")
  }
}

/** Slash-command input in the composer. */
object SlashParser {
  /** The partial command name while the user types the first word ("/rev" → "rev"). */
  fun partialCommand(draft: String): String? = if (draft.startsWith("/") && draft.none { it.isWhitespace() }) draft.drop(1) else null

  /** The argument of a "/goal …" message ("" for a bare "/goal"). */
  fun goalArgument(text: String): String? = argument(text, "/goal")

  /** The question of a "/btw …" message: a side question, not a turn. */
  fun sideQuestion(text: String): String? = argument(text, "/btw")?.takeIf { it.isNotEmpty() }

  private fun argument(text: String, command: String): String? {
    if (!text.startsWith(command)) return null
    val rest = text.drop(command.length)
    if (rest.isNotEmpty() && !rest.first().isWhitespace()) return null
    return rest.trim()
  }
}

/** One MCP server the chat's agent loaded. */
data class McpServer(
  val name: String,
  val status: Status,
  val enabled: Boolean = true,
  val error: String? = null,
  val scope: String? = null,
  val serverInfo: String? = null,
  val toolCount: Int? = null,
) {
  enum class Status(val raw: String) {
    CONNECTED("connected"), PENDING("pending"), FAILED("failed"), NEEDS_AUTH("needs-auth"), DISABLED("disabled"), UNKNOWN("unknown");

    companion object {
      fun of(raw: String?): Status = entries.firstOrNull { it.raw == raw } ?: UNKNOWN
    }
  }

  val statusLabel: String
    get() = when (status) {
      Status.CONNECTED -> "已连接"
      Status.PENDING -> "连接中"
      Status.FAILED -> "连接失败"
      Status.NEEDS_AUTH -> "需要登录"
      Status.DISABLED -> "已停用"
      // Codex starts a chat's MCP servers with its thread; before that there is no state.
      Status.UNKNOWN -> if (error == null) "未启动" else "未知"
    }

  val activity: ActivityState
    get() = when (status) {
      Status.CONNECTED -> ActivityState.DONE
      Status.PENDING -> ActivityState.RUNNING
      Status.FAILED -> ActivityState.FAILED
      Status.NEEDS_AUTH -> ActivityState.NEEDS_ACTION
      Status.DISABLED, Status.UNKNOWN -> ActivityState.IDLE
    }

  val scopeLabel: String?
    get() = when (scope) {
      "user" -> "用户"
      "project" -> "项目"
      "local" -> "本地"
      "claudeai" -> "claude.ai"
      "managed" -> "托管"
      else -> scope
    }
}

/** `GET /api/sessions/{id}/{claude|codex}/mcp`. */
data class McpStatus(
  val available: Boolean,
  /** Claude: the SDK connection generation control requests must name. */
  val generation: String?,
  val servers: List<McpServer>,
  val truncated: Boolean,
  val reason: String?,
  /** Whether servers can be reconnected, enabled or disabled from the app. */
  val controllable: Boolean,
) {
  companion object {
    fun parse(json: JsonElement?, backend: AgentBackend): McpStatus? {
      if (json["version"].int != 1) return null
      val servers = json["servers"].array.mapNotNull { item ->
        val name = item["name"].string ?: return@mapNotNull null
        val info = item["server_info"]
        val infoText = info["name"].text?.let { name -> info["version"].text?.let { "$name $it" } ?: name }
        McpServer(
          name, McpServer.Status.of(item["status"].string), item["enabled"].bool ?: true, item["error"].text,
          item["scope"].string, infoText, item["tool_count"].int,
        )
      }
      val available = json["available"].bool ?: false
      val generation = json["generation"].string
      return McpStatus(
        available, generation, servers, json["truncated"].bool ?: false, json["reason"]["message"].text,
        controllable = available && backend == AgentBackend.CLAUDE && generation != null,
      )
    }
  }
}

/** How full a chat's context window is, as the provider last reported it. */
data class ContextUsage(val tokens: Int, val window: Int, val percent: Double) {
  companion object {
    /** From a runtime response's `context_usage`, or a socket signal's. */
    fun parse(json: JsonElement?): ContextUsage? {
      val tokens = json["context_tokens"].int ?: json["totalTokens"].int ?: return null
      val window = json["effective_context_window"].int ?: json["context_window"].int ?: json["maxTokens"].int ?: return null
      if (window <= 0) return null
      val percent = json["context_percent"].double ?: json["percentage"].double ?: tokens * 100.0 / window
      return ContextUsage(tokens, window, percent.coerceIn(0.0, 100.0))
    }

    /** The usage a runtime response or `provider_runtime_changed` frame carries, `NONE` when the provider says there is none. */
    fun from(json: JsonElement?): Result {
      val snapshot = if (json["context_usage"].isNull) json["context_usage_snapshot"] else json["context_usage"]
      parse(snapshot)?.let { return Result.Known(it) }
      return if (json["context_usage_state"].string.let { it != null && it != "available" }) Result.None else Result.Unchanged
    }
  }

  sealed interface Result {
    data class Known(val usage: ContextUsage) : Result
    data object None : Result
    data object Unchanged : Result
  }
}

/** A subagent the chat's agent started, as `GET …/subagents` reports it. */
data class Subagent(
  val id: String,
  val name: String,
  val kind: String?,
  val state: ActivityState,
  val statusLabel: String,
  val activity: String?,
  val summary: String?,
  val startedAt: Instant?,
  val log: List<String>,
) {
  companion object {
    fun parse(json: JsonElement?): List<Subagent> = json["subagents"].array.mapNotNull { item ->
      val id = item["id"].string ?: return@mapNotNull null
      val status = item["subagent_status"].string ?: ""
      val (state, label) = when (status) {
        "starting" -> ActivityState.RUNNING to "启动中"
        "running" -> ActivityState.RUNNING to "运行中"
        "completed" -> ActivityState.DONE to "已完成"
        "failed" -> ActivityState.FAILED to "失败"
        "stopped" -> ActivityState.IDLE to "已停止"
        "tracking_lost" -> ActivityState.IDLE to "失去跟踪"
        else -> ActivityState.IDLE to status.ifEmpty { "未知" }
      }
      Subagent(
        id, item["subagent_title"].text ?: item["subagent_name"].text ?: item["subagent_nickname"].text ?: "子代理",
        item["subagent_kind"].text, state, label, item["subagent_activity"].text, item["subagent_summary"].text,
        item["subagent_started_at"].instant, item["subagent_log"].array.mapNotNull { it["text"].text },
      )
    }
  }
}

/** A chat's token totals from its record (`token_totals`). */
data class TokenTotals(val total: Int, val input: Int?, val cached: Int?, val output: Int?) {
  companion object {
    fun parse(json: JsonElement?): TokenTotals? {
      val total = json["total_tokens"].int ?: return null
      return TokenTotals(total, json["input_tokens"].int, json["cached_input_tokens"].int, json["output_tokens"].int)
    }
  }
}

/** "182.4k", "1.2M": a token count as the Mac shows it. */
fun compactCount(value: Int): String =
  when {
    value >= 1_000_000 -> "%.1fM".format(value / 1_000_000.0).replace(".0M", "M")
    value >= 1_000 -> "%.1fk".format(value / 1_000.0).replace(".0k", "k")
    else -> value.toString()
  }
