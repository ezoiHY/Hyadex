package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.instant
import dev.hyadex.android.api.int
import dev.hyadex.android.api.obj
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import java.time.Duration
import java.time.Instant
import kotlin.math.max
import kotlin.math.roundToInt
import kotlinx.serialization.json.JsonElement

/**
 * Turns a chat's event log into timeline items, incrementally: a port of the
 * Mac app's `SessionProjection` (apps/macos/Sources/HyadexCore/EventProjection.swift).
 * Feed events in `seq` order with [apply]; duplicates and stale events are ignored.
 */
class SessionProjection(val backend: AgentBackend, val cwd: String, private val home: String = "") {
  val items = mutableListOf<TimelineItem>()
  var lastSeq = 0
    private set
  var running = false
    private set
  val pending = mutableMapOf<String, List<String>>()
  val queued = mutableListOf<QueuedMessage>()
  var turnStartedAt: Instant? = null
    private set
  /** How long each finished turn took, keyed by the id of its user message. */
  val turnSeconds = mutableMapOf<String, Int>()
  /** `client_request_id`s the server has accepted (turn started or queued). */
  val acceptedRequests = mutableSetOf<String>()

  private var turnUserItem: String? = null
  private val index = mutableMapOf<String, Int>()
  private val toolStarts = mutableMapOf<String, Instant>()
  /** When each item first appeared, and where turns ended (item count → time). */
  private val itemTimes = mutableMapOf<String, Instant>()
  private val turnEnds = mutableMapOf<Int, Instant>()
  private var eventTime = Instant.now()
  /** The current turn's thoughts, to drop a partial copy of one already recorded. */
  private val turnThoughts = mutableSetOf<String>()
  private val uploads = mutableMapOf<String, MediaFile>()
  private var lastPublication: String? = null
  private val decided = mutableMapOf<String, ApprovalDecision>()
  private val subagentLogs = mutableMapOf<String, SubagentLog>()

  private class SubagentLog {
    val tools = mutableListOf<Triple<String, String, Boolean>>()
    var text = ""
  }

  val agent: Author get() = Author.agent(backend.raw, backend)

  /**
   * How long each run of work took, by its first step: from the turn's start
   * (the turn's first run) or the run's first step, to what ended it — the
   * next item shown or the end of the turn. A run still going has none.
   */
  val workSeconds: Map<String, Int>
    get() {
      val result = mutableMapOf<String, Int>()
      var first: String? = null
      var start: Instant? = null
      var turnStart: Instant? = null
      fun close(end: Instant?) {
        val id = first
        val begin = start
        if (id != null && begin != null && end != null) result[id] = max(0, (Duration.between(begin, end).toMillis() / 1000.0).roundToInt())
        first = null
        start = null
      }
      for ((position, item) in items.withIndex()) {
        turnEnds[position]?.let {
          close(it)
          turnStart = null
        }
        if (item is TimelineItem.Message && item.author.kind == Author.Kind.USER) {
          close(item.time)
          turnStart = item.time
          continue
        }
        if (item.isWorkStep) {
          if (first == null) {
            first = item.id
            start = turnStart ?: itemTimes[item.id]
          }
          continue
        }
        if (item.floatsPastWork) continue
        close(itemTimes[item.id])
        turnStart = null
      }
      close(turnEnds[items.size])
      return result
    }

  fun apply(events: List<JsonElement>) = events.forEach(::apply)

