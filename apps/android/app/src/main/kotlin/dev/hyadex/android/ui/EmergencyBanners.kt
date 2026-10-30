package dev.hyadex.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.core.EmergencyAlert
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/**
 * Agents' emergency alerts, as the Mac's red banners: up to three, each with
 * 打开 (its chat) and 确认 (acknowledge). `onOpen` is null where the chat is already open.
 */
@Composable
fun EmergencyBanners(alerts: List<EmergencyAlert>, onOpen: ((String) -> Unit)?, onAcknowledge: (EmergencyAlert) -> Unit) {
  AnimatedVisibility(alerts.isNotEmpty(), enter = unfold, exit = fold) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
      for (alert in alerts.take(3)) EmergencyBanner(alert, onOpen, onAcknowledge)
      if (alerts.size > 3) Text("还有 ${alerts.size - 3} 条紧急提醒", style = HyadexType.meta, color = Hyadex.colors.red, modifier = Modifier.padding(start = 8.dp))
    }
  }
}

@Composable
private fun EmergencyBanner(alert: EmergencyAlert, onOpen: ((String) -> Unit)?, onAcknowledge: (EmergencyAlert) -> Unit) {
  val colors = Hyadex.colors
  val shape = RoundedCornerShape(Hyadex.cornerRadius)
  Row(
    Modifier.fillMaxWidth().background(colors.red.copy(alpha = 0.1f), shape).border(1.dp, colors.red.copy(alpha = 0.45f), shape)
      .padding(start = 12.dp, top = 10.dp, bottom = 4.dp, end = 4.dp)
      // Read out as it appears, like the system's own alerts.
      .semantics { liveRegion = LiveRegionMode.Assertive },
    horizontalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    Icon(Icons.Filled.Warning, null, tint = colors.red, modifier = Modifier.padding(top = 2.dp).size(18.dp))
    Column(Modifier.weight(1f)) {
      Text(
        "紧急提醒 · ${alert.title}" + if (alert.count > 1) "（${alert.count} 条）" else "",
        style = HyadexType.secondary, color = colors.red, maxLines = 1, overflow = TextOverflow.Ellipsis,
      )
      Text(alert.message, style = HyadexType.secondary, color = colors.primary, maxLines = 3, overflow = TextOverflow.Ellipsis, modifier = Modifier.padding(top = 2.dp))
      Row(Modifier.align(Alignment.End), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
        if (onOpen != null) TextButton(onClick = { onOpen(alert.chatID) }) { Text("打开") }
        TextButton(onClick = { onAcknowledge(alert) }) { Text("确认", color = colors.red) }
      }
    }
  }
}
