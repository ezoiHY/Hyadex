"""Codex threads: parameters, instructions, hygiene, resume and rollover."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import logging.handlers
from typing import Any

from hyadex.core.config import BACKEND_CODEX, CODEX_TRANSPORT_APP_SERVER, DEFAULT_BACKEND
from hyadex.core.paths import codex_manifest_path, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    register_session_task,
    session_prompt_addendum,
    STORE,
)
from hyadex.core.session_settings import codex_user_config_path, load_codex_user_config
from hyadex.core.session_codex import (
    codex_compaction_count,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    native_codex_compaction_terminal_aliases,
    session_codex_thread_id,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.providers.authority import terminal_session_name
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.codex.app_server import CodexAppServerError
from hyadex.providers.codex.history import (
    codex_hygiene_int_setting,
    CODEX_ROLLOUT_OVERSIZED_BYTES,
    codex_rollout_stats,
    CODEX_ROLLOUT_WARN_BYTES,
)
from hyadex.providers.instructions import CODEX_PROMPT_PRELUDE, CODEX_THREAD_POLICY_VERSION

logger = logging.getLogger("hyadex-server")

CODEX_APP_SERVER_THREAD_LRU: OrderedDict[str, float] = OrderedDict()
CODEX_APP_SERVER_PINNED_THREADS: set[str] = set()


CODEX_APP_SERVER_INVALIDATED_THREADS: set[str] = set()
CODEX_APP_SERVER_THREAD_LRU_LOCK = asyncio.Lock()


CODEX_COMPACTION_WARN_COUNT = codex_hygiene_int_setting(
    "CODEX_COMPACTION_WARN_COUNT", 100
)
CODEX_HYGIENE_TASKS: dict[str, set[asyncio.Task[Any]]] = {}


def session_references_codex_thread(
    session: dict[str, Any],
    thread_id: str,
) -> bool:
    """Return whether a chat owns a Codex identity, active or parked."""

    codex_thread_id = str(session.get("codex_thread_id") or "").strip()
    active_session_id = (
        str(session.get("session_id") or "").strip()
        if str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
        == BACKEND_CODEX
        else ""
    )
    return thread_id in {codex_thread_id, active_session_id}


async def recover_abandoned_codex_compactions_after_start(
    *,
    forced_restart_request_id: str | None = None,
) -> int:
    """Close compaction rows whose provider process vanished mid-operation.

    The bounded pending index lives in sessions.json, so startup does not scan
    large transcripts. A terminal marker uses the original lifecycle identity
    and therefore updates the existing row at its arrival position.
    """

    pending: list[tuple[str, dict[str, Any]]] = []
    for session_id, session in tuple(STORE.sessions.items()):
        value = session.get("_active_codex_compactions")
        if not isinstance(value, dict):
            continue
        for compaction in value.values():
            if isinstance(compaction, dict):
                pending.append((session_id, dict(compaction)))

    recovered = 0
    for session_id, compaction in pending:
        if session_id not in STORE.sessions:
            continue
        payload = {
            key: compaction.get(key)
            for key in (
                "compaction_id",
                "operation_id",
                "thread_id",
                "turn_id",
                "item_id",
                "run_id",
            )
            if compaction.get(key) is not None
        }
        payload.update({
            "status": "interrupted",
            "recovered_after_restart": True,
            "message": (
                "Context compaction was interrupted when HyadexServer restarted."
            ),
        })
        if forced_restart_request_id:
            payload.update({
                "forced_restart": True,
                "restart_request_id": forced_restart_request_id,
                "message": (
                    "Context compaction was forcibly interrupted when "
                    "HyadexServer restarted."
                ),
            })
        try:
            await append_event(
                session_id,
                "codex_compaction_completed",
                payload,
            )
        except Exception as exc:
            logger.warning(
                "could not close abandoned Codex compaction session=%s key=%s error=%s",
                session_id,
                compaction.get("lifecycle_key") or "unknown",
                concise_error_message(exc),
            )
        else:
            recovered += 1
    if recovered:
        logger.warning(
            "closed abandoned Codex compactions after restart count=%d",
            recovered,
        )
    return recovered


def codex_native_compaction_was_terminal(
    session_id: str,
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> bool:
    """Return whether this provider compaction already reached a terminal row.

    A notification that carries an item id is matched only by its exact
    identity or its thread+item alias. The thread+turn alias remembered for an
    earlier compaction is deliberately ignored here, otherwise the second and
    later automatic compactions of one turn would be treated as replays and
    never mirrored into the timeline.
    """

    aliases = set(native_codex_compaction_terminal_aliases(
        thread_id,
        turn_id,
        item_id,
    ))
    if not aliases:
        return False
    session = STORE.sessions.get(session_id) or {}
    for field in (
        "_codex_compaction_native_terminal_keys",
        "_codex_compaction_terminal_keys",
    ):
        values = session.get(field)
        if isinstance(values, list) and aliases.intersection(map(str, values)):
            return True
    return False


async def codex_thread_hygiene(session: dict[str, Any]) -> dict[str, Any]:
    """Describe how bloated the chat's current Codex thread has become."""

    thread_id = str(session_provider_id(session) or "").strip()
    stats: dict[str, Any] = {
        "rollout_path": None,
        "rollout_bytes": None,
        "rollout_mtime": None,
    }
    if thread_id and str(session.get("backend") or DEFAULT_BACKEND) == BACKEND_CODEX:
        try:
            stats = await asyncio.to_thread(codex_rollout_stats, thread_id)
        except Exception as exc:
            logger.debug(
                "codex rollout stats unavailable thread=%s: %s",
                thread_id,
                concise_error_message(exc),
            )
    return {
        **stats,
        "compaction_count": codex_compaction_count(session),
        "thread_started_at": session.get("codex_thread_started_at"),
    }