  fun apply(event: JsonElement) {
    val seq = event["seq"].int ?: return
    if (seq <= lastSeq) return
    lastSeq = seq
    val id = event["id"].string ?: "seq-$seq"
    val time = event["ts"].instant ?: Instant.now()
    eventTime = time
    val parent = event["parent_tool_id"].string
    if (!parent.isNullOrEmpty()) return applySubagent(event, parent, id)
    when (event["type"].string) {
      "turn_started" -> {
        running = true
        turnStartedAt = time
        turnUserItem = id
        turnThoughts.clear()
        event["client_request_id"].string?.let { acceptedRequests += it }
        event["queued_id"].string?.let { queuedID -> queued.removeAll { it.id == queuedID } }
        val prompt = event["display_prompt"].text ?: event["prompt"].text ?: ""
        if (prompt.isNotEmpty() || event["client_request_id"].string != null) {
          append(TimelineItem.Message(id, Author.user, prompt, time))
        }
        val attached = (if (event["display_file_ids"].array.isEmpty()) event["file_ids"] else event["display_file_ids"]).array.mapNotNull { it.string }
        if (attached.isNotEmpty()) {
          append(TimelineItem.Media("$id-files", Author.user, attached.map { uploads[it] ?: MediaFile(it, it, "") }))
        }
      }
      "file_uploaded" -> mediaFile(event["file"])?.let { uploads[it.id] = it }
      "artifact_created" -> {
        val file = mediaFile(event["artifact"]) ?: return
        val last = items.lastOrNull()
        val publication = event["publication_id"].string
        // One publication of several files shows as one group.
        if (last is TimelineItem.Media && last.author.kind == Author.Kind.AGENT && publication != null && publication == lastPublication) {
          replace(last.id, last.copy(files = last.files + file))
        } else {
          append(TimelineItem.Media(id, agent, listOf(file)))
        }
        lastPublication = publication
      }
      "assistant_text" -> {
        val text = event["text"].text ?: return
        // Claude's /compact prints "Compacted"; its compaction landmark already says so.
        if (event["local_command"].string?.trim('/') == "compact" || event["local_command_detached"].bool == true) return
        append(TimelineItem.Message(id, agent, text, time))
      }
      "reasoning_summary", "reasoning_text" -> {
        val text = event["text"].text ?: return
        // An older server kept the turn's last thought a second time, as partial.
        val thought = text.trim()
        if (event["partial"].bool == true && thought in turnThoughts) return
        turnThoughts += thought
        val last = items.lastOrNull()
        if (last is TimelineItem.Thinking) {
          replace(last.id, last.copy(text = last.text + "\n\n" + text))
        } else {
          append(TimelineItem.Thinking(id, 0, text))
        }
      }
      "chat_consult_asked" -> {
        // The user asked another chat from the Mac (@ in its composer).
        val consultID = event["consult_id"].string ?: return
        append(
          TimelineItem.Consult(
            "consult-$consultID", event["target"]["title"].string ?: "聊天", event["mode"].string == "deep",
            event["question"].string ?: "", running = false, byUser = true,
          )
        )
      }
      "tool_started" -> {
        val tool = event["tool"]
        val toolID = tool["id"].string ?: return
        toolStarts[toolID] = time
        // A question to another chat is its own card, not folded into the work.
        consultCall(tool)?.let { (target, deep, question) ->
          append(TimelineItem.Consult(toolID, target, deep, question, running = true))
          return
        }
        val (kind, title) = describeTool(tool)
        append(TimelineItem.Tool(toolID, kind, title, "", ToolStatus.RUNNING, 0.0))
      }
      "tool_finished" -> {
        val tool = event["tool"]
        val toolID = event["tool_id"].string ?: tool["id"].string ?: return
        val consult = index[toolID]?.let { items[it] } as? TimelineItem.Consult
          ?: consultCall(tool)?.let { (target, deep, question) -> TimelineItem.Consult(toolID, target, deep, question, running = true) }
        if (consult != null) {
          val receipt = consultReceipt(event["output"].string ?: "", failed = event["is_error"].bool == true)
          replace(toolID, consult.copy(running = false, answer = receipt.first, error = receipt.second))
          return
        }
        val (kind, title) = describeTool(tool)
        val seconds = toolStarts[toolID]?.let { max(0.0, Duration.between(it, time).toMillis() / 1000.0) } ?: 0.0
        val failed = event["is_error"].bool == true || (event["exit_code"].int ?: 0) != 0
        val output = subagentOutput(toolID, clip(event["output"].string ?: ""))
        val item = TimelineItem.Tool(toolID, kind, title, output, if (failed) ToolStatus.FAILED else ToolStatus.SUCCEEDED, seconds)
        if (index[toolID] != null) replace(toolID, item) else append(item)
      }
      "claude_interaction_requested", "codex_interaction_requested" -> {
        val interaction = event["interaction"]
        val interactionID = interaction["id"].string ?: return
        val method = interaction["method"].string ?: ""
        val params = interaction["params"]
        pending[interactionID] = params["availableDecisions"].array.mapNotNull { it.string }
        val form = InteractionForm.parse(method, params)
        if (form != null) {
          append(TimelineItem.Form(interactionID, agent, form, ApprovalDecision.Pending))
          return
        }
        val request = describeInteraction(interaction)
        append(
          TimelineItem.Approval(
            interactionID, agent, request.first, request.second, request.third, ApprovalDecision.Pending,
            ApprovalChoice.choices(method, params),
          )
        )
      }
      "claude_interaction_resolved", "codex_interaction_resolved" -> {
        val interactionID = event["interaction_id"].string ?: return
        pending.remove(interactionID)
        setDecision(decided[interactionID] ?: ApprovalDecision.Resolved, interactionID)
      }
      "code_diff" -> {
        val files = event["diff_files"].array.mapNotNull { file ->
          val path = file["path"].string ?: return@mapNotNull null
          FileChange(path, file["additions"].int ?: 0, file["deletions"].int ?: 0, root = event["repository_root"].string ?: cwd)
        }
        if (files.isNotEmpty()) append(TimelineItem.Changes(id, FileChangeSet(files), event["run_id"].string))
      }
      // Codex records a stopped turn as finished with `stopped`; it reads as stopped, as Codex shows it.
      "turn_finished" if event["stopped"].bool != true -> {
        running = false
        closeTurn(time)
        pending.clear()
        settleRunningTools(ToolStatus.SUCCEEDED)
      }
      "turn_finished", "turn_stopped", "turn_aborted" -> {
        running = false
        closeTurn(time)
        pending.clear()
        settleRunningTools(ToolStatus.FAILED)
        // One stop recorded twice (a hard stop and the runner's own finish) shows once.
        if ((items.lastOrNull() as? TimelineItem.Divider)?.text == "已停止") return
        append(TimelineItem.Divider(id, "已停止"))
      }
      "turn_failed", "error" -> {
        running = false
        closeTurn(time)
        settleRunningTools(ToolStatus.FAILED)
        val message = event["message"].text ?: event["error"].text ?: "运行出错"
        append(TimelineItem.Divider(id, "出错：" + message.take(160)))
      }
      "turn_queued" -> {
        event["client_request_id"].string?.let { acceptedRequests += it }
        val queuedID = event["queued_id"].string ?: return
        if (queued.none { it.id == queuedID }) queued += QueuedMessage(queuedID, event["display_prompt"].text ?: event["prompt"].text ?: "")
      }
      "turn_unqueued", "turn_queue_run_now" -> event["queued_id"].string?.let { queuedID -> queued.removeAll { it.id == queuedID } }
      "turn_queue_paused" -> {
        val ids = event["queued_ids"].array.mapNotNull { it.string }.toSet()
        queued.replaceAll { if (it.id in ids && it.hold == null) it.copy(hold = QueuedMessage.Hold.STOPPED) else it }
      }
      "turn_queue_delivery_fenced" -> event["queued_id"].string?.let { queuedID ->
        queued.replaceAll { if (it.id == queuedID) it.copy(hold = QueuedMessage.Hold.DELIVERY_UNCERTAIN) else it }
      }
      "turn_queue_resumed" -> {
        val ids = event["queued_ids"].array.mapNotNull { it.string }.toSet()
        queued.replaceAll { if (it.id in ids && it.hold == QueuedMessage.Hold.STOPPED) it.copy(hold = null) else it }
      }
      "turn_queue_reordered" -> {
        // The server's new order, by position (moved up or down a place).
        val positions = event["positions"].array.mapNotNull { place -> place["queued_id"].string?.let { it to (place["position"].int ?: Int.MAX_VALUE) } }.toMap()
        if (positions.isNotEmpty()) queued.sortBy { positions[it.id] ?: Int.MAX_VALUE }
      }
      "turn_queue_updated" -> {
        val queuedID = event["queued_id"].string ?: return
        val text = event["display_prompt"].text ?: event["prompt"].text ?: return
        queued.replaceAll { if (it.id == queuedID) it.copy(text = text) else it }
      }
      "turn_steered" -> {
        // Sent into the running turn: the user's words join it where they arrived.
        event["queued_id"].string?.let { queuedID -> queued.removeAll { it.id == queuedID } }
        event["client_request_id"].string?.let { acceptedRequests += it }
        val prompt = event["display_prompt"].text ?: event["prompt"].text
        if (!prompt.isNullOrEmpty()) append(TimelineItem.Message(id, Author.user, prompt, time))
      }
      "agent_notification" -> event["message"].text?.let {
        val important = event["notification_level"].string == "important"
        append(TimelineItem.Divider(id, (if (important) "重要通知：" else "通知：") + it))
      }
      "emergency_alert_raised" -> append(TimelineItem.Divider(id, "紧急提醒：" + (event["message"].text ?: "")))
      "emergency_alert_acknowledged" -> append(TimelineItem.Divider(id, "已确认紧急提醒"))
      "session_forked" -> append(TimelineItem.Divider(id, "已分叉出一个新聊天" + (event["copied_events"].int?.let { "（$it 条记录）" } ?: "")))
      "history_imported" -> if (event["copied_events"].int != null) append(TimelineItem.Divider(id, "从原聊天分叉而来，以上是复制的对话"))
      "claude_compaction_completed" -> append(TimelineItem.Divider(id, "上下文已压缩"))
      "codex_compaction_started" -> append(TimelineItem.Divider(id, "正在压缩上下文…"))
      "codex_compaction_completed" -> {
        val failed = event["status"].string?.let { it != "completed" } ?: false
        append(TimelineItem.Divider(id, if (failed) "上下文压缩失败" else "上下文已压缩"))
      }
    }
  }

