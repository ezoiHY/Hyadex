package dev.hyadex.android.core

import java.time.Instant

enum class AgentBackend(val raw: String, val displayName: String) {
  CLAUDE("claude", "Claude"),
  CODEX("codex", "Codex");

  companion object {
    fun named(raw: String?): AgentBackend? = entries.firstOrNull { it.raw == raw?.lowercase() }
  }
}

data class Author(val name: String, val kind: Kind, val backend: AgentBackend? = null) {
  enum class Kind { USER, AGENT, SYSTEM, ORCHESTRATOR }

  companion object {
    val user = Author("你", Kind.USER)

    fun agent(handle: String, backend: AgentBackend) = Author(handle, Kind.AGENT, backend)
  }
}

data class FileChange(val path: String, val added: Int, val removed: Int, val diff: String = "", val root: String? = null)

/** A run's unified diff split by file path, as the Mac's change panel reads it. */
fun splitDiff(unified: String): Map<String, String> {
  val result = mutableMapOf<String, String>()
  var path: String? = null
  val lines = mutableListOf<String>()
  fun flush() {
    path?.let { result[it] = lines.joinToString("\n") }
    lines.clear()
  }
  for (line in unified.split("\n")) {
    if (line.startsWith("diff --git ")) {
      flush()
      path = line.substringAfterLast(" b/")
      continue
    }
    if (line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ") || line.startsWith("new file mode") || line.startsWith("deleted file mode")) continue
    lines += line
  }
  flush()
  return result
}

data class FileChangeSet(val files: List<FileChange>) {
  val added: Int get() = files.sumOf { it.added }
  val removed: Int get() = files.sumOf { it.removed }
}

enum class ToolKind(val activeVerb: String, val verb: String) {
  COMMAND("正在运行", "运行"),
  READ("正在读取", "读取"),
  EDIT("正在编辑", "编辑"),
  SEARCH("正在搜索", "搜索"),
  MCP("正在调用", "调用"),
  WEB("正在访问", "访问"),
}

enum class ToolStatus { RUNNING, SUCCEEDED, FAILED }

sealed interface ApprovalDecision {
  data object Pending : ApprovalDecision
  data object Allowed : ApprovalDecision
  data object AllowedForSession : ApprovalDecision
  data object AllowedAlways : ApprovalDecision
  data object Denied : ApprovalDecision
  data class Answered(val summary: String) : ApprovalDecision
  data object Skipped : ApprovalDecision
  data object Resolved : ApprovalDecision
}

data class MediaFile(val id: String, val name: String, val contentType: String) {
  val isImage: Boolean
    get() =
      contentType.startsWith("image/") ||
        name.substringAfterLast('.', "").lowercase() in setOf("png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "svg")
}

/** One entry of a chat's timeline, as the Mac app's `TimelineItem`. */
sealed interface TimelineItem {
  val id: String

  data class Message(
    override val id: String,
    val author: Author,
    val text: String,
    val time: Instant,
    val streaming: Boolean = false,
  ) : TimelineItem

  data class Thinking(override val id: String, val seconds: Int, val text: String) : TimelineItem

  data class Tool(
    override val id: String,
    val kind: ToolKind,
    val title: String,
    val output: String,
    val status: ToolStatus,
    val seconds: Double,
  ) : TimelineItem

  /** What a run changed; `runID` loads the diffs (`GET …/diffs/{run}`). */
  data class Changes(override val id: String, val set: FileChangeSet, val runID: String? = null) : TimelineItem

  data class Approval(
    override val id: String,
    val author: Author,
    val title: String,
    val command: String,
    val reason: String,
    val decision: ApprovalDecision,
    val choices: List<ApprovalChoice>,
  ) : TimelineItem

  data class Form(override val id: String, val author: Author, val form: InteractionForm, val decision: ApprovalDecision) :
    TimelineItem

  data class Divider(override val id: String, val text: String) : TimelineItem

  data class Media(override val id: String, val author: Author, val files: List<MediaFile>) : TimelineItem

  /** A question to another chat (the Hyadex `ask` helper, or the user's own @ on the Mac). */
  data class Consult(
    override val id: String,
    val target: String,
    val deep: Boolean,
    val question: String,
    val running: Boolean,
    val answer: String? = null,
    val error: String? = null,
    val byUser: Boolean = false,
  ) : TimelineItem

  /** Thinking, tool calls and answered approvals fold into a turn's "已处理" row. */
  val isWorkStep: Boolean
    get() =
      when (this) {
        is Thinking, is Tool -> true
        is Approval -> decision != ApprovalDecision.Pending
        is Form -> decision != ApprovalDecision.Pending
        else -> false
      }

  /** An agent's image published mid-run shows after that run instead of splitting it. */
  val floatsPastWork: Boolean get() = this is Media && author.kind == Author.Kind.AGENT
}

fun List<TimelineItem>.pendingApproval(): TimelineItem.Approval? =
  lastOrNull { it is TimelineItem.Approval && it.decision == ApprovalDecision.Pending } as? TimelineItem.Approval

fun List<TimelineItem>.pendingForm(): TimelineItem.Form? =
  lastOrNull { it is TimelineItem.Form && it.decision == ApprovalDecision.Pending } as? TimelineItem.Form

enum class ActivityState { IDLE, QUEUED, RUNNING, NEEDS_ACTION, FAILED, DONE, PAUSED }

data class QueuedMessage(val id: String, val text: String, val hold: Hold? = null) {
  enum class Hold {
    /** Kept after the user stopped the turn; waits for 继续发送 or 立即发送. */
    STOPPED,
    /** The provider may already have it; the user should check before sending it again. */
    DELIVERY_UNCERTAIN;

    companion object {
      fun of(reason: String?): Hold = if (reason == "delivery_uncertain") DELIVERY_UNCERTAIN else STOPPED
    }
  }

  /** Waiting on the user rather than on the agent. */
  val paused: Boolean get() = hold != null
}

data class ChatSummary(
  val id: String,
  val title: String,
  val backend: AgentBackend,
  val model: String,
  val effort: String,
  val permission: String,
  val cwd: String,
  val lastActivity: Instant,
  val state: ActivityState,
  val pinned: Boolean,
  val unread: Boolean,
  val archived: Boolean,
  val folder: String?,
  /** The seq of the chat's latest agent event: grows when its agent does anything. */
  val agentSeq: Int = 0,
  /** Tokens the chat has used so far, when the list carries them. */
  val tokens: TokenTotals? = null,
)

/** What the agent is doing now: starting, thinking, running a tool, or waiting for the user. */
data class LiveActivity(
  val kind: Kind,
  val label: String,
  val detail: String? = null,
  val since: Instant,
  val backend: AgentBackend? = null,
) {
  enum class Kind { STARTING, THINKING, TOOL, WAITING }
}

/** A message sent but not yet accepted by the server (shown at once). */
data class PendingSend(val id: String, val text: String, val time: Instant = Instant.now())