def codex_thread_hygiene_warning(hygiene: dict[str, Any]) -> str | None:
    """Return None, "large", or "oversized" for one hygiene snapshot."""

    rollout_bytes = hygiene.get("rollout_bytes")
    size = (
        int(rollout_bytes)
        if isinstance(rollout_bytes, int) and not isinstance(rollout_bytes, bool)
        else 0
    )
    if size >= CODEX_ROLLOUT_OVERSIZED_BYTES:
        return "oversized"
    count = hygiene.get("compaction_count")
    compactions = (
        int(count) if isinstance(count, int) and not isinstance(count, bool) else 0
    )
    if size >= CODEX_ROLLOUT_WARN_BYTES or compactions >= CODEX_COMPACTION_WARN_COUNT:
        return "large"
    return None


def codex_thread_hygiene_public(hygiene: dict[str, Any]) -> dict[str, Any]:
    """Attach the warning level and thresholds for runtime consumers."""

    return {
        **hygiene,
        "warning": codex_thread_hygiene_warning(hygiene),
        "thresholds": {
            "rollout_warn_bytes": CODEX_ROLLOUT_WARN_BYTES,
            "rollout_oversized_bytes": CODEX_ROLLOUT_OVERSIZED_BYTES,
            "compaction_warn_count": CODEX_COMPACTION_WARN_COUNT,
        },
    }


CODEX_HYGIENE_WARNING_RANK = {None: 0, "large": 1, "oversized": 2}


def format_byte_size(value: int | None) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "unknown size"
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f} GB"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.0f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} bytes"


def codex_thread_hygiene_message(
    warning: str,
    hygiene: dict[str, Any],
) -> str:
    size_text = format_byte_size(hygiene.get("rollout_bytes"))
    count = hygiene.get("compaction_count")
    compactions = (
        int(count) if isinstance(count, int) and not isinstance(count, bool) else 0
    )
    detail = f"its on-disk rollout is {size_text}"
    if compactions:
        detail += f" after {compactions} context compaction(s)"
    if warning == "oversized":
        return (
            f"This Codex thread is oversized: {detail}. Resuming it is slow and "
            "the app-server may stall or time out. Use \"Start fresh thread\" "
            "to rotate to a new thread with a handoff summary."
        )
    return (
        f"This Codex thread is getting large: {detail}. Long threads resume "
        "slowly and compaction gets less effective. Consider \"Start fresh "
        "thread\" to rotate to a new thread with a handoff summary."
    )