  /** Replaces the queue with the server's current list (from `GET /api/sessions/{id}`). */
  fun setQueue(turns: List<JsonElement>) {
    queued.clear()
    turns.mapNotNullTo(queued) { turn ->
      val id = turn["id"].string ?: turn["queued_id"].string ?: return@mapNotNullTo null
      val hold = if (turn["paused"].bool == true) QueuedMessage.Hold.of(turn["pause_reason"].string) else null
      QueuedMessage(id, turn["display_prompt"].text ?: turn["prompt"].text ?: "", hold)
    }
  }

  /** Records the user's own answer so the resolved card says what was chosen. */
  fun noteDecision(decision: ApprovalDecision, interactionID: String) {
    decided[interactionID] = decision
    setDecision(decision, interactionID)
  }

  // Subagents: a subagent's tool calls and narration fold into its Agent tool's
  // card; they never become the main agent's messages.

  private fun applySubagent(event: JsonElement, parent: String, id: String) {
    val position = index[parent]
    val card = position?.let { items[it] } as? TimelineItem.Tool
    if (card == null) {
      // Without a known parent (history paged in mid-turn) they stay with the turn's folded work.
      when (event["type"].string) {
        "assistant_text", "reasoning_summary", "reasoning_text" -> event["text"].text?.let { append(TimelineItem.Thinking(id, 0, it)) }
        "tool_started" -> {
          val tool = event["tool"]
          val toolID = tool["id"].string ?: return
          val (kind, title) = describeTool(tool)
          append(TimelineItem.Tool(toolID, kind, title, "", ToolStatus.RUNNING, 0.0))
        }
        "tool_finished" -> {
          val toolID = event["tool_id"].string ?: event["tool"]["id"].string ?: return
          val tool = index[toolID]?.let { items[it] } as? TimelineItem.Tool ?: return
          val failed = event["is_error"].bool == true
          replace(toolID, tool.copy(output = clip(event["output"].string ?: ""), status = if (failed) ToolStatus.FAILED else ToolStatus.SUCCEEDED))
        }
      }
      return
    }
    val log = subagentLogs.getOrPut(parent) { SubagentLog() }
    when (event["type"].string) {
      "tool_started" -> {
        val tool = event["tool"]
        val toolID = tool["id"].string ?: return
        if (log.tools.any { it.first == toolID }) return
        log.tools += Triple(toolID, describeTool(tool).second, false)
      }
      "tool_finished" -> {
        val toolID = event["tool_id"].string ?: event["tool"]["id"].string ?: return
        val entry = log.tools.indexOfFirst { it.first == toolID }
        if (entry < 0) return
        log.tools[entry] = log.tools[entry].copy(third = event["is_error"].bool == true)
      }
      "assistant_text" -> log.text = event["text"].text ?: return
      else -> return
    }
    val result = if (card.status == ToolStatus.RUNNING) "" else card.output.split(SUBAGENT_RESULT_MARKER).last()
    items[position] = card.copy(output = subagentOutput(parent, result))
  }

