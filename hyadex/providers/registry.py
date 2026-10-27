"""Provider registry: backends, catalogs, models, efforts, runtime diagnostics and settings."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import copy_context
import hashlib
import hmac
import ipaddress
import json
import logging.handlers
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Literal

from fastapi import HTTPException, Request

from hyadex.core.config import (
    AGENT_TOKEN,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_BIN,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_PRINT,
    CODEX_BIN,
    CODEX_DEFAULT_EFFORT,
    CODEX_DEFAULT_MODEL,
    CODEX_FALLBACK_EFFORTS,
    CODEX_FALLBACK_MODEL_EFFORTS,
    CODEX_FALLBACK_MODELS,
    CODEX_GOALS_ENABLED,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    EMERGENCY_AUTHORITY_DENIED_PURPOSES,
    PROVIDER_CHILD_PROC_ROOT,
    PROVIDER_CHILDREN_FILE,
    RETIRED_QUEUED_TURN_FIELDS,
    RETIRED_TURN_PURPOSES,
    RUNTIME_CATALOG_BUDGET_SECONDS,
    RUNTIME_CATALOG_DEADLINE,
    RUNTIME_DIAGNOSTIC_TTL_SECONDS,
    STATE_DIR,
    VALID_BACKENDS,
)
from hyadex.core.paths import now_iso
from hyadex.core.processes import (
    _provider_child_ps,
    process_group_for_pid,
    provider_child_parent_pid,
    PROVIDER_CHILD_PROBE_TIMEOUT_SECONDS,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    clamp_codex_runtime_effort,
    codex_default_service_tier,
    codex_goal_time_budget_remaining,
    codex_retired_model_replacements,
    codex_user_config_defaults,
    effective_provider_jobs_access,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    REMOVED_PROVIDER_MESSAGE,
    retired_session_runtime_message,
    revoke_provider_authority,
    RUNTIME_DIAGNOSTICS,
    RUNTIME_DIAGNOSTICS_LOCK,
    STORE,
)
from hyadex.core.state import AGENT_HELPER_PROXY_HEADERS
from hyadex.core.util import (
    compact_memory_text,
    merged_file_ids,
    normalized_network_address,
    runner_env,
    server_identity,
    utc_timestamp,
)
from hyadex.features.files import file_attachment_prompt_lines
from hyadex.features.usage import broadcast_provider_runtime_changed
from hyadex.providers.authority import (
    issue_provider_authority,
    NativeSteerHandoffError,
    provider_authority_runtime_env,
    provider_capability_header,
    provider_capability_is_attached_to_live_run,
)
from hyadex.providers.claude.runtime import (
    claude_background_work_labels_from_events,
    claude_event_file_fingerprints,
    claude_sdk_dependency_available,
    loaded_claude_background_session_state,
    parse_claude_help_catalog,
    run_catalog_command,
    runtime_catalog_budget_expired,
    runtime_command,
    runtime_option,
    RuntimeCatalogBudgetExpired,
    title_effort_label,
    title_model_label,
    unique_runtime_options,
)
from hyadex.providers.instructions import HYADEX_CONTEXT_END_MARKER
from hyadex.providers.runtime import assert_provider_user_message_unchanged, ProviderTurnPayload
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import prepare_codex_subagent_terminal_snapshot
from hyadex.store.files import atomic_json
import local_session_ownership
from provider_commands import ProviderCommandInventory

logger = logging.getLogger("hyadex-server")

# Provider command selectors are deliberately scoped to one server process.
# The native path remains opaque even to an authenticated inventory client.
# A restart rotates this key; durable queued selections then fail visibly and
# are terminally removed without blocking later FIFO rows.
PROVIDER_COMMAND_SELECTOR_SECRET = secrets.token_hex(32)
PROVIDER_COMMAND_EMPTY_FALLBACK_SECRET = secrets.token_hex(32)


def provider_command_selector_secret() -> str:
    """Return this process's private HMAC key for opaque native selectors."""

    return PROVIDER_COMMAND_SELECTOR_SECRET


def network_host_is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def request_has_agent_helper_proxy_headers(request: Request) -> bool:
    """Reject requests whose socket peer may be an identity-hiding proxy."""

    return any(
        bytes(name).lower() in AGENT_HELPER_PROXY_HEADERS
        or bytes(name).lower().startswith(b"tailscale-user-")
        for name, _value in request.scope.get("headers", [])
    )


def request_client_is_loopback(request: Request) -> bool:
    # A reverse proxy itself connects from loopback, so the socket peer alone
    # does not prove this is the local provider helper. Standard proxy and
    # Tailscale Serve identity headers must fail closed before a per-run
    # capability is inspected. The local CLIs connect straight to 127.0.0.1
    # and never send these headers.
    if request_has_agent_helper_proxy_headers(request):
        return False
    client = request.client
    return bool(client and network_host_is_loopback(str(client.host or "")))


def request_client_is_provider_helper_local(request: Request) -> bool:
    """Accept a local provider process across the supported server bind matrix."""

    if request_client_is_loopback(request):
        return True
    if request_has_agent_helper_proxy_headers(request):
        return False
    client = request.client
    if client is None:
        return False
    try:
        peer = normalized_network_address(str(client.host or ""))
        configured = normalized_network_address(str(SERVER.bind_address or ""))
    except ValueError:
        return False
    # Wildcard listeners are always addressed through a family-matched
    # loopback above. A specific LAN/Tailscale bind is reachable locally only
    # through that exact interface address; the process-secret header remains
    # mandatory in middleware.
    return not configured.is_unspecified and peer == configured


class ProviderCommandSelectionInvalid(HTTPException):
    """A selected provider inventory entry must be chosen again."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=409, detail=detail)


class ProviderCommandSelectionUnavailable(HTTPException):
    """A selected provider command could not be revalidated right now."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=503, detail=detail)


RUNTIME_DIAGNOSTIC_GENERATIONS: dict[str, int] = {}


def normalize_steering_lineage(value: Any) -> list[dict[str, Any]]:
    """Return persisted steering messages without any generated provider text."""
    if not isinstance(value, list):
        return []
    lineage: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "")
        file_ids_value = item.get("file_ids")
        file_ids = merged_file_ids(file_ids_value if isinstance(file_ids_value, (list, tuple)) else [])
        if prompt.strip() or file_ids:
            lineage.append({"prompt": prompt, "file_ids": file_ids})
    return lineage


def build_user_provider_prompt(
    session_id: str,
    prompt: str,
    file_ids: list[str] | tuple[str, ...],
) -> str:
    provider_prompt = prompt
    attachment_lines = file_attachment_prompt_lines(session_id, file_ids)
    if attachment_lines:
        provider_prompt += "\n\n[Attached files]\n"
        provider_prompt += "\n".join(attachment_lines) + "\n"
        provider_prompt += "Use these local paths directly when needed.\n"
    return provider_prompt


