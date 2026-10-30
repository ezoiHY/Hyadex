package dev.hyadex.android

import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updateLayoutParams
import com.google.zxing.client.android.R as Zxing
import com.journeyapps.barcodescanner.CaptureActivity

/**
 * The pairing-code scanner. Android draws it edge to edge, which would leave
 * its prompt under the navigation bar, so the prompt sits just above it.
 */
class ScanActivity : CaptureActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    val prompt = findViewById<View>(Zxing.id.zxing_status_view) ?: return
    val gap = (20 * resources.displayMetrics.density).toInt()
    ViewCompat.setOnApplyWindowInsetsListener(prompt) { view, insets ->
      val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
      view.updateLayoutParams<ViewGroup.MarginLayoutParams> { bottomMargin = bars.bottom + gap }
      insets
    }
  }
}
