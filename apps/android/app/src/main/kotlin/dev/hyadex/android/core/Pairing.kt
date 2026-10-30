package dev.hyadex.android.core

import java.net.URI
import java.net.URLDecoder

/**
 * The link in the pairing code the Mac app shows under 允许手机连接:
 * `hyadex://connect?address=http://100.64.0.1:7870&token=…`.
 */
data class PairingLink(val address: String, val token: String) {
  companion object {
    fun parse(text: String?): PairingLink? {
      val uri = runCatching { URI(text?.trim() ?: return null) }.getOrNull() ?: return null
      if (uri.scheme != "hyadex" || uri.host != "connect") return null
      val query = uri.rawQuery?.split('&').orEmpty().associate { part ->
        val (key, value) = part.split('=', limit = 2).let { it[0] to it.getOrElse(1) { "" } }
        URLDecoder.decode(key, "UTF-8") to URLDecoder.decode(value, "UTF-8")
      }
      val address = query["address"]?.trim()?.takeIf { it.isNotEmpty() } ?: return null
      val token = query["token"]?.trim()?.takeIf { it.isNotEmpty() } ?: return null
      return PairingLink(address, token)
    }
  }
}
