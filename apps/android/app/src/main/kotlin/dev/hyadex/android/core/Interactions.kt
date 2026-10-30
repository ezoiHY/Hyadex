package dev.hyadex.android.core

import dev.hyadex.android.api.array
import dev.hyadex.android.api.bool
import dev.hyadex.android.api.get
import dev.hyadex.android.api.jsonOf
import dev.hyadex.android.api.obj
import dev.hyadex.android.api.string
import dev.hyadex.android.api.text
import dev.hyadex.android.api.withoutNulls
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** What the user sends back for one agent interaction, and how the answered card reads. */
data class InteractionReply(val response: JsonElement, val outcome: ApprovalDecision)

/** One answer an approval card offers, in its provider's terms (as the Mac app's `ApprovalChoice`). */
data class ApprovalChoice(val label: String, val role: Role, val reply: InteractionReply) {
  enum class Role { ALLOW, ALLOW_MORE, DENY }

  companion object {
    val standard: List<ApprovalChoice> by lazy {
      fromDecisions(listOf("accept", "acceptForSession", "decline").map(::JsonPrimitive))
    }

    fun choices(method: String, params: JsonElement?): List<ApprovalChoice> =
      when (method) {
        "execCommandApproval", "applyPatchApproval" ->
          listOf(
            ApprovalChoice("拒绝", Role.DENY, InteractionReply(jsonOf("decision" to "denied"), ApprovalDecision.Denied)),
            ApprovalChoice(
              "本会话内始终允许",
              Role.ALLOW_MORE,
              InteractionReply(jsonOf("decision" to "approved_for_session"), ApprovalDecision.AllowedForSession),
            ),
            ApprovalChoice("允许", Role.ALLOW, InteractionReply(jsonOf("decision" to "approved"), ApprovalDecision.Allowed)),
          )
        "item/permissions/requestApproval" -> {
          val grant = params["permissions"].withoutNulls()
          listOf(
            ApprovalChoice(
              "拒绝",
              Role.DENY,
              InteractionReply(jsonOf("permissions" to JsonObject(emptyMap()), "scope" to "turn"), ApprovalDecision.Denied),
            ),
            ApprovalChoice(
              "本会话内允许",
              Role.ALLOW_MORE,
              InteractionReply(jsonOf("permissions" to grant, "scope" to "session"), ApprovalDecision.AllowedForSession),
            ),
            ApprovalChoice("允许", Role.ALLOW, InteractionReply(jsonOf("permissions" to grant, "scope" to "turn"), ApprovalDecision.Allowed)),
          )
        }
        else -> {
          val offered = params["availableDecisions"].array
          // Codex may omit the list; then every plain decision is available.
          if (offered.isEmpty()) standard else fromDecisions(offered)
        }
      }

    private fun fromDecisions(decisions: List<JsonElement>): List<ApprovalChoice> =
      decisions.mapNotNull { decision ->
        fun reply(outcome: ApprovalDecision) = InteractionReply(jsonOf("decision" to decision), outcome)
        when (decision.string) {
          "accept" -> return@mapNotNull ApprovalChoice("允许", Role.ALLOW, reply(ApprovalDecision.Allowed))
          "acceptForSession" -> return@mapNotNull ApprovalChoice("本会话内始终允许", Role.ALLOW_MORE, reply(ApprovalDecision.AllowedForSession))
          "decline" -> return@mapNotNull ApprovalChoice("拒绝", Role.DENY, reply(ApprovalDecision.Denied))
          // Refuses and stops the turn, so the user can say what to do instead.
          "cancel" -> return@mapNotNull ApprovalChoice("拒绝并停止", Role.DENY, reply(ApprovalDecision.Denied))
          null -> Unit
          else -> return@mapNotNull null
        }
        val prefix = decision["acceptWithExecpolicyAmendment"]["execpolicy_amendment"].array.mapNotNull { it.string }
        if (prefix.isNotEmpty()) {
          var shown = SessionProjection.abbreviateHome(SessionProjection.unwrapShell(prefix.joinToString(" "))).lines().first()
          if (shown.length > 48) shown = shown.take(48) + "…"
          return@mapNotNull ApprovalChoice("允许，以后不再询问以 $shown 开头的命令", Role.ALLOW_MORE, reply(ApprovalDecision.AllowedAlways))
        }
        val network = decision["applyNetworkPolicyAmendment"]["network_policy_amendment"]
        val host = network["host"].string ?: return@mapNotNull null
        if (network["action"].string == "deny") {
          ApprovalChoice("始终阻止访问 $host", Role.DENY, reply(ApprovalDecision.Denied))
        } else {
          ApprovalChoice("始终允许访问 $host", Role.ALLOW_MORE, reply(ApprovalDecision.AllowedAlways))
        }
      }
  }
}

