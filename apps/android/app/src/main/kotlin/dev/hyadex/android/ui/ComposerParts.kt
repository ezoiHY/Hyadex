package dev.hyadex.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.HelpOutline
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.AttachFile
import androidx.compose.material.icons.outlined.Flag
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.Forum
import androidx.compose.material.icons.outlined.Image
import androidx.compose.material.icons.outlined.MoreHoriz
import androidx.compose.material.icons.outlined.PauseCircle
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Warning
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.ChatMention
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.ConsultTarget
import dev.hyadex.android.core.ProviderCommand
import dev.hyadex.android.core.QueuedMessage
import dev.hyadex.android.core.SlashParser
import dev.hyadex.android.core.compactCount
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlin.math.roundToInt

/**
 * What the composer suggests while you type, as the Mac's: the agent's commands
 * and skills after "/" (with /goal and /btw), and other chats to ask after "@".
 */
@Composable
fun ComposerSuggestions(model: AppViewModel, text: String, onText: (String) -> Unit) {
  val tools = model.tools
  val partial = SlashParser.partialCommand(text)
  val mention = if (partial == null) ChatMention.partial(text) else null
  // Loaded the first time they are needed, then kept for the chat.
  LaunchedEffect(partial != null) { if (partial != null) tools.loadCommands() }
  LaunchedEffect(mention != null) { if (mention != null) tools.loadTargets() }
  val commands = when {
    partial == null -> emptyList()
    else -> {
      val own = builtIns(tools.goalSupport?.available == true)
      // The agent's own /goal is the same command: offered once, as the app's.
      own.filter { it.invocation.drop(1).startsWith(partial.lowercase()) } +
        tools.commands?.matches(partial, limit = 12).orEmpty().filter { command -> own.none { it.invocation == command.invocation } }
    }
  }
  val targets = mention?.let { ChatMention.matches(it, tools.targets.orEmpty()) }.orEmpty()
  AnimatedVisibility(commands.isNotEmpty() || targets.isNotEmpty(), enter = unfold, exit = fold) {
    Column(Modifier.fillMaxWidth().heightIn(max = 232.dp).clip(RoundedCornerShape(Hyadex.cornerRadius)).card().verticalScroll(rememberScrollState())) {
      for (command in commands) CommandRow(command) { onText(command.invocation + " ") }
      for (target in targets) TargetRow(target) { onText(ChatMention.complete(text, target)) }
    }
  }
}

/** /goal and /btw: the app's own commands, offered beside the agent's. */
private fun builtIns(goals: Boolean): List<ProviderCommand> =
  listOfNotNull(
    if (goals) ProviderCommand("hyadex-goal", "goal", "目标", "设定目标，代理会一直做到它达成", "/goal", skill = false) else null,
    ProviderCommand("hyadex-btw", "btw", "侧问", "问代理一句，不打断主对话", "/btw", skill = false),
  )

