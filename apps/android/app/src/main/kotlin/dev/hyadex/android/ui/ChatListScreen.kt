package dev.hyadex.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Cancel
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Archive
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.PushPin
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxState
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.app.Connection
import dev.hyadex.android.core.ActivityState
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.ProjectSection
import dev.hyadex.android.core.projectSections
import dev.hyadex.android.core.relativeShort
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlin.math.roundToInt

/** The Mac app's sidebar as a phone's first screen: Work chats by project. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatListScreen(model: AppViewModel, onOpen: (String) -> Unit, onSettings: () -> Unit, animate: Boolean = true, modifier: Modifier = Modifier) {
  val colors = Hyadex.colors
  var query by rememberSaveable { mutableStateOf("") }
  var creating by rememberSaveable { mutableStateOf(false) }
  var renaming by remember { mutableStateOf<ChatSummary?>(null) }
  var moving by remember { mutableStateOf<ChatSummary?>(null) }
  var deleting by remember { mutableStateOf<ChatSummary?>(null) }
  var archivedOpen by rememberSaveable { mutableStateOf(false) }
  val chats = model.chats
  val previews = model.previews
  val byProject = model.settings.groupByProject
  val needle = query.trim().lowercase()
  fun matches(chat: ChatSummary) = needle.isEmpty() || needle in chat.title.lowercase() || needle in (previews[chat.id] ?: "").lowercase()
  val sections = remember(chats, needle, previews.toMap(), byProject) {
    val shown = chats.filter(::matches)
    val pinned = shown.filter { it.pinned }.sortedByDescending { it.lastActivity }
    val rest = shown.filter { !it.pinned }
    val grouped = if (byProject) projectSections(rest) else listOfNotNull(rest.takeIf { it.isNotEmpty() }?.let { ProjectSection("recent", "最近", it.sortedByDescending { chat -> chat.lastActivity }) })
    listOfNotNull(pinned.takeIf { it.isNotEmpty() }?.let { ProjectSection("pinned", "置顶", it) }) + grouped
  }
  val archived = remember(model.archivedChats, needle, previews.toMap()) { model.archivedChats.filter(::matches) }
  // Message search runs on the server as you type; title matches show at once above it.
  LaunchedEffect(query, model.client) { model.search(query) }
  val known = remember(chats, model.archivedChats) { (chats + model.archivedChats).associateBy { it.id } }
  val hits = if (needle.length >= 2) model.searchHits.filter { it.chatID in known } else emptyList()
  val actions = remember(model) {
    ChatRowActions(
      open = onOpen,
      rename = { renaming = it },
      move = { moving = it },
      delete = { deleting = it },
      fork = { chat -> model.fork(chat) { id -> onOpen(id) } },
    )
  }
  Column(modifier.fillMaxSize().background(colors.sidebar).statusBarsPadding()) {
    Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 12.dp, top = 10.dp, bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
      Text("Work", style = HyadexType.title, color = colors.primary, modifier = Modifier.weight(1f))
      // The Mac sidebar's round + button.
      Box(Modifier.size(40.dp).clip(CircleShape).background(colors.bubble).clickable { creating = true }, contentAlignment = Alignment.Center) {
        Icon(Icons.Outlined.Add, "新建聊天", tint = colors.primary)
      }
    }
    SearchField(query) { query = it }
    EmergencyBanners(model.emergencies, onOpen = onOpen, onAcknowledge = model::acknowledge)
    PullToRefreshBox(isRefreshing = model.refreshing, onRefresh = model::refresh, modifier = Modifier.weight(1f)) {
      LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 12.dp)) {
        for (section in sections) {
          item(key = "section-" + section.key, contentType = "section") { SectionHeader(section) }
          items(section.chats, key = { section.key + "/" + it.id }, contentType = { "chat" }) { chat ->
            // A chat that moves up (new activity) slides to its place.
            Box(Modifier.animateItem()) {
              SwipeToArchive(onArchive = { model.archive(chat) }) {
                ChatRow(chat, model.previews[chat.id], animate, model, actions)
              }
            }
          }
        }
        if (hits.isNotEmpty()) {
          item(key = "hits", contentType = "section") { PlainHeader("消息内容") }
          items(hits, key = { "hit/" + it.chatID }, contentType = { "hit" }) { hit ->
            SearchHitRow(hit, known.getValue(hit.chatID), query) { onOpen(hit.chatID) }
          }
        }
        if (archived.isNotEmpty()) {
          // Folded away unless opened, or unless a search finds something in it.
          val open = archivedOpen || needle.isNotEmpty()
          item(key = "archived", contentType = "section") {
            ArchivedHeader(archived.size, open, enabled = needle.isEmpty()) { archivedOpen = !archivedOpen }
          }
          if (open) {
            items(archived, key = { "archived/" + it.id }, contentType = { "chat" }) { chat ->
              Box(Modifier.animateItem()) { ChatRow(chat, model.previews[chat.id], animate, model, actions) }
            }
          }
        }
        if (sections.isEmpty() && hits.isEmpty() && archived.isEmpty()) {
          item {
            Text(
              if (query.isBlank()) "还没有聊天，点右上角 + 新建一个" else "没有匹配的聊天",
              style = HyadexType.secondary, color = colors.secondary, modifier = Modifier.padding(24.dp),
            )
          }
        }
      }
    }
    HorizontalDivider(color = colors.separator, thickness = 0.5.dp)
    val usage = if (model.settings.showUsage) model.usage.values.mapNotNull { usage -> usage.tightest?.usedPercent?.let { usage.backend to it } } else emptyList()
    ConnectionFooter(model.connection, model.offline, usage.sortedBy { it.first.ordinal }, onSettings)
  }
  if (creating) NewChatSheet(model, onDismiss = { creating = false }) { id ->
    creating = false
    onOpen(id)
  }
  renaming?.let { chat -> RenameDialog(chat.title, onDismiss = { renaming = null }) { title -> model.updateChat(chat.id, mapOf("title" to title)); renaming = null } }
  moving?.let { chat ->
    MoveToProjectDialog(chat, model.knownProjects, onDismiss = { moving = null }) { project ->
      moving = null
      model.moveToProject(chat, project)
    }
  }
  deleting?.let { chat ->
    DeleteChatDialog(chat, onDismiss = { deleting = null }) {
      deleting = null
      model.deleteChat(chat)
    }
  }
}

@Composable
private fun SearchField(query: String, onChange: (String) -> Unit) {
  val colors = Hyadex.colors
  Row(
    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp).clip(RoundedCornerShape(10.dp)).background(colors.bubble).padding(horizontal = 12.dp, vertical = 10.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    Icon(Icons.Outlined.Search, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
    Box(Modifier.weight(1f)) {
      if (query.isEmpty()) Text("搜索聊天", style = HyadexType.secondary, color = colors.tertiary)
      BasicTextField(query, onChange, singleLine = true, textStyle = HyadexType.secondary.copy(color = colors.primary), cursorBrush = SolidColor(colors.accent), modifier = Modifier.fillMaxWidth())
    }
    AnimatedVisibility(query.isNotEmpty(), enter = fadeIn() + scaleIn(), exit = fadeOut() + scaleOut()) {
      Box(Modifier.size(32.dp).clip(CircleShape).clickable { onChange("") }, contentAlignment = Alignment.Center) {
        Icon(Icons.Filled.Cancel, "清除", tint = colors.tertiary, modifier = Modifier.size(18.dp))
      }
    }
  }
}

@Composable
private fun SectionHeader(section: ProjectSection) {
  val colors = Hyadex.colors
  Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 16.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
    val icon = when (section.key) {
      "pinned" -> Icons.Outlined.PushPin
      "recent" -> Icons.Outlined.History
      else -> Icons.Outlined.Folder
    }
    Icon(icon, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
    Text(section.title, style = HyadexType.headline, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis)
  }
}

@Composable
private fun PlainHeader(title: String) {
  val colors = Hyadex.colors
  Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 16.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
    Icon(Icons.Outlined.Search, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
    Text(title, style = HyadexType.headline, color = colors.primary)
  }
}

/** "已归档 · 3", folded by default as in the Mac sidebar. */
@Composable
private fun ArchivedHeader(count: Int, open: Boolean, enabled: Boolean, onToggle: () -> Unit) {
  val colors = Hyadex.colors
  Row(
    Modifier.fillMaxWidth().clickable(enabled = enabled, onClick = onToggle).padding(start = 20.dp, end = 20.dp, top = 16.dp, bottom = 8.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    Icon(Icons.Outlined.Archive, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
    Text("已归档 · $count", style = HyadexType.headline, color = colors.secondary, modifier = Modifier.weight(1f))
    if (enabled) Chevron(open, Modifier.size(18.dp))
  }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ChatRow(chat: ChatSummary, preview: String?, animate: Boolean, model: AppViewModel, actions: ChatRowActions) {
  val colors = Hyadex.colors
  var menu by remember { mutableStateOf(false) }
  val haptics = LocalHapticFeedback.current
  Box {
    Row(
      Modifier.fillMaxWidth().background(colors.sidebar).combinedClickable(
        onClick = { actions.open(chat.id) },
        onLongClick = {
          haptics.performHapticFeedback(HapticFeedbackType.LongPress)
          menu = true
        },
      ).padding(horizontal = 20.dp, vertical = 10.dp),
      horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
      ProviderMark(chat.backend, 22.dp, Modifier.padding(top = 2.dp))
      Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
          Text(
            chat.title, style = HyadexType.body.copy(fontWeight = if (chat.unread) FontWeight.SemiBold else FontWeight.Normal),
            color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
          )
          StateMark(chat, animate, emergency = model.emergencies.any { it.chatID == chat.id })
          Text(relativeShort(chat.lastActivity), style = HyadexType.meta, color = colors.tertiary)
        }
        Text(preview ?: " ", style = HyadexType.secondary, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis)
      }
    }
    DropdownMenu(menu, onDismissRequest = { menu = false }) {
      ChatMenuItems(chat, model, actions) { menu = false }
    }
  }
}

/** Running, waiting for the user, or unread — one mark, as in the Mac sidebar. */
@Composable
private fun StateMark(chat: ChatSummary, animate: Boolean, emergency: Boolean) {
  val colors = Hyadex.colors
  when {
    emergency -> Icon(Icons.Filled.Warning, "紧急提醒", tint = colors.red, modifier = Modifier.size(16.dp))
    chat.state == ActivityState.NEEDS_ACTION -> Icon(Icons.Filled.Error, "等待你处理", tint = colors.orange, modifier = Modifier.size(16.dp))
    // Spins only while the list is on screen.
    chat.state == ActivityState.RUNNING ->
      if (animate) CircularProgressIndicator(Modifier.size(12.dp).semantics { contentDescription = "运行中" }, strokeWidth = 1.5.dp, color = colors.accent)
      else Box(Modifier.size(9.dp).clip(CircleShape).background(colors.accent))
    chat.unread -> Box(Modifier.size(9.dp).clip(CircleShape).background(colors.accent).semantics { contentDescription = "未读" })
  }
}

@Composable
private fun ConnectionFooter(connection: Connection, offline: Boolean, usage: List<Pair<AgentBackend, Double>>, onSettings: () -> Unit) {
  val colors = Hyadex.colors
  Row(
    Modifier.fillMaxWidth().clickable(onClick = onSettings).navigationBarsPadding().padding(horizontal = 20.dp, vertical = 12.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    val (tint, title, detail) = when (connection) {
      is Connection.Connected ->
        if (offline) Triple(colors.yellow, connection.serverName ?: connection.address, "无法连接，正在重试…")
        else Triple(colors.green, connection.serverName ?: connection.address, "已连接" + (connection.version?.let { " · v$it" } ?: ""))
      is Connection.Failed -> Triple(colors.red, connection.address, connection.message)
      Connection.Connecting -> Triple(colors.yellow, "正在连接…", "")
      Connection.NotConfigured -> Triple(colors.tertiary, "未连接", "")
    }
    Box(Modifier.size(9.dp).clip(CircleShape).background(tint))
    Column(Modifier.weight(1f)) {
      Text(title, style = HyadexType.secondary, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis)
      if (detail.isNotEmpty()) Text(detail, style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
    // Each provider's tightest plan limit, as the Mac sidebar's usage meter.
    for ((backend, used) in usage) {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        ProviderMark(backend, 14.dp)
        Text("${used.roundToInt()}%", style = HyadexType.meta, color = if (used >= 90) colors.red else if (used >= 70) colors.orange else colors.secondary)
      }
    }
  }
}

@Composable
fun RenameDialog(title: String, onDismiss: () -> Unit, onSave: (String) -> Unit) {
  // The old title starts selected, so typing replaces it.
  var text by remember { mutableStateOf(TextFieldValue(title, TextRange(0, title.length))) }
  val focus = remember { FocusRequester() }
  LaunchedEffect(Unit) { focus.requestFocus() }
  AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("重命名聊天") },
    text = { OutlinedTextField(text, { text = it }, singleLine = true, modifier = Modifier.focusRequester(focus)) },
    confirmButton = { TextButton(onClick = { onSave(text.text.trim()) }, enabled = text.text.isNotBlank()) { Text("保存") } },
    dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
  )
}

/** Swipe a chat to the left to archive it (the snackbar offers undo). */
@Composable
private fun SwipeToArchive(onArchive: () -> Unit, content: @Composable () -> Unit) {
  val colors = Hyadex.colors
  val haptics = LocalHapticFeedback.current
  // Not saved with the row: a chat brought back by undo starts in place, and archives only when swiped again.
  val state = remember { SwipeToDismissBoxState(SwipeToDismissBoxValue.Settled, positionalThreshold = { it * 0.4f }) }
  SwipeToDismissBox(
    state = state,
    enableDismissFromStartToEnd = false,
    onDismiss = { value ->
      if (value == SwipeToDismissBoxValue.EndToStart) {
        haptics.performHapticFeedback(HapticFeedbackType.Confirm)
        onArchive()
      }
    },
    backgroundContent = {
      Box(Modifier.fillMaxSize().background(colors.orange.copy(alpha = 0.85f)).padding(horizontal = 24.dp), contentAlignment = Alignment.CenterEnd) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
          Icon(Icons.Outlined.Archive, null, tint = Color.White, modifier = Modifier.size(18.dp))
          Text("归档", style = HyadexType.secondary, color = Color.White)
        }
      }
    },
  ) {
    content()
  }
}