  /** A subagent tool's card: its recent steps, then its result once it finished. */
  private fun subagentOutput(toolID: String, result: String): String {
    val log = subagentLogs[toolID] ?: return result
    val lines = log.tools.takeLast(8).map { "· " + it.second + (if (it.third) "（失败）" else "") }.toMutableList()
    if (log.tools.size > 8) lines.add(0, "…还有 ${log.tools.size - 8} 次工具调用")
    if (log.text.isNotEmpty() && result.isEmpty()) lines += clip(log.text, 400)
    val steps = "子代理用了 ${log.tools.size} 次工具\n" + lines.joinToString("\n")
    return if (result.isEmpty()) steps else steps + SUBAGENT_RESULT_MARKER + result
  }

  // Helpers

  private fun closeTurn(time: Instant) {
    turnEnds[items.size] = time
    val start = turnStartedAt
    val item = turnUserItem
    if (start != null && item != null) turnSeconds[item] = max(0, (Duration.between(start, time).toMillis() / 1000.0).roundToInt())
    turnStartedAt = null
    turnUserItem = null
  }

  private fun append(item: TimelineItem) {
    val position = index[item.id]
    if (position != null) {
      items[position] = item
      return
    }
    itemTimes[item.id] = eventTime
    index[item.id] = items.size
    items += item
  }

