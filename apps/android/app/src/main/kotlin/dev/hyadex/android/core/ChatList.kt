package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.obj
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.serialization.json.JsonElement

/** Event types that only occur while a turn is in progress (servers without `running`). */
private val activeEventTypes =
  setOf(
    "turn_started", "process_started", "provider_session", "tool_started", "tool_finished", "assistant_text",
    "reasoning_summary", "reasoning_text", "claude_interaction_requested", "codex_interaction_requested",
    "claude_interaction_resolved", "codex_interaction_resolved", "codex_thread_status", "code_diff",
    "subagent_state", "claude_goal_changed",
  )

/** The folder the server gives every chat that is in no project. */
private const val SERVER_DEFAULT_FOLDER = "General"

/** A chat record from `GET /api/sessions`, as the Mac app's `LiveMapping.chat`. */
fun chatSummary(json: JsonElement): ChatSummary? {
  val id = json["id"].string ?: return null
  val backend = AgentBackend.named(json["backend"].string) ?: return null
  val running = json["running"].bool ?: (json["latest_event_type"].string in activeEventTypes)
  var state = if (running) ActivityState.RUNNING else ActivityState.IDLE
  if (json["claude_needs_user_action"].bool == true || json["codex_needs_user_action"].bool == true) state = ActivityState.NEEDS_ACTION
  val readSeq = json["last_read_agent_event_seq"].int ?: 0
  val agentSeq = json["latest_agent_event_seq"].int ?: 0
  return ChatSummary(
    id = id,
    title = json["title"].text ?: "未命名聊天",
    backend = backend,
    model = json["model"].text ?: "",
    effort = json["effort"].text ?: "",
    permission = permissionLabel(json, backend),
    cwd = json["cwd"].string ?: "~",
    lastActivity = json["updated_at"].instant ?: Instant.EPOCH,
    state = state,
    pinned = json["pinned"].bool == true,
    unread = json["manual_unread"].bool == true || agentSeq > readSeq,
    archived = json["archived"].bool == true,
    folder = json["folder"].string?.trim()?.takeIf { it.isNotEmpty() && it != SERVER_DEFAULT_FOLDER },
    agentSeq = agentSeq,
  )
}

/** The permission preset's name in the provider's own terms. */
fun permissionLabel(json: JsonElement, backend: AgentBackend): String =
  when (backend) {
    AgentBackend.CLAUDE ->
      when (json["claude_permission_mode"].string ?: "default") {
        "acceptEdits" -> "Accept edits"
        "plan" -> "Plan"
        "auto" -> "Auto"
        "dontAsk" -> "Don't Ask"
        "bypassPermissions" -> "Bypass Permissions"
        else -> "Manual"
      }
    AgentBackend.CODEX -> {
      val sandbox = json["codex_sandbox_mode"].string ?: "workspace-write"
      val policy = json["codex_approval_policy"].string ?: "on-request"
      when {
        sandbox == "read-only" && policy == "on-request" -> "Read Only"
        sandbox == "workspace-write" && policy == "on-request" -> "Default"
        sandbox == "danger-full-access" && policy == "never" -> "Full Access"
        else -> "Custom ($sandbox, $policy)"
      }
    }
  }

/** True for the risky presets the Mac app shows in orange. */
fun isRiskyPermission(label: String) = label == "Bypass Permissions" || label == "Full Access"