async def warn_codex_thread_hygiene(
    session_id: str,
    thread_id: str,
    run_id: str | None,
) -> None:
    """Append one visible warning the first time a thread crosses a level."""

    session = STORE.sessions.get(session_id)
    if not session or str(session_provider_id(session) or "") != thread_id:
        return
    hygiene = await codex_thread_hygiene(session)
    warning = codex_thread_hygiene_warning(hygiene)
    if warning is None:
        return
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if not current or str(session_provider_id(current) or "") != thread_id:
            return
        warned = current.get("_codex_hygiene_warned")
        warned_level = (
            str(warned.get("level") or "") or None
            if isinstance(warned, dict)
            and str(warned.get("thread_id") or "") == thread_id
            else None
        )
        if (
            CODEX_HYGIENE_WARNING_RANK.get(warning, 0)
            <= CODEX_HYGIENE_WARNING_RANK.get(warned_level, 0)
        ):
            return
        current["_codex_hygiene_warned"] = {
            "thread_id": thread_id,
            "level": warning,
            "warned_at": now_iso(),
        }
        await STORE.save()
    await append_event(
        session_id,
        "codex_thread_hygiene",
        {
            "run_id": run_id or None,
            "backend": BACKEND_CODEX,
            "thread_id": thread_id,
            "warning": warning,
            "rollout_path": hygiene.get("rollout_path"),
            "rollout_bytes": hygiene.get("rollout_bytes"),
            "compaction_count": hygiene.get("compaction_count"),
            "suggested_action": "codex_rotate",
            "message": codex_thread_hygiene_message(warning, hygiene),
        },
    )


def schedule_codex_thread_hygiene_check(
    session_id: str,
    thread_id: str,
    run_id: str | None,
) -> None:
    """Fire-and-forget hygiene check so a turn start is never delayed by it."""

    if not thread_id:
        return

    async def runner() -> None:
        try:
            await warn_codex_thread_hygiene(session_id, thread_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "codex thread hygiene check failed session=%s: %s",
                session_id,
                concise_error_message(exc),
            )

    register_session_task(
        CODEX_HYGIENE_TASKS,
        session_id,
        asyncio.create_task(runner()),
    )


def is_codex_compaction_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return "remote compact task" in lowered and "stream disconnected" in lowered


def codex_compaction_retry_mode(turns: Any, failed_turn_id: str, run_id: str) -> str | None:
    """Prove which input, if any, a same-thread compaction retry can use.

    An empty/summary snapshot is not evidence. Unknown items, a later turn,
    or any execution forbid recovery, even if no final reply was seen. When
    Codex persisted this exact user message before failing, continue with empty
    input: native turn/start supports that without adding another user item.
    """
    if not failed_turn_id or not isinstance(turns, list) or not turns:
        return None
    turn = turns[0]
    if not isinstance(turn, dict) or (
        turn.get("id") != failed_turn_id
        or turn.get("status") != "failed"
        or turn.get("itemsView") != "full"
    ):
        return None
    items = turn.get("items")
    if not isinstance(items, list) or not items or any(
        not isinstance(item, dict) or item.get("type") not in {"contextCompaction", "userMessage"}
        for item in items
    ):
        return None
    users = [item for item in items if item.get("type") == "userMessage"]
    if not users:
        return "resubmit"
    if len(users) == 1 and run_id and users[0].get("clientId") == run_id:
        return "continue"
    return None


def codex_compaction_error_message(model: str, native_error: str) -> str:
    # The native error may name the previous model even after native fallback.
    # Do not invent its hidden fallback error or infer retirement from a stream error.
    return (
        f"Codex context compaction failed with {model or 'the current model'} selected. "
        "The original conversation and history were kept. The provider error below "
        "may refer to an earlier model; it does not mean your selection was ignored. "
        "Any additional native fallback failure may not be exposed by Codex. "
        "Try Compact context in Codex Thread Controls, then send again. "
        f"Provider error: {native_error}"
    )


def is_codex_resume_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "no conversation found with session id",
            "conversation not found",
            "thread not found",
            "unknown thread",
            "thread does not exist",
            "thread is not loaded",
            "session not found",
            "failed to resume",
            "could not resume",
            "unable to resume",
        )
    )


def should_recover_codex_resume(
    *,
    allow_rollover: bool,
    resumed_provider_id: str | None,
    stopped: bool,
    stream_error: str | None,
    idle_killed: bool,
    resume_stalled: bool,
    returncode: int | None,
    produced_activity: bool,
    terminal_error: str,
) -> bool:
    if (
        not allow_rollover
        or not resumed_provider_id
        or stopped
        or stream_error
        or idle_killed
        or produced_activity
        # Compaction is not evidence of a missing thread. Never silently
        # replace original context with a bounded-memory fork on this error.
        or is_codex_compaction_failure(terminal_error)
    ):
        return False
    if resume_stalled or returncode in (0, None):
        return True
    return is_codex_resume_failure(terminal_error)