  private fun replace(id: String, item: TimelineItem) {
    val position = index[id] ?: return append(item)
    items[position] = item
  }

  private fun setDecision(decision: ApprovalDecision, interactionID: String) {
    val position = index[interactionID] ?: return
    when (val item = items[position]) {
      is TimelineItem.Approval -> items[position] = item.copy(decision = decision)
      is TimelineItem.Form -> items[position] = item.copy(decision = decision)
      else -> Unit
    }
  }

  private fun settleRunningTools(status: ToolStatus) {
    items.replaceAll {
      when {
        it is TimelineItem.Tool && it.status == ToolStatus.RUNNING -> it.copy(status = status)
        it is TimelineItem.Consult && it.running -> it.copy(running = false, error = if (status == ToolStatus.FAILED) "已停止" else it.error)
        else -> it
      }
    }
  }

  private fun mediaFile(record: JsonElement?): MediaFile? {
    val id = record["id"].string ?: return null
    return MediaFile(id, record["filename"].text ?: record["title"].text ?: id, record["content_type"].string ?: "")
  }

  /** Shows paths relative to the chat's folder. */
  fun relative(path: String): String {
    var shown = path.removePrefix("/private")
    val base = cwd.removePrefix("/private")
    if (base.isNotEmpty() && shown.startsWith("$base/")) return shown.drop(base.length + 1)
    if (home.isNotEmpty() && shown.startsWith("$home/")) shown = "~" + shown.drop(home.length)
    return shown
  }

