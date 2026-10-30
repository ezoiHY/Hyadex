package dev.hyadex.android.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material.icons.outlined.QuestionAnswer
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import dev.hyadex.android.core.ApprovalDecision
import dev.hyadex.android.core.FormField
import dev.hyadex.android.core.InteractionForm
import dev.hyadex.android.core.InteractionReply
import dev.hyadex.android.core.TimelineItem
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** Questions an agent asks, or an MCP server's form, answered in place. */
@Composable
fun FormCard(item: TimelineItem.Form, onReply: (InteractionReply) -> Unit) {
  val colors = Hyadex.colors
  val form = item.form
  if (item.decision != ApprovalDecision.Pending) {
    val summary = (item.decision as? ApprovalDecision.Answered)?.summary
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
      Icon(Icons.Outlined.CheckCircle, null, tint = if (item.decision == ApprovalDecision.Denied) colors.red else colors.green, modifier = Modifier.size(16.dp))
      Text(decisionLabel(item.decision) + (summary?.takeIf { it.isNotEmpty() }?.let { "：$it" } ?: ""), style = HyadexType.secondary, color = colors.secondary, maxLines = 2)
    }
    return
  }
  // A new request starts with empty answers.
  val answers = remember(item.id) { mutableStateMapOf<String, List<String>>().apply { form.fields.forEach { put(it.id, it.initial) } } }
  val uriHandler = LocalUriHandler.current
  Column(Modifier.fillMaxWidth().card(colors.orange).padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
      Icon(Icons.Outlined.QuestionAnswer, null, tint = colors.orange, modifier = Modifier.size(16.dp))
      Text(form.title, style = HyadexType.headline, color = colors.primary, modifier = Modifier.weight(1f))
      Text("等待你回答", style = HyadexType.meta, color = colors.orange)
    }
    if (form.message.isNotEmpty()) Text(form.message, style = HyadexType.secondary, color = colors.secondary)
    form.url?.let { url ->
      Text(url, style = HyadexType.mono, color = colors.accent, modifier = Modifier.clickable { runCatching { uriHandler.openUri(url) } })
    }
    for (field in form.fields) FieldView(field, answers[field.id] ?: emptyList()) { answers[field.id] = it }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(onClick = { onReply(form.dismissal) }) { Text(if (form.kind == InteractionForm.Kind.QUESTIONS) "跳过" else "拒绝") }
      Spacer(Modifier.weight(1f))
      Button(onClick = { onReply(form.reply(answers.toMap())) }, enabled = form.isComplete(answers.toMap())) {
        Text(if (form.url != null) "已完成" else "提交")
      }
    }
  }
}

@Composable
private fun FieldView(field: FormField, values: List<String>, onChange: (List<String>) -> Unit) {
  val colors = Hyadex.colors
  Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
    if (field.header.isNotEmpty()) Text(field.header, style = HyadexType.meta, color = colors.secondary)
    if (field.prompt.isNotEmpty() && field.input != FormField.Input.Toggle) {
      Text(field.prompt + if (field.required) "" else "（可选）", style = HyadexType.secondary, color = colors.primary)
    }
    if (field.detail.isNotEmpty()) Text(field.detail, style = HyadexType.meta, color = colors.secondary)
    when (val input = field.input) {
      is FormField.Input.Choice -> {
        val known = input.options.map { it.value }.toSet()
        for (option in input.options) {
          val selected = option.value in values
          Row(
            Modifier.fillMaxWidth().clickable {
              onChange(if (input.multiple) (if (selected) values - option.value else values + option.value) else listOf(option.value))
            },
            verticalAlignment = Alignment.CenterVertically,
          ) {
            if (input.multiple) Checkbox(selected, onCheckedChange = null) else RadioButton(selected, onClick = null)
            Column(Modifier.padding(start = 8.dp, top = 6.dp, bottom = 6.dp)) {
              Text(option.label, style = HyadexType.secondary, color = colors.primary)
              if (option.detail.isNotEmpty()) Text(option.detail, style = HyadexType.meta, color = colors.secondary)
            }
          }
        }
        if (input.allowsOther) {
          val other = values.firstOrNull { it !in known } ?: ""
          OutlinedTextField(
            value = other,
            onValueChange = { text -> onChange((if (input.multiple) values.filter { it in known } else emptyList()) + listOfNotNull(text.takeIf { it.isNotEmpty() })) },
            placeholder = { Text("其他") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
          )
        }
      }
      is FormField.Input.Text ->
        OutlinedTextField(
          value = values.firstOrNull() ?: "",
          onValueChange = { onChange(listOf(it)) },
          visualTransformation = if (input.secret) PasswordVisualTransformation() else VisualTransformation.None,
          modifier = Modifier.fillMaxWidth(),
        )
      is FormField.Input.Number ->
        OutlinedTextField(
          value = values.firstOrNull() ?: "",
          onValueChange = { onChange(listOf(it)) },
          singleLine = true,
          keyboardOptions = KeyboardOptions(keyboardType = if (input.integer) KeyboardType.Number else KeyboardType.Decimal),
          modifier = Modifier.fillMaxWidth(),
        )
      FormField.Input.Toggle ->
        Row(verticalAlignment = Alignment.CenterVertically) {
          Text(field.prompt, style = HyadexType.secondary, color = colors.primary, modifier = Modifier.weight(1f))
          Switch(values.firstOrNull() == "true", onCheckedChange = { onChange(listOf(if (it) "true" else "false")) })
        }
    }
  }
}
