package dev.hyadex.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.outlined.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.core.ActivityState
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.ChatSettings
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.FileChange
import dev.hyadex.android.core.McpServer
import dev.hyadex.android.core.SideChat
import dev.hyadex.android.core.compactCount
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import dev.hyadex.android.ui.theme.tint
import kotlinx.coroutines.delay

/**
 * A bottom sheet that opens fully, in the app's colours. The sheet keeps its
 * content clear of the navigation bar and the keyboard itself; padding the content
 * for them again pushes a tall sheet off the screen while typing.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Sheet(onDismiss: () -> Unit, content: @Composable () -> Unit) {
  ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true), containerColor = Hyadex.colors.sidebar) {
    content()
  }
}

/**
 * A screen over the chat for typing (settings, a goal, side questions). A bottom
 * sheet loses its place when the keyboard opens; a pane keeps above it.
 */
@Composable
private fun Pane(title: String, onBack: () -> Unit, actions: @Composable () -> Unit = {}, content: @Composable ColumnScope.() -> Unit) {
  Column(Modifier.fillMaxSize().background(Hyadex.colors.sidebar).imePadding()) {
    PaneTopBar(title, onBack, actions)
    Column(Modifier.weight(1f).navigationBarsPadding(), content = content)
  }
}

/**
 * The chat's details, as the Mac's ⓘ popover: its agent and settings, tokens,
 * the commands it offers, its MCP servers and subagents, and the way to 高级设置.
 */
@Composable
fun ChatDetailsSheet(model: AppViewModel, chat: ChatSummary, onDismiss: () -> Unit, onAdvanced: () -> Unit) {
  val colors = Hyadex.colors
  val tools = model.tools
  LaunchedEffect(Unit) {
    tools.loadCommands()
    tools.refreshMcp()
  }
  // Subagents are watched while the sheet is open: often while one runs, rarely otherwise.
  LaunchedEffect(Unit) {
    while (true) {
      tools.refreshSubagents()
      delay(if (tools.subagents.any { it.state == ActivityState.RUNNING }) 4_000 else 30_000)
    }
  }
  Sheet(onDismiss) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp).padding(bottom = 24.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.padding(horizontal = 4.dp)) {
        ProviderMark(chat.backend, 24.dp)
        Text(chat.title, style = HyadexType.title, color = colors.primary, maxLines = 2, overflow = TextOverflow.Ellipsis)
      }
      val catalog = model.catalogs[chat.backend]
      SettingsSection("概况") {
        SettingRow("代理", detail = chat.backend.displayName)
        SettingsDivider()
        SettingRow("模型", detail = catalog?.models?.firstOrNull { it.value == chat.model }?.label ?: chat.model.ifEmpty { "默认" })
        SettingsDivider()
        SettingRow("推理强度", detail = catalog?.efforts(chat.model)?.firstOrNull { it.value == chat.effort }?.label ?: chat.effort.ifEmpty { "默认" })
        SettingsDivider()
        SettingRow("权限", detail = chat.permission)
        SettingsDivider()
        SettingRow("状态", detail = stateLabel(chat.state))
        SettingsDivider()
        val clipboard = LocalClipboardManager.current
        SettingRow("文件夹", detail = chat.cwd, path = true, onClick = {
          clipboard.setText(AnnotatedString(chat.cwd))
          model.tell("已拷贝路径")
        })
        SettingsDivider()
        SettingRow("命令与技能", detail = tools.commands?.let { "${it.commands.size} 个" } ?: "正在读取…")
        chat.tokens?.let { tokens ->
          SettingsDivider()
          SettingRow("Tokens", detail = compactCount(tokens.total) + (tokens.cached?.let { " · 缓存 ${compactCount(it)}" } ?: ""))
        }
      }
      McpSection(model)
      if (tools.subagents.isNotEmpty()) SubagentSection(model)
      SettingsSection("聊天") {
        SettingRow("高级设置…", chevron = true, onClick = onAdvanced)
      }
    }
  }
}

private fun stateLabel(state: ActivityState): String =
  when (state) {
    ActivityState.RUNNING -> "运行中"
    ActivityState.NEEDS_ACTION -> "等待你处理"
    ActivityState.QUEUED -> "排队中"
    ActivityState.FAILED -> "出错"
    ActivityState.DONE -> "已完成"
    ActivityState.PAUSED -> "已暂停"
    ActivityState.IDLE -> "空闲"
  }

