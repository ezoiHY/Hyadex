package dev.hyadex.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PanTool
import androidx.compose.material.icons.outlined.Block
import androidx.compose.material.icons.outlined.Checklist
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.Edit
import androidx.compose.material.icons.outlined.Extension
import androidx.compose.material.icons.outlined.InsertDriveFile
import androidx.compose.material.icons.outlined.Lightbulb
import androidx.compose.material.icons.outlined.PanTool
import androidx.compose.material.icons.outlined.Public
import androidx.compose.material.icons.outlined.QuestionAnswer
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.Terminal
import androidx.compose.material.icons.outlined.VerifiedUser
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil3.ImageLoader
import coil3.compose.AsyncImage
import dev.hyadex.android.core.ApprovalChoice
import dev.hyadex.android.core.ApprovalDecision
import dev.hyadex.android.core.Author
import dev.hyadex.android.core.FileChangeSet
import dev.hyadex.android.core.InteractionReply
import dev.hyadex.android.core.LiveActivity
import dev.hyadex.android.core.MediaFile
import dev.hyadex.android.core.PendingConsult
import dev.hyadex.android.core.TimelineItem
import dev.hyadex.android.core.TimelineRow
import dev.hyadex.android.core.ToolKind
import dev.hyadex.android.core.ToolStatus
import dev.hyadex.android.core.processSummary
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** What a timeline row needs from the screen: answering cards and showing files. */
class TimelineActions(
  val onReply: (String, InteractionReply) -> Unit,
  val onConsultDecision: (String, String) -> Unit,
  val fileUrl: (String) -> String?,
  val imageLoader: ImageLoader?,
  val onOpenImage: (String) -> Unit,
)

@Composable
fun TimelineRowView(row: TimelineRow, activity: LiveActivity?, actions: TimelineActions) {
  when (row) {
    is TimelineRow.Item -> TimelineItemView(row.item, actions)
    is TimelineRow.Process -> ProcessRow(row, if (row.live) activity else null, actions)
    TimelineRow.Activity -> activity?.let { ActivityLine(it) }
    is TimelineRow.ConsultApproval -> ConsultApprovalCard(row.consult) { actions.onConsultDecision(row.consult.id, it) }
  }
}

@Composable
fun TimelineItemView(item: TimelineItem, actions: TimelineActions) {
  when (item) {
    is TimelineItem.Message ->
      when (item.author.kind) {
        Author.Kind.USER -> UserBubble(item.text)
        Author.Kind.AGENT -> AgentMessage(item.text, item.streaming)
        else -> Text(item.text, style = HyadexType.secondary, color = Hyadex.colors.secondary)
      }
    is TimelineItem.Thinking -> ThinkingRow(item)
    is TimelineItem.Tool -> ToolRow(item)
    is TimelineItem.Changes -> ChangesCard(item.set)
    is TimelineItem.Approval -> ApprovalCard(item) { actions.onReply(item.id, it) }
    is TimelineItem.Form -> FormCard(item) { actions.onReply(item.id, it) }
    is TimelineItem.Media -> MediaGroup(item.files, item.author.kind == Author.Kind.USER, actions)
    is TimelineItem.Divider -> DividerRow(item.text)
    is TimelineItem.Consult -> ConsultCard(item)
  }
}

/** A question to another chat: who was asked, then the answer (or why there is none). */
@Composable
private fun ConsultCard(item: TimelineItem.Consult) {
  val colors = Hyadex.colors
  var expanded by rememberSaveable(item.id) { mutableStateOf(false) }
  Column(Modifier.fillMaxWidth().card().clickable(enabled = !item.running) { expanded = !expanded }.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
      Icon(Icons.Outlined.QuestionAnswer, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
      // The title takes the room it needs; the badge follows it, the spinner sits at the end.
      Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
        Text(
          (if (item.byUser) "你问「" else "问「") + item.target + "」", style = HyadexType.secondary.copy(fontWeight = FontWeight.Medium),
          color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false),
        )
        if (item.deep) {
          Text("深度", style = HyadexType.small, color = colors.accent, modifier = Modifier.background(colors.accent.copy(alpha = 0.12f), RoundedCornerShape(4.dp)).padding(horizontal = 5.dp, vertical = 1.dp))
        }
      }
      if (item.running) CircularProgressIndicator(Modifier.size(12.dp), strokeWidth = 1.5.dp, color = colors.secondary)
    }
    if (item.question.isNotEmpty()) Text(item.question, style = HyadexType.meta, color = colors.secondary, maxLines = if (expanded) Int.MAX_VALUE else 2, overflow = TextOverflow.Ellipsis)
    when {
      item.running -> Text("正在询问…", style = HyadexType.meta, color = colors.tertiary)
      // The other agent answers in Markdown: a clean preview folded, the full answer unfolded.
      item.answer != null ->
        if (expanded) MessageText(item.answer)
        else Text(plainMarkdown(item.answer), style = HyadexType.secondary, color = colors.primary, maxLines = 4, overflow = TextOverflow.Ellipsis)
      item.error != null -> Text(item.error, style = HyadexType.meta, color = colors.red)
    }
  }
}