def codex_session_id_for_thread(thread_id: str) -> str | None:
    if not thread_id:
        return None
    with CODEX_SUBAGENT_INDEX_LOCK:
        child_session_id = CODEX_SUBAGENT_SESSION_INDEX.get(thread_id)
    if child_session_id:
        child_session = STORE.sessions.get(child_session_id)
        if (
            child_session is not None
            and child_session_id not in DELETING_SESSIONS
            and child_session_id not in DELETED_SESSION_TOMBSTONES
        ):
            if session_codex_thread_id(child_session) == thread_id:
                # Root ownership wins over a stale child receiver record.
                with CODEX_SUBAGENT_INDEX_LOCK:
                    CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
                    CODEX_SUBAGENT_STATE.pop(thread_id, None)
                    CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
                    CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
                CODEX_THREAD_SESSION_INDEX[thread_id] = child_session_id
            return child_session_id
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
            CODEX_SUBAGENT_STATE.pop(thread_id, None)
            CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
            CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
    indexed = CODEX_THREAD_SESSION_INDEX.get(thread_id)
    if indexed:
        session = STORE.sessions.get(indexed)
        if (
            session
            and indexed not in DELETING_SESSIONS
            and indexed not in DELETED_SESSION_TOMBSTONES
            and str(session_provider_id(session) or "") == thread_id
        ):
            return indexed
        CODEX_THREAD_SESSION_INDEX.pop(thread_id, None)
    for session_id, session in STORE.sessions.items():
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            continue
        if str(session_provider_id(session) or "") == thread_id:
            CODEX_THREAD_SESSION_INDEX[thread_id] = session_id
            return session_id
    return None


def codex_thread_status_message(status: dict[str, Any]) -> str:
    status_type = str(status.get("type") or "")
    if status_type == "active":
        flags = [
            str(flag)
            for flag in status.get("activeFlags") or []
            if str(flag or "")
        ]
        if "waitingOnApproval" in flags:
            return "Codex is waiting for approval."
        return "Codex is working."
    if status_type == "idle":
        return "Codex is idle."
    if status_type == "notLoaded":
        return "Codex thread is not loaded."
    if status_type == "systemError":
        detail = concise_error_message(
            status.get("error") or status.get("message") or ""
        )
        return (
            f"Codex thread error: {detail[:500]}"
            if detail and detail != "Unknown error"
            else "Codex thread encountered an error."
        )
    return "Codex thread status changed."


def invalidated_codex_thread_error(thread_id: str) -> CodexAppServerError:
    return CodexAppServerError(
        (
            "the cached Codex thread is being refreshed after a provider "
            f"account change ({thread_id})"
        ),
        request_sent=False,
        safe_to_retry=True,
    )


def codex_user_developer_instructions() -> str:
    try:
        payload = load_codex_user_config(codex_user_config_path())
    except Exception as exc:
        logger.debug("codex developer instruction discovery skipped: %s", exc)
        return ""
    return str(payload.get("developer_instructions") or "").strip()


def codex_thread_instructions(session_id: str, sess: dict[str, Any]) -> str:
    """Return stable thread-level Codex instructions, never turn/user content."""
    provider_context = CODEX_PROMPT_PRELUDE.format(
        manifest_path=str(codex_manifest_path(session_id)),
        terminal_session=terminal_session_name(session_id),
        chat_id=session_id,
    )
    user_developer_instructions = codex_user_developer_instructions()
    return "\n\n".join(
        value
        for value in (
            user_developer_instructions,
            provider_context.rstrip(),
            session_prompt_addendum(sess).strip(),
        )
        if value
    )


def codex_thread_instruction_hash(session_id: str, sess: dict[str, Any]) -> str:
    payload = (
        f"hyadex-policy-v{CODEX_THREAD_POLICY_VERSION}\0"
        f"{codex_thread_instructions(session_id, sess)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def codex_thread_has_local_run_owner(
    session_id: str,
    thread_id: str,
) -> bool:
    """Return whether Hyadex currently owns work on this exact thread.

    A native goal and a provider thread status are durable control state, not
    run ownership. Only the local BUSY/ACTIVE bookkeeping may authorize a
    provider continuation.
    """

    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        return bool(
            session_id in RUNS.busy
            and active
            and active.get("backend") == BACKEND_CODEX
            and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
            and str(
                active.get("provider_thread_id")
                or active.get("provider_session_id")
                or ""
            ).strip()
            == thread_id
        )