@Composable
private fun McpSection(model: AppViewModel) {
  val colors = Hyadex.colors
  val tools = model.tools
  val mcp = tools.mcp
  Row(Modifier.fillMaxWidth().padding(start = 14.dp, top = 20.dp), verticalAlignment = Alignment.CenterVertically) {
    Text("MCP 服务器", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.weight(1f))
    if (tools.mcpBusy) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 1.5.dp)
    else IconButton(onClick = tools::refreshMcp) { Icon(Icons.Outlined.Refresh, "刷新", tint = colors.secondary, modifier = Modifier.size(18.dp)) }
  }
  Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(Hyadex.cornerRadius)).card()) {
    when {
      mcp == null -> SettingRow("正在读取…")
      !mcp.available -> Text(mcp.reason ?: "这个聊天的代理现在读不到 MCP 状态。", style = HyadexType.secondary, color = colors.secondary, modifier = Modifier.padding(14.dp))
      mcp.servers.isEmpty() -> SettingRow("没有加载 MCP 服务器")
      else -> mcp.servers.forEachIndexed { index, server ->
        if (index > 0) SettingsDivider()
        McpRow(server, controllable = mcp.controllable) { action -> tools.controlMcp(action, server) }
      }
    }
    val failed = mcp?.servers?.count { it.status == McpServer.Status.FAILED } ?: 0
    if (mcp?.controllable == true && failed > 0) {
      SettingsDivider()
      SettingRow("重连失败的（$failed）", tint = colors.accent, onClick = { tools.controlMcp("reconnect_all", null) })
    }
  }
}

@Composable
private fun McpRow(server: McpServer, controllable: Boolean, onAction: (String) -> Unit) {
  val colors = Hyadex.colors
  var menu by remember { mutableStateOf(false) }
  Box {
    Row(
      Modifier.fillMaxWidth().clickable(enabled = controllable) { menu = true }.padding(horizontal = 14.dp, vertical = 10.dp),
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
      Box(Modifier.size(8.dp).clip(CircleShape).background(server.activity.tint(colors)))
      Column(Modifier.weight(1f)) {
        Text(server.name, style = HyadexType.secondary, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis)
        val detail = listOfNotNull(server.statusLabel, server.toolCount?.let { "$it 个工具" }, server.scopeLabel, server.serverInfo).joinToString(" · ")
        Text(detail, style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis)
        server.error?.let { Text(it, style = HyadexType.small, color = colors.orange, maxLines = 2, overflow = TextOverflow.Ellipsis) }
      }
    }
    DropdownMenu(menu, onDismissRequest = { menu = false }) {
      DropdownMenuItem(text = { Text("重新连接") }, onClick = { menu = false; onAction("reconnect") })
      DropdownMenuItem(text = { Text(if (server.enabled) "停用" else "启用") }, onClick = { menu = false; onAction(if (server.enabled) "disable" else "enable") })
    }
  }
}

