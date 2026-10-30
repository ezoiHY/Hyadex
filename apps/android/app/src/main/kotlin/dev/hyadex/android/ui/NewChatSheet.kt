package dev.hyadex.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** A new Work chat: the agent and the folder it works in (a server path). */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NewChatSheet(model: AppViewModel, onDismiss: () -> Unit, onCreated: (String) -> Unit) {
  val colors = Hyadex.colors
  val folders = model.knownFolders
  var backend by rememberSaveable { mutableStateOf(AgentBackend.CLAUDE) }
  var cwd by rememberSaveable { mutableStateOf(folders.firstOrNull() ?: "") }
  var project by rememberSaveable { mutableStateOf("") }
  ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true), containerColor = colors.background) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp).navigationBarsPadding().imePadding(), verticalArrangement = Arrangement.spacedBy(14.dp)) {
      Text("新建聊天", style = HyadexType.title, color = colors.primary)
      Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        for (option in AgentBackend.entries) {
          val selected = option == backend
          Row(
            Modifier.weight(1f).clip(RoundedCornerShape(Hyadex.cornerRadius))
              .background(if (selected) colors.accent.copy(alpha = 0.12f) else colors.card)
              .border(if (selected) 1.5.dp else 0.5.dp, if (selected) colors.accent else colors.separator, RoundedCornerShape(Hyadex.cornerRadius))
              .clickable { backend = option }.padding(14.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
          ) {
            ProviderMark(option, 22.dp)
            Text(option.displayName, style = HyadexType.headline, color = colors.primary)
          }
        }
      }
      OutlinedTextField(cwd, { cwd = it }, label = { Text("文件夹（服务器上的路径）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
      if (folders.isNotEmpty()) {
        Text("最近用过", style = HyadexType.meta, color = colors.secondary)
        Column(Modifier.clip(RoundedCornerShape(Hyadex.cornerRadius)).card()) {
          for (folder in folders.take(5)) {
            Text(
              folder, style = HyadexType.mono, color = if (folder == cwd) colors.accent else colors.primary, maxLines = 1, overflow = TextOverflow.StartEllipsis,
              modifier = Modifier.fillMaxWidth().clickable { cwd = folder }.padding(horizontal = 12.dp, vertical = 10.dp),
            )
          }
        }
      }
      OutlinedTextField(project, { project = it }, label = { Text("项目（可选）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
      Button(onClick = { model.createChat(backend, cwd, project, onCreated) }, enabled = cwd.isNotBlank(), modifier = Modifier.fillMaxWidth().padding(bottom = 12.dp)) {
        Text("创建")
      }
    }
  }
}