def build_turn_provider_prompt(
    session_id: str,
    prompt: str,
    file_ids: list[str] | tuple[str, ...],
    steering_lineage: list[dict[str, Any]] | None = None,
) -> str:
    """Return only the newest immutable user message for the resumed provider thread."""
    lineage = normalize_steering_lineage(steering_lineage)
    if len(lineage) >= 2:
        current = lineage[-1]
        return build_user_provider_prompt(
            session_id,
            str(current.get("prompt") or ""),
            list(current.get("file_ids") or []),
        )
    return build_user_provider_prompt(session_id, prompt, file_ids)


def provider_turn_may_raise_emergency(purpose: Any) -> bool:
    return str(purpose or "").strip() not in EMERGENCY_AUTHORITY_DENIED_PURPOSES


def native_steer_provider_actions(
    source_session_id: str,
    selected: dict[str, Any],
) -> tuple[set[str], str]:
    """Return the helper ceiling for a plain steering message."""

    if (
        queued_turn_uses_retired_routing(selected)
        or selected.get("secure_peer_route_snapshots")
    ):
        raise NativeSteerHandoffError(
            "route-bearing work cannot reuse a native provider turn",
            safe_to_requeue=True,
        )
    jobs_access = effective_provider_jobs_access(
        STORE.sessions.get(source_session_id)
    )
    actions = {"publish"}
    if provider_turn_may_raise_emergency(selected.get("purpose")):
        actions.update({"emergency", "notify"})
    if jobs_access != "blocked" and AGENT_TOKEN:
        actions.add("jobs")
    return actions, jobs_access


async def issue_native_steer_provider_authority(
    source_session_id: str,
    candidate_run_id: str,
    selected: dict[str, Any],
    request_prompt: str,
    transition_nonce: str,
    backend: str | None = None,
) -> tuple[str, Path, dict[str, str]]:
    """Issue a fresh authority without mutating the steering user message."""

    del backend
    actions, _jobs_access = native_steer_provider_actions(
        source_session_id,
        selected,
    )
    authority_path = await issue_provider_authority(
        source_session_id,
        candidate_run_id,
        actions=actions,
        native_transition_nonce=transition_nonce,
    )
    if authority_path is None:
        raise NativeSteerHandoffError(
            "could not issue provider authority for the steering run",
            safe_to_requeue=True,
        )
    try:
        runtime_env = await provider_authority_runtime_env(
            candidate_run_id,
            authority_path,
            source_session_id,
        )
        payload = ProviderTurnPayload(request_prompt, "", runtime_env)
        assert_provider_user_message_unchanged(
            payload.user_prompt,
            request_prompt,
        )
    except BaseException:
        await revoke_provider_authority(candidate_run_id)
        raise
    return payload.user_prompt, authority_path, payload.runtime_env


def queued_turn_uses_retired_routing(row: dict[str, Any]) -> bool:
    """Whether a durable queue row depends on removed chat routing."""

    return str(row.get("purpose") or "") in RETIRED_TURN_PURPOSES or any(
        row.get(key) for key in RETIRED_QUEUED_TURN_FIELDS
    )


async def turn_slot_is_owned(session_id: str, run_id: str) -> bool:
    """Return whether ``run_id`` still owns this chat's admitted turn slot."""

    async with RUNS.active_lock:
        if session_id not in RUNS.busy:
            return False
        active = RUNS.active.get(session_id)
        current_turn = RUNS.current_turns.get(session_id)
        active_run_id = (
            str(active.get("run_id") or "")
            if isinstance(active, dict)
            else ""
        )
        current_run_id = (
            str(current_turn.get("run_id") or "")
            if isinstance(current_turn, dict)
            else ""
        )
        return (
            run_id in {active_run_id, current_run_id}
            and all(
                owner_id in {"", run_id}
                for owner_id in (active_run_id, current_run_id)
            )
        )


async def authorize_provider_action(
    request: Request,
    *,
    action: Literal[
        "jobs",
        "publish",
        "emergency",
        "notify",
    ],
    session_id: str,
) -> dict[str, Any]:
    if not request_client_is_provider_helper_local(request):
        raise HTTPException(status_code=403, detail="agent helper route is restricted to local provider clients")
    token = provider_capability_header(request)
    if not token:
        raise HTTPException(status_code=403, detail="provider capability is required")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    authorized: dict[str, Any]
    async with PROVIDER_AUTHORITY_LOCK:
        capability = PROVIDER_AUTHORITIES.get(token_hash)
        if not capability:
            raise HTTPException(status_code=403, detail="provider capability is invalid")
        if capability.get("server_identity") != server_identity():
            raise HTTPException(status_code=403, detail="provider capability belongs to another server")
        if str(capability.get("source_session_id") or "") != session_id:
            raise HTTPException(status_code=403, detail="provider capability is bound to another chat")
        run_id = str(capability.get("source_run_id") or "")
        allow_native_transition = action == "jobs"
        if not provider_capability_is_attached_to_live_run(
            session_id,
            run_id,
            str(capability.get("native_transition_nonce") or ""),
            allow_native_transition=allow_native_transition,
        ):
            if float(capability.get("expires_at") or 0) <= time.time():
                PROVIDER_AUTHORITIES.pop(token_hash, None)
            raise HTTPException(status_code=403, detail="provider capability is no longer attached to a live turn")
        if action not in capability.get("actions", set()):
            raise HTTPException(status_code=403, detail="provider action was not authorized")
        authorized = dict(capability)
    return authorized


def strip_hyadex_provider_context(text: str, *, allow_jobless_legacy: bool = False) -> str:
    """Remove launch-only context from a Codex transcript user message."""
    if not text.startswith("[Hyadex context]"):
        return text
    exact_marker = f"\n{HYADEX_CONTEXT_END_MARKER}\n\n"
    marker_index = text.find(exact_marker)
    if marker_index >= 0:
        return text[marker_index + len(exact_marker):]

    # Legacy codex-exec turns put the base context, job snapshot, and optional
    # per-chat instructions directly in the user message. Parse only those
    # server-owned boundaries; never strip a marker from ordinary API input.
    legacy_base_marker = "User prompt follows.\n]\n\n"
    base_index = text.find(legacy_base_marker)
    if base_index < 0:
        return text
    remainder = text[base_index + len(legacy_base_marker):]
    scheduled_start = remainder.find("Scheduled jobs:")
    snapshot_start = remainder.find(
        "Current jobs for this chat (turn-start snapshot; prompts omitted):",
        max(scheduled_start, 0),
    )
    if scheduled_start < 0 or snapshot_start < 0:
        # Early codex-exec prompts predate the jobs snapshot. Enable this
        # only for display previews, not reconciliation/durable history keys.
        # A bare bracket label or an ordinary human quotation is insufficient.
        if not (allow_jobless_legacy and base_index <= 64 * 1024 and text.startswith((
            "[Hyadex context]\nYou are responding through Hyadex, backed by HyadexServer.\n",
            "[Hyadex context]\nYou are operating through Hyadex, backed by HyadexServer.\n",
        ))):
            return text
    else:
        scheduled_end = remainder.find("\n\n", snapshot_start)
        if scheduled_end < 0:
            return text
        remainder = remainder[scheduled_end + 2:]
    if remainder.startswith("\n[Per-chat system instructions]\n"):
        remainder = remainder[1:]
    if remainder.startswith("[Per-chat system instructions]\n"):
        system_end_marker = "\n[End per-chat system instructions]\n\n"
        system_end = remainder.find(system_end_marker)
        if system_end < 0:
            return text
        remainder = remainder[system_end + len(system_end_marker):]
    return remainder


