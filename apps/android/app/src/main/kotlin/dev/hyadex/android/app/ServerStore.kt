package dev.hyadex.android.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The server this phone connects to. The address is kept in preferences; the
 * token is encrypted with a key that never leaves the Android Keystore.
 */
class ServerStore(context: Context) {
  private val preferences = context.getSharedPreferences("server", Context.MODE_PRIVATE)

  data class Saved(val address: String, val token: String)

  fun load(): Saved? {
    val address = preferences.getString(ADDRESS, null) ?: return null
    val sealed = preferences.getString(TOKEN, null) ?: return Saved(address, "")
    val token = runCatching { open(sealed) }.getOrNull() ?: return Saved(address, "")
    return Saved(address, token)
  }

  fun save(address: String, token: String) {
    preferences.edit().putString(ADDRESS, address).putString(TOKEN, seal(token)).apply()
  }

  fun clear() {
    preferences.edit().clear().apply()
  }

  private fun key(): SecretKey {
    val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
    (keyStore.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
    generator.init(
      KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .build()
    )
    return generator.generateKey()
  }

  private fun seal(text: String): String {
    val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
    val sealed = cipher.iv + cipher.doFinal(text.toByteArray())
    return Base64.encodeToString(sealed, Base64.NO_WRAP)
  }

  private fun open(encoded: String): String {
    val sealed = Base64.decode(encoded, Base64.NO_WRAP)
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_LENGTH))
    return String(cipher.doFinal(sealed, IV_LENGTH, sealed.size - IV_LENGTH))
  }

  private companion object {
    const val KEYSTORE = "AndroidKeyStore"
    const val ALIAS = "hyadex-server-token"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val IV_LENGTH = 12
    const val ADDRESS = "address"
    const val TOKEN = "token"
  }
}
