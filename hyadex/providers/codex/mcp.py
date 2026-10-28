"""Codex MCP server status for one chat, in the Claude MCP v1 envelope.

Read-only: the app-server's native ``mcpServerStatus/list`` is projected
through the same display-safe allowlist as the Claude route. Raw provider
errors, endpoints (``httpOrigin``), commands, environment values and tool
schemas are never returned.

Status vocabulary (``servers[].status``), from ``runtimeStatus``:

- ``connected`` <- ``connected``
- ``pending`` <- ``starting`` and ``notStarted`` (the server is configured but
  this thread has not connected it yet)
- ``needs-auth`` <- ``authenticationRequired``, or any non-disabled status
  whose ``authStatus`` is ``notLoggedIn``
- ``failed`` <- ``failed`` and ``cancelled``
- ``disabled`` <- ``disabled`` (``enabled`` is false)
- ``unknown`` <- ``null`` (no loaded thread runtime) or an unrecognized value;
  ``error`` stays null when ``runtimeStatus`` is null.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_PROVIDER_MCP_NAME,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    hyadex_setting,
)
from hyadex.core.session_codex import existing_codex_app_server_manager, session_codex_thread_id
from hyadex.core.sessions import ensure_session_not_deleting, STORE
from hyadex.providers.claude.mcp import (
    CLAUDE_MCP_CONTRACT_VERSION,
    CLAUDE_MCP_MAX_SERVERS,
    CLAUDE_MCP_MAX_TOOL_COUNT,
    CLAUDE_MCP_STATUS_SCAN_LIMIT,
    claude_mcp_display_metadata,
    claude_mcp_display_string,
    claude_mcp_error_detail,
)
from hyadex.providers.codex.commands import codex_app_server_manager

CODEX_MCP_STATUS_TIMEOUT_SECONDS = max(
    1.0,
    float(hyadex_setting("CODEX_MCP_STATUS_TIMEOUT_SECONDS", "15")),
)
# Pages fetched per request; together with the scan limit this bounds the
# work one status read can ask of the app-server.
CODEX_MCP_STATUS_MAX_PAGES = 10

CODEX_MCP_RUNTIME_STATUS = {
    "connected": "connected",
    "starting": "pending",
    "notStarted": "pending",
    "authenticationRequired": "needs-auth",
    "failed": "failed",
    "cancelled": "failed",
    "disabled": "disabled",
}
CODEX_MCP_STATUS_ERRORS = {
    "failed": "Codex could not connect to this MCP server.",
    "needs-auth": "This MCP server needs authentication.",
}
CODEX_MCP_UNKNOWN_STATUS_ERROR = "Codex reported an unrecognized MCP server state."


def public_codex_mcp_server(value: Any) -> dict[str, Any] | None:
    """Project one ``McpServerStatus`` through an explicit allowlist."""

    if not isinstance(value, dict):
        return None
    raw_name = value.get("name")
    name = (
        claude_mcp_display_string(raw_name, 512)
        if isinstance(raw_name, str)
        else None
    )
    if not name or name != raw_name:
        return None
    raw_status = value.get("runtimeStatus")
    if raw_status is None:
        status, error = "unknown", None
    else:
        status = CODEX_MCP_RUNTIME_STATUS.get(str(raw_status), "unknown")
        if status != "disabled" and value.get("authStatus") == "notLoggedIn":
            status = "needs-auth"
        error = (
            CODEX_MCP_UNKNOWN_STATUS_ERROR
            if status == "unknown"
            else CODEX_MCP_STATUS_ERRORS.get(status)
        )
    raw_server_info = value.get("serverInfo")
    server_info: dict[str, str] | None = None
    if isinstance(raw_server_info, dict):
        info_name = claude_mcp_display_metadata(raw_server_info.get("name"), 240)
        info_version = claude_mcp_display_metadata(raw_server_info.get("version"), 120)
        if info_name and info_version:
            server_info = {"name": info_name, "version": info_version}
    tools = value.get("tools")
    tool_count = (
        len(tools)
        if isinstance(tools, (dict, list)) and len(tools) <= CLAUDE_MCP_MAX_TOOL_COUNT
        else None
    )
    return {
        "name": name,
        "status": status,
        "enabled": status != "disabled",
        "error": error,
        # Codex has no user/project/local scope for a configured server.
        "scope": None,
        "server_info": server_info,
        "tool_count": tool_count,
    }


def public_codex_mcp_servers(raw_servers: list[Any], *, incomplete: bool) -> tuple[list[dict[str, Any]], bool]:
    by_name: dict[str, dict[str, Any]] = {}
    scan_count = min(len(raw_servers), CLAUDE_MCP_STATUS_SCAN_LIMIT)
    truncated = incomplete or len(raw_servers) > scan_count
    for item in raw_servers[:scan_count]:
        # Hyadex's own provider-control server is not a user integration.
        if isinstance(item, dict) and item.get("name") == CODEX_PROVIDER_MCP_NAME:
            continue
        projected = public_codex_mcp_server(item)
        if projected is None or projected["name"] in by_name:
            truncated = True
            continue
        if len(by_name) >= CLAUDE_MCP_MAX_SERVERS:
            truncated = True
            break
        by_name[projected["name"]] = projected
    servers = sorted(
        by_name.values(),
        key=lambda item: (str(item["name"]).casefold(), str(item["name"])),
    )
    return servers, truncated


def codex_mcp_generation(servers: list[dict[str, Any]], thread_id: str | None) -> str:
    """An opaque token that changes when the projected state changes."""

    digest = hashlib.sha256(
        json.dumps([thread_id or "", servers], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return f"codexmcp_{digest}"


def unavailable_codex_mcp_snapshot(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "version": CLAUDE_MCP_CONTRACT_VERSION,
        "available": False,
        "transport": CODEX_TRANSPORT_EXEC if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC else CODEX_TRANSPORT_APP_SERVER,
        "generation": None,
        "session_loaded": False,
        "servers": [],
        "truncated": False,
        "reason": claude_mcp_error_detail(code, message, retryable=retryable),
        "action": None,
    }


async def list_codex_mcp_status(manager: Any, thread_id: str | None) -> tuple[list[Any], bool]:
    """Read every page up to the scan limit; return the rows and whether more remain."""

    rows: list[Any] = []
    cursor: str | None = None
    for _page in range(CODEX_MCP_STATUS_MAX_PAGES):
        params: dict[str, Any] = {"detail": "toolsAndAuthOnly"}
        if thread_id:
            params["threadId"] = thread_id
        if cursor:
            params["cursor"] = cursor
        response = await manager.request(
            "mcpServerStatus/list",
            params,
            timeout=CODEX_MCP_STATUS_TIMEOUT_SECONDS,
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise ValueError("malformed mcpServerStatus/list response")
        rows.extend(response["data"])
        next_cursor = response.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            return rows, False
        public_rows = sum(
            1 for item in rows
            if not (isinstance(item, dict) and item.get("name") == CODEX_PROVIDER_MCP_NAME)
        )
        # More rows than the panel shows are already known: stop paging.
        if public_rows > CLAUDE_MCP_MAX_SERVERS or len(rows) >= CLAUDE_MCP_STATUS_SCAN_LIMIT:
            return rows, True
        cursor = next_cursor
    return rows, True


async def codex_mcp_snapshot(session_id: str) -> dict[str, Any]:
    ensure_session_not_deleting(session_id)
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CODEX:
        raise HTTPException(
            status_code=409,
            detail=claude_mcp_error_detail(
                "codex_mcp_wrong_backend",
                "Codex MCP status requires a Codex chat.",
                retryable=False,
            ),
        )
    if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
        return unavailable_codex_mcp_snapshot(
            "codex_mcp_exec_transport",
            "MCP status requires the Codex app-server transport.",
            retryable=False,
        )

    # Only a thread the app-server already holds reports its runtime status;
    # an unloaded thread is never resumed just to read MCP status.
    thread_id = session_codex_thread_id(session) or None
    loaded = False
    if thread_id:
        existing = existing_codex_app_server_manager(session)
        checker = getattr(existing, "is_thread_loaded", None)
        if callable(checker):
            try:
                loaded = bool(checker(thread_id))
            except Exception:
                loaded = False
    selected_thread = thread_id if loaded else None
    try:
        manager = await codex_app_server_manager(session)
        rows, incomplete = await list_codex_mcp_status(manager, selected_thread)
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        return unavailable_codex_mcp_snapshot(
            "codex_mcp_connection_failed",
            "Codex MCP status is temporarily unavailable.",
            retryable=True,
        )
    servers, truncated = public_codex_mcp_servers(rows, incomplete=incomplete)
    return {
        "version": CLAUDE_MCP_CONTRACT_VERSION,
        "available": True,
        "transport": CODEX_TRANSPORT_APP_SERVER,
        "generation": codex_mcp_generation(servers, selected_thread),
        "session_loaded": loaded,
        "servers": servers,
        "truncated": truncated,
        "reason": None,
        "action": None,
    }
