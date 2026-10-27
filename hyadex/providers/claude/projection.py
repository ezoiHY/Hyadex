"""Projecting Claude SDK messages into chat events."""
from __future__ import annotations

import json
from typing import Any
import uuid

from hyadex.core.config import BACKEND_CLAUDE, CLAUDE_TRANSPORT_AGENT_SDK
from hyadex.core.runs import RUNS
from hyadex.core.sessions import append_event, event_output_text
from hyadex.core.util import _patch_changed_paths, clean_assistant_text, normalized_tool_name
from hyadex.features.files import run_event_metadata
from hyadex.providers.claude.subagents import (
    claude_sdk_field,
    claude_sdk_subagent_lifecycle_raw,
    claude_sdk_type,
)


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
) -> dict[str, Any] | None:
    """Project one typed SDK message into the existing durable timeline."""

    message_type = claude_sdk_type(message)
    provider_id = str(claude_sdk_field(message, "session_id") or "")
    if message_type == "SystemMessage":
        data = claude_sdk_field(message, "data", {})
        if isinstance(data, dict):
            provider_id = str(data.get("session_id") or provider_id)
    if provider_id:
        await mark_provider_turn_ready(session_id, run_id, provider_id)

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
        for block in content if isinstance(content, list) else []:
            block_type = claude_sdk_type(block)
            if block_type in {"TextBlock", "text"}:
                text = clean_assistant_text(
                    str(claude_sdk_field(block, "text") or "")
                )
                if text:
                    text_parts.append(text)
                    await append_event(session_id, "assistant_text", {
                        "run_id": run_id,
                        "text": text,
                        "backend": BACKEND_CLAUDE,
                        **({"provider_message_id": message_uuid}
                           if (message_uuid := claude_sdk_field(message, "uuid")) else {}),
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
        return claude_sdk_result_details(message)
    await project_subagent_lifecycle()
    return None
