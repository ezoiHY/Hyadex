package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.int
import dev.hyadex.android.api.string
import kotlinx.serialization.json.JsonElement

/** A chat's advanced settings, read from the server's full chat record (the Mac's 高级设置). */
data class ChatSettings(
  /** Extra instructions sent with every turn ("" = none). */
  val systemPrompt: String = "",
  /** Most subagents running at once; null inherits the agent's own setting. */
  val subagentLimit: Int? = null,
  val subagentLimitSupported: Boolean = true,
  val subagentLimitNote: String = "",
  /** Whether the server names the chat from its first turns. */
  val autoTitle: Boolean = true,
  /** What the agent may do with scheduled jobs from inside a turn: full, read_only or blocked. */
  val jobsAccess: String = "full",
  /** Which questions other chats in the project may send this chat: off, basic or deep. */
  val consultInbound: String = "deep",
  /** Whether this chat's agent needs your approval before asking another chat in depth. */
  val consultDeepApproval: Boolean = true,
  /** Chats it may always ask in depth ("这个聊天以后都允许" on an approval). */
  val consultDeepAllowed: List<String> = emptyList(),
) {
  /** The PATCH fields that turn this into `updated`; unchanged fields are left out. */
  fun changes(updated: ChatSettings): Map<String, Any?> {
    val fields = mutableMapOf<String, Any?>()
    val prompt = updated.systemPrompt.trim()
    if (prompt != systemPrompt.trim()) fields["system_prompt"] = prompt.ifEmpty { null }
    if (updated.subagentLimit != subagentLimit) fields["subagent_limit"] = updated.subagentLimit
    if (updated.autoTitle != autoTitle) fields["auto_title_enabled"] = updated.autoTitle
    if (updated.jobsAccess != jobsAccess) fields["provider_jobs_access"] = updated.jobsAccess
    if (updated.consultInbound != consultInbound) fields["consult_inbound"] = updated.consultInbound
    if (updated.consultDeepApproval != consultDeepApproval) fields["consult_deep_approval"] = updated.consultDeepApproval
    if (updated.consultDeepAllowed != consultDeepAllowed) fields["consult_deep_allowed"] = updated.consultDeepAllowed
    return fields
  }

  companion object {
    /** Longest system prompt the server accepts by default. */
    const val MAX_SYSTEM_PROMPT = 24_000
    val SUBAGENT_LIMITS = listOf(1, 2, 3, 4, 6, 8, 12, 16)
    val JOBS_ACCESS = listOf("full" to "允许", "read_only" to "只读", "blocked" to "禁止")
    val CONSULT_INBOUND = listOf("off" to "关闭", "basic" to "只允许基础", "deep" to "基础和深度")

    fun parse(session: JsonElement?): ChatSettings {
      val control = session["subagent_limit_control"]
      return ChatSettings(
        systemPrompt = session["system_prompt"].string ?: "",
        subagentLimit = session["subagent_limit"].int,
        subagentLimitSupported = control["supported"].bool ?: true,
        subagentLimitNote = control["message"].string ?: "",
        autoTitle = session["auto_title_enabled"].bool ?: true,
        jobsAccess = session["provider_jobs_access"].string?.takeIf { value -> JOBS_ACCESS.any { it.first == value } } ?: "full",
        consultInbound = session["consult_inbound"].string?.takeIf { value -> CONSULT_INBOUND.any { it.first == value } } ?: "deep",
        consultDeepApproval = session["consult_deep_approval"].bool ?: true,
        consultDeepAllowed = session["consult_deep_allowed"].array.mapNotNull { it.string },
      )
    }
  }
}

/** Why an asked chat gave no answer, in a line (the Mac's consult `problem`); null when its status says it all. */
fun consultProblem(status: String?, errorCode: String?, error: String?, deep: Boolean): String? {
  if (status in setOf("awaiting_approval", "queued", "running", "completed")) return null
  return when (errorCode) {
    "turn_ended" -> "提问的那一轮已经结束"
    "abandoned" -> "提问的一方不再等待"
    "chat_deleted" -> "有一方聊天已被删除"
    "approval_expired" -> "10 分钟内没有人批准"
    "interrupted" -> "服务器重启，没来得及回答"
    "timeout" -> if (deep) "5 分钟内没有查完" else "2 分钟内没有回答"
    "denied" -> "已拒绝"
    "cancelled" -> "已停止"
    else -> error ?: when (status) {
      "denied" -> "已拒绝"
      "cancelled" -> "已停止"
      else -> "没有回答"
    }
  }
}
