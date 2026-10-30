package dev.hyadex.android.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.rememberTransformableState
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.outlined.Add
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.KeyboardArrowDown
import androidx.compose.material.icons.outlined.PauseCircle
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.ripple
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import coil3.compose.AsyncImage
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.core.Author
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.LiveActivity
import dev.hyadex.android.core.PermissionPreset
import dev.hyadex.android.core.QueuedMessage
import dev.hyadex.android.core.RuntimeOption
import dev.hyadex.android.core.SessionProjection
import dev.hyadex.android.core.TimelineItem
import dev.hyadex.android.core.TimelineRow
import dev.hyadex.android.core.isRiskyPermission
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlinx.coroutines.launch

@Composable
fun ChatScreen(model: AppViewModel, chat: ChatSummary, onBack: () -> Unit) {
  val colors = Hyadex.colors
  val view = model.chatView
  var viewing by remember { mutableStateOf<String?>(null) }
  val haptics = LocalHapticFeedback.current
  val actions = remember(model.imageLoader) {
    TimelineActions(
      onReply = { id, reply ->
        haptics.performHapticFeedback(HapticFeedbackType.Confirm)
        model.reply(id, reply)
      },
      onConsultDecision = { id, decision ->
        haptics.performHapticFeedback(HapticFeedbackType.Confirm)
        model.decideConsult(id, decision)
      },
      fileUrl = model::fileUrl,
      imageLoader = model.imageLoader,
      onOpenImage = { viewing = it },
    )
  }
  Column(Modifier.fillMaxSize().background(colors.background).imePadding()) {
    Row(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 4.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
      IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, "返回", tint = colors.primary) }
      ProviderMark(chat.backend, 20.dp)
      Text(chat.title, style = HyadexType.headline, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(start = 10.dp).weight(1f))
    }
    HorizontalDivider(color = colors.separator, thickness = 0.5.dp)
    // The live stream dropped: say so until it is back (events are caught up then).
    AnimatedVisibility(view?.reconnecting == true, enter = expandVertically() + fadeIn(), exit = shrinkVertically() + fadeOut()) {
      Row(Modifier.fillMaxWidth().background(colors.orange.copy(alpha = 0.12f)).padding(horizontal = 16.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        CircularProgressIndicator(Modifier.size(12.dp), strokeWidth = 1.5.dp, color = colors.orange)
        Text("连接中断，正在重新连接…", style = HyadexType.meta, color = colors.orange)
      }
    }
    Box(Modifier.weight(1f)) {
      if (view == null || !view.loaded) {
        CircularProgressIndicator(Modifier.align(Alignment.Center).size(24.dp), strokeWidth = 2.dp)
      } else {
        Timeline(view.rows, chat, view.activity, view.olderHistory, model::loadOlder, actions)
      }
    }
    if (view != null && view.queued.isNotEmpty()) QueueList(view.queued, model::removeQueued, model::resumeQueue)
    Composer(model, chat, running = view?.running == true)
  }
  viewing?.let { url -> ImageViewer(url, model) { viewing = null } }
}

