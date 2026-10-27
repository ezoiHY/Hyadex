"""Chat settings requests and validation: backend, model effort, permissions, job access."""
from __future__ import annotations

import logging.handlers
import os
from pathlib import Path
import shlex
import threading
import time
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import codex_auth
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_DEFAULT_PERMISSION_MODE,
    CLAUDE_PERMISSION_MODES,
    CODEX_DEFAULT_EFFORT,
    CODEX_DEFAULT_MODEL,
    CODEX_EFFORT_ALIASES,
    CODEX_EFFORT_ORDER,
    CODEX_EFFORTS,
    CODEX_FALLBACK_MODEL_EFFORTS,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    PROVIDER_JOBS_ACCESS_DEFAULT,
    PROVIDER_JOBS_ACCESS_MODE_SET,
    RUNTIME_DIAGNOSTIC_TTL_SECONDS,
    VALID_BACKENDS,
)
from hyadex.core.paths import existing_cwd
from hyadex.core.runs import RUNS
from hyadex.core.session_codex import codex_app_server_env, codex_default_service_tier
from hyadex.providers.instructions import MAX_SESSION_SYSTEM_PROMPT_CHARS
from hyadex.providers.state import PROVIDERS

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 agent hosts
    tomllib = None

logger = logging.getLogger("hyadex-server")


class CreateSessionRequest(BaseModel):
    title: str | None = None
    auto_title_enabled: bool | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    subagent_limit: int | None = Field(default=None, strict=True, ge=1)
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None
    provider_session_id: str | None = None
    session_id: str | None = None
    claude_session_id: str | None = None
    codex_thread_id: str | None = None
    claude_permission_mode: Literal[
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
        "dontAsk",
        "auto",
    ] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    codex_permission_profile: str | None = Field(default=None, max_length=240)
    codex_approvals_reviewer: Literal["user", "auto_review", "guardian_subagent"] | None = None
    provider_jobs_access: Literal["full", "read_only", "blocked"] | None = None
    import_history: bool | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    auto_title_enabled: bool | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    subagent_limit: int | None = Field(default=None, strict=True, ge=1)
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None
    claude_permission_mode: Literal[
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
        "dontAsk",
        "auto",
    ] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    codex_permission_profile: str | None = Field(default=None, max_length=240)
    codex_approvals_reviewer: Literal["user", "auto_review", "guardian_subagent"] | None = None
    provider_jobs_access: Literal["full", "read_only", "blocked"] | None = None


class ReadSessionRequest(BaseModel):
    last_read_agent_event_seq: int | None = None


def normalize_runtime_effort(backend: str, effort: Any, *, strict: bool = False) -> str | None:
    clean = str(effort or "").strip().lower()
    if not clean:
        return None
    if str(backend or "").strip().lower() != BACKEND_CODEX:
        return clean

    normalized = CODEX_EFFORT_ALIASES.get(clean, clean)
    if normalized in CODEX_EFFORTS:
        return normalized
    if strict:
        supported = ", ".join(sorted(CODEX_EFFORTS))
        raise HTTPException(status_code=400, detail=f"Codex effort must be one of: {supported}")
    logger.warning("dropping unsupported Codex effort value=%r", effort)
    return None


def normalize_runtime_effort_for_model(
    backend: str,
    model: Any,
    effort: Any,
    *,
    strict: bool = False,
) -> str | None:
    """Normalize an effort and reject known-incompatible Codex model pairs.

    Unknown/custom model ids remain provider-owned. For picker-visible Codex
    models, fail before launch rather than advertising a saved effort that the
    provider will ignore or reject later.
    """
    normalized = normalize_runtime_effort(backend, effort, strict=strict)
    if normalized is None or str(backend or "").strip().lower() != BACKEND_CODEX:
        return normalized

    clean_model = str(model or "").strip()
    if not clean_model:
        configured_model, _configured_effort, _configured_service_tier = (
            codex_user_config_defaults()
        )
        clean_model = configured_model or CODEX_DEFAULT_MODEL
    supported = CODEX_FALLBACK_MODEL_EFFORTS.get(clean_model)
    if supported is None or normalized in supported:
        return normalized
    if strict:
        choices = ", ".join(supported)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Codex model {clean_model} does not support effort {normalized}; "
                f"choose one of: {choices}"
            ),
        )
    logger.warning(
        "dropping unsupported Codex model/effort pair model=%s effort=%s",
        clean_model,
        normalized,
    )
    return None


