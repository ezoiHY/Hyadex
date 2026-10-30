package dev.hyadex.android.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** What a chat's menu can ask the screen for: the steps that need a dialog or another screen. */
class ChatRowActions(
  val open: (String) -> Unit,
  val rename: (ChatSummary) -> Unit,
  val move: (ChatSummary) -> Unit,
  val delete: (ChatSummary) -> Unit,
  val fork: (ChatSummary) -> Unit,
)

/** A chat's menu, as the Mac sidebar's right-click menu: read state, pin, rename, fork, project, archive, delete. */
@Composable
fun ChatMenuItems(chat: ChatSummary, model: AppViewModel, actions: ChatRowActions, dismiss: () -> Unit) {
  val colors = Hyadex.colors
  fun pick(action: () -> Unit): () -> Unit = {
    dismiss()
    action()
  }
  DropdownMenuItem(text = { Text(if (chat.unread) "标为已读" else "标为未读") }, onClick = pick { if (chat.unread) model.markRead(chat.id) else model.markUnread(chat.id) })
  if (!chat.archived) DropdownMenuItem(text = { Text(if (chat.pinned) "取消置顶" else "置顶") }, onClick = pick { model.updateChat(chat.id, mapOf("pinned" to !chat.pinned)) })
  DropdownMenuItem(text = { Text("重命名…") }, onClick = pick { actions.rename(chat) })
  if (!chat.archived) DropdownMenuItem(text = { Text("分叉") }, onClick = pick { actions.fork(chat) })
  DropdownMenuItem(text = { Text("移到项目…") }, onClick = pick { actions.move(chat) })
  if (chat.archived) {
    DropdownMenuItem(text = { Text("取消归档") }, onClick = pick { model.unarchive(chat) })
  } else {
    DropdownMenuItem(text = { Text("归档") }, onClick = pick { model.archive(chat) })
  }
  HorizontalDivider(color = colors.separator, thickness = 0.5.dp)
  DropdownMenuItem(text = { Text("删除…", color = colors.red) }, onClick = pick { actions.delete(chat) })
}

/** Moves a chat into one of the projects in use, a new one, or out of its project. */
@Composable
fun MoveToProjectDialog(chat: ChatSummary, projects: List<String>, onDismiss: () -> Unit, onMove: (String?) -> Unit) {
  val colors = Hyadex.colors
  var name by remember { mutableStateOf("") }
  AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("移到项目") },
    text = {
      Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (projects.isNotEmpty()) {
          Column(Modifier.heightIn(max = 260.dp).verticalScroll(rememberScrollState())) {
            for (project in projects) {
              Row(
                Modifier.fillMaxWidth().clickable { onMove(project) }.padding(vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp),
              ) {
                Icon(Icons.Outlined.Folder, null, tint = colors.secondary, modifier = Modifier.size(18.dp))
                Text(project, style = HyadexType.body, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                if (project == chat.folder) Icon(Icons.Outlined.Check, "当前项目", tint = colors.accent, modifier = Modifier.size(18.dp))
              }
            }
          }
        }
        OutlinedTextField(name, { name = it }, label = { Text("新项目") }, singleLine = true, modifier = Modifier.fillMaxWidth())
      }
    },
    confirmButton = { TextButton(onClick = { onMove(name.trim()) }, enabled = name.isNotBlank()) { Text("新建并移入") } },
    dismissButton = {
      Row {
        if (chat.folder != null) TextButton(onClick = { onMove(null) }) { Text("移出项目") }
        TextButton(onClick = onDismiss) { Text("取消") }
      }
    },
  )
}

@Composable
fun DeleteChatDialog(chat: ChatSummary, onDismiss: () -> Unit, onDelete: () -> Unit) {
  AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("删除「${chat.title}」？") },
    text = { Text("聊天和它的全部记录会从服务器上删除，无法恢复。") },
    confirmButton = { TextButton(onClick = onDelete) { Text("删除", color = Hyadex.colors.red) } },
    dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
  )
}
