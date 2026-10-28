"""Claude MCP server status and control for one chat (v1 contract).

The SDK client and manager own the provider calls; this module admits one
lifecycle-serialized, maintenance-reserved operation and projects the SDK
status through an explicit, display-safe allowlist.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
)
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.paths import codex_manifest_path, existing_cwd
from hyadex.core.runs import RUNS
from hyadex.core.sessions import ensure_session_not_deleting, session_lifecycle_lock, STORE
from hyadex.providers.claude.context_usage import claude_sdk_dependency_available
from hyadex.providers.claude.runtime import claude_sdk_manager
from hyadex.providers.claude.sdk import (
    CLAUDE_PROVIDER_MCP_SERVER_NAME,
    CLAUDE_SDK_MCP_STATUS_TRUNCATED_KEY,
    ClaudeSDKConfigurationConflict,
    ClaudeSDKControlTimeout,
    ClaudeSDKGenerationChanged,
    ClaudeSDKMCPServerNotFound,
    ClaudeSDKRunActive,
    ClaudeSDKUnavailable,
    canonical_claude_mcp_identifier,
)
from hyadex.providers.codex.commands import build_claude_sdk_options

CLAUDE_MCP_CONTRACT_VERSION = 1
CLAUDE_MCP_MAX_SERVERS = 100
CLAUDE_MCP_STATUS_SCAN_LIMIT = 500
CLAUDE_MCP_MAX_TOOL_COUNT = 100_000


class ClaudeMCPControlRequest(BaseModel):
    version: Literal[1]
    action: Literal["reconnect", "reconnect_all", "enable", "disable"]
    server_name: str | None = Field(default=None, max_length=512)
    expected_generation: str = Field(min_length=1, max_length=128)


def claude_mcp_error_detail(
    code: str,
    message: str,
    *,
    retryable: bool,
) -> dict[str, Any]:
    """Return the stable, display-safe v1 MCP error envelope."""

    return {
        "code": str(code),
        "message": str(message),
        "retryable": bool(retryable),
    }


def claude_mcp_display_string(value: Any, max_chars: int) -> str | None:
    """Return exact, bounded NFC metadata safe for display and round trips."""

    return canonical_claude_mcp_identifier(value, max_chars)


def claude_mcp_display_metadata(value: Any, max_chars: int) -> str | None:
    """Allow bounded handshake labels while rejecting credential/URL shapes."""

    text = claude_mcp_display_string(value, max_chars)
    if text is None or re.search(
        r"(?i)(?:[a-z][a-z0-9+.-]*://|authorization|bearer|token|secret|password|api[_-]?key)",
        text,
    ):
        return None
    return text


def public_claude_mcp_server(value: Any) -> dict[str, Any] | None:
    """Project one SDK status through an explicit non-secret allowlist.

    Raw provider errors and config contain potentially sensitive commands,
    environment values, headers, and configuration URLs. They are never
    returned by this API. Clients receive only a generic status-specific error
    summary.
    """

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
    raw_status = str(value.get("status") or "").strip().lower()
    allowed_statuses = {
        "connected",
        "pending",
        "failed",
        "needs-auth",
        "disabled",
    }
    status = raw_status if raw_status in allowed_statuses else "unknown"
    safe_error = {
        "failed": "Claude could not connect to this MCP server.",
        "needs-auth": "This MCP server needs authentication.",
        "unknown": "Claude reported an unrecognized MCP server state.",
    }.get(status)
    raw_scope = str(value.get("scope") or "").strip().lower()
    scope = (
        raw_scope
        if raw_scope in {"user", "project", "local", "claudeai", "managed"}
        else None
    )
    raw_server_info = value.get("serverInfo")
    server_info: dict[str, str] | None = None
    if isinstance(raw_server_info, dict):
        info_name = claude_mcp_display_metadata(raw_server_info.get("name"), 240)
        info_version = claude_mcp_display_metadata(
            raw_server_info.get("version"),
            120,
        )
        if info_name and info_version:
            server_info = {"name": info_name, "version": info_version}
    tools = value.get("tools")
    tool_count = (
        len(tools)
        if isinstance(tools, list) and len(tools) <= CLAUDE_MCP_MAX_TOOL_COUNT
        else None
    )
    return {
        "name": name,
        "status": status,
        "enabled": status != "disabled",
        "error": safe_error,
        "scope": scope,
        "server_info": server_info,
        "tool_count": tool_count,
    }


def public_claude_mcp_servers(raw_servers: Any) -> tuple[list[dict[str, Any]], bool]:
    """Return a deterministic, deduplicated, bounded MCP server projection."""

    if not isinstance(raw_servers, list):
        return [], False
    by_name: dict[str, dict[str, Any]] = {}
    scan_count = min(len(raw_servers), CLAUDE_MCP_STATUS_SCAN_LIMIT)
    truncated = len(raw_servers) > scan_count
    for index in range(scan_count):
        item = raw_servers[index]
        # This in-process server is HyadexServer's private provider-control
        # transport, not a user MCP integration. It must never appear in the
        # public panel or count as malformed/truncated public state.
        if (
            isinstance(item, dict)
            and item.get("name") == CLAUDE_PROVIDER_MCP_SERVER_NAME
        ):
            continue
        projected = public_claude_mcp_server(item)
        if projected is None:
            truncated = True
            continue
        name = str(projected["name"])
        if name in by_name:
            truncated = True
            continue
        if len(by_name) >= CLAUDE_MCP_MAX_SERVERS:
            truncated = True
            break
        by_name[name] = projected
    servers = sorted(
        by_name.values(),
        key=lambda item: (str(item["name"]).casefold(), str(item["name"])),
    )
    return servers, truncated


def claude_mcp_snapshot(
    raw: dict[str, Any],
    *,
    generation: str,
    action: str | None = None,
    server_name: str | None = None,
) -> dict[str, Any]:
    servers, truncated = public_claude_mcp_servers(raw.get("mcpServers"))
    truncated = bool(
        truncated or raw.get(CLAUDE_SDK_MCP_STATUS_TRUNCATED_KEY)
    )
    return {
        "version": CLAUDE_MCP_CONTRACT_VERSION,
        "available": True,
        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
        "generation": str(generation),
        "session_loaded": True,
        "servers": servers,
        "truncated": truncated,
        "reason": None,
        "action": (
            {"type": action, "server_name": server_name}
            if action is not None
            else None
        ),
    }


def unavailable_claude_mcp_snapshot() -> dict[str, Any]:
    if CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT:
        reason = claude_mcp_error_detail(
            "claude_mcp_print_transport",
            "MCP management requires the Claude Agent SDK transport.",
            retryable=False,
        )
    else:
        reason = claude_mcp_error_detail(
            "claude_mcp_sdk_unavailable",
            "Claude MCP management is unavailable on this HyadexServer.",
            retryable=False,
        )
    return {
        "version": CLAUDE_MCP_CONTRACT_VERSION,
        "available": False,
        "transport": CLAUDE_TRANSPORT_PRINT,
        "generation": None,
        "session_loaded": False,
        "servers": [],
        "truncated": False,
        "reason": reason,
        "action": None,
    }


def claude_mcp_http_error(
    exc: BaseException,
    *,
    mutating: bool,
) -> HTTPException:
    if isinstance(exc, ClaudeSDKRunActive):
        return HTTPException(
            status_code=409,
            detail=claude_mcp_error_detail(
                "claude_mcp_turn_active",
                "Wait for the active Claude turn to finish before managing MCP servers.",
                retryable=True,
            ),
        )
    if isinstance(exc, (ClaudeSDKGenerationChanged, ClaudeSDKConfigurationConflict)):
        return HTTPException(
            status_code=409,
            detail=claude_mcp_error_detail(
                "claude_mcp_generation_changed",
                "Claude MCP state changed. Refresh the panel before trying again.",
                retryable=True,
            ),
        )
    if isinstance(exc, ClaudeSDKMCPServerNotFound):
        return HTTPException(
            status_code=404,
            detail=claude_mcp_error_detail(
                "claude_mcp_server_not_found",
                "That MCP server is no longer in this Claude profile.",
                retryable=False,
            ),
        )
    if isinstance(exc, ClaudeSDKControlTimeout):
        return HTTPException(
            status_code=503,
            detail=claude_mcp_error_detail(
                (
                    "claude_mcp_control_failed"
                    if mutating
                    else "claude_mcp_connection_failed"
                ),
                "Claude MCP management timed out. Refresh and try again.",
                retryable=True,
            ),
        )
    if isinstance(exc, ClaudeSDKUnavailable):
        return HTTPException(
            status_code=503,
            detail=claude_mcp_error_detail(
                "claude_mcp_connection_failed",
                "Claude MCP management could not connect on this server.",
                retryable=True,
            ),
        )
    return HTTPException(
        status_code=503,
        detail=claude_mcp_error_detail(
            "claude_mcp_control_failed" if mutating else "claude_mcp_connection_failed",
            (
                "Claude could not update MCP servers. Refresh and try again."
                if mutating
                else "Claude MCP status is temporarily unavailable."
            ),
            retryable=True,
        ),
    )


async def manage_claude_mcp(
    session_id: str,
    request: ClaudeMCPControlRequest | None = None,
) -> dict[str, Any]:
    """Run one lifecycle-serialized, bounded MCP status/control operation."""

    mutating = request is not None
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
            raise HTTPException(
                status_code=409,
                detail=claude_mcp_error_detail(
                    "claude_mcp_wrong_backend",
                    "MCP management requires a Claude chat.",
                    retryable=False,
                ),
            )

        sdk_available = claude_sdk_dependency_available()
        if CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT or not sdk_available:
            unavailable = unavailable_claude_mcp_snapshot()
            if not mutating:
                return unavailable
            raise HTTPException(
                status_code=409,
                detail=claude_mcp_error_detail(
                    "claude_mcp_unavailable",
                    str((unavailable.get("reason") or {}).get("message") or ""),
                    retryable=False,
                ),
            )

        action: str | None = None
        server_name: str | None = None
        expected_generation = ""
        if request is not None:
            action = str(request.action)
            expected_generation = str(request.expected_generation)
            if action == "reconnect_all":
                if request.server_name is not None:
                    raise HTTPException(
                        status_code=400,
                        detail=claude_mcp_error_detail(
                            "claude_mcp_invalid_request",
                            "reconnect_all does not accept a server name.",
                            retryable=False,
                        ),
                    )
                server_name = None
            else:
                raw_server_name = request.server_name
                server_name = (
                    claude_mcp_display_string(raw_server_name, 512)
                    if isinstance(raw_server_name, str)
                    else None
                )
                if not server_name or server_name != raw_server_name:
                    raise HTTPException(
                        status_code=400,
                        detail=claude_mcp_error_detail(
                            "claude_mcp_invalid_request",
                            "A canonical server name is required for this MCP action.",
                            retryable=False,
                        ),
                    )
                if server_name == CLAUDE_PROVIDER_MCP_SERVER_NAME:
                    raise HTTPException(
                        status_code=404,
                        detail=claude_mcp_error_detail(
                            "claude_mcp_server_not_found",
                            "That MCP server is not user-manageable.",
                            retryable=False,
                        ),
                    )

        maintenance_reserved = False
        try:
            async with RUNS.active_lock:
                provider_starting = any(
                    not task.done()
                    for task in tuple(RUNS.turn_tasks.get(session_id) or ())
                )
                if (
                    session_id in RUNS.busy
                    or RUNS.active.get(session_id) is not None
                    or RUNS.current_turns.get(session_id) is not None
                    or provider_starting
                ):
                    raise claude_mcp_http_error(
                        ClaudeSDKRunActive(
                            f"Claude chat {session_id} has active provider work"
                        ),
                        mutating=mutating,
                    )
                if session_id in RUNS.maintenance:
                    raise HTTPException(
                        status_code=409,
                        detail=claude_mcp_error_detail(
                            "claude_mcp_maintenance_active",
                            "Another provider maintenance request is using this chat.",
                            retryable=True,
                        ),
                    )
                lifecycle_blocker = server_lifecycle_admission_blocker()
                if lifecycle_blocker:
                    raise HTTPException(status_code=503, detail=lifecycle_blocker)
                RUNS.maintenance.add(session_id)
                maintenance_reserved = True

            cwd = existing_cwd(str(session.get("cwd") or DEFAULT_CWD))
            try:
                options, configuration_key, _cli_path = build_claude_sdk_options(
                    session_id,
                    session,
                    cwd,
                    codex_manifest_path(session_id),
                )
                manager = await claude_sdk_manager()
                if request is None:
                    raw, generation = await manager.get_mcp_status(
                        session_id,
                        options=options,
                        configuration_key=configuration_key,
                    )
                else:
                    raw, generation = await manager.mutate_mcp_server(
                        session_id,
                        action=action or "",
                        server_name=server_name,
                        expected_generation=expected_generation,
                        options=options,
                        configuration_key=configuration_key,
                    )
            except asyncio.CancelledError:
                # Manager cancellation cleanup retires the exact idle owner
                # before this lifecycle/maintenance fence is released.
                raise
            except HTTPException:
                raise
            except Exception as exc:
                raise claude_mcp_http_error(exc, mutating=mutating) from exc

            current = STORE.sessions.get(session_id)
            if (
                current is not session
                or str((current or {}).get("backend") or DEFAULT_BACKEND)
                != BACKEND_CLAUDE
            ):
                raise HTTPException(
                    status_code=409,
                    detail=claude_mcp_error_detail(
                        "claude_mcp_generation_changed",
                        "Claude MCP state changed. Refresh the panel before trying again.",
                        retryable=True,
                    ),
                )
            return claude_mcp_snapshot(
                raw,
                generation=generation,
                action=action,
                server_name=server_name,
            )
        finally:
            if maintenance_reserved:
                async with RUNS.active_lock:
                    RUNS.maintenance.discard(session_id)
