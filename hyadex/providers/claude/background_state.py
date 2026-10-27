"""Claude background work: task receipts and reconciliation state."""
from __future__ import annotations

from typing import Any
import uuid

from hyadex.providers.claude.background_reconciliation import (
    CONSUMED_EVENT as CLAUDE_BACKGROUND_CONSUMED_EVENT,
    normalize_task_receipts,
    pending_reconciliation_state,
    RECONCILED_EVENT as CLAUDE_BACKGROUND_RECONCILED_EVENT,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
)
from hyadex.core.config import BACKEND_CLAUDE, DEFAULT_BACKEND
from hyadex.core.paths import events_path
from hyadex.core.runs import RUNS
from hyadex.core.sessions import append_event, STORE
from hyadex.providers.claude.subagents import build_claude_subagent_snapshot


async def persist_claude_background_task_receipts(
    session_id: str,
    run_id: str,
    provider_id: str,
    handle: Any,
    *,
    tracking_lost: bool = False,
) -> dict[str, Any] | None:
    """Seal exact provider task facts before handing off their logical owner."""
    tasks = normalize_task_receipts(
        getattr(handle, "background_task_receipts", ()),
        run_id=run_id,
        provider_session_id=provider_id,
        tracking_lost=tracking_lost,
    )
    overflow = getattr(handle, "background_task_overflow_count", 0)
    overflow = min(overflow, 1_000_000) if type(overflow) is int and overflow > 0 else 0
    if not provider_id or (not tasks and not overflow):
        return None
    batch = {
        "reconciliation_id": f"reconcile_{uuid.uuid4().hex}",
        "tasks": tasks,
        "overflow_count": overflow,
    }
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if session is None:
            return None
        previous = session.get(CLAUDE_BACKGROUND_SESSION_FIELD)
        pending = pending_reconciliation_state(previous, batch, provider_session_id=provider_id)
        session[CLAUDE_BACKGROUND_SESSION_FIELD] = pending
        # Observed task facts are not rollbackable actions. Keep this exact
        # checkpoint on failure; cancelled STORE.save has already committed.
        await STORE.save(durable=True)
    stored = await append_event(session_id, CLAUDE_BACKGROUND_RECONCILED_EVENT, {
        "run_id": run_id, "backend": BACKEND_CLAUDE,
        "provider_session_id": provider_id, **batch,
    })
    return None if stored.get("discarded") else pending


async def acknowledge_claude_background_reconciliation(
    session_id: str,
    run_id: str,
    provider_id: str,
    handle: Any,
    batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Query acceptance is not delivery: only the bound context hook consumes."""
    if not batches or getattr(handle, "background_task_reconciliation_consumed", False) is not True:
        return batches
    identifiers = [batch["reconciliation_id"] for batch in batches]
    stored = await append_event(session_id, CLAUDE_BACKGROUND_CONSUMED_EVENT, {
        "run_id": run_id, "backend": BACKEND_CLAUDE,
        "provider_session_id": provider_id,
        "reconciliation_ids": identifiers,
    })
    if stored.get("discarded"):
        return batches
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        pending = session.get(CLAUDE_BACKGROUND_SESSION_FIELD) if session is not None else None
        if isinstance(pending, dict) and pending.get("provider_session_id") == provider_id and pending.get("reconciliation_id") in identifiers:
            session.pop(CLAUDE_BACKGROUND_SESSION_FIELD, None)
            try:
                await STORE.save(durable=True)
            except Exception:
                # A failed write retains pending facts for a later retry.
                # CancelledError is different: STORE.save already committed
                # its covering snapshot, so the clear must remain in memory.
                session[CLAUDE_BACKGROUND_SESSION_FIELD] = pending
                raise
    return []


PROVIDER_WORK_LABEL_LIMIT = 32
MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES = 64 * 1024 * 1024
CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL = (
    "Claude provider state exceeds safe inspection limit"
)


def loaded_claude_background_session_state(
    manager: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Capture bounded loaded Claude ownership without missing overflow work.

    Event folding is intentionally capped, but the cap must not make a loaded
    supervisor beyond the first page invisible. Once more loaded sessions
    exist than can be inspected, retain a conservative blocker instead of
    treating the uninspected suffix as idle.
    """

    if manager is None:
        return (), ()
    loaded_session_ids: list[str] = []
    unknown_labels: list[str] = []
    for session_id, session in tuple(STORE.sessions.items()):
        if (
            str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
            != BACKEND_CLAUDE
            or session_id in RUNS.busy
            or session_id in RUNS.maintenance
        ):
            continue
        try:
            loaded = manager.is_loaded(session_id)
        except Exception:
            if len(unknown_labels) < PROVIDER_WORK_LABEL_LIMIT:
                unknown_labels.append(
                    f"Claude provider state unknown in {session_id}"
                )
            continue
        if loaded:
            if len(loaded_session_ids) < PROVIDER_WORK_LABEL_LIMIT:
                loaded_session_ids.append(session_id)
                continue
            # Scanning arbitrary numbers of histories makes restart/update
            # admission unbounded. One extra loaded supervisor is sufficient
            # proof that the capped scan cannot establish an idle state.
            if len(unknown_labels) < PROVIDER_WORK_LABEL_LIMIT:
                unknown_labels.append(
                    CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL
                )
            elif unknown_labels:
                # Existing unknown labels already fail closed, but use a
                # stable overflow marker so revalidation records why the
                # enumeration stopped.
                unknown_labels[-1] = CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL
            break
    return tuple(loaded_session_ids), tuple(unknown_labels)


def claude_background_work_labels_from_events(
    session_ids: tuple[str, ...],
) -> list[str]:
    """Inspect bounded durable Claude child state off the asyncio thread."""

    labels: list[str] = []
    for session_id in session_ids:
        try:
            event_bytes = events_path(session_id).stat().st_size
        except FileNotFoundError:
            event_bytes = 0
        except OSError:
            labels.append(f"Claude provider state unknown in {session_id}")
            continue
        if event_bytes > MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES:
            # Never make restart/update admission parse an arbitrarily large
            # transcript. Failing closed is safe and the operator can Stop the
            # loaded chat (which evicts its provider supervisor) before retry.
            labels.append(f"Claude provider state too large in {session_id}")
            continue
        try:
            snapshot = build_claude_subagent_snapshot(session_id, limit=1)
            active_count = max(0, int(snapshot.get("active_count") or 0))
        except Exception:
            # A loaded provider process whose durable child state cannot be
            # inspected is not proof that it is safe to terminate.
            active_count = 1
        if active_count:
            labels.append(f"Claude background work in {session_id}")
    return labels