@Composable
private fun SubagentSection(model: AppViewModel) {
  val colors = Hyadex.colors
  SettingsSection("子代理") {
    model.tools.subagents.take(12).forEachIndexed { index, agent ->
      if (index > 0) SettingsDivider()
      var open by remember(agent.id) { mutableStateOf(false) }
      Column(Modifier.fillMaxWidth().clickable(enabled = agent.log.isNotEmpty() || agent.summary != null) { open = !open }.padding(horizontal = 14.dp, vertical = 10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
          Box(Modifier.size(8.dp).clip(CircleShape).background(agent.state.tint(colors)))
          Text(agent.name, style = HyadexType.secondary, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
          Text(agent.statusLabel, style = HyadexType.meta, color = colors.secondary)
        }
        (agent.activity ?: agent.summary)?.let { Text(it, style = HyadexType.meta, color = colors.secondary, maxLines = if (open) 12 else 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(start = 18.dp, top = 2.dp)) }
        if (open && agent.log.isNotEmpty()) {
          Text(agent.log.takeLast(8).joinToString("\n"), style = HyadexType.mono.copy(fontSize = HyadexType.small.fontSize), color = colors.secondary, modifier = Modifier.padding(start = 18.dp, top = 4.dp))
        }
      }
    }
  }
}

/** The chat's 高级设置: extra instructions, subagents, naming, jobs and questions from other chats. */
@Composable
fun AdvancedSettingsPane(model: AppViewModel, chat: ChatSummary, onDismiss: () -> Unit) {
  val colors = Hyadex.colors
  var loaded by remember { mutableStateOf<ChatSettings?>(null) }
  var edited by remember { mutableStateOf<ChatSettings?>(null) }
  var saving by remember { mutableStateOf(false) }
  LaunchedEffect(chat.id) {
    val settings = model.loadChatSettings(chat.id)
    loaded = settings
    edited = settings
  }
  Pane("高级设置", onDismiss) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp).padding(bottom = 24.dp)) {
      val settings = edited
      if (settings == null) {
        CircularProgressIndicator(Modifier.padding(24.dp).size(24.dp), strokeWidth = 2.dp)
        return@Column
      }
      SettingsSection("系统提示词") {
        OutlinedTextField(
          settings.systemPrompt, { if (it.length <= ChatSettings.MAX_SYSTEM_PROMPT) edited = settings.copy(systemPrompt = it) },
          placeholder = { Text("每一轮都附上的额外说明（可选）") }, minLines = 3, maxLines = 8,
          modifier = Modifier.fillMaxWidth().padding(10.dp),
        )
        Text("${settings.systemPrompt.length} / ${ChatSettings.MAX_SYSTEM_PROMPT}", style = HyadexType.small, color = colors.tertiary, modifier = Modifier.align(Alignment.End).padding(end = 14.dp, bottom = 8.dp))
      }
      SettingsSection("代理") {
        SubagentLimitRow(settings) { edited = settings.copy(subagentLimit = it) }
        if (settings.subagentLimitNote.isNotEmpty()) Text(settings.subagentLimitNote, style = HyadexType.small, color = colors.secondary, modifier = Modifier.padding(start = 14.dp, end = 14.dp, bottom = 10.dp))
        SettingsDivider()
        SwitchRow("自动命名", settings.autoTitle) { edited = settings.copy(autoTitle = it) }
        SettingsDivider()
        ChoiceRow("定时任务", ChatSettings.JOBS_ACCESS, ChatSettings.JOBS_ACCESS.first { it.first == settings.jobsAccess }, { it.second }) { edited = settings.copy(jobsAccess = it.first) }
      }
      SettingsSection("其他聊天来问") {
        ChoiceRow("允许的提问", ChatSettings.CONSULT_INBOUND, ChatSettings.CONSULT_INBOUND.first { it.first == settings.consultInbound }, { it.second }) { edited = settings.copy(consultInbound = it.first) }
        SettingsDivider()
        SwitchRow("深度询问别的聊天前要我批准", settings.consultDeepApproval) { edited = settings.copy(consultDeepApproval = it) }
        if (settings.consultDeepAllowed.isNotEmpty()) {
          SettingsDivider()
          val names = settings.consultDeepAllowed.map { id -> (model.chats + model.archivedChats).firstOrNull { it.id == id }?.title ?: id }
          Row(Modifier.fillMaxWidth().padding(start = 14.dp, end = 4.dp, top = 6.dp, bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
              Text("总是允许深度询问", style = HyadexType.body, color = colors.primary)
              Text(names.joinToString("、"), style = HyadexType.meta, color = colors.secondary, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            TextButton(onClick = { edited = settings.copy(consultDeepAllowed = emptyList()) }) { Text("清除") }
          }
        }
      }
      Button(
        onClick = {
          val before = loaded ?: return@Button
          saving = true
          model.saveChatSettings(chat.id, before, settings) {
            saving = false
            onDismiss()
          }
        },
        enabled = !saving && settings != loaded,
        modifier = Modifier.fillMaxWidth().padding(top = 20.dp),
      ) { Text(if (saving) "正在保存…" else "保存") }
    }
  }
}