/** Markdown as plain lines, for a short preview: emphasis, code marks and heading marks removed. */
private fun plainMarkdown(text: String): String =
  text.lines().joinToString("\n") { line -> line.replace("**", "").replace("__", "").replace("`", "").trimStart('#', ' ').let { if (line.startsWith("#")) it else it.ifEmpty { line } } }
    .replace(Regex("""\n{3,}"""), "\n\n").trim()

/** A deep question waits for the user: a read-only copy of the other chat would investigate its folder. */
@Composable
private fun ConsultApprovalCard(consult: PendingConsult, onDecide: (String) -> Unit) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth().card(colors.orange).padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
      Icon(Icons.Filled.PanTool, null, tint = colors.orange, modifier = Modifier.size(16.dp))
      Text("深度询问「${consult.target}」", style = HyadexType.headline, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
      Text("等待你的决定", style = HyadexType.meta, color = colors.orange)
    }
    Text("会让那个聊天的只读副本查看它的文件夹来回答。", style = HyadexType.meta, color = colors.secondary)
    if (consult.question.isNotEmpty()) CodeBlock(consult.question, wraps = true)
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(onClick = { onDecide("deny") }, modifier = Modifier.weight(1f)) { Text("拒绝", maxLines = 1) }
      Button(
        onClick = { onDecide("allow") },
        colors = ButtonDefaults.buttonColors(containerColor = colors.orange, contentColor = androidx.compose.ui.graphics.Color.White),
        modifier = Modifier.weight(1f),
      ) { Text("允许", maxLines = 1) }
    }
    TextButton(onClick = { onDecide("always") }, modifier = Modifier.align(Alignment.End)) { Text("这个聊天以后都允许") }
  }
}

/** What the agent is doing: the provider's mark at work, "正在思考" with the elapsed time, and the latest thought. */
@Composable
fun ActivityLine(activity: LiveActivity, expanded: Boolean? = null) {
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
      Box(Modifier.width(16.dp), contentAlignment = Alignment.Center) { ActivityMark(activity) }
      ShimmerText(activity.label, active = activity.kind != LiveActivity.Kind.WAITING)
      if (activity.kind == LiveActivity.Kind.TOOL && activity.detail != null) {
        Text(
          activity.detail, style = HyadexType.mono, color = colors.secondary, maxLines = 1, overflow = TextOverflow.MiddleEllipsis,
          modifier = Modifier.weight(1f, fill = false),
        )
      }
      Elapsed(activity.since)
      if (expanded != null) Chevron(expanded, Modifier.size(18.dp))
    }
    if (activity.kind == LiveActivity.Kind.THINKING && activity.detail != null) {
      Text(
        activity.detail, style = HyadexType.meta, color = colors.secondary, maxLines = 2, overflow = TextOverflow.StartEllipsis,
        modifier = Modifier.padding(start = 23.dp),
      )
    }
  }
}

/** Claude's spark turning, Codex's cloud breathing; otherwise a symbol for the step. */
@Composable
private fun ActivityMark(activity: LiveActivity) {
  val backend = activity.backend
  when {
    backend != null && (activity.kind == LiveActivity.Kind.THINKING || activity.kind == LiveActivity.Kind.STARTING) ->
      ProviderMark(backend, 14.dp, if (backend.raw == "claude") Modifier.turning() else Modifier.breathing())
    activity.kind == LiveActivity.Kind.TOOL -> Icon(Icons.Outlined.Settings, null, tint = Hyadex.colors.secondary, modifier = Modifier.size(15.dp).breathing())
    activity.kind == LiveActivity.Kind.WAITING -> Icon(Icons.Outlined.PanTool, null, tint = Hyadex.colors.secondary, modifier = Modifier.size(15.dp))
    else -> CircularProgressIndicator(Modifier.size(12.dp), strokeWidth = 1.5.dp, color = Hyadex.colors.secondary)
  }
}

