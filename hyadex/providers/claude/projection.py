"""Projecting Claude SDK messages into chat events.

Two Claude-native outputs are not assistant messages:

* ``compact_boundary`` (from ``/compact`` or automatic compaction) becomes a
  durable ``claude_compaction_completed`` event: ``run_id``, ``backend``,
  ``status`` ("completed"), ``trigger`` ("manual" or "auto"), ``pre_tokens``,
  ``post_tokens`` when Claude reports them, ``provider_message_id`` and
  ``message``.
* A local slash command's output (``/compact`` prints "Compacted") becomes an
  ``assistant_text`` event carrying ``local_command`` (the command name) and
  ``local_command_stream`` ("stdout" or "stderr"), so every client shows it
  where native Claude Code prints it under the command. Output that Claude
  replays as a user message is held until the terminal result confirms the
  turn was a local command, so a pasted look-alike is never shown as output.
"""
from __future__ import annotations

from contextlib import suppress
import json
from typing import Any
import uuid

from hyadex.core.config import BACKEND_CLAUDE, CLAUDE_TRANSPORT_AGENT_SDK
from hyadex.core.runs import RUNS
from hyadex.core.sessions import (
    append_event,
    event_output_text,
    mark_provider_context_usage_unavailable,
)
from hyadex.core.util import _patch_changed_paths, clean_assistant_text, normalized_tool_name
from hyadex.features.files import run_event_metadata
from hyadex.providers.claude.native_goals import claude_local_command_output
from hyadex.providers.token_totals import add_claude_turn_usage
from hyadex.providers.claude.subagents import (
    claude_sdk_field,
    claude_sdk_subagent_lifecycle_raw,
    claude_sdk_type,
)


CLAUDE_COMPACTION_COMPLETED_MESSAGE = "Context compaction completed."
CLAUDE_LOCAL_COMMAND_OUTPUT_LIMIT = 16


def claude_compaction_completed_payload(run_id: str, data: Any) -> dict[str, Any]:
    """Project one ``compact_boundary`` record into a timeline landmark."""

    record = data if isinstance(data, dict) else {}
    metadata = record.get("compact_metadata")
    if not isinstance(metadata, dict):
        metadata = record.get("compactMetadata")
    if not isinstance(metadata, dict):
        metadata = {}

    def tokens(*names: str) -> int | None:
        for name in names:
            value = metadata.get(name)
            if type(value) is int and value >= 0:
                return value
        return None

    trigger = str(metadata.get("trigger") or "").strip().lower()
    boundary_id = record.get("uuid")
    payload: dict[str, Any] = {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "status": "completed",
        "trigger": trigger if trigger in {"manual", "auto"} else None,
        "pre_tokens": tokens("pre_tokens", "preTokens"),
        "post_tokens": tokens("post_tokens", "postTokens"),
        "provider_message_id": (
            boundary_id
            if isinstance(boundary_id, str) and 0 < len(boundary_id) <= 256
            else None
        ),
        "message": CLAUDE_COMPACTION_COMPLETED_MESSAGE,
    }
    return {key: value for key, value in payload.items() if value is not None}


def claude_local_command_name(value: Any) -> str:
    name = str(value or "").strip()
    return name if 0 < len(name) <= 128 else ""


def claude_local_command_fields_for_turn(message: Any, session_id: str, run_id: str) -> dict[str, Any]:
    """Local-command fields, marking output that belongs to an earlier priority command."""

    fields = claude_local_command_fields(message)
    if fields and not claude_turn_ran_command(session_id, run_id, fields["local_command"]):
        fields["local_command_detached"] = True
    return fields


def claude_local_command_fields(message: Any) -> dict[str, Any]:
    """Identify an assistant message that is a local command's output."""

    run = claude_sdk_field(message, "local_command_run")
    name = claude_local_command_name(run.get("command")) if isinstance(run, dict) else ""
    if not name:
        return {}
    fields: dict[str, Any] = {"local_command": name}
    source = claude_local_command_output(
        claude_sdk_field(message, "local_command_source")
    )
    if source is not None:
        fields["local_command_stream"] = source[0]
    return fields


def remember_claude_local_command_output(
    outputs: list[dict[str, Any]] | None,
    content: Any,
    message_id: Any,
) -> None:
    """Hold replayed ``<local-command-stdout>`` text until the result arrives."""

    if outputs is None or len(outputs) >= CLAUDE_LOCAL_COMMAND_OUTPUT_LIMIT:
        return
    output = claude_local_command_output(content)
    if output is None:
        return
    stream, text = output
    outputs.append({
        "stream": stream,
        "text": text,
        "provider_message_id": (
            message_id
            if isinstance(message_id, str) and 0 < len(message_id) <= 256
            else None
        ),
    })


