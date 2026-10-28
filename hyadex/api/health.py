"""Server health, connection addresses, runtime catalog and usage, working-directory completion."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
import websockets

from hyadex.api.common import require_native_admin_control
from hyadex.core.config import (
    AGENT_TOKEN,
    API_CONTRACT_VERSION,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    canonical_server_display_name,
    CLAUDE_PERMISSION_MODE_OPTIONS,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_GOALS_ENABLED,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    EMERGENCY_ACTIVE_ALERT_LIMIT,
    EMERGENCY_MESSAGE_MAX_CHARS,
    EMERGENCY_REQUESTS_PER_RUN,
    EMERGENCY_WEBSOCKET_PROTOCOL,
    enabled_backends,
    EVENTS_WEBSOCKET_PROTOCOL,
    HOST_HEALTH_FILE,
    MAX_WORKING_DIRECTORY_COMPLETIONS,
    MAX_WORKSPACE_PATH_CHARS,
    NOTIFICATION_REQUESTS_PER_RUN,
    PROVIDER_JOBS_ACCESS_DEFAULT,
    PROVIDER_JOBS_ACCESS_MODES,
    SERVER_INSTANCE_ID,
    SERVER_VERSION,
    STATE_DIR,
    TERMINAL_WEBSOCKET_PROTOCOL,
)
from hyadex.core.host import host_pressure_snapshot
from hyadex.core.http import router
from hyadex.core.paths import complete_working_directory_sync, existing_cwd
from hyadex.core.queue_admission import queue_recovery_readiness
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.sessions import CLAUDE_STOP_FENCE_SESSIONS, STORE
from hyadex.core.turn_start import reconcile_idle_queued_turns_from_health_poll
from hyadex.core.util import bounded_lock, server_identity
from hyadex.features.jobs import JOBS, scheduled_job_resource_diagnostics
from hyadex.features.terminal import tmux_capability
from hyadex.features.usage import PROVIDER_USAGE
from hyadex.ops.lifecycle import (
    current_server_restart_blocker_snapshot,
    server_restart_capability,
    SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS,
)
from hyadex.providers.claude.context_usage import claude_sdk_dependency_available
from hyadex.providers.claude.catalog import read_claude_account_usage
from hyadex.providers.codex.commands import (
    codex_app_server_manager,
    refresh_codex_app_server_binary,
    refresh_codex_app_server_login,
)
from hyadex.providers.registry import discover_runtime_catalog, runtime_diagnostics_snapshot
from hyadex.providers.state import PROVIDERS
from hyadex.providers.commands import MAX_PROVIDER_COMMANDS
from hyadex.providers import usage as provider_usage
from hyadex.features import side_question_service as side_questions

router = APIRouter()

HYADEX_SERVER_DISPLAY_NAME = canonical_server_display_name(
    os.environ.get("HYADEX_SERVER_NAME")
)


@router.get("/api/connection-addresses")
async def connection_addresses(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    # Discover only on explicit QR sharing, never on the frequently polled health path.
    from hyadex.ops.instances import candidate_addresses, connection_choices, tailscale_status

    def discover() -> list[str]:
        choices = connection_choices(
            SERVER.bind_address, SERVER.port,
            candidate_addresses(SERVER.bind_address, SERVER.port), tailscale_status(),
        )
        return list(dict.fromkeys(filter(None, [choices["tailscale"], *choices["lan"]])))[:6]

    return {"version": 1, "server_identity": server_identity(), "urls": await asyncio.to_thread(discover)}


@router.get("/api/health")
async def health() -> dict[str, Any]:
    await reconcile_idle_queued_turns_from_health_poll()
    # Health must answer even when an admission lock is wedged; the reads
    # below are consistent without the lock on a single-threaded loop.
    async with bounded_lock(RUNS.active_lock, SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS):
        active = sorted(RUNS.busy)
    async with bounded_lock(RUNS.queue_lock, SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS):
        queued = {sid: len(queue) for sid, queue in RUNS.queued.items() if queue}
    restart_blocker_snapshot = await current_server_restart_blocker_snapshot()
    pressure = host_pressure_snapshot()
    tmux = tmux_capability(use_cache=True)
    return {
        "ok": True,
        "chat_readiness": queue_recovery_readiness(),
        "server_version": SERVER_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
        "server_identity": server_identity(),
        "server_instance_id": SERVER_INSTANCE_ID,
        "server_name": HYADEX_SERVER_DISPLAY_NAME,
        "state_dir": str(STATE_DIR),
        "default_backend": DEFAULT_BACKEND,
        "default_cwd": existing_cwd(DEFAULT_CWD),
        "auth_required": bool(AGENT_TOKEN),
        "capabilities": {
            "group_discussions_v1": {"version": 1, "modes": ["sequential", "parallel"], "jobs": True,
                                     "goals": True, "enabled_backends": sorted(enabled_backends())},
            "autopilot_v1": {"version": 1, "approval": ["plan", "step", "none"],
                             "isolation": ["worktree", "in_place"], "reviewer": ["planner", "fresh", "none"]},
            "subagent_limit_v1": {"version": 1, "backends": ["codex", "claude"]},
            "provider_usage": {"available": bool(AGENT_TOKEN), "version": 1, "backends": ["codex", "claude"]},
            "side_questions": side_questions.capability(),
            "websocket_auth_v1": {
                "available": True,
                "required": False,
                "version": 1,
                "message": (
                    "Event and terminal sockets support URL-free token "
                    "authentication."
                ),
                "action": None,
                "event_protocol": EVENTS_WEBSOCKET_PROTOCOL,
                "terminal_protocol": TERMINAL_WEBSOCKET_PROTOCOL,
            },
            "event_stream_liveness_v1": {"available": True, "version": 1},
            "queue_resume_v1": {
                "available": True,
                "version": 1,
                "endpoint": "/api/sessions/{session_id}/queue/resume",
                "message": "Messages held by Stop can be resumed in queue order.",
            },
            "session_fork_completed_prefix_v1": {
                "available": True,
                "version": 1,
                "supported_backends": [BACKEND_CLAUDE, BACKEND_CODEX],
            },
            "local_provider_commands_v1": {
                "available": True,
                "required": False,
                "version": 1,
                "endpoint": "/api/sessions/{session_id}/provider-commands",
                "supported_backends": [BACKEND_CODEX, BACKEND_CLAUDE],
                "client_capability_by_backend": {
                    BACKEND_CODEX: CODEX_INTERACTIVE_CLIENT_CAPABILITY,
                    BACKEND_CLAUDE: CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
                },
                "supported_kinds_by_backend": {
                    BACKEND_CODEX: ["skill"],
                    BACKEND_CLAUDE: ["command"],
                },
                "max_items": MAX_PROVIDER_COMMANDS,
                "message": (
                    "Session-scoped Claude commands and Codex skills are available."
                ),
                "action": None,
            },
            "server_restart": server_restart_capability(
                restart_blocker_snapshot
            ),
            "tmux": tmux,
            "working_directory_completion": {
                "available": True,
                "required": False,
                "message": "Working-directory suggestions are available from this server.",
                "action": None,
                "version": 1,
                "max_results": MAX_WORKING_DIRECTORY_COMPLETIONS,
            },
            "scheduled_jobs": {
                "available": True,
                "required": False,
                "message": (
                    "Scheduled jobs support parent-chat and standalone "
                    "provider contexts."
                ),
                "action": None,
                "version": 6,
                "context_modes": ["chat", "standalone"],
                "default_context_mode": "chat",
                "features": {
                    "next_run_reset": True,
                    "interval_next_run_reanchors": True,
                },
            },
            "provider_jobs_access_control_v1": {
                "available": bool(AGENT_TOKEN),
                "required": False,
                "message": (
                    "Per-chat provider scheduled-jobs access control is available."
                    if AGENT_TOKEN
                    else "Provider scheduled-jobs access control requires an authenticated HyadexServer configuration."
                ),
                "action": None if AGENT_TOKEN else "Configure an HyadexServer client token.",
                "version": 1,
                "modes": list(PROVIDER_JOBS_ACCESS_MODES),
                "default": PROVIDER_JOBS_ACCESS_DEFAULT,
            },
            "agent_emergency_alerts_v1": {
                "available": True,
                "required": False,
                "message": "Agents can raise explicit, durable emergency alerts for immediate user attention.",
                "action": None,
                "version": 1,
                "max_message_chars": EMERGENCY_MESSAGE_MAX_CHARS,
                "max_requests_per_run": EMERGENCY_REQUESTS_PER_RUN,
                "max_active_alerts": EMERGENCY_ACTIVE_ALERT_LIMIT,
                "stream_path": "/api/emergency-alerts/events",
                "stream_protocol": EMERGENCY_WEBSOCKET_PROTOCOL,
                "acknowledge_through_alert_id": True,
            },
            "agent_notification_levels_v1": {
                "available": bool(AGENT_TOKEN),
                "required": False,
                "message": "Agents can choose durable normal, important, or emergency notification levels.",
                "action": None if AGENT_TOKEN else "Configure an HyadexServer client token.",
                "version": 1,
                "levels": ["normal", "important", "emergency"],
                "max_message_chars": EMERGENCY_MESSAGE_MAX_CHARS,
                "max_requests_per_run": NOTIFICATION_REQUESTS_PER_RUN,
            },
            "codex_controls": {
                "available": CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC,
                "required": False,
                "message": (
                    "Interactive Codex app-server controls are available to opted-in clients."
                    if CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC
                    else "Codex controls require the app-server transport."
                ),
                "action": (
                    None
                    if CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC
                    else "Set HYADEX_CODEX_TRANSPORT=app-server or auto."
                ),
                "version": 2,
                "interactive_client_capability": CODEX_INTERACTIVE_CLIENT_CAPABILITY,
                "features": {
                    "approvals": True,
                    "questions": True,
                    "goals": CODEX_GOALS_ENABLED,
                    "compaction": True,
                    "thread_status": True,
                },
            },
            "claude_controls": {
                "available": (
                    CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
                    and claude_sdk_dependency_available()
                ),
                "required": False,
                "message": (
                    "Per-chat Claude Agent SDK controls are available to opted-in clients."
                    if (
                        CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
                        and claude_sdk_dependency_available()
                    )
                    else "Claude uses the compatible print transport."
                ),
                "action": (
                    None
                    if claude_sdk_dependency_available()
                    else "Install the server release with Claude Agent SDK support."
                ),
                "version": 3,
                "interactive_client_capability": (
                    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY
                ),
                "permission_modes": list(CLAUDE_PERMISSION_MODE_OPTIONS),
                "features": {
                    # Every real turn now carries run-scoped out-of-band
                    # authority. In-place provider steering cannot prove that
                    # a delayed pre-steer tool call belongs to the successor
                    # run, so Force Send deliberately uses Stop -> fresh turn.
                    "native_steer": False,
                    "force_send": True,
                    "interrupt": True,
                    "approvals": True,
                    "questions": True,
                    "permission_mode_control": True,
                    "permission_modes": True,
                    "context_usage_refresh": True,
                    "mcp_management": True,
                    "resume": True,
                    "fork": True,
                },
                "fallback_transport": CLAUDE_TRANSPORT_PRINT,
            },
        },
        "websocket_runtime": True,
        "websocket_runtime_version": websockets.__version__,
        "active": active,
        "active_count": len(active),
        "claude_stop_fence_count": len(CLAUDE_STOP_FENCE_SESSIONS),
        "queued": queued,
        "jobs": len(JOBS.jobs),
        "job_guard": {**pressure, **scheduled_job_resource_diagnostics(pressure, len(active))},
        "host_health_log": str(HOST_HEALTH_FILE),
        "runtimes": runtime_diagnostics_snapshot(),
    }


@router.get("/api/runtime/catalog")
async def runtime_catalog(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        await refresh_codex_app_server_binary(force=True)
        await refresh_codex_app_server_login(source="catalog_refresh")
    return await asyncio.to_thread(discover_runtime_catalog, force_runtime_probe=refresh)


@router.get("/api/runtime/usage")
async def runtime_provider_usage(
    request: Request, backend: str, session_id: str | None = None,
    refresh: bool = False,
) -> JSONResponse:
    require_native_admin_control(request)
    session = STORE.sessions.get(session_id) if session_id else None
    if session_id and session is None:
        raise HTTPException(404, "session not found")
    if backend not in {BACKEND_CODEX, BACKEND_CLAUDE}:
        result = provider_usage.unavailable(backend, "unsupported_provider")
    elif session is not None and str(session.get("backend") or DEFAULT_BACKEND).lower() != backend:
        result = provider_usage.unavailable(backend, "selection_changed")
    elif backend == BACKEND_CODEX:
        if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
            result = provider_usage.unavailable(backend, "unsupported_transport")
        else:
            try:
                manager = await codex_app_server_manager(session, allow_retired_login=True)
                result = await PROVIDER_USAGE.read_codex(manager, refresh=refresh)
            except Exception:
                result = provider_usage.unavailable(backend, "temporarily_unavailable")
    else:
        result = await read_claude_account_usage(refresh=refresh)
        if result.get("status") != "available":
            generation = (PROVIDERS.claude_sdk_manager.usage_generation(session_id)
                          if session_id and PROVIDERS.claude_sdk_manager is not None else None)
            observed = PROVIDER_USAGE.read_claude(session_id or "", generation)
            if observed.get("status") == "available":
                result = observed
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/working-directories/complete")
async def complete_working_directory(
    path: str = Query(default="", max_length=MAX_WORKSPACE_PATH_CHARS),
    limit: int = Query(default=24, ge=1, le=MAX_WORKING_DIRECTORY_COMPLETIONS),
) -> dict[str, Any]:
    return await asyncio.to_thread(complete_working_directory_sync, path, limit)
