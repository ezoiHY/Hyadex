package dev.hyadex.android.api

import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull

/**
 * Lenient reads of the server's JSON, the way the Mac client's `JSON` reads it:
 * a missing key or a value of another type reads as null (or empty).
 */
val JsonCodec = Json { ignoreUnknownKeys = true }

operator fun JsonElement?.get(key: String): JsonElement? = (this as? JsonObject)?.get(key)

val JsonElement?.obj: JsonObject? get() = this as? JsonObject

/** A string, or a number written as one. */
val JsonElement?.string: String?
  get() {
    val primitive = this as? JsonPrimitive ?: return null
    if (primitive is JsonNull) return null
    if (primitive.isString) return primitive.content
    val number = primitive.doubleOrNull ?: return null
    return if (number == Math.rint(number) && !number.isInfinite()) number.toLong().toString() else number.toString()
  }

/** Trimmed text, or null when empty. */
val JsonElement?.text: String? get() = string?.trim()?.takeIf { it.isNotEmpty() }

val JsonElement?.int: Int?
  get() {
    val primitive = this as? JsonPrimitive ?: return null
    if (primitive is JsonNull) return null
    return if (primitive.isString) primitive.content.toIntOrNull() else primitive.doubleOrNull?.toInt()
  }

val JsonElement?.double: Double?
  get() = (this as? JsonPrimitive)?.takeIf { !it.isString && it !is JsonNull }?.doubleOrNull

val JsonElement?.bool: Boolean?
  get() = (this as? JsonPrimitive)?.takeIf { !it.isString && it !is JsonNull }?.booleanOrNull

val JsonElement?.array: List<JsonElement> get() = (this as? JsonArray) ?: emptyList()

val JsonElement?.isNull: Boolean get() = this == null || this is JsonNull

/** ISO-8601 strings (sessions, events) or Unix seconds (groups, jobs). */
val JsonElement?.instant: Instant?
  get() {
    val primitive = this as? JsonPrimitive ?: return null
    if (primitive is JsonNull) return null
    if (!primitive.isString) return primitive.doubleOrNull?.let { Instant.ofEpochMilli((it * 1000).toLong()) }
    return try {
      OffsetDateTime.parse(primitive.content).toInstant()
    } catch (_: DateTimeParseException) {
      try {
        Instant.parse(primitive.content)
      } catch (_: DateTimeParseException) {
        null
      }
    }
  }

fun jsonOf(vararg pairs: Pair<String, Any?>): JsonObject = JsonObject(pairs.associate { (key, value) -> key to toJson(value) })

fun toJson(value: Any?): JsonElement =
  when (value) {
    null -> JsonNull
    is JsonElement -> value
    is String -> JsonPrimitive(value)
    is Number -> JsonPrimitive(value)
    is Boolean -> JsonPrimitive(value)
    is Map<*, *> -> JsonObject(value.entries.associate { (key, item) -> key.toString() to toJson(item) })
    is List<*> -> JsonArray(value.map(::toJson))
    else -> JsonPrimitive(value.toString())
  }

/** Codex sends absent permissions as `null`; a grant leaves them out. */
fun JsonElement?.withoutNulls(): JsonElement =
  when (this) {
    null -> JsonNull
    is JsonObject -> JsonObject(filterValues { it !is JsonNull }.mapValues { it.value.withoutNulls() })
    is JsonArray -> JsonArray(map { it.withoutNulls() })
    else -> this
  }