def clamp_codex_runtime_effort(model: str, effort: Any) -> str:
    """Return a launch-safe effort without rewriting the saved preference.

    The Codex config file can outlive a model or can contain an effort copied
    from another model.  Known fallback models are clamped to their nearest
    supported level at launch. Unknown/discovered model ids remain owned by
    Codex because a static server table must not reject newer capabilities.
    """

    normalized = normalize_runtime_effort(BACKEND_CODEX, effort)
    supported = CODEX_FALLBACK_MODEL_EFFORTS.get(str(model or "").strip())
    if supported is None:
        return normalized or CODEX_DEFAULT_EFFORT
    if normalized in supported:
        return normalized

    if normalized in CODEX_EFFORT_ORDER:
        requested_index = CODEX_EFFORT_ORDER.index(normalized)
        ranked_supported = [
            (CODEX_EFFORT_ORDER.index(candidate), candidate)
            for candidate in supported
            if candidate in CODEX_EFFORT_ORDER
        ]
        lower_or_equal = [
            item for item in ranked_supported if item[0] <= requested_index
        ]
        if lower_or_equal:
            clamped = max(lower_or_equal)[1]
        elif ranked_supported:
            clamped = min(ranked_supported)[1]
        else:
            clamped = supported[0]
    else:
        default_effort = normalize_runtime_effort(
            BACKEND_CODEX,
            CODEX_DEFAULT_EFFORT,
        )
        clamped = (
            default_effort
            if default_effort in supported
            else supported[-1]
        )
    logger.warning(
        "clamping unsupported Codex runtime effort model=%s requested=%r effective=%s",
        model,
        effort,
        clamped,
    )
    return clamped


REMOVED_PROVIDER_MESSAGE = "This provider was removed. Its saved chat history remains readable."
REMOVED_ENDPOINT_MESSAGE = "Custom API endpoints were removed. This chat's saved history remains readable."


def retired_session_runtime_message(sess: dict[str, Any]) -> str | None:
    """Chats whose runtime no longer exists keep readable history but never run.

    A chat saved against a removed custom endpoint must not silently continue
    on the native login: that would send its history to another account.
    """
    if str(sess.get("backend") or DEFAULT_BACKEND).lower() not in VALID_BACKENDS:
        return REMOVED_PROVIDER_MESSAGE
    if "custom" in (sess.get("codex_provider"), sess.get("provider_connection")):
        return REMOVED_ENDPOINT_MESSAGE
    return None