@Composable
private fun SubagentLimitRow(settings: ChatSettings, onPick: (Int?) -> Unit) {
  val colors = Hyadex.colors
  var open by remember { mutableStateOf(false) }
  val options = (ChatSettings.SUBAGENT_LIMITS + listOfNotNull(settings.subagentLimit)).distinct().sorted()
  Box {
    SettingRow(
      "同时运行的子代理", detail = settings.subagentLimit?.toString() ?: "默认",
      tint = if (settings.subagentLimitSupported) null else colors.tertiary,
      onClick = if (settings.subagentLimitSupported) ({ open = true }) else null,
    )
    DropdownMenu(open, onDismissRequest = { open = false }) {
      DropdownMenuItem(text = { Text("默认（跟随代理）") }, onClick = { open = false; onPick(null) })
      for (limit in options) DropdownMenuItem(text = { Text("$limit") }, onClick = { open = false; onPick(limit) })
    }
  }
}

/** Sets the chat's native goal: Claude's completion condition, or Codex's objective with budgets. */
@Composable
fun GoalPane(model: AppViewModel, onDismiss: () -> Unit) {
  val colors = Hyadex.colors
  val tools = model.tools
  val goal = tools.goal
  val support = tools.goalSupport
  var text by remember { mutableStateOf(goal?.takeIf { it.isOpen }?.text ?: "") }
  var tokens by remember { mutableStateOf(goal?.tokenBudget?.toString() ?: "") }
  var minutes by remember { mutableStateOf(goal?.timeBudgetSeconds?.let { (it / 60).toString() } ?: "") }
  val codex = tools.backend == AgentBackend.CODEX
  Pane("目标", onDismiss) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp).padding(top = 8.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
      Text(
        if (codex) "Codex 会持续推进这个目标，可以给它 token 和时间预算。" else "Claude Code 会一直做下去，直到这个条件成立（/goal）。",
        style = HyadexType.secondary, color = colors.secondary,
      )
      if (support?.available == false) {
        UnavailableNote(support.reason ?: "这个聊天不能设定目标。")
        return@Column
      }
      OutlinedTextField(text, { if (it.length <= 4_000) text = it }, label = { Text(if (codex) "目标" else "完成条件") }, minLines = 2, maxLines = 6, modifier = Modifier.fillMaxWidth())
      if (codex) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
          OutlinedTextField(tokens, { tokens = it.filter(Char::isDigit) }, label = { Text("token 预算") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.weight(1f))
          OutlinedTextField(minutes, { minutes = it.filter(Char::isDigit) }, label = { Text("时间（分钟）") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.weight(1f))
        }
      }
      Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        if (goal?.isOpen == true) TextButton(onClick = { tools.clearGoal(); onDismiss() }) { Text("清除目标", color = colors.red) }
        Box(Modifier.weight(1f))
        TextButton(onClick = onDismiss) { Text("取消") }
        Button(
          onClick = {
            tools.setGoal(text.trim(), tokens.toIntOrNull(), minutes.toIntOrNull())
            onDismiss()
          },
          enabled = text.isNotBlank() && !tools.goalBusy,
        ) { Text("设定") }
      }
    }
  }
}

/** Side questions: ask the agent on the side; the main conversation is untouched (Claude's /btw, a Codex fork). */
@Composable
fun SidePane(model: AppViewModel, onDismiss: () -> Unit) {
  val colors = Hyadex.colors
  val tools = model.tools
  val side = tools.side
  var question by remember { mutableStateOf("") }
  LaunchedEffect(Unit) { tools.refreshSide() }
  // An answer on its way is polled, as the Mac does, besides the stream's updates.
  LaunchedEffect(side?.isRunning) {
    while (tools.side?.isRunning == true) {
      delay(2_000)
      tools.refreshSide()
    }
  }
  Pane("侧问", onDismiss, actions = { if (side?.exchanges?.isNotEmpty() == true) TextButton(onClick = tools::clearSide) { Text("清空") } }) {
    Column(Modifier.fillMaxWidth().weight(1f).padding(horizontal = 16.dp)) {
      Text("问代理一句，不打断、也不改变主对话。", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.padding(start = 4.dp, bottom = 8.dp))
      side?.contextNote?.let { Text(it, style = HyadexType.small, color = colors.tertiary, modifier = Modifier.padding(start = 4.dp, bottom = 8.dp)) }
      Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (side == null) Text("正在读取…", style = HyadexType.secondary, color = colors.tertiary)
        else if (side.exchanges.isEmpty()) Text("还没有侧问", style = HyadexType.secondary, color = colors.tertiary)
        for (exchange in side?.exchanges.orEmpty()) SideExchange(exchange) { tools.cancelSide(exchange.requestID) }
      }
      tools.sideError?.let { Text(it, style = HyadexType.meta, color = colors.orange, modifier = Modifier.padding(top = 8.dp)) }
      Row(Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 10.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        OutlinedTextField(question, { question = it }, placeholder = { Text("问一句…") }, maxLines = 4, modifier = Modifier.weight(1f))
        IconButton(
          onClick = {
            tools.askSide(question.trim())
            question = ""
          },
          enabled = question.isNotBlank() && side?.isRunning != true,
        ) { Icon(Icons.AutoMirrored.Filled.Send, "发送", tint = if (question.isNotBlank()) colors.accent else colors.tertiary) }
      }
    }
  }
}