async def publish_claude_local_command_outputs(
    session_id: str,
    run_id: str,
    local_command: Any,
    outputs: list[dict[str, Any]] | None,
    text_parts: list[str] | None = None,
) -> bool:
    """Show held local-command output once the provider confirms the command.

    Returns whether the provider reported a local command for this turn.
    """

    command = claude_local_command_name(local_command)
    held = list(outputs or ())
    if outputs is not None:
        outputs.clear()
    if not command:
        return False
    # A priority command sent during an earlier turn (for example clearing a
    # goal) can deliver its result in a later turn's stream. Record it, so
    # history sync does not import it again, but mark it as not this turn's.
    detached = not claude_turn_ran_command(session_id, run_id, command)
    for output in held:
        text = str(output.get("text") or "")
        if not text:
            continue
        if text_parts is not None:
            text_parts.append(text)
        await append_event(session_id, "assistant_text", {
            "run_id": run_id,
            "text": text,
            "backend": BACKEND_CLAUDE,
            **({"provider_message_id": output["provider_message_id"]}
               if output.get("provider_message_id") else {}),
            "local_command": command,
            "local_command_stream": str(output.get("stream") or "stdout"),
            **({"local_command_detached": True} if detached else {}),
            **run_event_metadata(run_id),
        })
    return not detached


def claude_turn_ran_command(session_id: str, run_id: str, command: str) -> bool:
    """Whether this turn's own prompt invoked ``command`` (``/compact …``)."""

    turn = RUNS.current_turns.get(session_id) or {}
    if turn.get("run_id") not in {None, run_id}:
        return True  # Not the turn we are projecting: do not second-guess it.
    prompt = str(turn.get("prompt") or "").strip()
    if not prompt:
        return True  # A turn Claude started itself (a goal continuation): its output is its own.
    if not prompt.startswith("/"):
        return False
    invoked = prompt[1:].split(None, 1)[0] if len(prompt) > 1 else ""
    return invoked.lstrip("/").casefold() == command.lstrip("/").casefold()


