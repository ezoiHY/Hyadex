package dev.hyadex.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.hyadex.android.core.ChatSummary
import dev.hyadex.android.core.SearchHit
import dev.hyadex.android.core.relativeShort
import dev.hyadex.android.core.searchMatches
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** A chat whose messages match the search: its title, how many matches, and the newest one with the words in bold. */
@Composable
fun SearchHitRow(hit: SearchHit, chat: ChatSummary, query: String, onOpen: () -> Unit) {
  val colors = Hyadex.colors
  val snippet = remember(hit.snippet, query, colors.primary) { highlighted(hit.snippet, query, colors.primary) }
  Row(
    Modifier.fillMaxWidth().background(colors.sidebar).clickable(onClick = onOpen).padding(horizontal = 20.dp, vertical = 10.dp),
    horizontalArrangement = Arrangement.spacedBy(12.dp),
  ) {
    ProviderMark(chat.backend, 22.dp, Modifier.padding(top = 2.dp))
    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(chat.title, style = HyadexType.body, color = colors.primary, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        Text(if (hit.matches > 1) "${hit.matches} 处" else (hit.time?.let(::relativeShort) ?: ""), style = HyadexType.meta, color = colors.tertiary)
      }
      Text(snippet, style = HyadexType.secondary, color = colors.secondary, maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
  }
}

private fun highlighted(text: String, query: String, strong: Color): AnnotatedString = buildAnnotatedString {
  append(text)
  for (range in searchMatches(text, query)) addStyle(SpanStyle(fontWeight = FontWeight.SemiBold, color = strong), range.first, range.last + 1)
}