/** Claude's spark turning; the value is read while drawing, so nothing recomposes each frame. */
@Composable
private fun Modifier.turning(): Modifier {
  val turn = rememberInfiniteTransition(label = "turn").animateFloat(0f, 360f, infiniteRepeatable(tween(7_200, easing = LinearEasing)), label = "turn")
  return graphicsLayer { rotationZ = turn.value }
}

/** Codex's cloud (and a running tool) breathing, read while drawing. */
@Composable
private fun Modifier.breathing(): Modifier {
  val breath = rememberInfiniteTransition(label = "breath").animateFloat(0.88f, 1f, infiniteRepeatable(tween(1_000), RepeatMode.Reverse), label = "breath")
  return graphicsLayer {
    scaleX = breath.value
    scaleY = breath.value
  }
}

/** One run of work collapsed to a summary ("已处理 12 秒 · 运行 2 条命令"); while running, headed by the live status. */
@Composable
private fun ProcessRow(row: TimelineRow.Process, live: LiveActivity?, actions: TimelineActions) {
  var expanded by rememberSaveable(row.key) { mutableStateOf(false) }
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Row(
      Modifier.fillMaxWidth().clickable { expanded = !expanded }.padding(vertical = 2.dp),
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
      if (live != null) {
        ActivityLine(live, expanded)
      } else {
        Icon(Icons.Outlined.Checklist, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
        Text(processSummary(row.steps, row.seconds, row.inProgress), style = HyadexType.secondary, color = colors.secondary, modifier = Modifier.weight(1f, fill = false))
        Chevron(expanded, Modifier.size(18.dp))
      }
    }
    AnimatedVisibility(expanded, enter = unfold, exit = fold) {
      Row(Modifier.height(IntrinsicSize.Min)) {
        Box(Modifier.width(2.dp).fillMaxHeight().background(colors.secondary.copy(alpha = 0.25f)))
        Column(Modifier.padding(start = 10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
          for (step in row.steps) TimelineItemView(step, actions)
        }
      }
    }
  }
}

@Composable
private fun UserBubble(text: String) {
  Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
    Spacer(Modifier.width(48.dp))
    SelectionContainer {
      // As in the native apps, your own message shows as you typed it.
      Text(
        text,
        style = HyadexType.body,
        color = Hyadex.colors.primary,
        modifier = Modifier.clip(RoundedCornerShape(16.dp)).background(Hyadex.colors.bubble).padding(horizontal = 13.dp, vertical = 9.dp),
      )
    }
  }
}

@Composable
private fun AgentMessage(text: String, streaming: Boolean) {
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
    if (text.isEmpty() && streaming) {
      Text("正在回复…", style = HyadexType.body, color = colors.secondary)
    } else if (streaming) {
      StreamingMessageText(text)
    } else {
      MessageText(text)
    }
    if (!streaming && text.isNotEmpty()) CopyButton(text)
  }
}

/** Copies the whole answer as the agent wrote it (Markdown). */
@Composable
private fun CopyButton(text: String) {
  val clipboard = LocalClipboardManager.current
  val scope = rememberCoroutineScope()
  var copied by remember { mutableStateOf(false) }
  // 48 dp to touch; drawn small, and pulled up so the answer keeps its rhythm.
  IconButton(
    onClick = {
      clipboard.setText(AnnotatedString(text))
      copied = true
      scope.launch {
        delay(1_500)
        copied = false
      }
    },
    modifier = Modifier.offset(x = (-12).dp, y = (-8).dp),
  ) {
    Icon(if (copied) Icons.Filled.Check else Icons.Outlined.ContentCopy, contentDescription = if (copied) "已拷贝" else "拷贝回答", tint = Hyadex.colors.secondary, modifier = Modifier.size(16.dp))
  }
}

@Composable
private fun ThinkingRow(item: TimelineItem.Thinking) {
  var expanded by rememberSaveable(item.id) { mutableStateOf(false) }
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Row(Modifier.clickable { expanded = !expanded }, verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
      Icon(Icons.Outlined.Lightbulb, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
      Text(if (item.seconds > 0) "思考了 ${item.seconds} 秒" else "思考过程", style = HyadexType.secondary, color = colors.secondary)
      Chevron(expanded, Modifier.size(18.dp))
    }
    AnimatedVisibility(expanded, enter = unfold, exit = fold) {
      Row(Modifier.height(IntrinsicSize.Min)) {
        Box(Modifier.width(2.dp).fillMaxHeight().background(colors.secondary.copy(alpha = 0.3f)))
        SelectionContainer { Text(item.text, style = HyadexType.secondary, color = colors.secondary, modifier = Modifier.padding(start = 10.dp)) }
      }
    }
  }
}