/** A provider's permission preset, named as its own client names it, with the session fields that set it. */
data class PermissionPreset(val label: String, val detail: String, val fields: Map<String, String>) {
  companion object {
    fun presets(backend: AgentBackend): List<PermissionPreset> =
      when (backend) {
        AgentBackend.CLAUDE -> listOf(
          PermissionPreset("Manual", "编辑文件、运行命令前先问你", mapOf("claude_permission_mode" to "default")),
          PermissionPreset("Accept edits", "直接改文件，运行命令前仍会问你", mapOf("claude_permission_mode" to "acceptEdits")),
          PermissionPreset("Plan", "只读分析并给出计划，不改任何东西", mapOf("claude_permission_mode" to "plan")),
          PermissionPreset("Auto", "由 Claude 的分类器自动批准或拒绝每次工具调用", mapOf("claude_permission_mode" to "auto")),
          PermissionPreset("Don't Ask", "从不询问；没有预先允许的操作一律拒绝", mapOf("claude_permission_mode" to "dontAsk")),
          PermissionPreset("Bypass Permissions", "跳过所有权限检查，谨慎使用", mapOf("claude_permission_mode" to "bypassPermissions")),
        )
        AgentBackend.CODEX -> listOf(
          PermissionPreset("Read Only", "可读取工作区文件；改文件或联网需要你批准", mapOf("codex_sandbox_mode" to "read-only", "codex_approval_policy" to "on-request")),
          PermissionPreset("Default", "可读写工作区、运行命令；联网或改工作区外的文件需要你批准", mapOf("codex_sandbox_mode" to "workspace-write", "codex_approval_policy" to "on-request")),
          PermissionPreset("Full Access", "可改工作区外的文件、联网，都不再询问，谨慎使用", mapOf("codex_sandbox_mode" to "danger-full-access", "codex_approval_policy" to "never")),
        )
      }
  }
}

data class RuntimeOption(val value: String, val label: String)

/** A backend's models and reasoning efforts from `GET /api/runtime/catalog`. */
data class RuntimeCatalog(val models: List<RuntimeOption>, val efforts: List<RuntimeOption>, val modelEfforts: Map<String, List<RuntimeOption>>) {
  fun efforts(model: String): List<RuntimeOption> = modelEfforts[model]?.takeIf { it.isNotEmpty() } ?: efforts

  companion object {
    private fun options(json: JsonElement?): List<RuntimeOption> =
      json.array.mapNotNull { item ->
        val value = item["value"].string ?: return@mapNotNull null
        RuntimeOption(value, item["label"].text ?: if (value.isEmpty()) "Server default" else value)
      }

    fun parse(json: JsonElement?): RuntimeCatalog? {
      val models = options(json["models"])
      if (models.isEmpty()) return null
      val modelEfforts = mutableMapOf<String, List<RuntimeOption>>()
      json["model_efforts"].obj?.forEach { (model, efforts) -> modelEfforts[model] = options(efforts) }
      // Codex lists each model's efforts with the model.
      for (model in json["models"].array) {
        val value = model["value"].string ?: continue
        val efforts = options(model["efforts"])
        if (efforts.isNotEmpty()) modelEfforts[value] = efforts
      }
      return RuntimeCatalog(models, options(json["efforts"]), modelEfforts)
    }
  }
}

data class ProjectSection(val key: String, val title: String, val chats: List<ChatSummary>)

/** Chats by project (the server's folder), then by working directory; the most recently active first. */
fun projectSections(chats: List<ChatSummary>): List<ProjectSection> {
  val groups = linkedMapOf<String, Pair<String, MutableList<ChatSummary>>>()
  for (chat in chats) {
    val folder = chat.folder
    val directory = chat.cwd.removePrefix("/private").trimEnd('/')
    val home = SessionProjection.homeOf(directory)
    val (key, title) =
      when {
        folder != null -> "project:$folder" to folder
        directory.isNotEmpty() && directory != "~" && directory != home -> "directory:$directory" to directory.substringAfterLast('/')
        else -> "other" to "其他"
      }
    groups.getOrPut(key) { title to mutableListOf() }.second += chat
  }
  return groups
    .map { (key, value) -> ProjectSection(key, value.first, value.second.sortedByDescending { it.lastActivity }) }
    .sortedByDescending { section -> section.chats.maxOf { it.lastActivity } }
}

private val timeFormat = DateTimeFormatter.ofPattern("HH:mm")
private val dayFormat = DateTimeFormatter.ofPattern("M月d日")

/** "刚刚", "5 分钟", "14:03", "昨天", "10月28日" for compact lists. */
fun relativeShort(time: Instant, now: Instant = Instant.now()): String {
  val seconds = now.epochSecond - time.epochSecond
  if (seconds < 60) return "刚刚"
  if (seconds < 3600) return "${seconds / 60} 分钟"
  val zone = ZoneId.systemDefault()
  val day = time.atZone(zone).toLocalDate()
  val today = LocalDate.now(zone)
  return when (day) {
    today -> timeFormat.format(time.atZone(zone))
    today.minusDays(1) -> "昨天"
    else -> dayFormat.format(time.atZone(zone))
  }
}