/** Newest at the bottom (a reversed list), following the agent as it writes unless you scroll away. */
@Composable
private fun Timeline(rows: List<TimelineRow>, chat: ChatSummary, activity: LiveActivity?, older: Boolean, loadOlder: () -> Unit, actions: TimelineActions) {
  val state = rememberLazyListState()
  val scope = rememberCoroutineScope()
  val reversed = remember(rows) { rows.asReversed() }
  val nearTop by remember { derivedStateOf { state.layoutInfo.visibleItemsInfo.lastOrNull()?.index?.let { it >= state.layoutInfo.totalItemsCount - 3 } == true } }
  // Keyed on the rows too: still at the top after a page arrives, the next one loads.
  LaunchedEffect(nearTop, older, rows.size) { if (nearTop && older) loadOlder() }
  // A new row at the end would otherwise appear below the screen (the list keeps
  // the row that was in view). Only your own scrolling decides whether to follow,
  // and your own message always comes into view.
  var following by remember { mutableStateOf(true) }
  LaunchedEffect(state) {
    snapshotFlow { state.isScrollInProgress }.collect { scrolling ->
      if (!scrolling) following = state.firstVisibleItemIndex == 0 && state.firstVisibleItemScrollOffset < 48
    }
  }
  val newest = reversed.firstOrNull()
  // Something arrived below while you read further up.
  var unseen by remember { mutableStateOf(false) }
  LaunchedEffect(newest?.key, reversed.size) {
    val mine = ((newest as? TimelineRow.Item)?.item as? TimelineItem.Message)?.author?.kind == Author.Kind.USER
    if ((following || mine) && newest != null) state.animateScrollToItem(0) else if (newest != null) unseen = true
  }
  LaunchedEffect(following) { if (following) unseen = false }
  if (rows.isEmpty()) {
    EmptyChat(chat)
    return
  }
  Box(Modifier.fillMaxSize()) {
    LazyColumn(
      state = state,
      reverseLayout = true,
      contentPadding = PaddingValues(horizontal = 16.dp, vertical = 14.dp),
      verticalArrangement = Arrangement.spacedBy(14.dp),
      modifier = Modifier.fillMaxSize(),
    ) {
      // One content type per kind of row, so a scrolled-in message reuses a message's slot.
      items(reversed, key = { it.key }, contentType = { row -> (row as? TimelineRow.Item)?.item?.let { it::class } ?: row::class }) { row ->
        // Only the status rows read the live activity; the others skip when it ticks.
        val live = row is TimelineRow.Activity || (row is TimelineRow.Process && row.live)
        // New rows fade in; rows do not slide, so following the newest never fights the scroll.
        Box(Modifier.animateItem(fadeInSpec = tween(220), placementSpec = null, fadeOutSpec = tween(150))) {
          TimelineRowView(row, if (live) activity else null, actions)
        }
      }
      if (older) {
        item(key = "older", contentType = "older") {
          TextButton(onClick = loadOlder, modifier = Modifier.fillMaxWidth()) { Text("加载更早的消息", style = HyadexType.meta) }
        }
      }
    }
    // Scrolled up while new content arrives: one tap back to the newest.
    // A pill that rises from the bottom, as in Android's own chat sample.
    AnimatedVisibility(
      !following,
      enter = slideInVertically(spring(stiffness = Spring.StiffnessMediumLow)) { it * 2 } + fadeIn(),
      exit = slideOutVertically(tween(160)) { it * 2 } + fadeOut(tween(160)),
      modifier = Modifier.align(Alignment.BottomCenter).padding(bottom = 12.dp),
    ) {
      val colors = Hyadex.colors
      Row(
        Modifier.height(36.dp).clip(CircleShape).background(colors.card).border(0.5.dp, colors.separator, CircleShape)
          .clickable { scope.launch { following = true; state.animateScrollToItem(0) } }.padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
      ) {
        Icon(Icons.Filled.ArrowDownward, null, tint = if (unseen) colors.accent else colors.secondary, modifier = Modifier.size(16.dp))
        Text(if (unseen) "新消息" else "回到最新", style = HyadexType.meta, color = if (unseen) colors.accent else colors.secondary)
      }
    }
  }
}

/** A chat with no messages yet: the agent's mark and where it works. */
@Composable
private fun EmptyChat(chat: ChatSummary) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
    ProviderMark(chat.backend, 40.dp)
    Text("给 ${chat.backend.displayName} 发第一条消息", style = HyadexType.headline, color = colors.primary, modifier = Modifier.padding(top = 16.dp))
    Text(shortPath(chat.cwd), style = HyadexType.meta, color = colors.secondary, modifier = Modifier.padding(top = 6.dp))
  }
}

@Composable
private fun QueueList(queued: List<QueuedMessage>, onRemove: (String) -> Unit, onResume: () -> Unit) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(top = 6.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
    // After a Stop the server holds the queue until the user sends it on.
    if (queued.any { it.paused }) {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Text("停止后，这些消息已暂停", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.weight(1f))
        TextButton(onClick = onResume) { Text("继续发送") }
      }
    }
    for (message in queued) {
      Row(Modifier.fillMaxWidth().card().padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Icon(if (message.paused) Icons.Outlined.PauseCircle else Icons.Outlined.Schedule, null, tint = colors.secondary, modifier = Modifier.size(15.dp))
        Text((if (message.paused) "已暂停：" else "排队：") + message.text, style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        IconButton(onClick = { onRemove(message.id) }) { Icon(Icons.Filled.Close, "撤回", tint = colors.secondary, modifier = Modifier.size(16.dp)) }
      }
    }
  }
}

