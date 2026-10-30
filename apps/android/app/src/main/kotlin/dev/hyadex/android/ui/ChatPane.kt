package dev.hyadex.android.ui

import androidx.activity.BackEventCompat
import androidx.activity.compose.PredictiveBackHandler
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animate
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.launch

/**
 * The open chat over the chat list. It slides in from the right; back, the
 * arrow or the predictive back gesture slide it away. During the gesture it
 * shrinks toward the finger with rounded corners, showing the list behind it,
 * and springs back if the gesture is cancelled.
 */
@Composable
fun ChatPane(onClosed: () -> Unit, onCovering: (Boolean) -> Unit = {}, content: @Composable (close: () -> Unit) -> Unit) {
  BoxWithConstraints(Modifier.fillMaxSize()) {
    val width = constraints.maxWidth.toFloat()
    val scope = rememberCoroutineScope()
    // Slides in once: after a rotation or a restore the chat is simply there.
    var entered by rememberSaveable { mutableStateOf(false) }
    val slide = remember { Animatable(if (entered) 0f else width) }
    var progress by remember { mutableFloatStateOf(0f) }
    var fromLeft by remember { mutableStateOf(true) }
    var closing by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
      slide.animateTo(0f, spring(dampingRatio = Spring.DampingRatioNoBouncy, stiffness = Spring.StiffnessMediumLow))
      entered = true
    }
    // While the chat fully covers the list, the list need not draw or animate.
    LaunchedEffect(Unit) { snapshotFlow { slide.value == 0f && progress == 0f && !closing }.collect { onCovering(it) } }
    val close: () -> Unit = {
      if (!closing) {
        closing = true
        scope.launch {
          slide.animateTo(width, tween(220, easing = FastOutLinearInEasing))
          onClosed()
        }
      }
    }
    PredictiveBackHandler(enabled = !closing) { events ->
      try {
        events.collect { event ->
          progress = event.progress
          fromLeft = event.swipeEdge == BackEventCompat.EDGE_LEFT
        }
        closing = true
        slide.animateTo(width, tween(200, easing = FastOutLinearInEasing))
        onClosed()
      } catch (cancelled: CancellationException) {
        val from = progress
        scope.launch { animate(from, 0f, animationSpec = spring(stiffness = Spring.StiffnessMedium)) { value, _ -> progress = value } }
        throw cancelled
      }
    }
    // The list behind dims a little while the chat covers it.
    val covered = 1f - (slide.value / width).coerceIn(0f, 1f)
    Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.18f * covered * (1f - progress))))
    Box(
      // Touches stop here: none reach the list underneath.
      Modifier.fillMaxSize().pointerInput(Unit) {}.graphicsLayer {
        val scale = 1f - 0.1f * progress
        scaleX = scale
        scaleY = scale
        translationX = slide.value + (if (fromLeft) 1f else -1f) * progress * width * 0.05f
        if (progress > 0f) {
          shape = RoundedCornerShape((28 * progress).dp)
          clip = true
        }
      }
    ) {
      content(close)
    }
  }
}