data class FormOption(val value: String, val label: String, val detail: String = "") {
  companion object {
    fun constant(json: JsonElement): FormOption? {
      val value = json["const"].string ?: return null
      return FormOption(value, json["title"].text ?: value)
    }
  }
}

data class FormField(
  val id: String,
  val header: String = "",
  val prompt: String,
  val detail: String = "",
  val input: Input,
  val required: Boolean = true,
  val initial: List<String> = emptyList(),
) {
  sealed interface Input {
    data class Choice(val options: List<FormOption>, val multiple: Boolean, val allowsOther: Boolean) : Input
    data class Text(val secret: Boolean) : Input
    data class Number(val integer: Boolean) : Input
    data object Toggle : Input
  }

  fun label(value: String): String = (input as? Input.Choice)?.options?.firstOrNull { it.value == value }?.label ?: value

  fun value(values: List<String>): JsonElement =
    when (val input = input) {
      is Input.Choice -> if (input.multiple) JsonArray(values.map(::JsonPrimitive)) else JsonPrimitive(values[0])
      Input.Toggle -> JsonPrimitive(values[0] == "true")
      is Input.Number ->
        if (input.integer) values[0].toIntOrNull()?.let(::JsonPrimitive) ?: JsonPrimitive(values[0])
        else values[0].toDoubleOrNull()?.let(::JsonPrimitive) ?: JsonPrimitive(values[0])
      is Input.Text -> JsonPrimitive(values[0])
    }

  companion object {
    fun question(json: JsonElement): FormField? {
      val id = json["id"].string ?: return null
      val options = json["options"].array.mapNotNull { option ->
        val label = option["label"].text ?: return@mapNotNull null
        FormOption(label, label, option["description"].text ?: "")
      }
      val input =
        when {
          json["kind"].string == "number" -> Input.Number(integer = false)
          options.isEmpty() -> Input.Text(secret = json["isSecret"].bool == true)
          else -> Input.Choice(options, multiple = json["multiSelect"].bool == true, allowsOther = json["isOther"].bool == true)
        }
      return FormField(
        id = id,
        header = json["header"].text ?: "",
        prompt = json["question"].text ?: "",
        detail = json["description"].text ?: "",
        input = input,
      )
    }

    fun property(key: String, schema: JsonElement, required: Boolean): FormField {
      val title = schema["title"].text ?: key
      val detail = schema["description"].text ?: ""
      fun field(input: Input, initial: List<String>) = FormField(key, prompt = title, detail = detail, input = input, required = required, initial = initial)
      val default = schema["default"]
      val initial: List<String> =
        when {
          default is JsonArray -> default.mapNotNull { it.string }
          default == null || default is JsonNull -> emptyList()
          default.bool != null -> listOf(if (default.bool == true) "true" else "false")
          else -> listOfNotNull(default.string)
        }
      val names = schema["enumNames"].array.mapNotNull { it.string }
      val single =
        schema["oneOf"].array.mapNotNull(FormOption::constant).ifEmpty {
          schema["enum"].array.mapIndexedNotNull { index, value -> value.string?.let { FormOption(it, names.getOrElse(index) { _ -> it }) } }
        }
      return when (schema["type"].string) {
        "array" -> {
          val items = schema["items"]
          val options =
            items["anyOf"].array.mapNotNull(FormOption::constant).ifEmpty {
              items["enum"].array.mapNotNull { it.string }.map { FormOption(it, it) }
            }
          field(Input.Choice(options, multiple = true, allowsOther = false), initial)
        }
        "boolean" -> field(Input.Toggle, initial.ifEmpty { listOf("false") })
        "number", "integer" -> field(Input.Number(integer = schema["type"].string == "integer"), initial)
        else -> if (single.isNotEmpty()) field(Input.Choice(single, multiple = false, allowsOther = false), initial) else field(Input.Text(false), initial)
      }
    }
  }
}