@Composable
private fun CommandRow(command: ProviderCommand, onPick: () -> Unit) {
  val colors = Hyadex.colors
  Row(Modifier.fillMaxWidth().clickable(onClick = onPick).padding(horizontal = 12.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
    Text(command.invocation, style = HyadexType.mono, color = colors.primary, maxLines = 1)
    Text(
      listOf(command.label.takeIf { it != command.name }, command.detail.takeIf { it.isNotEmpty() }).filterNotNull().joinToString(" · "),
      style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
    )
    if (command.skill) Tag("技能")
  }
}

@Composable
private fun TargetRow(target: ConsultTarget, onPick: () -> Unit) {
  val colors = Hyadex.colors
  Row(Modifier.fillMaxWidth().clickable(onClick = onPick).padding(horizontal = 12.dp, vertical = 9.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
    target.backend?.let { ProviderMark(it, 18.dp) }
    Text(target.title, style = HyadexType.secondary, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
    if (target.working) Tag("工作中")
  }
}

@Composable
fun Tag(text: String, tint: androidx.compose.ui.graphics.Color = Hyadex.colors.secondary) {
  Text(text, style = HyadexType.small, color = tint, modifier = Modifier.background(tint.copy(alpha = 0.12f), RoundedCornerShape(4.dp)).padding(horizontal = 5.dp, vertical = 2.dp))
}

/** Shown while a message opens with another chat's @: it goes to that chat, not this agent. */
@Composable
fun DirectAskBar(target: ConsultTarget, agent: AgentBackend, deep: Boolean, onDeep: (Boolean) -> Unit) {
  val colors = Hyadex.colors
  Column(
    Modifier.fillMaxWidth().background(colors.accent.copy(alpha = 0.08f), RoundedCornerShape(Hyadex.cornerRadius)).padding(horizontal = 12.dp, vertical = 8.dp),
    verticalArrangement = Arrangement.spacedBy(6.dp),
  ) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      Icon(Icons.Outlined.Forum, null, tint = colors.secondary, modifier = Modifier.size(16.dp))
      Text("直接问「${target.title}」", style = HyadexType.meta.copy(fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold), color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
      ModeChoice("基础", !deep) { onDeep(false) }
      ModeChoice("深度", deep, enabled = target.allowsDeep) { onDeep(true) }
    }
    Text(
      if (target.allowsDeep) "回答显示在这里，不发给 ${agent.displayName}。深度会让它的只读副本查看文件后回答，较慢。"
      else "回答显示在这里，不发给 ${agent.displayName}。「${target.title}」只接受基础提问。",
      style = HyadexType.small, color = colors.secondary,
    )
  }
}

@Composable
private fun ModeChoice(label: String, selected: Boolean, enabled: Boolean = true, onClick: () -> Unit) {
  val colors = Hyadex.colors
  Text(
    label,
    style = HyadexType.meta,
    color = if (!enabled) colors.tertiary else if (selected) colors.accent else colors.secondary,
    modifier = Modifier.clip(RoundedCornerShape(6.dp)).background(if (selected) colors.accent.copy(alpha = 0.14f) else androidx.compose.ui.graphics.Color.Transparent)
      .clickable(enabled = enabled, onClick = onClick).padding(horizontal = 10.dp, vertical = 5.dp),
  )
}

/**
 * How full the chat's context is, as the Mac's context ring: the number shows
 * from 70 %, red from 90 %. Tapping it offers to compact the context now.
 */
@Composable
fun ContextRing(model: AppViewModel, running: Boolean) {
  val colors = Hyadex.colors
  val tools = model.tools
  val usage = tools.context ?: return
  var open by remember { mutableStateOf(false) }
  val tint = when {
    usage.percent >= 90 -> colors.red
    usage.percent >= 70 -> colors.orange
    else -> colors.secondary
  }
  // Claude compacts with its own /compact command, when it has one; Codex natively.
  val canCompact = !running && (tools.backend == AgentBackend.CODEX || tools.commands?.command("/compact") != null)
  Box {
    Row(
      Modifier.clip(RoundedCornerShape(8.dp)).clickable {
        open = true
        tools.loadCommands()
      }.padding(horizontal = 4.dp, vertical = 6.dp).semantics { contentDescription = "上下文已用 ${usage.percent.roundToInt()}%" },
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
      Box(contentAlignment = Alignment.Center) {
        CircularProgressIndicator(progress = { 1f }, modifier = Modifier.size(15.dp), color = colors.bubble, strokeWidth = 2.dp)
        CircularProgressIndicator(progress = { (usage.percent / 100).toFloat() }, modifier = Modifier.size(15.dp), color = tint, strokeWidth = 2.dp, trackColor = androidx.compose.ui.graphics.Color.Transparent)
      }
      if (usage.percent >= 70) Text("${usage.percent.roundToInt()}%", style = HyadexType.small, color = tint)
    }
    DropdownMenu(open, onDismissRequest = { open = false }) {
      Text(
        "上下文 ${compactCount(usage.tokens)} / ${compactCount(usage.window)} · ${usage.percent.roundToInt()}%",
        style = HyadexType.meta, color = colors.secondary, modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
      )
      DropdownMenuItem(
        text = { Text(if (running) "运行中不能压缩" else "立即压缩上下文") },
        enabled = canCompact,
        onClick = {
          open = false
          model.compact()
        },
      )
    }
  }
}

/** The composer's "+": photos, or any file the agent should read. */
@Composable
fun AttachButton(onImages: () -> Unit, onFiles: () -> Unit) {
  var open by remember { mutableStateOf(false) }
  Box {
    IconButton(onClick = { open = true }) { Icon(Icons.Outlined.Add, "添加附件", tint = Hyadex.colors.secondary) }
    DropdownMenu(open, onDismissRequest = { open = false }) {
      DropdownMenuItem(text = { Text("图片") }, leadingIcon = { Icon(Icons.Outlined.Image, null) }, onClick = { open = false; onImages() })
      DropdownMenuItem(text = { Text("文件") }, leadingIcon = { Icon(Icons.Outlined.AttachFile, null) }, onClick = { open = false; onFiles() })
    }
  }
}

/** The folder the chat works in, above the composer; tapping it copies the path. */
@Composable
fun FolderLine(chat: ChatSummary, shown: String, onCopied: () -> Unit) {
  val colors = Hyadex.colors
  val clipboard = LocalClipboardManager.current
  var open by remember { mutableStateOf(false) }
  Box {
    Row(
      Modifier.clip(RoundedCornerShape(6.dp)).clickable { open = true }.padding(horizontal = 6.dp, vertical = 2.dp),
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
      Icon(Icons.Outlined.Folder, null, tint = colors.secondary, modifier = Modifier.size(15.dp))
      Text(shown, style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.StartEllipsis)
    }
    DropdownMenu(open, onDismissRequest = { open = false }) {
      Text(chat.cwd, style = HyadexType.mono, color = colors.secondary, modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp).widthIn(max = 300.dp))
      DropdownMenuItem(text = { Text("拷贝路径") }, onClick = {
        open = false
        clipboard.setText(AnnotatedString(chat.cwd))
        onCopied()
      })
    }
  }
}

/**
 * Messages waiting behind the running turn, as the Mac's queue strip: each can be
 * sent now, taken back to edit, moved, or withdrawn. After a Stop they wait for 继续发送.
 */
@Composable
fun QueueStrip(queued: List<QueuedMessage>, model: AppViewModel) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(top = 6.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
    if (queued.any { it.hold == QueuedMessage.Hold.STOPPED }) {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Text("停止后，这些消息已暂停", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.weight(1f))
        TextButton(onClick = model::resumeQueue) { Text("继续发送") }
      }
    }
    queued.forEachIndexed { index, message ->
      var menu by remember(message.id) { mutableStateOf(false) }
      Row(Modifier.fillMaxWidth().card().padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        val (icon, label, tint) = when (message.hold) {
          QueuedMessage.Hold.STOPPED -> Triple(Icons.Outlined.PauseCircle, "已暂停：", colors.secondary)
          QueuedMessage.Hold.DELIVERY_UNCERTAIN -> Triple(Icons.Outlined.Warning, "投递未确认：", colors.orange)
          null -> Triple(Icons.Outlined.Schedule, "排队：", colors.secondary)
        }
        Icon(icon, null, tint = tint, modifier = Modifier.size(15.dp))
        Text(label + message.text, style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        Box {
          IconButton(onClick = { menu = true }) { Icon(Icons.Outlined.MoreHoriz, "更多", tint = colors.secondary, modifier = Modifier.size(18.dp)) }
          DropdownMenu(menu, onDismissRequest = { menu = false }) {
            DropdownMenuItem(text = { Text("立即发送") }, onClick = { menu = false; model.runQueuedNow(message.id) })
            DropdownMenuItem(text = { Text("编辑") }, onClick = { menu = false; model.editQueued(message) })
            if (index > 0) DropdownMenuItem(text = { Text("上移一位") }, onClick = { menu = false; model.moveQueued(message.id, up = true) })
            if (index < queued.size - 1) DropdownMenuItem(text = { Text("下移一位") }, onClick = { menu = false; model.moveQueued(message.id, up = false) })
            HorizontalDivider(color = colors.separator, thickness = 0.5.dp)
            DropdownMenuItem(text = { Text("撤回", color = colors.red) }, leadingIcon = { Icon(Icons.Filled.Close, null, tint = colors.red) }, onClick = { menu = false; model.removeQueued(message.id) })
          }
        }
      }
    }
  }
}