def other_local_instance_provider_keys() -> set[tuple[str, str]]:
    try:
        return local_session_ownership.other_instance_provider_keys(STATE_DIR)
    except (OSError, ValueError) as exc:
        logger.warning("local import ownership check failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Cannot verify chats already used by other local server instances. Check their configuration/session indexes and retry.",
        ) from exc


def standalone_provider_session(sess: dict[str, Any]) -> dict[str, Any]:
    """Clone chat settings without any provider-thread continuity.

    Standalone scheduled runs begin with the parent chat's backend, model, cwd,
    permissions, and developer instructions. Their isolated clone may select a
    different backend for that run without loosening the parent chat's backend
    lock. They intentionally do not inherit its provider thread, fork memory,
    or persistent Codex goal.
    """

    isolated = dict(sess)
    isolated["session_id"] = None
    isolated["claude_session_id"] = None
    isolated["codex_thread_id"] = None
    isolated["backend_locked"] = False
    isolated["fork_from"] = None
    isolated.pop("fork_resume_session_at", None)
    isolated["memory_seed"] = None
    isolated["memory_seed_used"] = False
    isolated["codex_goal"] = None
    isolated["codex_goal_time_budget_seconds"] = None
    isolated["codex_goal_time_budget_exhausted"] = False
    return isolated


def provider_context_goal_is_exhausted(
    sess: dict[str, Any],
    provider_context_mode: Literal["chat", "standalone"],
) -> bool:
    """Apply a parent chat's goal budget only when continuing that chat."""

    return (
        CODEX_GOALS_ENABLED
        and provider_context_mode == "chat"
        and str(sess.get("backend") or DEFAULT_BACKEND) == BACKEND_CODEX
        and codex_goal_time_budget_remaining(sess) == 0
    )


def runtime_executable(backend: str) -> str:
    if backend == BACKEND_CLAUDE:
        return CLAUDE_BIN
    if backend == BACKEND_CODEX:
        return CODEX_BIN
    raise HTTPException(410, "This provider runtime has been removed or is unsupported.")


def runtime_display_name(backend: str) -> str:
    return {BACKEND_CLAUDE: "Claude Code", BACKEND_CODEX: "Codex"}.get(backend, backend)


def runtime_action(
    backend: str,
    status: str,
    *,
    executable: str | None = None,
) -> str | None:
    if status in {"ready", "unknown"}:
        return None
    executable = executable or runtime_executable(backend)
    public_executable = executable
    if status == "missing":
        return f"Install {runtime_display_name(backend)} for the server user, make `{public_executable}` available on PATH, then restart the agent server."
    if status == "unauthenticated":
        if backend == BACKEND_CLAUDE:
            return (
                "Run `claude auth login` as the server user, then retry your "
                "message. Claude checks sign-in during the actual request."
            )
        else:
            command = "codex login"
        return f"Run `{command}` as the server user, then click Recheck CLIs."
    if status == "error":
        if backend == BACKEND_CLAUDE:
            return (
                f"Run `{public_executable} --version` as the server user to "
                "check the Claude installation, then retry your message."
            )
        else:
            command = "codex login status"
        return f"Run `{executable} --version` and `{command}` as the server user, then refresh runtime status."
    return None


def runtime_diagnostic_payload(
    backend: str,
    status: str,
    *,
    installed: bool | None,
    authenticated: bool | None,
    version: str | None = None,
    message: str | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    display = runtime_display_name(backend)
    default_messages = {
        "ready": f"{display} is installed and authenticated.",
        "missing": f"{display} is not installed or is not available to the agent server.",
        "unauthenticated": f"{display} is installed but the server user is not authenticated.",
        "error": f"{display} is installed but its runtime check failed.",
        "unknown": f"{display} has not been checked yet.",
    }
    return {
        "backend": backend,
        "status": status,
        "available": status == "ready",
        "installed": installed,
        "authenticated": authenticated,
        "version": version,
        "message": message or default_messages.get(status, default_messages["unknown"]),
        "action": runtime_action(
            backend,
            status,
            executable=executable,
        ),
        **({"_executable": executable} if executable else {}),
        "checked_at": now_iso(),
        "checked_at_epoch": time.time(),
        "last_error": None,
        "last_error_at": None,
    }


def safe_runtime_version(output: str) -> str | None:
    first_line = next((line.strip() for line in str(output or "").splitlines() if line.strip()), "")
    if not first_line:
        return None
    clean = re.sub(r"[^A-Za-z0-9._+()\- /]", "", first_line).strip()
    return clean[:120] or None


def auth_failure_text(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "authorizationrequired", "authentication failed",
        "authentication required", "not authenticated", "not logged in",
        "login required", "sign-in required", "sign in required",
        "unauthorized", "invalid api key", "missing api key",
        "please log in", "please login", "please sign in",
    ))