/** The Mac composer on a phone: folder line, the message box, then permissions, model, effort and send. */
@Composable
private fun Composer(model: AppViewModel, chat: ChatSummary, running: Boolean) {
  val colors = Hyadex.colors
  var text by rememberSaveable(chat.id) { mutableStateOf(model.drafts[chat.id] ?: "") }
  LaunchedEffect(model.drafts[chat.id]) { model.drafts[chat.id]?.let { text = it } }
  val attachments = model.attachments[chat.id].orEmpty()
  val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(6)) { uris -> if (uris.isNotEmpty()) model.attach(uris) }
  val canSend = text.isNotBlank() || attachments.any { it.fileID != null }
  val haptics = LocalHapticFeedback.current
  // Coming back to the app should not open the keyboard on its own.
  val focus = LocalFocusManager.current
  LifecycleEventEffect(Lifecycle.Event.ON_STOP) { focus.clearFocus() }
  Column(Modifier.fillMaxWidth().navigationBarsPadding().padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Row(Modifier.padding(horizontal = 6.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
      Icon(Icons.Outlined.Folder, null, tint = colors.secondary, modifier = Modifier.size(15.dp))
      Text(shortPath(chat.cwd), style = HyadexType.meta, color = colors.secondary, maxLines = 1, overflow = TextOverflow.StartEllipsis)
    }
    Column(
      Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(colors.card).border(0.5.dp, colors.separator, RoundedCornerShape(20.dp))
        .animateContentSize(spring(stiffness = Spring.StiffnessMediumLow)).padding(horizontal = 14.dp, vertical = 10.dp),
    ) {
      if (attachments.isNotEmpty()) {
        Row(Modifier.horizontalScroll(rememberScrollState()).padding(bottom = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          for (attachment in attachments) {
            Box {
              AsyncImage(attachment.uri, attachment.name, imageLoader = model.imageLoader ?: return@Box, contentScale = ContentScale.Crop, modifier = Modifier.size(56.dp).clip(RoundedCornerShape(8.dp)))
              if (attachment.fileID == null && !attachment.failed) CircularProgressIndicator(Modifier.align(Alignment.Center).size(18.dp), strokeWidth = 2.dp, color = Color.White)
              if (attachment.failed) Text("失败", style = HyadexType.small, color = Color.White, modifier = Modifier.align(Alignment.Center).background(colors.red, RoundedCornerShape(4.dp)).padding(2.dp))
              // A small mark on a large target: the whole top-right corner removes the image.
              Box(Modifier.align(Alignment.TopEnd).size(32.dp).clickable { model.removeAttachment(attachment.key) }, contentAlignment = Alignment.TopEnd) {
                Icon(
                  Icons.Filled.Close, "移除", tint = Color.White,
                  modifier = Modifier.padding(2.dp).size(18.dp).clip(CircleShape).background(Color.Black.copy(alpha = 0.6f)).padding(2.dp),
                )
              }
            }
          }
        }
      }
      Box(Modifier.fillMaxWidth().heightIn(min = 24.dp, max = 160.dp)) {
        if (text.isEmpty()) Text("给 ${chat.backend.displayName} 发消息", style = HyadexType.body, color = colors.tertiary)
        BasicTextField(
          text, { text = it; model.drafts[chat.id] = it }, textStyle = HyadexType.body.copy(color = colors.primary), cursorBrush = SolidColor(colors.accent),
          maxLines = 6,
          modifier = Modifier.fillMaxWidth(),
        )
      }
      Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(2.dp)) {
        IconButton(onClick = { picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }) {
          Icon(Icons.Outlined.Add, "添加图片", tint = colors.secondary)
        }
        // Measured last: the permission label gives way before the send button does.
        Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) { PermissionChip(model, chat) }
        ModelChips(model, chat)
        SendButton(canSend = canSend, running = running) {
          if (canSend) {
            haptics.performHapticFeedback(HapticFeedbackType.Confirm)
            // Kept when it cannot go yet (an image still uploading): nothing typed is lost.
            if (model.send(text)) text = ""
          } else if (running) {
            haptics.performHapticFeedback(HapticFeedbackType.Reject)
            model.stop()
          }
        }
      }
    }
  }
}

/** "…/scratchpad/imagefix-proj", as the Mac composer shows a chat's folder. */
private fun shortPath(path: String): String {
  val home = SessionProjection.homeOf(path)
  val shown = if (home.isNotEmpty() && path.startsWith(home)) "~" + path.removePrefix(home) else path
  val parts = shown.split('/').filter { it.isNotEmpty() }
  return if (parts.size > 2) ".../" + parts.takeLast(2).joinToString("/") else shown
}

@Composable
private fun SendButton(canSend: Boolean, running: Boolean, onClick: () -> Unit) {
  val colors = Hyadex.colors
  val stopping = !canSend && running
  val active = canSend || running
  val fill by animateColorAsState(if (canSend) colors.accent else if (running) colors.primary else colors.bubble, tween(180), label = "send")
  // 48 dp to touch, 34 dp to see.
  Box(
    Modifier.size(48.dp).clickable(enabled = active, onClick = onClick, role = Role.Button, indication = ripple(bounded = false, radius = 20.dp), interactionSource = null),
    contentAlignment = Alignment.Center,
  ) {
    Box(Modifier.size(34.dp).clip(CircleShape).background(fill), contentAlignment = Alignment.Center) {
      AnimatedContent(stopping, transitionSpec = { (scaleIn(initialScale = 0.6f) + fadeIn()) togetherWith (scaleOut(targetScale = 0.6f) + fadeOut()) }, label = "send icon") { stop ->
        if (stop) Icon(Icons.Filled.Stop, "停止", tint = colors.background, modifier = Modifier.size(18.dp))
        else Icon(Icons.Filled.ArrowUpward, "发送", tint = if (active) Color.White else colors.tertiary, modifier = Modifier.size(20.dp))
      }
    }
  }
}