/** The chat's open goal above the composer, as the Mac's goal bar: status, progress and its controls. */
@Composable
fun GoalBar(model: AppViewModel, onEdit: () -> Unit) {
  val colors = Hyadex.colors
  val tools = model.tools
  val goal = tools.goal?.takeIf { it.isOpen } ?: return
  val metrics = listOfNotNull(
    goal.iterations?.let { "已检查 $it 次" },
    goal.tokensUsed?.let { used -> "tokens ${compactCount(used)}" + (goal.tokenBudget?.let { " / ${compactCount(it)}" } ?: "") },
    goal.secondsUsed?.let { used -> "用时 ${dev.hyadex.android.core.formatElapsed(used.toLong())}" + (goal.timeBudgetSeconds?.let { " / ${dev.hyadex.android.core.formatElapsed(it.toLong())}" } ?: "") },
  )
  Row(
    Modifier.fillMaxWidth().background(colors.accent.copy(alpha = 0.08f), RoundedCornerShape(Hyadex.cornerRadius)).padding(start = 12.dp, top = 6.dp, bottom = 6.dp, end = 4.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    Icon(Icons.Outlined.Flag, null, tint = colors.accent, modifier = Modifier.size(16.dp))
    Column(Modifier.weight(1f)) {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Tag(goal.statusLabel, colors.accent)
        Text(goal.text, style = HyadexType.meta, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis)
      }
      if (metrics.isNotEmpty() || goal.reason != null) {
        Text((listOfNotNull(goal.reason) + metrics).joinToString(" · "), style = HyadexType.small, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis)
      }
    }
    if (tools.goalBusy) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 1.5.dp)
    var menu by remember { mutableStateOf(false) }
    Box {
      IconButton(onClick = { menu = true }, enabled = !tools.goalBusy) { Icon(Icons.Outlined.MoreHoriz, "目标操作", tint = colors.secondary, modifier = Modifier.size(18.dp)) }
      DropdownMenu(menu, onDismissRequest = { menu = false }) {
        if (goal.canPause) DropdownMenuItem(text = { Text("暂停") }, onClick = { menu = false; tools.setGoalPaused(true) })
        if (goal.canResume) DropdownMenuItem(text = { Text("继续") }, onClick = { menu = false; tools.setGoalPaused(false) })
        DropdownMenuItem(text = { Text("修改…") }, onClick = { menu = false; onEdit() })
        DropdownMenuItem(text = { Text("清除目标", color = colors.red) }, onClick = { menu = false; tools.clearGoal() })
      }
    }
  }
}

/** Shown where goals cannot be set, with why. */
@Composable
fun UnavailableNote(text: String) {
  Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
    Icon(Icons.AutoMirrored.Outlined.HelpOutline, null, tint = Hyadex.colors.tertiary, modifier = Modifier.size(15.dp))
    Text(text, style = HyadexType.meta, color = Hyadex.colors.secondary)
  }
}
