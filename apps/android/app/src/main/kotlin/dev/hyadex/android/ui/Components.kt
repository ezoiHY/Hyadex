package dev.hyadex.android.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.KeyboardArrowRight
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.hyadex.android.core.formatElapsed
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType
import java.time.Duration
import java.time.Instant
import kotlinx.coroutines.delay

/** How folded work opens and closes: it grows from its header and fades, as Android's own lists do. */
val unfold = expandVertically(spring(stiffness = Spring.StiffnessMediumLow), expandFrom = Alignment.Top) + fadeIn(tween(180))
val fold = shrinkVertically(spring(stiffness = Spring.StiffnessMedium), shrinkTowards = Alignment.Top) + fadeOut(tween(120))

/** The small rounded card behind tool output, approvals and changes. */
@Composable
fun Modifier.card(tint: Color? = null): Modifier {
  val colors = Hyadex.colors
  val shape = RoundedCornerShape(Hyadex.cornerRadius)
  return this
    .background(tint?.copy(alpha = 0.08f) ?: colors.card, shape)
    .border(if (tint == null) 0.5.dp else 1.dp, tint?.copy(alpha = 0.45f) ?: colors.separator, shape)
}

@Composable
fun Chevron(expanded: Boolean, modifier: Modifier = Modifier) {
  Icon(
    Icons.AutoMirrored.Outlined.KeyboardArrowRight,
    contentDescription = if (expanded) "收起" else "展开",
    tint = Hyadex.colors.tertiary,
    modifier = modifier.rotate(animateFloatAsState(if (expanded) 90f else 0f, spring(stiffness = Spring.StiffnessMediumLow), label = "chevron").value),
  )
}

/** Monospaced text in a card, scrolling past `maxHeight`. */
@Composable
fun CodeBlock(code: String, maxHeight: Dp = 220.dp, wraps: Boolean = false) {
  val scroll = rememberScrollState()
  val horizontal = rememberScrollState()
  Box(Modifier.fillMaxWidth().card().heightIn(max = maxHeight).verticalScroll(scroll)) {
    SelectionContainer {
      Text(
        code.trimEnd(),
        style = HyadexType.mono,
        color = Hyadex.colors.primary,
        softWrap = wraps,
        modifier = Modifier.padding(10.dp).let { if (wraps) it else it.horizontalScroll(horizontal) },
      )
    }
  }
}

/** "12 秒", "1:05", ticking once a second. */
@Composable
fun Elapsed(since: Instant, style: TextStyle = HyadexType.meta) {
  var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
  LaunchedEffect(since) {
    while (true) {
      now = System.currentTimeMillis()
      delay(1_000)
    }
  }
  Text(formatElapsed(Duration.between(since, Instant.ofEpochMilli(now)).seconds), style = style, color = Hyadex.colors.tertiary)
}

/**
 * Secondary text with a light sweeping across it while the agent works. The
 * sweep is drawn over the text's own pixels; its position is read while drawing,
 * so the text never recomposes for it.
 */
@Composable
fun ShimmerText(text: String, active: Boolean, style: TextStyle = HyadexType.secondary) {
  val colors = Hyadex.colors
  if (!active) {
    Text(text, style = style, color = colors.secondary)
    return
  }
  val phase = rememberInfiniteTransition(label = "shimmer").animateFloat(
    initialValue = -0.3f,
    targetValue = 1.3f,
    animationSpec = infiniteRepeatable(tween(1_600, easing = LinearEasing), RepeatMode.Restart),
    label = "phase",
  )
  val highlight = colors.primary
  Text(
    text,
    style = style,
    color = colors.secondary,
    modifier = Modifier.graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }.drawWithContent {
      drawContent()
      val center = size.width * phase.value
      val reach = size.width * 0.15f
      drawRect(
        Brush.horizontalGradient(listOf(Color.Transparent, highlight, Color.Transparent), startX = center - reach, endX = center + reach),
        blendMode = BlendMode.SrcAtop,
      )
    },
  )
}
