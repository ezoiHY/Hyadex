package dev.hyadex.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import dev.hyadex.android.R
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** The components inside the app, all under the Apache License 2.0, and that license's text. */
@Composable
fun LicensesScreen(onBack: () -> Unit) {
  val colors = Hyadex.colors
  val context = LocalContext.current
  val license = remember { context.resources.openRawResource(R.raw.apache_license_2_0).bufferedReader().use { it.readText() } }
  Column(Modifier.fillMaxSize().background(colors.sidebar)) {
    PaneTopBar("开源许可", onBack)
    LazyColumn(Modifier.weight(1f).navigationBarsPadding(), contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 16.dp, end = 16.dp, bottom = 24.dp)) {
      item {
        SettingsNote("Hyadex 以 Apache License 2.0 开源。这个 app 里用到的下列组件也都采用 Apache License 2.0。")
      }
      item {
        SettingsSection("组件") {
          COMPONENTS.forEachIndexed { index, (name, author) ->
            if (index > 0) SettingsDivider()
            Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 11.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
              Text(name, style = HyadexType.secondary, color = colors.primary, modifier = Modifier.weight(1f))
              Text(author, style = HyadexType.meta, color = colors.secondary)
            }
          }
        }
      }
      item {
        Text("Apache License 2.0", style = HyadexType.meta, color = colors.secondary, modifier = Modifier.padding(start = 14.dp, top = 20.dp, bottom = 6.dp))
      }
      item {
        SelectionContainer {
          Text(license, style = HyadexType.mono.copy(fontSize = HyadexType.small.fontSize, lineHeight = HyadexType.small.lineHeight), color = colors.secondary, modifier = Modifier.fillMaxWidth().card().padding(12.dp))
        }
      }
    }
  }
}

/** What the release APK carries (its runtime dependencies), by project. */
private val COMPONENTS = listOf(
  "AndroidX、Jetpack Compose" to "Google",
  "Accompanist Drawable Painter" to "Google",
  "Guava ListenableFuture" to "Google",
  "Kotlin、kotlinx.coroutines、kotlinx.serialization" to "JetBrains",
  "Compose Multiplatform" to "JetBrains",
  "JetBrains Markdown、JetBrains Annotations" to "JetBrains",
  "OkHttp、Okio" to "Square",
  "Coil" to "Coil Contributors",
  "Multiplatform Markdown Renderer" to "Mike Penz",
  "Highlights" to "SnipMe",
  "ZXing" to "ZXing Authors",
  "ZXing Android Embedded" to "JourneyApps",
  "JSpecify" to "JSpecify Authors",
)
