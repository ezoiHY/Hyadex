"""Codex provider-tool MCP endpoint used by agents inside a turn."""
from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_PROVIDER_MCP_PATH,
    CODEX_TRANSPORT_APP_SERVER,
    SERVER_VERSION,
)
from hyadex.core.http import route_auth, router
from hyadex.core.runs import RUNS
from hyadex.core.sessions import STORE
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.providers.authority import ProviderToolError
from hyadex.providers.codex.runtime import (
    codex_provider_mcp_jsonrpc_error,
    codex_provider_mcp_run_proof,
    codex_provider_mcp_tool_definition,
    execute_provider_tool_once,
    session_references_codex_thread,
)
from hyadex.providers.instructions import PROVIDER_TOOL_MAX_BODY_BYTES

router = APIRouter()


@router.post(CODEX_PROVIDER_MCP_PATH, dependencies=route_auth("provider_mcp", PROVIDER_TOOL_MAX_BODY_BYTES))
async def codex_provider_mcp(request: Request) -> Response:
    """Minimal Streamable-HTTP MCP endpoint for the static Codex tool."""

    if getattr(request.state, "codex_provider_mcp_authenticated", False) is not True:
        raise HTTPException(status_code=403, detail="Codex provider MCP is forbidden")
    try:
        message = await request.json()
    except Exception:
        return codex_provider_mcp_jsonrpc_error(None, -32700, "Parse error")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return codex_provider_mcp_jsonrpc_error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid params")
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or "2025-06-18")
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Hyadex", "version": SERVER_VERSION},
            },
        })
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [codex_provider_mcp_tool_definition()]},
        })
    if method != "tools/call":
        return codex_provider_mcp_jsonrpc_error(request_id, -32601, "Method not found")
    if params.get("name") != "run" or not isinstance(params.get("arguments"), dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid params")
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Missing turn metadata")
    turn_meta = meta.get("x-codex-turn-metadata")
    if not isinstance(turn_meta, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Missing turn metadata")
    if any(
        turn_meta.get(name) is not None
        for name in (
            "parent_thread_id",
            "parent_turn_id",
            "root_turn_id",
            "subagent_kind",
        )
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Subagent tool calls are forbidden")
    thread_id = str(turn_meta.get("thread_id") or "")
    turn_id = str(turn_meta.get("turn_id") or "")
    client_meta = turn_meta.get("responsesapi_client_metadata")
    if not isinstance(client_meta, dict):
        client_meta = turn_meta.get("responsesapiClientMetadata")
    if not isinstance(client_meta, dict):
        client_meta = turn_meta
    run_id = str(client_meta.get("hyadex_run_id") or "")
    proof = str(client_meta.get("hyadex_run_proof") or "")
    core_call_id = str(meta.get("callId") or "")
    if (
        not thread_id
        or len(thread_id) > 256
        or not turn_id
        or len(turn_id) > 256
        or re.fullmatch(r"run_[A-Za-z0-9_-]{1,128}", run_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", proof) is None
        or not core_call_id
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Incomplete turn metadata")
    if len(core_call_id) > 512:
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid call identity")
    standalone_active_owner = False
    async with RUNS.active_lock:
        owners = [
            session_id
            for session_id, active in RUNS.active.items()
            if (
                active.get("backend") == BACKEND_CODEX
                and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                and str(active.get("provider_thread_id") or "") == thread_id
                and str(active.get("run_id") or "") == run_id
                and session_id in RUNS.busy
            )
        ]
        if len(owners) == 1:
            standalone_active_owner = bool(
                (RUNS.active.get(owners[0]) or {}).get(
                    "standalone_provider_context"
                )
            )
    if len(owners) != 1:
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Stale turn metadata")
    session_id = owners[0]
    stored_owners = [
        stored_session_id
        for stored_session_id, stored in STORE.sessions.items()
        if (
            stored_session_id not in DELETING_SESSIONS
            and stored_session_id not in DELETED_SESSION_TOMBSTONES
            and session_references_codex_thread(stored, thread_id)
        )
    ]
    if stored_owners != [session_id] and not (
        not stored_owners and standalone_active_owner
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Ambiguous thread owner")
    expected_proof = codex_provider_mcp_run_proof(session_id, thread_id, run_id)
    if not hmac.compare_digest(proof, expected_proof):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid turn proof")
    try:
        text, is_error = await execute_provider_tool_once(
            session_id,
            run_id,
            params["arguments"],
            replay_key="codex:" + core_call_id,
            backend=BACKEND_CODEX,
            provider_thread_id=thread_id,
            provider_turn_id=turn_id,
        )
    except ProviderToolError as exc:
        text, is_error = str(exc), True
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    })
