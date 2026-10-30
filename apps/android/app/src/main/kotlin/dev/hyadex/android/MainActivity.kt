package dev.hyadex.android

import android.Manifest
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.app.Connection
import dev.hyadex.android.app.RunWatchService
import dev.hyadex.android.core.PairingLink
import dev.hyadex.android.ui.ChatListScreen
import dev.hyadex.android.ui.ChatPane
import dev.hyadex.android.ui.ChatScreen
import dev.hyadex.android.ui.ConnectScreen
import dev.hyadex.android.ui.SettingsDialog
import dev.hyadex.android.ui.theme.HyadexTheme
import kotlinx.coroutines.flow.filterNotNull

class MainActivity : ComponentActivity() {
  private val model: AppViewModel by viewModels()

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    enableEdgeToEdge()
    // Only a fresh start: a rotation or a restore must not act on the old notification or link again.
    if (savedInstanceState == null) takeChat(intent)
    setContent { HyadexTheme { HyadexApp(model) } }
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    takeChat(intent)
  }

  /** A tapped notification names the chat to show; a pairing link names a server. Each is taken once. */
  private fun takeChat(intent: Intent?) {
    intent ?: return
    intent.getStringExtra(RunWatchService.EXTRA_CHAT)?.let { model.openRequest = it }
    PairingLink.parse(intent.dataString)?.let { model.pairing = it }
    intent.removeExtra(RunWatchService.EXTRA_CHAT)
    intent.data = null
  }

  override fun onStart() {
    super.onStart()
    model.setForeground(true)
    RunWatchService.stop(this)
  }

  override fun onStop() {
    // Still allowed to start a foreground service here: the app is only now leaving the screen.
    // A rotation is not leaving: the stream and the list stay as they are.
    if (!isChangingConfigurations) RunWatchService.startIfBusy(this, model.busyChats(), model.busyBaselines())
    super.onStop()
    if (!isChangingConfigurations) model.setForeground(false)
  }
}

/** Connect, then the chat list; a chat opens over it (back returns to the list). */
@Composable
fun HyadexApp(model: AppViewModel) {
  var openID by rememberSaveable { mutableStateOf<String?>(null) }
  var listCovered by remember { mutableStateOf(false) }
  var settings by remember { mutableStateOf(false) }
  val snackbar = remember { SnackbarHostState() }
  // Collected, not keyed on: clearing the notice must not cancel the snackbar showing it.
  LaunchedEffect(Unit) {
    snapshotFlow { model.notice }.filterNotNull().collect { notice ->
      model.notice = null
      val result = snackbar.showSnackbar(notice.text, actionLabel = notice.action, duration = SnackbarDuration.Short)
      if (result == SnackbarResult.ActionPerformed) notice.run()
    }
  }
  val connected = model.client != null
  LaunchedEffect(openID, connected) { if (connected) openID?.let(model::openChat) ?: model.closeChat() }
  // The open chat archived or deleted elsewhere (or closed by the server): back to the list.
  LaunchedEffect(model.chats, model.listLoaded, model.closedChat) {
    val id = openID ?: return@LaunchedEffect
    if (model.closedChat == id || (model.listLoaded && model.chats.none { it.id == id })) {
      if (model.closedChat != id) model.tell("这个聊天已归档或删除")
      model.closedChat = null
      openID = null
    }
  }
  LaunchedEffect(model.openRequest, model.chats) {
    val request = model.openRequest ?: return@LaunchedEffect
    if (model.chats.any { it.id == request }) {
      openID = request
      model.openRequest = null
    }
  }
  // Asked once, after the first connection: replies and approvals can then reach the user away from the app.
  val context = LocalContext.current
  val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
  LaunchedEffect(connected) {
    if (!connected || Build.VERSION.SDK_INT < 33) return@LaunchedEffect
    val preferences = context.getSharedPreferences("ui", Context.MODE_PRIVATE)
    if (preferences.getBoolean("asked_notifications", false)) return@LaunchedEffect
    preferences.edit().putBoolean("asked_notifications", true).apply()
    permission.launch(Manifest.permission.POST_NOTIFICATIONS)
  }
  // Landscape: content stays clear of the camera cutout and the side navigation bar.
  Box(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal))) {
    if (!connected && model.connection !is Connection.Connected) {
      ConnectScreen(model)
    } else {
      val chat = model.chats.firstOrNull { it.id == openID }
      // The list stays underneath, so the predictive back gesture shows it behind the chat;
      // while the chat covers it, it is neither drawn nor animated.
      val hidden = chat != null && listCovered
      ChatListScreen(model, onOpen = { openID = it }, onSettings = { settings = true }, animate = !hidden, modifier = Modifier.graphicsLayer { alpha = if (hidden) 0f else 1f })
      if (chat != null) {
        key(chat.id) {
          ChatPane(onClosed = { openID = null; listCovered = false }, onCovering = { listCovered = it }) { close -> ChatScreen(model, chat, onBack = close) }
        }
      }
    }
    SnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).navigationBarsPadding().imePadding().padding(bottom = 96.dp))
  }
  if (settings) SettingsDialog(model) { settings = false }
  // A pairing link while connected: never switch servers without asking.
  val pairing = model.pairing
  if (pairing != null && connected) {
    AlertDialog(
      onDismissRequest = { model.pairing = null },
      title = { Text("连接到另一台服务器？") },
      text = { Text("${pairing.address}\n\n当前连接和这台手机上保存的令牌会被替换。") },
      confirmButton = {
        TextButton(onClick = {
          model.pairing = null
          openID = null
          model.disconnect()
          model.connect(pairing.address, pairing.token)
        }) { Text("连接") }
      },
      dismissButton = { TextButton(onClick = { model.pairing = null }) { Text("取消") } },
    )
  }
}
