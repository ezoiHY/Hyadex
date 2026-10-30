package dev.hyadex.android.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.hyadex.android.core.AgentBackend
import dev.hyadex.android.ui.theme.ClaudeOrange
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * The provider's mark, drawn as vectors (a port of the Mac app's ProviderMark):
 * Claude's terracotta spark and Codex's cloud with a terminal prompt.
 */
@Composable
fun ProviderMark(backend: AgentBackend, size: Dp = 16.dp, modifier: Modifier = Modifier) {
  Canvas(modifier.size(size)) {
    val side = min(this.size.width, this.size.height)
    val center = Offset(this.size.width / 2, this.size.height / 2)
    when (backend) {
      AgentBackend.CLAUDE -> drawPath(claudeSpark(center, side / 2), ClaudeOrange)
      AgentBackend.CODEX -> {
        drawPath(
          codexCloud(center, side),
          Brush.verticalGradient(listOf(Color(0xFFBDA8FF), Color(0xFF7385FF), Color(0xFF4554FA)), startY = center.y - side / 2, endY = center.y + side / 2),
        )
        val origin = Offset(center.x - side / 2, center.y - side / 2)
        fun point(x: Float, y: Float) = Offset(origin.x + x * side, origin.y + y * side)
        val prompt = Path().apply {
          moveTo(point(0.32f, 0.37f).x, point(0.32f, 0.37f).y)
          lineTo(point(0.43f, 0.48f).x, point(0.43f, 0.48f).y)
          lineTo(point(0.32f, 0.59f).x, point(0.32f, 0.59f).y)
          moveTo(point(0.52f, 0.6f).x, point(0.52f, 0.6f).y)
          lineTo(point(0.68f, 0.6f).x, point(0.68f, 0.6f).y)
        }
        drawPath(prompt, Color.White, style = Stroke(width = side * 0.075f, cap = StrokeCap.Round, join = StrokeJoin.Round))
      }
    }
  }
}

/** (angle in degrees, length as a fraction of the radius) */
private val rays =
  listOf(6 to 0.98, 36 to 0.78, 63 to 0.96, 90 to 0.84, 116 to 1.0, 146 to 0.8, 176 to 0.97, 204 to 0.76, 232 to 0.94, 262 to 0.88, 292 to 0.98, 322 to 0.8)

/** Twelve tapered rays of uneven length around a small hub. */
private fun claudeSpark(center: Offset, radius: Float): Path {
  val path = Path()
  path.addOval(Rect(center, radius * 0.2f))
  for ((degrees, length) in rays) {
    val angle = degrees * PI / 180
    val dx = cos(angle).toFloat()
    val dy = -sin(angle).toFloat()
    val nx = -dy
    val ny = dx
    val base = radius * 0.11f
    val tip = radius * 0.065f
    val reach = radius * length.toFloat() - tip
    fun point(along: Float, across: Float) = Offset(center.x + dx * along + nx * across, center.y + dy * along + ny * across)
    val ray = Path()
    val start = point(radius * 0.05f, base)
    ray.moveTo(start.x, start.y)
    val edge = point(reach, tip)
    ray.lineTo(edge.x, edge.y)
    val cap = point(reach, 0f)
    val startAngle = Math.toDegrees(atan2(ny.toDouble(), nx.toDouble())).toFloat()
    ray.arcTo(Rect(cap, tip), startAngle, -180f, false)
    val end = point(radius * 0.05f, -base)
    ray.lineTo(end.x, end.y)
    ray.close()
    path.addPath(ray)
  }
  return path
}

/** A scalloped cloud: eight overlapping lobes around a core. */
private fun codexCloud(center: Offset, side: Float): Path {
  val path = Path()
  path.addOval(Rect(center, side * 0.36f))
  val ring = side * 0.27f
  val lobe = side * 0.2f
  for (index in 0 until 8) {
    val angle = index * PI / 4 + PI / 8
    path.addOval(Rect(Offset(center.x + cos(angle).toFloat() * ring, center.y + sin(angle).toFloat() * ring), lobe))
  }
  return path
}