  fun describeTool(tool: JsonElement?): Pair<ToolKind, String> {
    val name = tool["name"].string ?: "tool"
    val input = tool["input"]
    val path = (input["file_path"].string ?: input["path"].string ?: input["notebook_path"].string)?.let(::relative)
    return when (name) {
      "Bash", "shell", "exec_command", "local_shell", "BashOutput" -> {
        val command = abbreviateHome(unwrapShell(input["command"].string ?: input["cmd"].string ?: name), home)
        ToolKind.COMMAND to command.lines().first()
      }
      "Read", "NotebookRead" -> ToolKind.READ to (path ?: name)
      "Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "fileChange" -> ToolKind.EDIT to (path ?: "补丁")
      "Grep", "Glob", "LS" -> ToolKind.SEARCH to (input["pattern"].string ?: path ?: name)
      "WebFetch", "WebSearch", "web_search" -> ToolKind.WEB to (input["url"].string ?: input["query"].string ?: name)
      "TodoWrite" -> ToolKind.EDIT to "更新待办清单"
      "ImageGeneration" -> ToolKind.MCP to "生成图片"
      "Task", "Agent" -> ToolKind.MCP to (input["description"].string ?: "子代理")
      else ->
        when {
          isHyadexTool(name) -> {
            val helper = input["helper"].string ?: "helper"
            if (helper == "ask") ToolKind.MCP to "查看可以询问的聊天"
            else ToolKind.MCP to "Hyadex · $helper ${input["arguments"].array.mapNotNull { it.string }.take(2).joinToString(" ")}".trim()
          }
          name.startsWith("mcp__") -> ToolKind.MCP to name.split("_").filter { it.isNotEmpty() }.drop(1).joinToString(" · ")
          else -> ToolKind.MCP to name
        }
    }
  }

  /** (title, detail, reason) of an approval request. */
  fun describeInteraction(interaction: JsonElement?): Triple<String, String, String> {
    val method = interaction["method"].string ?: ""
    val params = interaction["params"]
    val reason = params["reason"].text ?: ""
    fun runIn() = reason.ifEmpty { "在 ${relative(params["cwd"].string ?: cwd)} 中运行" }
    if (method == "execCommandApproval") {
      return Triple("运行命令", abbreviateHome(unwrapShell(params["command"].array.mapNotNull { it.string }.joinToString(" ")), home), runIn())
    }
    if (method == "applyPatchApproval") {
      return Triple("修改文件", (params["fileChanges"].obj ?: emptyMap()).keys.sorted().joinToString("\n") { relative(it) }, reason)
    }
    if (method == "item/permissions/requestApproval") {
      val permissions = params["permissions"]
      val lines = mutableListOf<String>()
      if (permissions["network"]["enabled"].bool == true) lines += "访问网络"
      val files = permissions["fileSystem"]
      lines += files["read"].array.mapNotNull { it.string }.map { "读取 " + relative(it) }
      lines += files["write"].array.mapNotNull { it.string }.map { "写入 " + relative(it) }
      for (entry in files["entries"].array) {
        val path = entry["path"]
        val target = path["path"].string?.let(::relative) ?: path["pattern"].string ?: path["value"]["kind"].string ?: "?"
        val access = when (entry["access"].string) {
          "write" -> "写入"
          "deny" -> "禁止访问"
          else -> "读取"
        }
        lines += "$access $target"
      }
      return Triple("更多权限", lines.joinToString("\n"), reason)
    }
    if ("commandExecution" in method || params["kind"].string == "command") {
      return Triple("运行命令", abbreviateHome(unwrapShell(params["command"].string ?: ""), home), runIn())
    }
    if ("fileChange" in method) {
      val changes = params["changes"].array
      var detail = changes.mapNotNull { (it["file_path"].string ?: it["path"].string)?.let(::relative) }.joinToString("\n")
      val content = changes.singleOrNull()?.get("content").string
      if (!content.isNullOrEmpty()) detail += "\n\n" + clip(content, 1_200)
      if (detail.isEmpty()) detail = params["displayName"].string ?: ""
      return Triple("修改文件", detail, reason)
    }
    val name = params["displayName"].string ?: params["toolName"].string ?: method
    val input = params["toolInput"]
    val detail = unwrapShell(input["command"].string ?: input["file_path"].string ?: params["description"].string ?: name)
    return Triple("使用 $name", detail, reason.ifEmpty { params["description"].text ?: "" })
  }