@Composable
private fun PermissionChip(model: AppViewModel, chat: ChatSummary) {
  val colors = Hyadex.colors
  var open by remember { mutableStateOf(false) }
  val tint = if (isRiskyPermission(chat.permission)) colors.orange else colors.secondary
  Box {
    Row(Modifier.clip(RoundedCornerShape(8.dp)).clickable { open = true }.padding(horizontal = 6.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
      Icon(Icons.Outlined.Shield, null, tint = tint, modifier = Modifier.size(15.dp))
      Text(chat.permission, style = HyadexType.meta, color = tint, maxLines = 1)
    }
    DropdownMenu(open, onDismissRequest = { open = false }) {
      for (preset in PermissionPreset.presets(chat.backend)) {
        DropdownMenuItem(
          text = {
            Column {
              Text(preset.label, style = HyadexType.secondary, color = if (isRiskyPermission(preset.label)) colors.orange else colors.primary)
              Text(preset.detail, style = HyadexType.meta, color = colors.secondary)
            }
          },
          onClick = { open = false; model.updateOpenChat(preset.fields) },
        )
      }
    }
  }
}

@Composable
private fun ModelChips(model: AppViewModel, chat: ChatSummary) {
  val catalog = model.catalogs[chat.backend]
  val models = catalog?.models.orEmpty()
  val efforts = catalog?.efforts(chat.model).orEmpty()
  OptionChip(shortLabel(models, chat.model), 104.dp, models) { model.updateOpenChat(mapOf("model" to it)) }
  OptionChip(shortLabel(efforts, chat.effort), 72.dp, efforts) { model.updateOpenChat(mapOf("effort" to it)) }
}

/** The chosen option as the Mac composer names it: "Server default (Sonnet)" reads "Sonnet". */
private fun shortLabel(options: List<RuntimeOption>, value: String): String {
  val label = options.firstOrNull { it.value == value }?.label ?: value.replaceFirstChar { it.uppercase() }
  if (value.isNotEmpty()) return label.substringBefore(" (")
  return Regex("""\(([^)]+)\)""").find(label)?.groupValues?.get(1) ?: "默认"
}

@Composable
private fun OptionChip(label: String, maxWidth: androidx.compose.ui.unit.Dp, options: List<RuntimeOption>, onPick: (String) -> Unit) {
  val colors = Hyadex.colors
  var open by remember { mutableStateOf(false) }
  Box {
    Row(Modifier.clip(RoundedCornerShape(8.dp)).clickable(enabled = options.isNotEmpty()) { open = true }.padding(horizontal = 4.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
      Text(label, style = HyadexType.meta, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.widthIn(max = maxWidth))
      Icon(Icons.Outlined.KeyboardArrowDown, null, tint = colors.secondary, modifier = Modifier.size(14.dp))
    }
    DropdownMenu(open, onDismissRequest = { open = false }) {
      for (option in options) DropdownMenuItem(text = { Text(option.label) }, onClick = { open = false; onPick(option.value) })
    }
  }
}

/** An image full screen inside the app; pinch to zoom, tap to close. */
@Composable
private fun ImageViewer(url: String, model: AppViewModel, onDismiss: () -> Unit) {
  var scale by remember { mutableFloatStateOf(1f) }
  var offsetX by remember { mutableFloatStateOf(0f) }
  var offsetY by remember { mutableFloatStateOf(0f) }
  val transform = rememberTransformableState { zoom, pan, _ ->
    scale = (scale * zoom).coerceIn(1f, 6f)
    offsetX += pan.x
    offsetY += pan.y
  }
  Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
    Box(Modifier.fillMaxSize().background(Color.Black).clickable(onClick = onDismiss), contentAlignment = Alignment.Center) {
      AsyncImage(
        url, "图片", imageLoader = model.imageLoader ?: return@Box, contentScale = ContentScale.Fit,
        modifier = Modifier.fillMaxSize().transformable(transform).graphicsLayer(scaleX = scale, scaleY = scale, translationX = offsetX, translationY = offsetY),
      )
    }
  }
}