def probe_runtime(backend: str) -> dict[str, Any]:
    configured = runtime_executable(backend)
    resolved = shutil.which(configured, path=runner_env().get("PATH"))
    if not resolved:
        return runtime_diagnostic_payload(
            backend,
            "missing",
            installed=False,
            authenticated=False,
            executable=None,
        )

    try:
        version_result = runtime_command([resolved, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s version probe failed: %s", backend, type(exc).__name__)
        return runtime_diagnostic_payload(
            backend,
            "error",
            installed=True,
            authenticated=None,
            executable=None,
        )
    version = safe_runtime_version(version_result.stdout or version_result.stderr)
    if version_result.returncode != 0:
        return runtime_diagnostic_payload(
            backend,
            "error",
            installed=True,
            authenticated=None,
            version=version,
            executable=None,
        )


    if backend == BACKEND_CLAUDE:
        # Even `auth status` can start OAuth renewal during CLI initialization
        # and exit before the replacement credential is saved. Never invoke it
        # for startup, catalog refresh, manual recheck, or turn admission.
        # Actual Claude runs own authentication and update the cached result.
        return runtime_diagnostic_payload(
            backend,
            "unknown",
            installed=True,
            authenticated=None,
            version=version,
            message=(
                "Claude Code is installed. Authentication will be checked by "
                "Claude when you send a message."
            ),
        )
    auth_cmd = [resolved, "login", "status"]
    try:
        auth_result = runtime_command(auth_cmd)
    except subprocess.TimeoutExpired:
        logger.warning("%s authentication check timed out", backend)
        return runtime_diagnostic_payload(
            backend,
            "error",
            installed=True,
            authenticated=None,
            version=version,
            message=(
                f"{runtime_display_name(backend)} authentication check timed out. "
                "Authentication could not be confirmed; this does not mean you are signed out. "
                "Try Re-check again."
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s auth probe failed: %s", backend, type(exc).__name__)
        return runtime_diagnostic_payload(
            backend,
            "error",
            installed=True,
            authenticated=None,
            version=version,
            executable=None,
        )

    combined = f"{auth_result.stdout}\n{auth_result.stderr}"
    if auth_result.returncode == 0:
        result = runtime_diagnostic_payload(backend, "ready", installed=True, authenticated=True, version=version)
        if backend == BACKEND_CODEX:
            # Retain only the kind, never login-status output (which may show a
            # masked API key). No additional auth/refresh subprocess is needed.
            text = combined.lower()
            result["_codex_auth_mode"] = "apiKey" if "api key" in text else "chatgpt" if "chatgpt" in text else "unknown"
            result["_codex_auth_mode_checked_at_epoch"] = result["checked_at_epoch"]
        return result
    if auth_failure_text(combined):
        return runtime_diagnostic_payload(backend, "unauthenticated", installed=True, authenticated=False, version=version)
    return runtime_diagnostic_payload(backend, "error", installed=True, authenticated=None, version=version)


def store_runtime_diagnostic(diagnostic: dict[str, Any], *, preserve_last_error: bool = True) -> dict[str, Any]:
    backend = str(diagnostic.get("backend") or "")
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = RUNTIME_DIAGNOSTICS.get(backend) or {}
        current = dict(diagnostic)
        if preserve_last_error and previous.get("last_error"):
            current["last_error"] = previous.get("last_error")
            current["last_error_at"] = previous.get("last_error_at")
        RUNTIME_DIAGNOSTICS[backend] = current
        RUNTIME_DIAGNOSTIC_GENERATIONS[backend] = (
            RUNTIME_DIAGNOSTIC_GENERATIONS.get(backend, 0) + 1
        )
        return dict(current)


def runtime_diagnostic(backend: str, *, force: bool = False) -> dict[str, Any]:
    with RUNTIME_DIAGNOSTICS_LOCK:
        cached = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
        generation = RUNTIME_DIAGNOSTIC_GENERATIONS.get(backend, 0)
    checked_at = cached.get("_installation_checked_at_epoch", cached.get("checked_at_epoch"))
    if backend == BACKEND_CODEX and cached.get("_codex_auth_mode_checked_at_epoch") is not None:
        checked_at = min(checked_at or 0, cached["_codex_auth_mode_checked_at_epoch"])
    if not force and isinstance(checked_at, (int, float)) and time.time() - checked_at < RUNTIME_DIAGNOSTIC_TTL_SECONDS:
        return cached
    try:
        if runtime_catalog_budget_expired():
            raise RuntimeCatalogBudgetExpired()
        probed = probe_runtime(backend)
    except RuntimeCatalogBudgetExpired:
        # Do not cache an incomplete check or make old evidence look fresh.
        # In particular, budget starvation must not mark another provider
        # missing, incompatible, or signed out for the diagnostic cache TTL.
        with RUNTIME_DIAGNOSTICS_LOCK:
            current = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
        if current:
            return current
        return runtime_diagnostic_payload(
            backend, "unknown", installed=None, authenticated=None,
            message=(
                f"{runtime_display_name(backend)} check did not finish before the "
                "runtime refresh deadline. Try Re-check again."
            ),
        )
    with RUNTIME_DIAGNOSTICS_LOCK:
        if RUNTIME_DIAGNOSTIC_GENERATIONS.get(backend, 0) != generation:
            current = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
            if current:
                return current
        if (
            backend == BACKEND_CLAUDE
            and probed.get("status") == "unknown"
            and probed.get("installed") is True
            and cached.get("status") in {"ready", "unauthenticated"}
        ):
            # Rechecking the executable is not new authentication evidence.
            # Preserve the native run's timestamp and error as well as status;
            # cache installation checks separately so each turn need not probe.
            probed = {
                **cached,
                "installed": True,
                "version": probed.get("version"),
                "_installation_checked_at_epoch": probed.get("checked_at_epoch"),
            }
        return store_runtime_diagnostic(probed)


def refresh_runtime_diagnostics(*, force: bool = False) -> dict[str, dict[str, Any]]:
    return {backend: runtime_diagnostic(backend, force=force) for backend in sorted(VALID_BACKENDS)}


def public_runtime_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in diagnostic.items()
        if key != "checked_at_epoch" and not str(key).startswith("_")
    }


def runtime_diagnostics_snapshot() -> dict[str, dict[str, Any]]:
    with RUNTIME_DIAGNOSTICS_LOCK:
        return {
            backend: public_runtime_diagnostic(dict(RUNTIME_DIAGNOSTICS.get(backend) or runtime_diagnostic_payload(
                backend, "unknown", installed=None, authenticated=None
            )))
            for backend in sorted(VALID_BACKENDS)
        }


def record_runtime_failure(
    backend: str,
    error: Any,
    *,
    spawn_failure: bool = False,
    executable: str | None = None,
    auth_failure: bool | None = None,
    runtime_error: bool = False,
) -> None:
    text = str(error or "").strip()
    lower = text.lower()
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = dict(RUNTIME_DIAGNOSTICS.get(backend) or runtime_diagnostic_payload(
            backend, "unknown", installed=None, authenticated=None
        ))
    is_auth_failure = (
        auth_failure
        if auth_failure is not None
        else auth_failure_text(text)
    )
    if backend == BACKEND_CLAUDE and auth_failure is None:
        is_auth_failure = is_auth_failure or "oauth session expired and could not be refreshed" in lower
    if is_auth_failure:
        if backend == BACKEND_CLAUDE:
            from claude_model_catalog import clear_native_models
            clear_native_models()
        current = runtime_diagnostic_payload(
            backend,
            "unauthenticated",
            installed=True,
            authenticated=False,
            version=previous.get("version"),
            executable=None,
        )
    elif runtime_error:
        current = runtime_diagnostic_payload(
            backend,
            "error",
            installed=True,
            authenticated=None,
            version=previous.get("version"),
            executable=None,
        )
    elif spawn_failure and (
        isinstance(error, FileNotFoundError)
        or any(marker in lower for marker in ("no such file", "enoent", "failed to start"))
    ):
        current = runtime_diagnostic_payload(
            backend, "missing", installed=False, authenticated=False, version=previous.get("version")
        )
    else:
        current = previous
    failure = compact_memory_text(text, 700) if text else ""
    current["last_error"] = failure or "The latest provider run failed. Open the chat error for details, then retry or refresh runtime status."
    current["last_error_at"] = now_iso()
    store_runtime_diagnostic(current, preserve_last_error=False)


def record_runtime_success(backend: str) -> None:
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
    current = runtime_diagnostic_payload(
        backend,
        "ready",
        installed=True,
        authenticated=True,
        message=("The last Claude request authenticated successfully." if backend == BACKEND_CLAUDE else None),
        version=previous.get("version"),
        executable=None,
    )
    if backend == BACKEND_CODEX and previous.get("_codex_auth_mode"):
        # A successful turn does not establish which login kind was used.
        # Preserve the existing observation without extending its lifetime.
        current["_codex_auth_mode"] = previous["_codex_auth_mode"]
        current["_codex_auth_mode_checked_at_epoch"] = previous.get(
            "_codex_auth_mode_checked_at_epoch", previous.get("checked_at_epoch", 0),
        )
    store_runtime_diagnostic(current, preserve_last_error=False)


async def ensure_runtime_available(backend: str, *, session: dict[str, Any] | None = None) -> dict[str, Any]:
    if backend not in VALID_BACKENDS:
        raise HTTPException(410, REMOVED_PROVIDER_MESSAGE)
    if session is not None and (retired := retired_session_runtime_message(session)):
        raise HTTPException(410, retired)
    diagnostic = await asyncio.to_thread(runtime_diagnostic, backend)
    if (
        backend == BACKEND_CLAUDE
        and diagnostic.get("installed") is True
        and diagnostic.get("status") in {"unknown", "ready", "unauthenticated"}
    ):
        # An unknown or previously rejected login must not prevent the native
        # runtime from renewing it, or noticing an external login on retry.
        return diagnostic
    if diagnostic.get("status") == "ready":
        if diagnostic.get("status") == "ready" and diagnostic.get(
            "_executable"
        ):
            return diagnostic
        return diagnostic
    raise HTTPException(status_code=503, detail={
        "code": "runtime_unavailable",
        "backend": backend,
        "status": diagnostic.get("status") or "unknown",
        "message": diagnostic.get("message") or f"{runtime_display_name(backend)} is unavailable.",
        "action": diagnostic.get("action"),
    })


def runtime_priority(model: dict[str, Any]) -> int:
    try:
        return int(model["priority"])
    except Exception:
        return 9999


def discover_codex_catalog() -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    model_options: list[dict[str, Any]] = []
    effort_options: list[dict[str, Any]] = []
    model_efforts: dict[str, list[dict[str, Any]]] = {}
    default_model = ""
    default_model_label = ""
    default_effort = ""
    default_effort_label = ""
    default_service_tier = ""
    model_source = "codex debug models"
    effort_source = "codex debug models"
    configured_model, configured_effort, configured_service_tier = codex_user_config_defaults()
    try:
        payload = json.loads(run_catalog_command([CODEX_BIN, "debug", "models"]))
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(raw_models, list):
            models = [model for model in raw_models if isinstance(model, dict)]
    except Exception as exc:
        logger.warning("codex model discovery failed: %s", exc)
        model_source = f"{model_source} failed"
        effort_source = f"{effort_source} failed"

    replacements = codex_retired_model_replacements()
    visible_models = [
        model for model in models
        if str(model.get("visibility") or "list") == "list" and model.get("supported_in_api", True) is not False
        and str(model.get("slug") or model.get("id") or "").strip() not in replacements
    ]
    visible_models.sort(key=runtime_priority)
    if replacements and visible_models:
        # Use an available configured default, then the native default/priority.
        # The fallback is only needed when the CLI cannot return a catalog.
        slugs = [str(entry.get("slug") or entry.get("id") or "").strip() for entry in visible_models]
        preferred = next((slug for slug in (configured_model, CODEX_DEFAULT_MODEL) if slug in slugs), None)
        if preferred is None:
            preferred = next((str(entry.get("slug") or entry.get("id")) for entry in visible_models if entry.get("is_default") is True or entry.get("isDefault") is True), slugs[0])
        if preferred:
            with RUNTIME_DIAGNOSTICS_LOCK:
                PROVIDERS.codex_model_migration_target = preferred
            replacements = {model: preferred for model in replacements}
    # The selectable "Server default" must describe the exact settings that
    # codex_runtime_settings() launches for a chat with no explicit override.
    # The CLI's discovered list can omit a configured/custom default (or a
    # legacy fallback), so the first discovered model is not a safe proxy.
    default_model = replacements.get(configured_model or CODEX_DEFAULT_MODEL, configured_model or CODEX_DEFAULT_MODEL)
    default_effort = clamp_codex_runtime_effort(
        default_model,
        configured_effort or CODEX_DEFAULT_EFFORT,
    )
    default_service_tier = (
        configured_service_tier or codex_default_service_tier(default_model)
    )
    default_entry = next(
        (
            model
            for model in visible_models
            if str(model.get("slug") or model.get("id") or "").strip()
            == default_model
        ),
        None,
    )
    default_model_label = str(
        (default_entry or {}).get("display_name")
        or title_model_label(default_model)
    ).strip()
    default_effort_label = (
        title_effort_label(default_effort) if default_effort else ""
    )
    for model in visible_models:
        slug = str(model.get("slug") or model.get("id") or "").strip()
        if not slug:
            continue
        label = str(model.get("display_name") or title_model_label(slug)).strip()
        model_effort_options: list[dict[str, Any]] = []
        levels = model.get("supported_reasoning_levels")
        if isinstance(levels, list):
            for level in levels:
                if not isinstance(level, dict):
                    continue
                effort = str(level.get("effort") or "").strip()
                if effort:
                    option = runtime_option(effort, title_effort_label(effort))
                    effort_options.append(option)
                    model_effort_options.append(option)
        if model_effort_options:
            model_efforts[slug] = unique_runtime_options(model_effort_options, None)[1:]
        service_tier = str(model.get("default_service_tier") or "").strip()
        model_options.append(runtime_option(slug, label, efforts=model_efforts.get(slug, []), service_tier=service_tier or None))

    if not model_options:
        for slug, label in CODEX_FALLBACK_MODELS:
            if slug in replacements:
                continue
            fallback_efforts = [
                runtime_option(effort, title_effort_label(effort))
                for effort in CODEX_FALLBACK_MODEL_EFFORTS.get(slug, CODEX_FALLBACK_EFFORTS)
            ]
            model_efforts[slug] = fallback_efforts
            service_tier = codex_default_service_tier(slug)
            model_options.append(runtime_option(slug, label, efforts=fallback_efforts, service_tier=service_tier or None))
    if not effort_options:
        effort_options.extend(runtime_option(effort, title_effort_label(effort)) for effort in CODEX_FALLBACK_EFFORTS)
    if configured_model and configured_model not in replacements and not any(option.get("value") == configured_model for option in model_options):
        model_options.insert(0, runtime_option(configured_model, title_model_label(configured_model)))
    if configured_effort and not any(option.get("value") == configured_effort for option in effort_options):
        effort_options.append(runtime_option(configured_effort, title_effort_label(configured_effort)))
    if default_model not in model_efforts:
        supported_default_efforts = CODEX_FALLBACK_MODEL_EFFORTS.get(
            default_model
        )
        if supported_default_efforts:
            model_efforts[default_model] = [
                runtime_option(effort, title_effort_label(effort))
                for effort in supported_default_efforts
            ]

    return {
        "models": unique_runtime_options(model_options, f"Server default ({default_model_label})" if default_model_label else None),
        "efforts": unique_runtime_options(effort_options, f"Server default ({default_effort_label})" if default_effort_label else None),
        "model_efforts": model_efforts,
        "model_source": model_source,
        "effort_source": effort_source,
        "default_model": default_model or None,
        "default_effort": default_effort or None,
        "default_service_tier": default_service_tier or None,
        "model_replacements": replacements,
    }


def discover_runtime_catalog(*, force_runtime_probe: bool = False) -> dict[str, Any]:
    deadline = time.monotonic() + RUNTIME_CATALOG_BUDGET_SECONDS
    existing_deadline = RUNTIME_CATALOG_DEADLINE.get()
    token = RUNTIME_CATALOG_DEADLINE.set(
        min(deadline, existing_deadline) if existing_deadline is not None else deadline
    )
    try:
        return discover_runtime_catalog_within_budget(force_runtime_probe=force_runtime_probe)
    finally:
        RUNTIME_CATALOG_DEADLINE.reset(token)


def discover_runtime_catalog_within_budget(*, force_runtime_probe: bool = False) -> dict[str, Any]:
    # Run each provider's entire pipeline independently: waiting for all
    # diagnostics first could starve a healthy provider's model discovery.
    # Each worker inherits the same deadline in its own context. The bounded
    # subprocess calls kill/reap children before the pool joins its workers.
    backends = sorted(VALID_BACKENDS)
    with ThreadPoolExecutor(max_workers=len(backends), thread_name_prefix="runtime-probe") as pool:
        pending = {
            backend: pool.submit(
                copy_context().run, discover_runtime_backend_catalog, backend,
                force_runtime_probe=force_runtime_probe,
            )
            for backend in backends
        }
        catalogs = {backend: future.result() for backend, future in pending.items()}
    return {"generated_at": now_iso(), "backends": catalogs}


def discover_runtime_backend_catalog(backend: str, *, force_runtime_probe: bool = False) -> dict[str, Any]:
    diagnostic = runtime_diagnostic(backend, force=force_runtime_probe)
    ready = diagnostic.get("status") == "ready"
    if backend == BACKEND_CLAUDE:
        catalog = parse_claude_help_catalog()
    elif backend == BACKEND_CODEX:
        catalog = discover_codex_catalog()
    else:
        raise HTTPException(400, "Unsupported provider runtime.")
    catalog["diagnostic"] = public_runtime_diagnostic(diagnostic)
    catalog["available"] = ready
    return catalog


def provider_command_support(
    available: bool,
    mode: str,
    reason: str | None = None,
) -> dict[str, Any]:
    support: dict[str, Any] = {
        "available": bool(available),
        "mode": str(mode),
    }
    if reason:
        support["reason"] = str(reason)
    return support


def provider_command_snapshot(
    inventory: ProviderCommandInventory,
    support: dict[str, Any],
) -> dict[str, Any]:
    return {
        "backend": inventory.backend,
        "revision": inventory.revision,
        "support": support,
        "commands": inventory.commands,
    }


def provider_command_prompt_matches(prompt: str, invocation: str) -> bool:
    """Require an exact byte-zero slash token and ordinary argument boundary."""

    return prompt == invocation or (
        prompt.startswith(invocation)
        and len(prompt) > len(invocation)
        and prompt[len(invocation)] in {" ", "\t", "\r", "\n"}
    )


def provider_command_invocation_token(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = re.match(
        r"^/[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}(?=$|[ \t\r\n])",
        prompt,
    )
    return match.group(0) if match is not None else None


def redacted_provider_argv(cmd: list[str], backend: str) -> list[str]:
    """Keep generated provider context and user prompts out of stored diagnostics."""
    redacted = list(cmd)
    if backend == BACKEND_CLAUDE:
        with suppress(ValueError, IndexError):
            prompt_index = redacted.index("--append-system-prompt") + 1
            redacted[prompt_index] = "<system-prompt>"
        return redacted
    if backend == BACKEND_CODEX:
        redacted = [
            'developer_instructions="<developer-instructions>"'
            if value.startswith("developer_instructions=")
            else value
            for value in redacted
        ]
        if redacted:
            redacted[-1] = "<prompt>"
        return redacted
    return redacted


async def persist_run_provider_session(
    session_id: str,
    run_id: str,
    backend: str,
    provider_id: str,
    *,
    cwd: str | None = None,
    codex_instruction_hash: str | None = None,
    standalone_provider_context: bool = False,
    emit_event: bool = True,
) -> bool:
    """Bind a provider thread only for chat-context runs.

    Standalone scheduled runs project their output into the parent timeline,
    but their provider identity must never replace the parent's resumable chat.
    """

    if standalone_provider_context or not provider_id:
        return False
    # Keep the exact owner stable through the durable provider binding. Stop
    # waits for provider cleanup while holding the lifecycle lock, so this
    # deliberately uses the established ACTIVE -> STORE lock order instead of
    # trying to acquire the lifecycle lock from a runner.
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        current_turn = RUNS.current_turns.get(session_id)
        active_run_id = (
            str(active.get("run_id") or "")
            if isinstance(active, dict)
            else ""
        )
        current_run_id = (
            str(current_turn.get("run_id") or "")
            if isinstance(current_turn, dict)
            else ""
        )
        if (
            session_id not in RUNS.busy
            or run_id not in {active_run_id, current_run_id}
            or any(
                owner_id not in {"", run_id}
                for owner_id in (active_run_id, current_run_id)
            )
        ):
            return False
        save_kwargs: dict[str, Any] = {}
        if cwd is not None:
            save_kwargs["cwd"] = cwd
        if codex_instruction_hash is not None:
            save_kwargs["codex_instruction_hash"] = codex_instruction_hash
        usage_signal = await STORE.save_provider_session(
            session_id,
            provider_id,
            backend,
            defer_runtime_broadcast=True,
            **save_kwargs,
        )
    # Do not hold the global ACTIVE lock through event disk I/O/broadcast. A
    # second exact-owner check prevents a delayed old trace from appearing
    # after a replacement has bound.
    still_owned = await turn_slot_is_owned(session_id, run_id)
    if isinstance(usage_signal, dict) and still_owned:
        await broadcast_provider_runtime_changed(session_id, usage_signal)
    if emit_event and still_owned:
        await append_event(session_id, "provider_session", {
            "run_id": run_id,
            "backend": backend,
            "provider_session_id": provider_id,
        })
    return True


PROVIDER_CHILDREN_LOCK = threading.Lock()


def _provider_child_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def read_provider_children_registry() -> list[dict[str, Any]]:
    """Return the registered provider children, dropping malformed entries."""

    try:
        value = json.loads(PROVIDER_CHILDREN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    raw_children = value.get("children") if isinstance(value, dict) else None
    children: list[dict[str, Any]] = []
    for entry in raw_children if isinstance(raw_children, list) else []:
        if not isinstance(entry, dict):
            continue
        pid = _provider_child_int(entry.get("pid"))
        pgid = _provider_child_int(entry.get("pgid"))
        if pid is None or pgid is None:
            continue
        children.append({
            "pid": pid,
            "pgid": pgid,
            "kind": str(entry.get("kind") or "")[:64],
            "started_at": str(entry.get("started_at") or "")[:64],
            "owner_pid": _provider_child_int(entry.get("owner_pid")),
            "boot_identity": str(entry.get("boot_identity") or "")[:256],
            "process_start_identity": str(
                entry.get("process_start_identity") or ""
            )[:256],
            "executable_identity": str(
                entry.get("executable_identity") or ""
            )[:1024],
            "command_fingerprint": str(
                entry.get("command_fingerprint") or ""
            )[:64],
        })
    return children


def write_provider_children_registry(children: list[dict[str, Any]]) -> None:
    atomic_json(PROVIDER_CHILDREN_FILE, {"children": list(children)})


def provider_host_boot_identity() -> str:
    """Return a boot-scoped host identity, or empty when it cannot be proved."""

    boot_id = PROVIDER_CHILD_PROC_ROOT / "sys" / "kernel" / "random" / "boot_id"
    with suppress(OSError):
        value = boot_id.read_text().strip()
        if value:
            return f"linux:{value[:200]}"
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=PROVIDER_CHILD_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            return ""
        value = " ".join(str(completed.stdout or "").split())
        if value:
            return f"darwin:{value[:220]}"
    return ""


def provider_child_process_start_identity(pid: int) -> str:
    """Return a PID-reuse-resistant process start marker where supported."""

    proc_stat = PROVIDER_CHILD_PROC_ROOT / str(pid) / "stat"
    if proc_stat.exists():
        with suppress(OSError, ValueError, IndexError):
            text = proc_stat.read_text()
            fields = text[text.rindex(")") + 2 :].split()
            # /proc/<pid>/stat field 22, relative to this post-comm slice.
            start_ticks = int(fields[19])
            return f"linux-ticks:{start_ticks}"
    value = " ".join(_provider_child_ps(pid, "lstart").split())
    return f"ps-lstart:{value}" if value else ""


def provider_child_executable_identity(pid: int) -> str:
    """Return the current executable identity without trusting argv text."""

    proc_exe = PROVIDER_CHILD_PROC_ROOT / str(pid) / "exe"
    with suppress(OSError):
        target = os.readlink(proc_exe)
        if target:
            return f"proc-exe:{target[:960]}"
    value = _provider_child_ps(pid, "comm")
    return f"ps-comm:{value[:960]}" if value else ""


def provider_child_command_fingerprint(command: str) -> str:
    return hashlib.sha256(str(command).encode("utf-8", "replace")).hexdigest()


def provider_child_registry_entry_is_owned(entry: dict[str, Any]) -> bool:
    """Prove that a durable row still names the exact registered process.

    A numeric PID/PGID and a Codex-looking argv are not durable ownership:
    both identifiers can be reused after a reboot or process exit. Signal a
    group only while its original leader is live and every recorded identity
    remains exact. A dead leader is deliberately unprovable even if a process
    later appears with the same group number.
    """

    pid = _provider_child_int(entry.get("pid"))
    pgid = _provider_child_int(entry.get("pgid"))
    recorded_boot = str(entry.get("boot_identity") or "")
    recorded_start = str(entry.get("process_start_identity") or "")
    recorded_executable = str(entry.get("executable_identity") or "")
    recorded_command = str(entry.get("command_fingerprint") or "")
    if (
        pid is None
        or pgid is None
        or not recorded_boot
        or not recorded_start
        or not recorded_executable
        or re.fullmatch(r"[0-9a-f]{64}", recorded_command) is None
    ):
        return False
    try:
        if pgid == os.getpgrp() or not provider_child_process_exists(pid):
            return False
    except (AttributeError, OSError):
        return False
    current_boot = provider_host_boot_identity()
    if not current_boot or not hmac.compare_digest(recorded_boot, current_boot):
        return False
    if process_group_for_pid(pid) != pgid:
        return False
    current_start = provider_child_process_start_identity(pid)
    current_executable = provider_child_executable_identity(pid)
    current_command = provider_child_command_line(pid)
    if "codex" not in current_command.lower():
        return False
    return (
        bool(current_start)
        and bool(current_executable)
        and hmac.compare_digest(recorded_start, current_start)
        and hmac.compare_digest(recorded_executable, current_executable)
        and hmac.compare_digest(
            recorded_command,
            provider_child_command_fingerprint(current_command),
        )
    )


def register_provider_child(
    pid: int,
    pgid: int | None,
    *,
    kind: str = "codex-app-server",
) -> None:
    """Record a provider child that lives in its own process group."""

    if _provider_child_int(pid) is None or _provider_child_int(pgid) is None:
        # Without a provably owned process group there is nothing safe to
        # signal later; never record a bare pid as a kill target.
        return
    assert pgid is not None
    boot_identity = provider_host_boot_identity()
    process_start_identity = provider_child_process_start_identity(pid)
    executable_identity = provider_child_executable_identity(pid)
    command = provider_child_command_line(pid)
    try:
        group_is_exact = (
            process_group_for_pid(pid) == pgid
            and pgid != os.getpgrp()
        )
    except (AttributeError, OSError):
        group_is_exact = False
    if (
        not group_is_exact
        or not boot_identity
        or not process_start_identity
        or not executable_identity
        or "codex" not in command.lower()
    ):
        # Persisting an unverifiable kill target is worse than losing best-
        # effort orphan cleanup. The live client still owns normal teardown.
        return
    with PROVIDER_CHILDREN_LOCK:
        children = [
            entry
            for entry in read_provider_children_registry()
            if entry["pid"] != pid
        ]
        children.append({
            "pid": int(pid),
            "pgid": int(pgid),
            "kind": kind,
            "started_at": utc_timestamp(),
            "owner_pid": os.getpid(),
            "boot_identity": boot_identity,
            "process_start_identity": process_start_identity,
            "executable_identity": executable_identity,
            "command_fingerprint": provider_child_command_fingerprint(command),
        })
        write_provider_children_registry(children)


def unregister_provider_child(pid: int) -> None:
    with PROVIDER_CHILDREN_LOCK:
        children = read_provider_children_registry()
        remaining = [entry for entry in children if entry["pid"] != pid]
        if len(remaining) != len(children) or not PROVIDER_CHILDREN_FILE.exists():
            write_provider_children_registry(remaining)


def provider_child_process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def provider_child_command_line(pid: int) -> str:
    """Best-effort command line of ``pid`` (empty when unknown)."""

    proc_cmdline = PROVIDER_CHILD_PROC_ROOT / str(pid) / "cmdline"
    if proc_cmdline.exists():
        with suppress(OSError):
            raw = proc_cmdline.read_bytes()
            return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return _provider_child_ps(pid, "command")


def kill_registered_provider_children() -> int:
    """SIGKILL registered groups whose durable leader identity is still exact.

    Used by the hard-kill watchdog right before it kills this server: provider
    children run in their own session, so killing the server alone would
    orphan them (a multi-GB Codex app-server kept running after one incident).
    Every probe is best-effort and nothing here may raise.
    """

    killed = 0
    for entry in read_provider_children_registry():
        pgid = entry["pgid"]
        with suppress(Exception):
            if not provider_child_registry_entry_is_owned(entry):
                continue
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
                killed += 1
    return killed


def sweep_orphaned_provider_children() -> int:
    """Reap provider children the previous server instance left behind.

    An entry whose leader is alive, has been reparented to pid 1, and is still
    a Codex process belongs to nobody: kill its group. The registry is then
    rewritten with only live entries this process owns (normally none).
    """

    reaped = 0
    surviving: list[dict[str, Any]] = []
    own_pid = os.getpid()
    with PROVIDER_CHILDREN_LOCK:
        children = read_provider_children_registry()
        for entry in children:
            pid = entry["pid"]
            pgid = entry["pgid"]
            try:
                if not provider_child_registry_entry_is_owned(entry):
                    # Legacy, dead, reboot-stale, and identity-mismatched rows
                    # are safe to drop but never safe to signal. In particular,
                    # a dead leader cannot prove a surviving numeric PGID still
                    # belongs to this server rather than a later process group.
                    continue
                parent_pid = provider_child_parent_pid(pid)
                if parent_pid == own_pid:
                    surviving.append(entry)
                    continue
                if parent_pid != 1:
                    # Unknown parent (probe failed) or a child still owned by
                    # another live server during an overlapping restart: keep
                    # the entry so the group can be reaped later, never drop
                    # a live child from the registry.
                    surviving.append(entry)
                    continue
                if "codex" not in provider_child_command_line(pid).lower():
                    continue
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
                reaped += 1
                logger.warning(
                    "reaped orphaned provider child kind=%s pid=%d pgid=%d",
                    entry.get("kind") or "unknown",
                    pid,
                    pgid,
                )
            except Exception as exc:
                logger.warning(
                    "provider child sweep skipped pid=%d error=%s",
                    pid,
                    concise_error_message(exc),
                )
        if children or PROVIDER_CHILDREN_FILE.exists():
            with suppress(Exception):
                write_provider_children_registry(surviving)
    return reaped


CLAUDE_BACKGROUND_WORK_CACHE_KEY: tuple[Any, ...] | None = None
CLAUDE_BACKGROUND_WORK_CACHE_LABELS: tuple[str, ...] = ()


async def prepare_provider_background_work_snapshot(
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Inspect provider work before admission locks and bind it to metadata."""

    global CLAUDE_BACKGROUND_WORK_CACHE_KEY
    global CLAUDE_BACKGROUND_WORK_CACHE_LABELS

    codex_snapshot = await prepare_codex_subagent_terminal_snapshot(
        timeout_seconds=timeout_seconds,
    )
    manager = PROVIDERS.claude_sdk_manager
    session_ids, unknown_labels = loaded_claude_background_session_state(manager)
    fingerprints_before = await asyncio.to_thread(
        claude_event_file_fingerprints,
        session_ids,
    )
    cache_key = (id(manager), session_ids, fingerprints_before)
    if CLAUDE_BACKGROUND_WORK_CACHE_KEY == cache_key:
        claude_labels = list(CLAUDE_BACKGROUND_WORK_CACHE_LABELS)
    else:
        scan_task = asyncio.create_task(
            asyncio.to_thread(
                claude_background_work_labels_from_events,
                session_ids,
            )
        )
        try:
            if timeout_seconds is None:
                claude_labels = await scan_task
            else:
                claude_labels = await asyncio.wait_for(
                    asyncio.shield(scan_task),
                    timeout=max(0.0, float(timeout_seconds)),
                )
        except (TimeoutError, asyncio.TimeoutError):
            scan_task.add_done_callback(
                lambda done: (
                    done.exception() if not done.cancelled() else None
                )
            )
            return {
                "codex": codex_snapshot,
                "manager": manager,
                "session_ids": session_ids,
                "unknown_labels": unknown_labels,
                "fingerprints": fingerprints_before,
                "claude_labels": (),
                "consistent": False,
                "error": "timeout",
            }
        except Exception:
            return {
                "codex": codex_snapshot,
                "manager": manager,
                "session_ids": session_ids,
                "unknown_labels": unknown_labels,
                "fingerprints": fingerprints_before,
                "claude_labels": (),
                "consistent": False,
                "error": "scan_failed",
            }

    fingerprints_after = await asyncio.to_thread(
        claude_event_file_fingerprints,
        session_ids,
    )
    consistent = fingerprints_before == fingerprints_after
    if consistent:
        CLAUDE_BACKGROUND_WORK_CACHE_KEY = cache_key
        CLAUDE_BACKGROUND_WORK_CACHE_LABELS = tuple(claude_labels)
    return {
        "codex": codex_snapshot,
        "manager": manager,
        "session_ids": session_ids,
        "unknown_labels": unknown_labels,
        "fingerprints": fingerprints_after,
        "claude_labels": tuple(claude_labels),
        "consistent": consistent,
        "error": None if consistent else "events_changed",
    }


def interactive_supported_backends() -> list[str]:
    supported: list[str] = []
    if CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC:
        supported.append(BACKEND_CODEX)
    if (
        CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
        and claude_sdk_dependency_available()
    ):
        supported.append(BACKEND_CLAUDE)
    return supported


def interactive_backend_supported(backend: str) -> bool:
    return str(backend or DEFAULT_BACKEND).strip().lower() in set(
        interactive_supported_backends()
    )