/** A question or form an agent asks the user to fill in (Codex's request_user_input, MCP elicitation). */
data class InteractionForm(
  val kind: Kind,
  val title: String,
  val message: String = "",
  val url: String? = null,
  val fields: List<FormField>,
) {
  enum class Kind { QUESTIONS, ELICITATION }

  fun isComplete(answers: Map<String, List<String>>): Boolean =
    fields.all { field ->
      val values = (answers[field.id] ?: emptyList()).filter { it.isNotBlank() }
      if (values.isEmpty()) return@all !(kind == Kind.QUESTIONS || field.required) || field.input == FormField.Input.Toggle
      val input = field.input
      if (input is FormField.Input.Number) {
        return@all if (input.integer) values[0].toIntOrNull() != null else values[0].toDoubleOrNull() != null
      }
      true
    }

  fun reply(answers: Map<String, List<String>>): InteractionReply {
    val filled = answers.mapValues { (_, values) -> values.filter { it.isNotBlank() } }.filterValues { it.isNotEmpty() }
    val summary =
      fields
        .mapNotNull { field ->
          val values = filled[field.id] ?: return@mapNotNull null
          val name = field.header.ifEmpty { field.prompt }
          val shown = if (field.input == FormField.Input.Text(true)) "••••" else values.joinToString(", ") { field.label(it) }
          if (name.isEmpty()) shown else "$name：$shown"
        }
        .joinToString("；")
    return when (kind) {
      Kind.QUESTIONS -> {
        val response = jsonOf("answers" to filled.mapValues { (_, values) -> mapOf("answers" to values) })
        InteractionReply(response, ApprovalDecision.Answered(summary))
      }
      Kind.ELICITATION -> {
        val content = fields.mapNotNull { field -> filled[field.id]?.let { field.id to field.value(it) } }.toMap()
        InteractionReply(
          jsonOf("action" to "accept", "content" to JsonObject(content)),
          if (url == null) ApprovalDecision.Answered(summary) else ApprovalDecision.Allowed,
        )
      }
    }
  }

  val dismissal: InteractionReply
    get() =
      when (kind) {
        Kind.QUESTIONS -> InteractionReply(jsonOf("answers" to JsonObject(emptyMap())), ApprovalDecision.Skipped)
        Kind.ELICITATION -> InteractionReply(jsonOf("action" to "decline"), ApprovalDecision.Denied)
      }

  companion object {
    fun parse(method: String, params: JsonElement?): InteractionForm? =
      when (method) {
        "item/tool/requestUserInput" ->
          InteractionForm(Kind.QUESTIONS, "需要你回答", fields = params["questions"].array.mapNotNull(FormField::question))
        "mcpServer/elicitation/request" -> {
          val server = params["serverName"].text ?: "MCP 服务器"
          val message = params["message"].text ?: ""
          if (params["mode"].string == "url") {
            InteractionForm(Kind.ELICITATION, "$server 请你打开链接", message, params["url"].string, emptyList())
          } else {
            val schema = params["requestedSchema"]
            val required = schema["required"].array.mapNotNull { it.string }.toSet()
            // Required fields first, then by name: an object's key order does not survive decoding.
            val fields =
              (schema["properties"].obj ?: emptyMap())
                .entries
                .sortedWith(compareBy({ if (it.key in required) 0 else 1 }, { it.key }))
                .map { FormField.property(it.key, it.value, it.key in required) }
            InteractionForm(Kind.ELICITATION, "$server 需要你填写", message, fields = fields)
          }
        }
        else -> null
      }
  }
}
