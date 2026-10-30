package dev.hyadex.android.ui

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import dev.hyadex.android.app.AppSettings
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.app.Connection
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.core.UsageWindow
import dev.hyadex.android.core.resetLabel
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlin.math.roundToInt

/** The Mac app's Settings on a phone: the server, how the app looks, notifications, and about. */
@Composable
fun SettingsScreen(model: AppViewModel, onBack: () -> Unit, onLicenses: () -> Unit) {
  val colors = Hyadex.colors
  val settings = model.settings
  val context = LocalContext.current
  var confirmDisconnect by remember { mutableStateOf(false) }
  // Read again on return: the user may have just changed it in the system's settings.
  var notificationsOn by remember { mutableStateOf(NotificationManagerCompat.from(context).areNotificationsEnabled()) }
  LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { notificationsOn = NotificationManagerCompat.from(context).areNotificationsEnabled() }
  val connection = model.connection
  Column(Modifier.fillMaxSize().background(colors.sidebar)) {
    PaneTopBar("设置", onBack)
    Column(
      Modifier.weight(1f).verticalScroll(rememberScrollState()).navigationBarsPadding().padding(horizontal = 16.dp).padding(bottom = 24.dp),
    ) {
      SettingsSection("服务器") {
        ServerStatusRow(connection, model.offline)
        if (connection is Connection.Failed || model.offline) {
          SettingsDivider()
          SettingRow("重新连接", tint = colors.accent, onClick = model::reconnect)
        }
        SettingsDivider()
        SettingRow("断开连接", tint = colors.red, onClick = { confirmDisconnect = true })
      }
      SettingsNote("断开后会删除这台手机上保存的地址和令牌，之后可以扫码连接其他服务器。")
      SettingsSection("通用") {
        ChoiceRow("主题", AppSettings.Theme.entries, settings.theme, { it.label }) { settings.theme = it }
        SettingsDivider()
        ChoiceRow("字号", AppSettings.TextSize.entries, settings.textSize, { it.label }) { settings.textSize = it }
        SettingsDivider()
        SwitchRow("默认展开处理过程", settings.expandWork) { settings.expandWork = it }
        SettingsDivider()
        SwitchRow("按项目分组", settings.groupByProject) { settings.groupByProject = it }
        SettingsDivider()
        SwitchRow("显示套餐用量", settings.showUsage) {
          settings.showUsage = it
          if (it) model.loadUsage()
        }
      }
      SettingsNote("字号在系统字体大小的基础上再放大一档。套餐用量显示在聊天列表底部。")
      SettingsSection("通知") {
        SettingRow(
          "系统通知", detail = if (notificationsOn) "已允许" else "未允许",
          detailTint = if (notificationsOn) null else colors.orange,
          onClick = {
            runCatching { context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)) }
          },
        )
        SettingsDivider()
        SwitchRow("离开 app 后继续留意", settings.watchInBackground) { settings.watchInBackground = it }
        SettingsDivider()
        SwitchRow("等你批准时通知", settings.notifyApproval) { settings.notifyApproval = it }
        SettingsDivider()
        SwitchRow("回复完成时通知", settings.notifyFinished) { settings.notifyFinished = it }
      }
      SettingsNote("离开 app 时如果有聊天还在运行，会继续留意最多 2 小时，回复完成或等你批准时发通知。")
      if (settings.showUsage && connection is Connection.Connected) UsageSection(model)
      SettingsSection("关于") {
        SettingRow("版本", detail = remember { appVersion(context) })
        if (connection is Connection.Connected) {
          SettingsDivider()
          SettingRow("服务器版本", detail = listOfNotNull(connection.version, connection.contract?.let { "API 契约 $it" }).joinToString(" · ").ifEmpty { "未知" })
        }
        SettingsDivider()
        SettingRow("源代码", detail = "github.com/ezoiHY/Hyadex", onClick = {
          runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://github.com/ezoiHY/Hyadex"))) }
        })
        SettingsDivider()
        SettingRow("开源许可", chevron = true, onClick = onLicenses)
      }
    }
  }
  if (confirmDisconnect) {
    AlertDialog(
      onDismissRequest = { confirmDisconnect = false },
      title = { Text("断开连接？") },
      text = { Text("这台手机上保存的地址和令牌会被删除。") },
      confirmButton = {
        TextButton(onClick = { confirmDisconnect = false; model.disconnect() }) { Text("断开", color = colors.red) }
      },
      dismissButton = { TextButton(onClick = { confirmDisconnect = false }) { Text("取消") } },
    )
  }
}

/** The server as the list's footer shows it, with its address. */
@Composable
private fun ServerStatusRow(connection: Connection, offline: Boolean) {
  val colors = Hyadex.colors
  val (tint, title, detail) = when (connection) {
    is Connection.Connected ->
      Triple(if (offline) colors.yellow else colors.green, connection.serverName ?: connection.address, if (offline) "无法连接，正在重试…" else "已连接 · " + connection.address)
    is Connection.Failed -> Triple(colors.red, connection.address, connection.message)
    Connection.Connecting -> Triple(colors.yellow, "正在连接…", "")
    Connection.NotConfigured -> Triple(colors.tertiary, "未连接", "")
  }
  Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
    Box(Modifier.size(9.dp).clip(CircleShape).background(tint))
    Column(Modifier.weight(1f)) {
      Text(title, style = HyadexType.body, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis)
      if (detail.isNotEmpty()) Text(detail, style = HyadexType.meta, color = colors.secondary, maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
  }
}

/** Each provider's plan limits as its own account reports them, as the Mac's usage popover. */
@Composable
private fun UsageSection(model: AppViewModel) {
  val colors = Hyadex.colors
  LaunchedEffect(Unit) { model.loadUsage() }
  SettingsSection("套餐用量") {
    AgentBackend.entries.forEachIndexed { index, backend ->
      if (index > 0) SettingsDivider()
      val usage = model.usage[backend]
      Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          ProviderMark(backend, 18.dp)
          Text(backend.displayName, style = HyadexType.body, color = colors.primary)
        }
        when {
          usage == null -> Text("正在读取…", style = HyadexType.meta, color = colors.tertiary)
          !usage.available || usage.windows.isEmpty() -> Text("暂时没有用量数据", style = HyadexType.meta, color = colors.tertiary)
          else -> for (window in usage.windows) UsageMeter(window)
        }
      }
    }
    SettingsDivider()
    SettingRow("刷新", tint = colors.accent, onClick = { model.loadUsage(refresh = true) })
  }
}

