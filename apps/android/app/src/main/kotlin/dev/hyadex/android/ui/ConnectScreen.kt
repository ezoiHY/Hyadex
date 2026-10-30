package dev.hyadex.android.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.ContentPaste
import androidx.compose.material.icons.outlined.QrCodeScanner
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.google.zxing.client.android.Intents
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import dev.hyadex.android.R
import dev.hyadex.android.ScanActivity
import dev.hyadex.android.app.AppViewModel
import dev.hyadex.android.app.Connection
import dev.hyadex.android.core.PairingLink
import dev.hyadex.android.ui.theme.Hyadex
import dev.hyadex.android.ui.theme.HyadexType

/** Pair by scanning the code the Mac shows, by pasting its link, or by typing the address and token. */
@Composable
fun ConnectScreen(model: AppViewModel) {
  val colors = Hyadex.colors
  val saved = remember { model.savedServer }
  var address by rememberSaveable { mutableStateOf(saved?.address ?: "") }
  var token by rememberSaveable { mutableStateOf(saved?.token ?: "") }
  var fromCode by rememberSaveable { mutableStateOf(false) }
  val connection = model.connection
  val clipboard = LocalClipboardManager.current
  // A link (scanned, pasted or opened) fills the form; connecting stays the user's tap.
  LaunchedEffect(model.pairing) {
    val pairing = model.pairing ?: return@LaunchedEffect
    address = pairing.address
    token = pairing.token
    fromCode = true
    model.pairing = null
  }
  val context = LocalContext.current
  // The camera is asked for here: without it the scanner could only show its own "camera problem" dialog.
  var cameraRefused by rememberSaveable { mutableStateOf(false) }
  val scan = rememberLauncherForActivityResult(ScanContract()) { result ->
    if (result.originalIntent?.hasExtra(Intents.Scan.MISSING_CAMERA_PERMISSION) == true) cameraRefused = true
    val text = result.contents ?: return@rememberLauncherForActivityResult
    PairingLink.parse(text)?.let { model.pairing = it } ?: model.tell("这不是 Hyadex 的连接二维码")
  }
  val askCamera = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
    cameraRefused = !granted
    if (granted) scan.launch(scanOptions())
  }
  Column(
    Modifier.fillMaxSize().background(colors.background).safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(24.dp),
    verticalArrangement = Arrangement.spacedBy(16.dp),
  ) {
    Image(
      painterResource(R.drawable.ic_launcher_art), contentDescription = null,
      modifier = Modifier.padding(top = 24.dp).size(64.dp).clip(RoundedCornerShape(16.dp)),
    )
    Text("连接 Hyadex", style = HyadexType.title, color = colors.primary)
    Text(
      "在 Mac 的 Hyadex 设置 › 服务器 里打开「允许手机连接」，然后扫描显示的二维码。手机和 Mac 要在同一 Wi-Fi，或都连上同一个 Tailscale。",
      style = HyadexType.secondary,
      color = colors.secondary,
    )
    Button(
      onClick = {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
          cameraRefused = false
          scan.launch(scanOptions())
        } else {
          askCamera.launch(Manifest.permission.CAMERA)
        }
      },
      modifier = Modifier.fillMaxWidth().height(48.dp),
    ) {
      Icon(Icons.Outlined.QrCodeScanner, null, modifier = Modifier.size(20.dp))
      Text("扫码连接", modifier = Modifier.padding(start = 8.dp))
    }
    AnimatedVisibility(cameraRefused, enter = unfold, exit = fold) {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
          "没有相机权限，扫不了码。可以在系统设置里允许，或者粘贴连接链接。",
          style = HyadexType.meta,
          color = colors.secondary,
          modifier = Modifier.weight(1f),
        )
        TextButton(
          onClick = {
            context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null)))
          }
        ) { Text("去设置") }
      }
    }
    TextButton(
      onClick = { PairingLink.parse(clipboard.getText()?.text)?.let { model.pairing = it } ?: model.tell("剪贴板里没有连接链接") },
      modifier = Modifier.align(Alignment.CenterHorizontally),
    ) {
      Icon(Icons.Outlined.ContentPaste, null, modifier = Modifier.size(18.dp))
      Text("粘贴连接链接", modifier = Modifier.padding(start = 6.dp))
    }
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
      HorizontalDivider(Modifier.weight(1f), color = colors.separator, thickness = 0.5.dp)
      Text("或手动填写", style = HyadexType.meta, color = colors.tertiary)
      HorizontalDivider(Modifier.weight(1f), color = colors.separator, thickness = 0.5.dp)
    }
    OutlinedTextField(
      value = address,
      onValueChange = { address = it; fromCode = false },
      label = { Text("地址") },
      placeholder = { Text("100.x.y.z:7870") },
      singleLine = true,
      keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
      modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
      value = token,
      onValueChange = { token = it; fromCode = false },
      label = { Text("令牌") },
      singleLine = true,
      visualTransformation = PasswordVisualTransformation(),
      keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
      modifier = Modifier.fillMaxWidth(),
    )
    if (fromCode) Text("已从二维码读取，点「连接」继续", style = HyadexType.meta, color = colors.accent)
    if (connection is Connection.Failed) Text(connection.message, style = HyadexType.secondary, color = colors.red)
    OutlinedButton(
      onClick = { model.connect(address, token) },
      enabled = address.isNotBlank() && connection != Connection.Connecting,
      modifier = Modifier.fillMaxWidth().height(48.dp),
    ) {
      if (connection == Connection.Connecting) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
      else Text("连接")
    }
    Text(
      "令牌只保存在这台手机上，用系统密钥库加密。不要把服务器端口直接暴露到公网。",
      style = HyadexType.meta,
      color = colors.tertiary,
    )
  }
}

private fun scanOptions() =
  ScanOptions().setCaptureActivity(ScanActivity::class.java).setDesiredBarcodeFormats(ScanOptions.QR_CODE)
    .setPrompt("扫描 Mac 上「允许手机连接」下的二维码").setBeepEnabled(false).setOrientationLocked(false)
    // Should the camera be taken away mid-scan, the scanner just closes and the hint above shows.
    .addExtra(Intents.Scan.SHOW_MISSING_CAMERA_PERMISSION_DIALOG, false)