  companion object {
    private const val SUBAGENT_RESULT_MARKER = "\n\n— 结果 —\n"

    /** (target, deep, question) of an `ask TITLE [--deep]` helper call; null for anything else, `ask list` included. */
    fun consultCall(tool: JsonElement?): Triple<String, Boolean, String>? {
      if (!isHyadexTool(tool["name"].string ?: "") || tool["input"]["helper"].string != "ask") return null
      val arguments = tool["input"]["arguments"].array.mapNotNull { it.string }
      if (arguments.firstOrNull() != "ask") return null
      var target: String? = null
      var question: String? = null
      var position = 1
      while (position < arguments.size) {
        val argument = arguments[position]
        if (argument == "--question" || argument == "--request-id") {
          if (argument == "--question" && position + 1 < arguments.size) question = arguments[position + 1]
          position += 2
          continue
        }
        if (argument.startsWith("--question=")) question = argument.removePrefix("--question=")
        if (!argument.startsWith("--")) {
          if (target == null) target = argument else if (question == null) question = argument
        }
        position++
      }
      val asked = question ?: tool["input"]["stdin"].string ?: ""
      return Triple(target ?: return null, "--deep" in arguments, asked.trim())
    }

    /**
     * (answer, error) from the helper's receipt: JSON, or plain text when it failed
     * early. Codex hands the receipt over as an MCP result, its text inside `content`.
     */
    fun consultReceipt(output: String, failed: Boolean): Pair<String?, String?> {
      var text = output.trim()
      var json = runCatching { dev.hyadex.android.api.JsonCodec.parseToJsonElement(text) }.getOrNull()
      if (json != null && json["ok"].bool == null && json["content"] is kotlinx.serialization.json.JsonArray) {
        text = json["content"].array.mapNotNull { it["text"].string }.joinToString("\n").trim()
        json = runCatching { dev.hyadex.android.api.JsonCodec.parseToJsonElement(text) }.getOrNull()
      }
      if (json == null || json["ok"].bool == null) return null to text.ifEmpty { if (failed) "询问失败" else null }
      return if (json["ok"].bool == true) json["answer"].string to null else null to (json["error"]["message"].string ?: json["error"].string ?: "询问失败")
    }

    fun clip(text: String, limit: Int = 4_000): String = if (text.length > limit) text.take(limit) + "\n…" else text

    fun abbreviateHome(text: String, home: String = ""): String = if (home.isEmpty()) text else text.replace("$home/", "~/")

    /** Codex wraps commands as `/bin/zsh -lc "…"`; show the command itself. */
    fun unwrapShell(command: String): String {
      for (prefix in listOf("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc ", "/bin/sh -c ")) {
        if (!command.startsWith(prefix)) continue
        var inner = command.drop(prefix.length)
        if (inner.length >= 2 && inner.first() == inner.last() && (inner.first() == '"' || inner.first() == '\'')) {
          inner = inner.drop(1).dropLast(1)
        }
        return inner.replace("\\\\", "\\")
      }
      return command
    }

    /** Hyadex's own helpers (jobs, publish, notify, ask…), reached through its provider tool. */
    fun isHyadexTool(name: String): Boolean = "_hyadex_internal_provider_" in name

    /** The server's home folder, from a chat's folder: `/Users/NAME` or `/home/NAME`. */
    fun homeOf(cwd: String): String {
      val parts = cwd.removePrefix("/private").split('/')
      return if (parts.size >= 3 && parts[1] in setOf("Users", "home")) "/${parts[1]}/${parts[2]}" else ""
    }
  }
}
