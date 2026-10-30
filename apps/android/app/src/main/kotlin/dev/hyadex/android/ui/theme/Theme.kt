package dev.hyadex.android.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.hyadex.android.core.ActivityState
import dev.hyadex.android.core.AgentBackend

/**
 * The Mac app's look on a phone: system grays in three tiers of text, the
 * system blue accent, orange for whatever waits on the user, and each
 * provider's own colour.
 */
@Immutable
data class HyadexColors(
  val background: Color,
  val sidebar: Color,
  val card: Color,
  val separator: Color,
  val primary: Color,
  val secondary: Color,
  val tertiary: Color,
  val bubble: Color,
  val accent: Color,
  val orange: Color,
  val green: Color,
  val red: Color,
  val yellow: Color,
)

private val Dark =
  HyadexColors(
    background = Color(0xFF242424),
    sidebar = Color(0xFF2A2A2A),
    card = Color(0xFF2F2F2F),
    separator = Color(0xFF3D3D3D),
    primary = Color(0xFFECECEC),
    secondary = Color(0xFF9D9D9D),
    tertiary = Color(0xFF6C6C6C),
    bubble = Color(0xFF363636),
    accent = Color(0xFF0A84FF),
    orange = Color(0xFFFF9F0A),
    green = Color(0xFF32D74B),
    red = Color(0xFFFF453A),
    yellow = Color(0xFFFFD60A),
  )

private val Light =
  HyadexColors(
    background = Color(0xFFFFFFFF),
    sidebar = Color(0xFFF5F5F5),
    card = Color(0xFFF7F7F7),
    separator = Color(0xFFE2E2E2),
    primary = Color(0xFF1D1D1F),
    secondary = Color(0xFF6E6E73),
    tertiary = Color(0xFFA1A1A6),
    bubble = Color(0xFFEFEFF0),
    accent = Color(0xFF007AFF),
    orange = Color(0xFFFF9500),
    green = Color(0xFF34C759),
    red = Color(0xFFFF3B30),
    yellow = Color(0xFFFFCC00),
  )

/** Claude's terracotta. */
val ClaudeOrange = Color(0xFFD97757)
val CodexBlue = Color(0xFF5C6EFC)

fun backendColor(backend: AgentBackend?): Color =
  when (backend) {
    AgentBackend.CLAUDE -> ClaudeOrange
    AgentBackend.CODEX -> CodexBlue
    null -> Color.Gray
  }

val LocalHyadexColors = staticCompositionLocalOf { Dark }

object Hyadex {
  val colors: HyadexColors
    @Composable get() = LocalHyadexColors.current

  val cornerRadius = 10.dp
  val smallRadius = 6.dp
}

fun ActivityState.tint(colors: HyadexColors): Color =
  when (this) {
    ActivityState.IDLE, ActivityState.QUEUED -> colors.secondary
    ActivityState.RUNNING -> colors.accent
    ActivityState.NEEDS_ACTION -> colors.orange
    ActivityState.FAILED -> colors.red
    ActivityState.DONE -> colors.green
    ActivityState.PAUSED -> colors.yellow
  }

/** The app's type scale (the Mac app's 14/13/12/11 pt, a step larger for a phone held closer). */
object HyadexType {
  val body = TextStyle(fontSize = 16.sp, lineHeight = 24.sp)
  val secondary = TextStyle(fontSize = 15.sp, lineHeight = 21.sp)
  val meta = TextStyle(fontSize = 13.sp, lineHeight = 18.sp)
  val small = TextStyle(fontSize = 11.sp, lineHeight = 14.sp)
  val headline = TextStyle(fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold)
  val title = TextStyle(fontSize = 19.sp, lineHeight = 24.sp, fontWeight = FontWeight.SemiBold)
  val mono = TextStyle(fontSize = 13.sp, lineHeight = 18.sp, fontFamily = FontFamily.Monospace)
}

@Composable
fun HyadexTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
  val colors = if (darkTheme) Dark else Light
  val scheme =
    if (darkTheme) {
      darkColorScheme(
        primary = colors.accent, onPrimary = Color.White, background = colors.background, onBackground = colors.primary,
        surface = colors.background, onSurface = colors.primary, surfaceVariant = colors.card, onSurfaceVariant = colors.secondary,
        surfaceContainer = colors.card, surfaceContainerHigh = colors.card, surfaceContainerHighest = colors.bubble,
        outline = colors.separator, outlineVariant = colors.separator, error = colors.red, inversePrimary = colors.accent,
      )
    } else {
      lightColorScheme(
        primary = colors.accent, onPrimary = Color.White, background = colors.background, onBackground = colors.primary,
        surface = colors.background, onSurface = colors.primary, surfaceVariant = colors.card, onSurfaceVariant = colors.secondary,
        surfaceContainer = colors.card, surfaceContainerHigh = colors.card, surfaceContainerHighest = colors.bubble,
        outline = colors.separator, outlineVariant = colors.separator, error = colors.red, inversePrimary = colors.accent,
      )
    }
  CompositionLocalProvider(LocalHyadexColors provides colors) {
    MaterialTheme(colorScheme = scheme, typography = Typography(bodyLarge = HyadexType.body, bodyMedium = HyadexType.secondary), content = content)
  }
}