@Composable
private fun SideExchange(exchange: SideChat.Exchange, onCancel: () -> Unit) {
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Text(exchange.question, style = HyadexType.secondary, color = colors.primary, modifier = Modifier.align(Alignment.End).background(colors.bubble, RoundedCornerShape(14.dp)).padding(horizontal = 12.dp, vertical = 8.dp))
    when (exchange.status) {
      SideChat.Exchange.Status.RUNNING -> Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 1.5.dp)
        Text("正在回答…", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.weight(1f))
        TextButton(onClick = onCancel) { Text("取消") }
      }
      SideChat.Exchange.Status.COMPLETED -> exchange.answer?.let { MessageText(it) }
      SideChat.Exchange.Status.CANCELLED -> Text("已取消", style = HyadexType.meta, color = colors.tertiary)
      else -> Text(exchange.error ?: "没有回答", style = HyadexType.meta, color = colors.orange)
    }
  }
}

/** A changed file's diff, as the Mac's change panel: added lines green, removed red, hunks blue. */
@Composable
fun DiffSheet(model: AppViewModel, file: FileChange, runID: String?, onDismiss: () -> Unit) {
  val colors = Hyadex.colors
  var diff by remember { mutableStateOf<String?>(file.diff.ifEmpty { null }) }
  var failed by remember { mutableStateOf(false) }
  LaunchedEffect(runID, file.path) {
    if (diff == null && runID != null) {
      val files = model.diffFiles(runID)
      diff = files?.get(file.path)
      failed = diff == null
    } else if (diff == null) {
      failed = true
    }
  }
  Sheet(onDismiss) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 16.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(horizontal = 4.dp, vertical = 4.dp)) {
        Text(file.path, style = HyadexType.mono, color = colors.primary, maxLines = 2, overflow = TextOverflow.StartEllipsis, modifier = Modifier.weight(1f))
        Text("+${file.added}", style = HyadexType.meta, color = colors.green)
        Text("−${file.removed}", style = HyadexType.meta, color = colors.red)
      }
      val text = diff
      when {
        text != null -> SelectionContainer {
          BoxWithConstraints(Modifier.fillMaxWidth().heightIn(max = 560.dp).clip(RoundedCornerShape(Hyadex.cornerRadius)).card()) {
            // Every line is as wide as the widest (or the card): the colours run the full row.
            Column(Modifier.verticalScroll(rememberScrollState()).horizontalScroll(rememberScrollState()).padding(vertical = 8.dp).width(IntrinsicSize.Max).widthIn(min = maxWidth)) {
              for (line in text.split("\n")) DiffLine(line)
            }
          }
        }
        failed -> Text("读不到这个文件的 diff。", style = HyadexType.secondary, color = colors.secondary, modifier = Modifier.padding(12.dp))
        else -> CircularProgressIndicator(Modifier.padding(24.dp).size(24.dp), strokeWidth = 2.dp)
      }
    }
  }
}

@Composable
private fun DiffLine(line: String) {
  val colors = Hyadex.colors
  val (tint, background) = when {
    line.startsWith("@@") -> colors.accent to colors.accent.copy(alpha = 0.08f)
    line.startsWith("+") -> colors.green to colors.green.copy(alpha = 0.1f)
    line.startsWith("-") -> colors.red to colors.red.copy(alpha = 0.1f)
    else -> colors.primary to Color.Transparent
  }
  Text(
    line.ifEmpty { " " }, style = HyadexType.mono, color = tint, softWrap = false,
    fontWeight = if (line.startsWith("@@")) FontWeight.Medium else null,
    modifier = Modifier.fillMaxWidth().background(background).padding(horizontal = 12.dp, vertical = 1.dp),
  )
}