@Composable
private fun UsageMeter(window: UsageWindow) {
  val colors = Hyadex.colors
  val used = window.usedPercent
  val tint = when {
    used == null -> colors.tertiary
    used >= 90 -> colors.red
    used >= 70 -> colors.orange
    else -> colors.accent
  }
  Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
    Row {
      Text(window.label, style = HyadexType.secondary, color = colors.primary, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
      Text(used?.let { "${it.roundToInt()}%" } ?: "—", style = HyadexType.secondary, color = tint)
    }
    LinearProgressIndicator(
      progress = { ((used ?: 0.0) / 100).toFloat().coerceIn(0f, 1f) },
      modifier = Modifier.fillMaxWidth().height(4.dp).clip(CircleShape),
      color = tint, trackColor = colors.bubble, drawStopIndicator = {},
    )
    window.resetsAt?.let { Text(resetLabel(it), style = HyadexType.meta, color = colors.tertiary) }
  }
}

private fun appVersion(context: Context): String {
  val info = if (Build.VERSION.SDK_INT >= 33) {
    context.packageManager.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0))
  } else {
    @Suppress("DEPRECATION")
    context.packageManager.getPackageInfo(context.packageName, 0)
  }
  return "${info.versionName}（${info.longVersionCode}）"
}

/** A titled group of rows on a card, as in the system's own settings. */
@Composable
fun SettingsSection(title: String, content: @Composable ColumnScope.() -> Unit) {
  val colors = Hyadex.colors
  Text(title, style = HyadexType.meta, color = colors.secondary, modifier = Modifier.padding(start = 14.dp, top = 20.dp, bottom = 6.dp))
  Column(Modifier.fillMaxWidth().clip(androidx.compose.foundation.shape.RoundedCornerShape(Hyadex.cornerRadius)).card(), content = content)
}

@Composable
fun SettingsNote(text: String) {
  Text(text, style = HyadexType.meta, color = Hyadex.colors.tertiary, modifier = Modifier.padding(start = 14.dp, end = 14.dp, top = 6.dp))
}

@Composable
fun SettingsDivider() {
  HorizontalDivider(Modifier.padding(start = 14.dp), color = Hyadex.colors.separator, thickness = 0.5.dp)
}

/** A row with a title, an optional value on the right, and an action when tapped. */
@Composable
fun SettingRow(
  title: String,
  detail: String? = null,
  tint: Color? = null,
  detailTint: Color? = null,
  chevron: Boolean = false,
  /** A path keeps its end (the folder's name) when it does not fit. */
  path: Boolean = false,
  onClick: (() -> Unit)? = null,
) {
  val colors = Hyadex.colors
  Row(
    Modifier.fillMaxWidth().let { if (onClick != null) it.clickable(onClick = onClick) else it }.padding(horizontal = 14.dp, vertical = 13.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    // The title never wraps; a long value gives way instead.
    Text(title, style = HyadexType.body, color = tint ?: colors.primary, maxLines = 1, softWrap = false)
    Text(
      detail ?: "", style = HyadexType.secondary, color = detailTint ?: colors.secondary, maxLines = 1,
      overflow = if (path) TextOverflow.StartEllipsis else TextOverflow.Ellipsis, textAlign = TextAlign.End, modifier = Modifier.weight(1f),
    )
    if (chevron) Chevron(expanded = false, modifier = Modifier.size(18.dp))
  }
}

@Composable
fun SwitchRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit) {
  val colors = Hyadex.colors
  Row(
    Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(start = 14.dp, end = 10.dp, top = 6.dp, bottom = 6.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(title, style = HyadexType.body, color = colors.primary, modifier = Modifier.weight(1f))
    Switch(
      checked, onChange,
      colors = SwitchDefaults.colors(
        checkedTrackColor = colors.accent, checkedThumbColor = Color.White,
        // Off reads clearly on the card in both themes.
        uncheckedTrackColor = colors.bubble, uncheckedThumbColor = colors.secondary, uncheckedBorderColor = colors.tertiary,
      ),
    )
  }
}

/** One of a few choices, as a segmented control under its title. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun <T> ChoiceRow(title: String, options: List<T>, selected: T, label: (T) -> String, onPick: (T) -> Unit) {
  val colors = Hyadex.colors
  Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
    Text(title, style = HyadexType.body, color = colors.primary)
    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
      options.forEachIndexed { index, option ->
        SegmentedButton(
          selected = option == selected,
          onClick = { onPick(option) },
          shape = SegmentedButtonDefaults.itemShape(index, options.size),
          icon = {},
          colors = SegmentedButtonDefaults.colors(
            activeContainerColor = colors.accent.copy(alpha = 0.14f), activeContentColor = colors.accent, activeBorderColor = colors.accent.copy(alpha = 0.5f),
            inactiveContainerColor = Color.Transparent, inactiveContentColor = colors.primary, inactiveBorderColor = colors.separator,
          ),
        ) { Text(label(option), style = HyadexType.secondary) }
      }
    }
  }
}
