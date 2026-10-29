"""Deep chat consults on Claude: a read-only fork of a chat investigates one question.

The fork resumes the chat's own Claude conversation with ``fork_session``, so
the chat's transcript, SDK process and queue are untouched. It is offered only
Read, Grep and Glob; every other tool is refused, no MCP server is loaded (so it
cannot ask another chat in turn), and its transcript is removed when it ends.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import shutil
from typing import Any, Callable

from hyadex.core.config import DEFAULT_CWD
from hyadex.core.paths import existing_cwd
from hyadex.providers.authority import agent_runner_env
from hyadex.providers.claude.catalog import claude_sdk_cli_path
from hyadex.providers.claude.runtime import claude_resume_file_for_cwd, resolve_claude_resume_provider
from hyadex.providers.claude.sdk import create_claude_agent_options, default_claude_sdk_client_factory
from hyadex.providers.runtime import PROVIDER_SECRET_ENV_NAMES, is_scrubbed_provider_runtime_env_name

CONSULT_TOOLS = ("Read", "Grep", "Glob")
CONSULT_MAX_TURNS = 40
CONSULT_SYSTEM_APPEND = (
    "This session is a temporary, read-only copy of a chat, answering a question another chat "
    "asked it. Only Read, Grep and Glob are available; never try to change anything."
)
SESSION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")


class ClaudeConsultUnavailable(RuntimeError):
    """The chat has no Claude conversation this folder can resume."""


def consult_step(name: str, tool_input: dict[str, Any], cwd: str) -> dict[str, str]:
    """One line of the fork's progress, as the asking chat shows it."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").rstrip("/")
    folder = cwd.rstrip("/")
    # Paths inside the chat's folder are shown relative to it; the folder itself not at all.
    for root in {folder, os.path.realpath(folder)}:
        if path == root:
            path = ""
        elif path.startswith(root + "/"):
            path = path[len(root) + 1:]
    if name == "Read":
        return {"kind": "read", "title": path or name}
    if name in {"Grep", "Glob"}:
        pattern = str(tool_input.get("pattern") or "")
        return {"kind": "search", "title": f"{pattern} · {path}" if pattern and path else pattern or path or name}
    return {"kind": "other", "title": name}


def remove_fork_transcript(fork_id: str | None, parent_id: str, cwd: str) -> None:
    """Delete the fork's own transcript; never the chat's."""
    if not fork_id or fork_id == parent_id or not SESSION_ID.match(fork_id):
        return
    transcript = claude_resume_file_for_cwd(fork_id, cwd)
    if transcript.name != f"{fork_id}.jsonl":
        return
    with suppress(OSError):
        transcript.unlink()
    # Tool results and subagents of a session sit in a folder of its id.
    folder = transcript.with_suffix("")
    if folder.name == fork_id and folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder, ignore_errors=True)


async def investigate(session_id: str, sess: dict[str, Any], prompt: str, *,
                      on_step: Callable[[dict[str, str]], None]) -> dict[str, Any]:
    """Fork the chat's Claude conversation read-only and answer `prompt`."""
    cwd = existing_cwd(str(sess.get("cwd") or DEFAULT_CWD))
    parent_id, reason = resolve_claude_resume_provider(sess, cwd)
    if not parent_id:
        raise ClaudeConsultUnavailable(reason or "the asked chat has no Claude conversation to read yet")
    env = agent_runner_env(session_id)
    # The SDK overlays options.env on the server's environment: shadow every
    # authority secret, as the chat's own runs do.
    for name in tuple(os.environ):
        if name in PROVIDER_SECRET_ENV_NAMES or is_scrubbed_provider_runtime_env_name(name):
            env[name] = ""

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any) -> Any:
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
        if tool_name in CONSULT_TOOLS:
            return PermissionResultAllow(updated_input=dict(input_data))
        return PermissionResultDeny(message="This consult is read-only: use Read, Grep or Glob.", interrupt=False)

    options = create_claude_agent_options(
        system_prompt={"type": "preset", "preset": "claude_code", "append": CONSULT_SYSTEM_APPEND},
        tools=list(CONSULT_TOOLS),
        allowed_tools=list(CONSULT_TOOLS),
        permission_mode="default",
        can_use_tool=can_use_tool,
        model=str(sess.get("model") or "").strip() or None,
        effort=str(sess.get("effort") or "").strip() or None,
        cwd=cwd,
        cli_path=claude_sdk_cli_path(env),
        env=env,
        mcp_servers={},
        strict_mcp_config=True,
        resume=parent_id,
        fork_session=True,
        setting_sources=["user", "project", "local"],
        max_turns=CONSULT_MAX_TURNS,
        thinking={"type": "adaptive", "display": "summarized"},
    )
    client = default_claude_sdk_client_factory(options)
    fork_id: str | None = None
    final_text: list[str] = []
    result: dict[str, Any] = {}
    try:
        await client.connect()
        await client.query(prompt)
        async for message in client.receive_response():
            kind = type(message).__name__
            if kind == "SystemMessage" and getattr(message, "subtype", "") == "init":
                fork_id = str((getattr(message, "data", None) or {}).get("session_id") or "") or fork_id
            elif kind == "AssistantMessage":
                for block in getattr(message, "content", None) or []:
                    block_kind = type(block).__name__
                    if block_kind == "ToolUseBlock":
                        # Text before a tool call is narration; the answer follows the last one.
                        final_text.clear()
                        on_step(consult_step(str(getattr(block, "name", "")),
                                             dict(getattr(block, "input", None) or {}), cwd))
                    elif block_kind == "TextBlock":
                        final_text.append(str(getattr(block, "text", "")))
            elif kind == "ResultMessage":
                fork_id = str(getattr(message, "session_id", "") or "") or fork_id
                result = {
                    "text": getattr(message, "result", None),
                    "error": getattr(message, "subtype", "") if getattr(message, "is_error", False) else "",
                    "usage": getattr(message, "usage", None) or {},
                    "cost": getattr(message, "total_cost_usd", None),
                }
    finally:
        # Close the fork's CLI even when the consult is cancelled mid-read,
        # then let the cancellation continue.
        closing = asyncio.ensure_future(client.disconnect())
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if closing.done() and not closing.cancelled():
            closing.exception()
        remove_fork_transcript(fork_id, parent_id, cwd)
        if cancelled:
            raise asyncio.CancelledError
    answer = str(result.get("text") or "").strip() or "\n\n".join(final_text).strip()
    if result.get("error") and not answer:
        raise RuntimeError(f"Claude ended the consult with {result['error']}")
    usage = result.get("usage") or {}
    tokens_in = sum(int(usage.get(key) or 0) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return {
        "answer": answer,
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": int(usage.get("output_tokens") or 0),
            **({"cost_usd": float(result["cost"])} if isinstance(result.get("cost"), (int, float)) else {}),
        },
    }


__all__ = ["CONSULT_TOOLS", "ClaudeConsultUnavailable", "consult_step", "investigate", "remove_fork_transcript"]