private val ToolKind.icon: ImageVector
  get() =
    when (this) {
      ToolKind.COMMAND -> Icons.Outlined.Terminal
      ToolKind.READ -> Icons.Outlined.Description
      ToolKind.EDIT -> Icons.Outlined.Edit
      ToolKind.SEARCH -> Icons.Outlined.Search
      ToolKind.MCP -> Icons.Outlined.Extension
      ToolKind.WEB -> Icons.Outlined.Public
    }

@Composable
private fun ToolRow(item: TimelineItem.Tool) {
  var expanded by rememberSaveable(item.id) { mutableStateOf(false) }
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Row(
      Modifier.fillMaxWidth().clickable(enabled = item.output.isNotEmpty()) { expanded = !expanded },
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(7.dp),
    ) {
      Icon(item.kind.icon, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
      Text(item.kind.verb, style = HyadexType.secondary, color = colors.secondary)
      Text(
        item.title,
        style = if (item.kind == ToolKind.COMMAND) HyadexType.mono.copy(fontSize = 14.sp) else HyadexType.secondary,
        color = colors.primary, maxLines = 1, overflow = TextOverflow.MiddleEllipsis, modifier = Modifier.weight(1f),
      )
      when (item.status) {
        ToolStatus.RUNNING -> {
          CircularProgressIndicator(Modifier.size(11.dp), strokeWidth = 1.5.dp, color = colors.secondary)
          Text("${item.seconds.toInt()} 秒", style = HyadexType.meta, color = colors.secondary)
        }
        ToolStatus.SUCCEEDED -> {
          if (item.seconds >= 1) Text("%.1f 秒".format(item.seconds), style = HyadexType.meta, color = colors.tertiary)
          Icon(Icons.Filled.Check, "成功", tint = colors.green, modifier = Modifier.size(14.dp))
        }
        ToolStatus.FAILED -> Icon(Icons.Filled.Close, "失败", tint = colors.red, modifier = Modifier.size(14.dp))
      }
      if (item.output.isNotEmpty()) Chevron(expanded, Modifier.size(18.dp))
    }
    AnimatedVisibility(expanded, enter = unfold, exit = fold) { CodeBlock(item.output) }
  }
}

@Composable
private fun DiffStat(added: Int, removed: Int) {
  Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
    Text("+$added", style = HyadexType.meta, color = Hyadex.colors.green)
    Text("−$removed", style = HyadexType.meta, color = Hyadex.colors.red)
  }
}

@Composable
private fun ChangesCard(set: FileChangeSet) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth().card()) {
    Row(Modifier.padding(horizontal = 12.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
      Icon(Icons.Outlined.InsertDriveFile, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
      Text("改动了 ${set.files.size} 个文件", style = HyadexType.secondary.copy(fontWeight = FontWeight.Medium), color = colors.primary)
      DiffStat(set.added, set.removed)
    }
    HorizontalDivider(color = colors.separator, thickness = 0.5.dp)
    Column(Modifier.padding(vertical = 3.dp)) {
      for (file in set.files) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          Text(file.path, style = HyadexType.mono, color = colors.primary, maxLines = 1, overflow = TextOverflow.StartEllipsis, modifier = Modifier.weight(1f))
          DiffStat(file.added, file.removed)
        }
      }
    }
  }
}

