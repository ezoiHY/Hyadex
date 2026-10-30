package dev.hyadex.android.ui

import android.util.LruCache
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.sp
import com.mikepenz.markdown.compose.components.markdownComponents
import com.mikepenz.markdown.compose.elements.highlightedCodeBlock
import com.mikepenz.markdown.compose.elements.highlightedCodeFence
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.m3.markdownColor
import com.mikepenz.markdown.m3.markdownTypography
import com.mikepenz.markdown.model.MarkdownState
import com.mikepenz.markdown.model.markdownAnimations
import com.mikepenz.markdown.model.State
import com.mikepenz.markdown.model.parseMarkdown
import com.mikepenz.markdown.model.rememberStreamingMarkdownState
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

/** Parsed answers by their text: a message scrolled back into view draws at once instead of parsing again. */
object MarkdownCache {
  // Sized by characters, so a few long answers cannot crowd out the rest (about 600 KB of text).
  private val cache = object : LruCache<String, State>(600_000) {
    override fun sizeOf(key: String, value: State) = key.length.coerceAtLeast(1)
  }

  fun get(text: String): State? = cache.get(text)

  fun parse(text: String): State = cache.get(text) ?: parseMarkdown(text).also { cache.put(text, it) }

  /** Parses a long answer ahead of showing it (off the main thread), so it never appears as plain text first. */
  fun warm(text: String) {
    if (text.length > INLINE_PARSE_LIMIT && cache.get(text) == null) parse(text)
  }
}

/** Short answers parse in about a millisecond, so they parse at once and the row never changes height. */
private const val INLINE_PARSE_LIMIT = 4_000

private class ParsedMarkdownState(parsed: State) : MarkdownState {
  override val state: StateFlow<State> = MutableStateFlow(parsed)
  override val links: StateFlow<Map<String, String>> = MutableStateFlow(emptyMap())

  override suspend fun parse(): State = state.value
}

/** An agent's finished answer as Markdown; a long one shows as plain text while it parses off the main thread. */
@Composable
fun MessageText(text: String) {
  val ready = remember(text) { MarkdownCache.get(text) ?: if (text.length <= INLINE_PARSE_LIMIT) MarkdownCache.parse(text) else null }
  val parsed by produceState(ready, text) {
    if (value == null) value = withContext(Dispatchers.Default) { MarkdownCache.parse(text) }
  }
  val state = parsed
  if (state == null) {
    Text(text, style = HyadexType.body, color = Hyadex.colors.primary)
    return
  }
  AnswerSurface { Markdown(remember(state) { ParsedMarkdownState(state) }, colors = answerColors(), typography = answerTypography(), components = answerComponents) }
}

/** An answer still being written: each update parses only the newly arrived tail. */
@Composable
fun StreamingMessageText(text: String) {
  // A provider that rewrites what it already sent starts the stream over.
  var generation by remember { mutableIntStateOf(0) }
  key(generation) {
    val stream = rememberStreamingMarkdownState()
    var appended by remember { mutableStateOf("") }
    LaunchedEffect(text) {
      if (!text.startsWith(appended)) {
        generation++
        return@LaunchedEffect
      }
      if (text.length > appended.length) {
        // The tail is parsed off the main thread.
        withContext(Dispatchers.Default) { stream.append(text.substring(appended.length)) }
        appended = text
      }
    }
    // Growing paragraphs are not size-animated: each update would animate and re-measure the list.
    AnswerSurface {
      Markdown(stream, colors = answerColors(), typography = answerTypography(), components = answerComponents, animations = markdownAnimations(animateTextSize = { this }))
    }
  }
}

/**
 * Links in answers: web pages open in the browser. Agents also link files on
 * the server ("hello.txt", "/Users/…"); a phone cannot open those, so they do nothing.
 */
private class WebOnlyUriHandler(private val system: UriHandler) : UriHandler {
  override fun openUri(uri: String) {
    val scheme = uri.substringBefore(':', "").lowercase()
    if (scheme == "http" || scheme == "https" || scheme == "mailto") runCatching { system.openUri(uri) }
  }
}

@Composable
private fun AnswerSurface(content: @Composable () -> Unit) {
  val system = LocalUriHandler.current
  val handler = remember(system) { WebOnlyUriHandler(system) }
  CompositionLocalProvider(LocalUriHandler provides handler) { SelectionContainer { content() } }
}

private val answerComponents = markdownComponents(codeBlock = highlightedCodeBlock, codeFence = highlightedCodeFence)

@Composable
private fun answerColors() =
  Hyadex.colors.let { markdownColor(text = it.primary, codeBackground = it.card, inlineCodeBackground = it.bubble, dividerColor = it.separator) }

@Composable
private fun answerTypography() =
  markdownTypography(
    h1 = HyadexType.title.copy(fontSize = 22.sp),
    h2 = HyadexType.title,
    h3 = HyadexType.headline,
    h4 = HyadexType.headline,
    h5 = HyadexType.headline,
    h6 = HyadexType.headline,
    text = HyadexType.body,
    paragraph = HyadexType.body,
    code = HyadexType.mono,
    inlineCode = HyadexType.body.copy(fontFamily = FontFamily.Monospace, fontSize = 14.sp),
    ordered = HyadexType.body,
    bullet = HyadexType.body,
    list = HyadexType.body,
    quote = HyadexType.body.copy(color = Hyadex.colors.secondary),
  )