def tool_changed_paths(tool: dict[str, Any]) -> set[str]:
    """Return only paths explicitly owned by a mutating provider tool call."""
    name = normalized_tool_name(tool)
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    paths: set[str] = set()
    direct_path_tools = {
        "edit", "write", "multiedit", "multi_edit", "notebookedit", "notebook_edit",
        "str_replace_editor", "create", "create_file", "delete", "delete_file",
        "move", "move_file", "rename", "rename_file",
    }
    if name in direct_path_tools:
        for key in (
            "file_path", "path", "notebook_path", "old_path", "new_path",
            "source_path", "destination_path", "from", "to",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
    if name in {"apply_patch", "applypatch", "patch"}:
        patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("value")
        if isinstance(patch, str):
            paths.update(_patch_changed_paths(patch))
    elif name in {"exec", "shell", "run_javascript", "javascript"}:
        for key in ("value", "code", "script", "input", "command"):
            source = tool_input.get(key)
            if isinstance(source, str) and "apply_patch" in source and "***" in source:
                paths.update(_patch_changed_paths(source))
    return paths


async def mark_provider_turn_ready(
    session_id: str,
    run_id: str,
    provider_session_id: str | None = None,
) -> None:
    """Mark that the provider accepted this turn before it can be interrupted."""
    if not provider_session_id:
        return
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if not active or str(active.get("run_id") or "") != run_id:
            return
        active["provider_turn_ready"] = True
        active["provider_session_id"] = provider_session_id


def claude_sdk_result_details(message: Any) -> dict[str, Any]:
    errors = claude_sdk_field(message, "errors")
    if isinstance(errors, list):
        error_text = "\n".join(str(value) for value in errors if str(value))
    else:
        error_text = ""
    result = clean_assistant_text(
        str(claude_sdk_field(message, "result") or "")
    )
    subtype = str(claude_sdk_field(message, "subtype") or "")
    terminal_reason = str(
        claude_sdk_field(message, "terminal_reason") or ""
    )
    is_error = bool(claude_sdk_field(message, "is_error", False))
    return {
        "session_id": str(claude_sdk_field(message, "session_id") or ""),
        "result_text": result,
        "is_error": is_error,
        "error": error_text or (result if is_error else "") or (
            f"Claude SDK ended with {subtype or 'an error'}" if is_error else ""
        ),
        "subtype": subtype,
        "terminal_reason": terminal_reason,
        "aborted": terminal_reason in {"aborted_streaming", "aborted_tools"},
        # Claude names the local slash command (``compact``) this result ended.
        "local_command": claude_local_command_name(
            claude_sdk_field(message, "local_command")
        ),
    }


async def project_claude_sdk_message(
    session_id: str,
    run_id: str,
    message: Any,
    *,
    text_parts: list[str],
    current_tools: dict[str, dict[str, Any]],
    changed_paths: set[str],
    tool_activity_run_ids: set[str] | None = None,
    error_run_ids: set[str] | None = None,
    local_command_outputs: list[dict[str, Any]] | None = None,
    native_output_run_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Project one typed SDK message into the existing durable timeline.

    ``native_output_run_ids`` collects runs that showed Claude-native output
    other than model text: a compaction or a local command's result.
    """

    message_type = claude_sdk_type(message)
    provider_id = str(claude_sdk_field(message, "session_id") or "")
    if message_type == "SystemMessage":
        data = claude_sdk_field(message, "data", {})
        if isinstance(data, dict):
            provider_id = str(data.get("session_id") or provider_id)
    if provider_id:
        await mark_provider_turn_ready(session_id, run_id, provider_id)

    if (
        message_type == "SystemMessage"
        and claude_sdk_field(message, "subtype") == "compact_boundary"
    ):
        await append_event(
            session_id,
            "claude_compaction_completed",
            claude_compaction_completed_payload(
                run_id,
                claude_sdk_field(message, "data", {}),
            ),
        )
        if native_output_run_ids is not None:
            native_output_run_ids.add(run_id)
        # The last sample measured the context before compaction. Clear it;
        # the terminal sample after this turn's result measures it again.
        with suppress(Exception):
            await mark_provider_context_usage_unavailable(
                session_id,
                BACKEND_CLAUDE,
            )
        return None

    async def project_tool_result(block: Any) -> None:
        if tool_activity_run_ids is not None:
            tool_activity_run_ids.add(run_id)
        tool_id = str(claude_sdk_field(block, "tool_use_id") or "")
        await append_event(session_id, "tool_finished", {
            "run_id": run_id,
            "tool_id": tool_id or None,
            "tool": current_tools.pop(tool_id, None),
            "output": event_output_text(
                claude_sdk_field(block, "content", "")
            ),
            "is_error": claude_sdk_field(block, "is_error") is True,
        })

    async def project_subagent_lifecycle() -> None:
        raw = claude_sdk_subagent_lifecycle_raw(message)
        if raw is None:
            return
        await append_event(session_id, "raw_event", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "raw": json.dumps(raw, separators=(",", ":")),
        })

    if message_type == "AssistantMessage":
        assistant_error = str(claude_sdk_field(message, "error") or "")
        if assistant_error:
            if error_run_ids is not None:
                error_run_ids.add(run_id)
            await append_event(session_id, "error", {
                "run_id": run_id,
                "backend": BACKEND_CLAUDE,
                "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                "message": f"Claude assistant error: {assistant_error}",
                **run_event_metadata(run_id),
            })
        content = claude_sdk_field(message, "content", [])
        local_command = claude_local_command_fields_for_turn(message, session_id, run_id)
        detached = bool(local_command.get("local_command_detached"))
        if local_command and not detached and native_output_run_ids is not None:
            native_output_run_ids.add(run_id)
        for block in content if isinstance(content, list) else []:
            block_type = claude_sdk_type(block)
            if block_type in {"TextBlock", "text"}:
                text = clean_assistant_text(
                    str(claude_sdk_field(block, "text") or "")
                )
                if text:
                    if not detached:
                        text_parts.append(text)
                    await append_event(session_id, "assistant_text", {
                        "run_id": run_id,
                        "text": text,
                        "backend": BACKEND_CLAUDE,
                        **({"provider_message_id": message_uuid}
                           if (message_uuid := claude_sdk_field(message, "uuid")) else {}),
                        **local_command,
                        **run_event_metadata(run_id),
                    })
            elif block_type in {"ThinkingBlock", "thinking"}:
                thinking = str(claude_sdk_field(block, "thinking") or "")
                if thinking:
                    await append_event(session_id, "reasoning_summary", {
                        "run_id": run_id,
                        "text": thinking,
                    })
            elif block_type in {
                "ToolUseBlock",
                "ServerToolUseBlock",
                "tool_use",
                "server_tool_use",
            }:
                if tool_activity_run_ids is not None:
                    tool_activity_run_ids.add(run_id)
                tool_id = str(
                    claude_sdk_field(block, "id")
                    or f"tool_{uuid.uuid4().hex[:8]}"
                )
                tool_input = claude_sdk_field(block, "input", {})
                tool = {
                    "id": tool_id,
                    "name": str(claude_sdk_field(block, "name") or "tool"),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
                current_tools[tool_id] = tool
                changed_paths.update(tool_changed_paths(tool))
                await append_event(session_id, "tool_started", {
                    "run_id": run_id,
                    "tool": tool,
                })
            elif block_type in {
                "ToolResultBlock",
                "ServerToolResultBlock",
                "tool_result",
                "server_tool_result",
                "advisor_tool_result",
            }:
                await project_tool_result(block)
        await project_subagent_lifecycle()
        return None

    if message_type == "UserMessage":
        content = claude_sdk_field(message, "content", [])
        if isinstance(content, str):
            remember_claude_local_command_output(
                local_command_outputs,
                content,
                claude_sdk_field(message, "uuid"),
            )
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolResultBlock",
                "ServerToolResultBlock",
                "tool_result",
                "server_tool_result",
                "advisor_tool_result",
            }:
                continue
            await project_tool_result(block)
        await project_subagent_lifecycle()
        return None

    if message_type in {"ResultMessage", "result"}:
        details = claude_sdk_result_details(message)
        await add_claude_turn_usage(session_id, claude_sdk_field(message, "usage"))
        if await publish_claude_local_command_outputs(
            session_id,
            run_id,
            details["local_command"],
            local_command_outputs,
            text_parts,
        ) and native_output_run_ids is not None:
            native_output_run_ids.add(run_id)
        return details
    await project_subagent_lifecycle()
    return None