/** An agent is blocked until the user decides: tinted, states the exact command, offers the provider's own answers. */
@Composable
private fun ApprovalCard(item: TimelineItem.Approval, onReply: (InteractionReply) -> Unit) {
  val colors = Hyadex.colors
  if (item.decision != ApprovalDecision.Pending) {
    val denied = item.decision == ApprovalDecision.Denied
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
      Icon(if (denied) Icons.Outlined.Block else Icons.Outlined.VerifiedUser, null, tint = if (denied) colors.red else colors.green, modifier = Modifier.size(16.dp))
      Text(decisionLabel(item.decision), style = HyadexType.secondary, color = colors.primary)
      Text(item.command, style = HyadexType.mono, color = colors.secondary, maxLines = 1, overflow = TextOverflow.MiddleEllipsis)
    }
    return
  }
  val deny = item.choices.firstOrNull { it.role == ApprovalChoice.Role.DENY }
  val allow = item.choices.firstOrNull { it.role == ApprovalChoice.Role.ALLOW }
  val session = item.choices.firstOrNull { it.reply.outcome == ApprovalDecision.AllowedForSession }
  val more = item.choices.filter { it != deny && it != allow && it != session }
  var menu by remember { mutableStateOf(false) }
  Column(Modifier.fillMaxWidth().card(colors.orange).padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
      Icon(Icons.Filled.PanTool, null, tint = colors.orange, modifier = Modifier.size(16.dp))
      Text("请求${item.title}", style = HyadexType.headline, color = colors.primary, modifier = Modifier.weight(1f))
      Text("等待你的决定", style = HyadexType.meta, color = colors.orange)
    }
    if (item.reason.isNotEmpty()) Text(item.reason, style = HyadexType.secondary, color = colors.secondary)
    if (item.command.isNotEmpty()) CodeBlock(item.command, wraps = true)
    // A phone's width: refuse and allow side by side (allow on the right, as on the Mac), the rest below.
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      deny?.let { OutlinedButton(onClick = { onReply(it.reply) }, modifier = Modifier.weight(1f)) { Text(it.label, maxLines = 1) } }
      allow?.let {
        Button(
          onClick = { onReply(it.reply) },
          colors = ButtonDefaults.buttonColors(containerColor = colors.orange, contentColor = androidx.compose.ui.graphics.Color.White),
          modifier = Modifier.weight(1f),
        ) {
          Text(it.label, maxLines = 1)
        }
      }
    }
    if (session != null || more.isNotEmpty()) {
      Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        session?.let { OutlinedButton(onClick = { onReply(it.reply) }, modifier = Modifier.weight(1f)) { Text(it.label, maxLines = 1, overflow = TextOverflow.Ellipsis) } }
        if (more.isNotEmpty()) {
          Box {
            TextButton(onClick = { menu = true }) { Text("更多选项") }
            DropdownMenu(menu, onDismissRequest = { menu = false }) {
              for (choice in more) DropdownMenuItem(text = { Text(choice.label) }, onClick = { menu = false; onReply(choice.reply) })
            }
          }
        }
      }
    }
  }
}

fun decisionLabel(decision: ApprovalDecision): String =
  when (decision) {
    ApprovalDecision.Denied -> "已拒绝"
    ApprovalDecision.AllowedForSession -> "已允许（本会话）"
    ApprovalDecision.AllowedAlways -> "已允许（以后不再询问）"
    ApprovalDecision.Resolved -> "已处理"
    ApprovalDecision.Skipped -> "已跳过"
    is ApprovalDecision.Answered -> "已回答"
    else -> "已允许"
  }

@Composable
private fun MediaGroup(files: List<MediaFile>, alignEnd: Boolean, actions: TimelineActions) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth(), horizontalAlignment = if (alignEnd) Alignment.End else Alignment.Start, verticalArrangement = Arrangement.spacedBy(8.dp)) {
    for (file in files) {
      val url = actions.fileUrl(file.id)
      if (file.isImage && url != null && actions.imageLoader != null) {
        AsyncImage(
          model = url,
          contentDescription = "图片 ${file.name}",
          imageLoader = actions.imageLoader,
          contentScale = ContentScale.Fit,
          modifier = Modifier.sizeIn(maxWidth = 300.dp, maxHeight = 300.dp).widthIn(min = 80.dp).heightIn(min = 60.dp)
            .clip(RoundedCornerShape(10.dp)).background(colors.card).clickable { actions.onOpenImage(url) },
        )
      } else {
        Row(Modifier.card().padding(horizontal = 10.dp, vertical = 7.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
          Icon(Icons.Outlined.InsertDriveFile, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
          Text(file.name, style = HyadexType.meta, color = colors.primary, maxLines = 1, overflow = TextOverflow.MiddleEllipsis)
        }
      }
    }
  }
}

@Composable
private fun DividerRow(text: String) {
  val colors = Hyadex.colors
  Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
    HorizontalDivider(Modifier.weight(1f), color = colors.separator, thickness = 0.5.dp)
    Text(text, style = HyadexType.meta, color = colors.tertiary, modifier = Modifier.widthIn(max = 260.dp))
    HorizontalDivider(Modifier.weight(1f), color = colors.separator, thickness = 0.5.dp)
  }
}