def preview_session_runtime_update(
    sess: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Validate and preview provider runtime fields without mutating state."""

    current_backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    retired = retired_session_runtime_message(sess)
    if retired:
        if set(patch) - {"title", "folder", "pinned", "archived", "auto_title_enabled"}:
            raise HTTPException(410, retired)
        # Organizing saved history must not bind credentials, migrate the
        # provider or validate settings for a runtime that no longer exists.
        return {**sess, "model": sess.get("model"), "effort": sess.get("effort")}
    prospective_backend = (
        str(patch["backend"]).lower()
        if patch.get("backend") is not None
        else current_backend
    )
    if prospective_backend not in VALID_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"backend must be one of {sorted(VALID_BACKENDS)}",
        )
    backend_changed = prospective_backend != current_backend
    if backend_changed and session_backend_locked(sess):
        raise HTTPException(
            status_code=409,
            detail=(
                "backend is locked after the chat starts; fork or create "
                "a new chat to use another backend"
            ),
        )

    prospective_model = (
        str(patch.get("model") or "").strip() or None
        if "model" in patch
        else None if backend_changed else sess.get("model")
    )
    previous_model = prospective_model
    if prospective_backend == BACKEND_CODEX and prospective_model:
        prospective_model = codex_available_model(str(prospective_model))
    prospective_effort = (
        patch.get("effort")
        if "effort" in patch
        else None if backend_changed else sess.get("effort")
    )
    if prospective_model != previous_model and prospective_effort:
        # Saved/queued settings may contain a level unsupported by the new
        # model. Validate the vocabulary, then preserve the nearest level.
        prospective_effort = clamp_codex_runtime_effort(
            str(prospective_model),
            normalize_runtime_effort(BACKEND_CODEX, prospective_effort, strict="effort" in patch),
        )
    normalized_effort = normalize_runtime_effort_for_model(
        prospective_backend, prospective_model, prospective_effort, strict="effort" in patch,
    )

    preview = dict(sess)
    preview["backend"] = prospective_backend
    preview["model"] = prospective_model
    preview["effort"] = normalized_effort
    if backend_changed:
        provider_id_field = {
            BACKEND_CLAUDE: "claude_session_id",
            BACKEND_CODEX: "codex_thread_id",
        }[prospective_backend]
        preview["session_id"] = sess.get(provider_id_field)
    return preview


def effective_claude_permission_mode(sess: dict[str, Any]) -> str:
    """Return one canonical SDK permission mode for persisted/legacy chats."""

    value = str(
        sess.get("claude_permission_mode")
        or CLAUDE_DEFAULT_PERMISSION_MODE
    ).strip()
    return (
        value
        if value in CLAUDE_PERMISSION_MODES
        else CLAUDE_DEFAULT_PERMISSION_MODE
    )


def effective_provider_jobs_access(sess: dict[str, Any] | None) -> str:
    """Return the durable agent Jobs policy for a persisted or legacy chat."""

    value = str(
        (sess or {}).get("provider_jobs_access")
        or PROVIDER_JOBS_ACCESS_DEFAULT
    ).strip().lower()
    return (
        value
        if value in PROVIDER_JOBS_ACCESS_MODE_SET
        else PROVIDER_JOBS_ACCESS_DEFAULT
    )


RUNTIME_DIAGNOSTICS: dict[str, dict[str, Any]] = {}
RUNTIME_DIAGNOSTICS_LOCK = threading.RLock()


def session_backend_locked(sess: dict[str, Any]) -> bool:
    return bool(sess.get("backend_locked")) or any(
        str(sess.get(key) or "").strip()
        for key in (
            "session_id",
            "claude_session_id",
            "codex_thread_id",
            "cursor_session_id",
            "opencode_session_id",
            "gemini_session_id",
        )
    )


def codex_user_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"


def load_codex_user_config(path: Path) -> dict[str, Any]:
    if tomllib is not None:
        with path.open("rb") as f:
            return tomllib.load(f)

    # Python 3.10 has no stdlib TOML parser. Only these top-level strings are
    # needed here; nested Codex configuration remains owned by the CLI.
    wanted = {"model", "model_reasoning_effort", "service_tier", "developer_instructions"}
    payload: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        values = list(lexer)
        if values:
            payload[key] = values[0]
    return payload


def codex_user_config_defaults() -> tuple[str, str, str]:
    path = codex_user_config_path()
    try:
        payload = load_codex_user_config(path)
    except Exception as exc:
        logger.debug("codex config default discovery skipped path=%s: %s", path, exc)
        return "", "", ""
    model = str(payload.get("model") or "").strip()
    effort = str(payload.get("model_reasoning_effort") or "").strip()
    service_tier = str(payload.get("service_tier") or "").strip()
    return model, effort, service_tier


def codex_retired_model_replacements() -> dict[str, str]:
    """Scope retirement to native ChatGPT auth, never API/custom endpoints."""
    with RUNTIME_DIAGNOSTICS_LOCK:
        diagnostic = RUNTIME_DIAGNOSTICS.get(BACKEND_CODEX) or {}
        checked_at = diagnostic.get("_codex_auth_mode_checked_at_epoch", diagnostic.get("checked_at_epoch")) or 0
        observed = (str(diagnostic.get("_codex_auth_mode") or "")
                    if time.time() - checked_at < RUNTIME_DIAGNOSTIC_TTL_SECONDS else "")
        replacement = PROVIDERS.codex_model_migration_target
    mode = codex_auth.native_model_auth_mode(
        codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD), observed_mode=observed,
    )
    if mode != "chatgpt":
        return {}
    return {model: replacement for model in sorted(codex_auth.CHATGPT_RETIRED_MODELS)}


def codex_available_model(model: str) -> str:
    return codex_retired_model_replacements().get(model, model)


def codex_runtime_settings(sess: dict[str, Any]) -> tuple[str, str, str]:
    configured_model, configured_effort, configured_service_tier = codex_user_config_defaults()
    model = codex_available_model(str(sess.get("model") or configured_model or CODEX_DEFAULT_MODEL).strip())
    effort = clamp_codex_runtime_effort(
        model,
        sess.get("effort") or configured_effort or CODEX_DEFAULT_EFFORT,
    )
    service_tier = configured_service_tier or codex_default_service_tier(model)
    return model, effort, service_tier


def codex_goal_time_budget_remaining(sess: dict[str, Any]) -> float | None:
    goal = sess.get("codex_goal")
    limit = sess.get("codex_goal_time_budget_seconds")
    if (
        not isinstance(goal, dict)
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or limit <= 0
    ):
        return None
    if sess.get("codex_goal_time_budget_exhausted") is True:
        return 0.0
    used = goal.get("timeUsedSeconds")
    used_seconds = (
        float(used)
        if isinstance(used, (int, float)) and not isinstance(used, bool)
        else 0.0
    )
    if used_seconds >= float(limit):
        return 0.0
    if str(goal.get("status") or "") != "active":
        return None
    return max(0.0, float(limit) - used_seconds)


def codex_goal_time_budget_is_exhausted(sess: dict[str, Any]) -> bool:
    return codex_goal_time_budget_remaining(sess) == 0


async def ensure_claude_permission_mode_update_allowed(
    session_id: str,
    current: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    """Allow a validated mode save for the next turn.

    ``_start_turn_locked`` snapshots the session before scheduling provider
    work, and Claude's permission callback reads the captured ACTIVE value.
    The lifecycle lock serializes that snapshot with this PATCH, so whichever
    operation wins applies atomically to exactly the intended turn.
    """

    _ = session_id, current, patch


async def ensure_backend_update_allowed(
    session_id: str,
    current: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    """Fence real backend changes while provider work owns the session.

    ``start_turn`` registers its request task before it acquires the session
    lifecycle lock or reserves ``BUSY_SESSIONS``.  Checking the task registry
    closes that launch race, including a first turn with no provider identity
    yet, while still allowing idempotent saves from older clients.
    """

    if "backend" not in patch or patch.get("backend") is None:
        return
    current_backend = str(
        current.get("backend") or DEFAULT_BACKEND
    ).strip().lower()
    requested_backend = str(
        patch.get("backend") or current_backend
    ).strip().lower()
    if requested_backend == current_backend:
        return

    async with RUNS.active_lock:
        provider_starting = any(
            not task.done()
            for task in tuple(RUNS.turn_tasks.get(session_id) or ())
        )
        if (
            session_id in RUNS.busy
            or RUNS.active.get(session_id) is not None
            or provider_starting
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "wait for or stop the active turn before changing its "
                    "backend"
                ),
            )
