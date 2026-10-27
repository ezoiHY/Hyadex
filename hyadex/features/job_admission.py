"""When a scheduled job may start: durable admissions and blockers."""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
    DEFAULT_BACKEND,
    JOB_MAX_ACTIVE_RUNS,
    JOB_MIN_AVAILABLE_MEM_MB,
)
from hyadex.core.host import host_pressure_snapshot
from hyadex.core.paths import events_path
from hyadex.core.restart_status import MANAGED_SERVER_RESTART_ACTIVE_DETAIL
from hyadex.core.runs import RUNS
from hyadex.core.session_alerts import reversed_jsonl_events
from hyadex.core.session_titles import (
    EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
    server_lifecycle_admission_blocker,
)
from hyadex.core.sessions import CLAUDE_STOP_FENCE_SESSIONS, STORE
from hyadex.core.util import low_available_memory_message, TransientAdmissionWait
from hyadex.features.side_questions import CODEX_GOALS_RECONFIGURING
from hyadex.providers.claude.runtime import stop_cleanup_in_progress
from hyadex.providers.codex.runtime import turn_start_blocker


def durable_scheduled_job_admissions(
    jobs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Find admitted occurrences that still exactly own persisted schedules.

    ``turn_started`` is the provider-admission boundary. A process can die
    after that fsynced event but before ``mark_ran`` advances the job file.
    Scan only for the exact current private revision and canonical occurrence;
    an edited/replaced schedule must remain untouched.
    """

    expected_by_session: dict[
        str,
        dict[str, tuple[str, float | None, bool]],
    ] = defaultdict(dict)
    for job_id, job in jobs.items():
        session_id = str(job.get("session_id") or "")
        revision = str(job.get("_revision") or "")
        manual_pending = bool(job.get("manual_run_pending"))
        scheduled_at = job.get("scheduled_run_at")
        if scheduled_at is None:
            scheduled_at = job.get("next_run_at")
        occurrence: float | None = None
        try:
            occurrence = float(scheduled_at)
        except (TypeError, ValueError):
            pass
        if occurrence is not None and not math.isfinite(occurrence):
            occurrence = None
        if (
            not session_id
            or not revision.startswith("job_rev_")
            or (occurrence is None and not manual_pending)
        ):
            continue
        expected_by_session[session_id][str(job_id)] = (
            revision,
            occurrence,
            manual_pending,
        )

    admitted: dict[str, dict[str, Any]] = {}
    for session_id in sorted(expected_by_session):
        pending = dict(expected_by_session[session_id])
        for event in reversed_jsonl_events(events_path(session_id)):
            if not pending:
                break
            if (
                event.get("type") != "turn_started"
                or event.get("purpose") != "scheduled_job"
            ):
                continue
            job_id = str(event.get("job_id") or "")
            expected = pending.get(job_id)
            if expected is None:
                continue
            revision, occurrence, manual_pending = expected
            if str(event.get("job_revision") or "") != revision:
                continue
            try:
                event_occurrence = float(event.get("job_scheduled_run_at"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(event_occurrence):
                continue
            event_is_manual = event.get("manual_run") is True
            if event_is_manual:
                if not manual_pending:
                    continue
            elif occurrence is None or event_occurrence != occurrence:
                continue
            admitted[job_id] = event
            pending.pop(job_id, None)
    return admitted


async def scheduled_job_blocker(
    session_id: str,
    *,
    manual: bool = False,
    group: bool = False,
) -> str | None:
    async with RUNS.active_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            return lifecycle_blocker
        # A group job only posts a message (busy members queue it), so only
        # the server-wide limits below apply, not its anchor chat's state.
        if not group:
            session = STORE.sessions.get(session_id) or {}
            backend = str(
                session.get("backend") or DEFAULT_BACKEND
            ).strip().lower()
            if CODEX_GOALS_RECONFIGURING and backend == BACKEND_CODEX:
                return "wait for Codex goals configuration to finish"
            if session_id in RUNS.maintenance:
                return (
                    "wait for Claude Stop recovery to finish"
                    if session_id in CLAUDE_STOP_FENCE_SESSIONS
                    else "wait for provider session maintenance to finish"
                )
            if stop_cleanup_in_progress(session_id):
                return "chat is finishing an explicit Stop"
            if session_id in RUNS.busy:
                return "chat already has a running turn"
        active_count = len(RUNS.busy)

    global_blocker = await turn_start_blocker()
    if global_blocker:
        return global_blocker

    if JOB_MAX_ACTIVE_RUNS > 0 and active_count >= JOB_MAX_ACTIVE_RUNS:
        return f"{active_count} active agent run(s)"

    pressure = host_pressure_snapshot()
    available_mem_mb = pressure.get("available_mem_mb")
    if (
        isinstance(available_mem_mb, int)
        and JOB_MIN_AVAILABLE_MEM_MB > 0
        and available_mem_mb < JOB_MIN_AVAILABLE_MEM_MB
    ):
        return low_available_memory_message(
            available_mem_mb, JOB_MIN_AVAILABLE_MEM_MB, action="start a scheduled job",
        )

    return None


def scheduled_job_lifecycle_admission_defer_reason(
    error: Exception,
) -> str | None:
    """Return a retry reason for a transient turn-admission fence.

    The scheduler checks its blockers before dispatch, but worker maintenance
    or another transient admission path can establish its fence after that
    check and before ``start_turn`` reserves the chat. That rejection did not
    execute the due occurrence, so it must be parked rather than recorded as a
    failed run. Other HTTP errors can be durable provider/configuration
    failures and keep the ordinary failure/recurrence behavior.
    """

    if isinstance(error, TransientAdmissionWait):
        return str(error.detail or "").strip() or "turn admission deferred"
    if not isinstance(error, HTTPException) or error.status_code != 503:
        return None
    reason = str(error.detail or "").strip()
    if reason in {
        EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
        MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    }:
        return reason
    return None
