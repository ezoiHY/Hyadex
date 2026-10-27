"""Scheduled jobs: the job store, schedules and runs."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque, OrderedDict
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging.handlers
import math
import re
import time
from typing import Any, Literal
import uuid

from fastapi import HTTPException, Query, Request

from hyadex.core.config import (
    BACKEND_CODEX,
    DEFAULT_BACKEND,
    JOB_BUSY_RETRY_SECONDS,
    JOB_DEFER_EVENT_MIN_SECONDS,
    JOB_MAX_ACTIVE_RUNS,
    JOB_MIN_AVAILABLE_MEM_MB,
    JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS,
    JOB_SCHEDULER_INTERVAL_SECONDS,
    JOBS_FILE,
    MAX_ACTIVE_AGENT_RUNS,
    MAX_JOB_TITLE_CHARS,
    MIN_START_AVAILABLE_MEM_MB,
    VALID_BACKENDS,
)
from hyadex.core.event_log import client_safe_event
from hyadex.core.timeline_index import (
    _build_timeline_index_locked,
    evict_timeline_index_cache_locked,
    scheduled_job_occurrence_key,
    scheduled_job_output_rank,
    scheduled_job_run_status,
    scheduled_job_status_should_replace,
    timeline_index_cached_entry,
    timeline_index_is_error,
    TIMELINE_INDEX_JOB_TYPES,
)
from hyadex.core.host import host_pressure_snapshot
from hyadex.core.event_seq import timeline_index_session_lock
from hyadex.core.http import CreateJobRequest
from hyadex.core.paths import events_path, now_iso
from hyadex.core.queue import request_fields_set
from hyadex.core.restart_status import MANAGED_SERVER_RESTART_ACTIVE_DETAIL
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_alerts import reversed_jsonl_events
from hyadex.core.session_files import list_session_file_records
from hyadex.core.session_settings import effective_provider_jobs_access, session_backend_locked
from hyadex.core.lifecycle_admission import (
    EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
    server_lifecycle_admission_blocker,
)
from hyadex.core.sessions import (
    append_event,
    CLAUDE_STOP_FENCE_SESSIONS,
    ScheduledJobChatReferenceRepairRequired,
    STORE,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    TIMELINE_INDEX_CACHE_LOCK,
)
from hyadex.core.turn_receipts import start_turn
from hyadex.core.util import ensure_dirs, low_available_memory_message, TransientAdmissionWait
from hyadex.core.wiring import WIRING
from hyadex.features.group_runtime import new_job_revision, start_group_job_run
from hyadex.features.job_access import provider_public_job, public_job
from hyadex.features.job_admission import (
    durable_scheduled_job_admissions,
    scheduled_job_blocker,
    scheduled_job_lifecycle_admission_defer_reason,
)
from hyadex.features.job_history import read_scheduled_job_runs
from hyadex.features.side_questions import CODEX_GOALS_RECONFIGURING
from hyadex.features.terminal import terminal_windows_snapshot
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.features.job_context import (
    job_context_mode,
    resolve_job_context_contract,
    VALID_JOB_CONTEXT_MODES,
)
from hyadex.features.job_schedules import (
    job_schedule_kind,
    next_job_occurrence,
    normalize_cron_expression,
    normalize_interval_seconds,
    normalize_job_timezone,
    normalize_rrule_expression,
    parse_job_timestamp,
    requested_job_schedule_kind,
    VALID_JOB_SCHEDULE_KINDS,
)
from hyadex.providers.claude.runtime import stop_cleanup_in_progress, TurnRequest
from hyadex.providers.codex.runtime import turn_start_blocker
from hyadex.providers.instructions import MAX_JOB_PROMPT_CHARS
from hyadex.providers.registry import authorize_provider_action
from hyadex.core.file_store import atomic_json

logger = logging.getLogger("hyadex-server")


def event_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in public_job(job).items() if key != "prompt"}


def clear_job_runtime_unavailable_deferral(job: dict[str, Any]) -> None:
    job.pop("_runtime_unavailable_occurrence", None)
    job.pop("_runtime_unavailable_defer_count", None)


def clear_job_update_park(job: dict[str, Any]) -> None:
    """Remove private ownership metadata for an idle-update job park."""

    job.pop("_update_park_schedule_id", None)
    job.pop("_update_park_occurrence", None)
    job.pop("_update_park_original_next_run_at", None)
    job.pop("_update_park_revision", None)


class ScheduledJobRevisionChanged(HTTPException):
    """A scheduled dispatch lost its race with an edit or revocation."""

    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail="scheduled job changed before dispatch; retry the current revision",
        )


def validate_job_text_bounds(title: Any, prompt: Any) -> None:
    """Enforce storage bounds even for internal callers bypassing Pydantic."""

    if len(str(title)) > MAX_JOB_TITLE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"job title must be at most {MAX_JOB_TITLE_CHARS} characters",
        )
    if len(str(prompt)) > MAX_JOB_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"job prompt must be at most {MAX_JOB_PROMPT_CHARS} characters",
        )


def write_jobs_registry(jobs: dict[str, dict[str, Any]]) -> None:
    """Atomically serialize the job registry outside the asyncio event loop."""

    ensure_dirs()
    atomic_json(JOBS_FILE, jobs)


class JobStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._save_sequence = 0
        self._last_saved_sequence = 0
        self.jobs: dict[str, dict[str, Any]] = {}
        self._scheduler_task: asyncio.Task | None = None
        # Durable intent lives on the job itself.  This set only claims the
        # short admission window so an HTTP retry and the scheduler cannot
        # launch the same pending manual run twice in one process.
        self._manual_runs_in_flight: set[str] = set()

    def _retired_provider(self, job: dict[str, Any]) -> bool:
        parent = STORE.sessions.get(str(job.get("session_id") or "")) or {}
        parent_backend = str(parent.get("backend") or DEFAULT_BACKEND).strip().lower()
        backend = str(job.get("backend") or parent_backend).strip().lower()
        return parent_backend not in VALID_BACKENDS or backend not in VALID_BACKENDS

    async def load(self) -> None:
        ensure_dirs()
        changed = False
        if JOBS_FILE.exists():
            try:
                loaded_jobs = json.loads(JOBS_FILE.read_text())
                if not isinstance(loaded_jobs, dict):
                    raise ValueError("jobs registry must be a JSON object")
                self.jobs = loaded_jobs
            except Exception as e:
                # Jobs have no secondary registry. Never turn a transient or
                # torn read into a successful empty load that the next save
                # permanently commits.
                raise RuntimeError(
                    f"refusing to replace unreadable jobs registry: {e}"
                ) from e
        for job in self.jobs.values():
            if not isinstance(job.get("team_references"), list):
                changed = True
            if "manual_run_pending" not in job:
                job["manual_run_pending"] = False
                changed = True
            if not str(job.get("_revision") or "").startswith("job_rev_"):
                job["_revision"] = new_job_revision()
                changed = True
            if job.get("schedule_kind") not in VALID_JOB_SCHEDULE_KINDS:
                job["schedule_kind"] = job_schedule_kind(job)
                changed = True
            if not str(job.get("timezone") or "").strip():
                job["timezone"] = "UTC"
                changed = True
            if "schedule_start_at" not in job:
                # Keep an upgraded interval job's exact next_run_at. This is a
                # recurrence anchor only; load must not reschedule legacy jobs.
                job["schedule_start_at"] = job.get("scheduled_run_at", job.get("next_run_at"))
                changed = True
            if "scheduled_run_at" not in job:
                job["scheduled_run_at"] = job.get("next_run_at") if job.get("enabled") else None
                changed = True
            context_mode_was_explicit = "context_mode" in job
            normalized_context_mode = job_context_mode(job)
            if not context_mode_was_explicit:
                parent_session = STORE.sessions.get(
                    str(job.get("session_id") or "")
                )
                if parent_session:
                    try:
                        normalized_context_mode, _backend = (
                            resolve_job_context_contract(
                                parent_session,
                                normalized_context_mode,
                                job.get("backend"),
                                context_mode_explicit=False,
                                enabled_backends=VALID_BACKENDS,
                                default_backend=DEFAULT_BACKEND,
                            )
                        )
                    except HTTPException:
                        # Do not make server startup fail on a corrupt legacy
                        # backend. Existing runtime validation will surface it
                        # if that job is run or edited.
                        normalized_context_mode = "chat"
            if job.get("context_mode") != normalized_context_mode:
                # Preserve the legacy contract: an omitted/same-provider job
                # continued its parent chat, while an explicitly different
                # provider ran independently.
                job["context_mode"] = normalized_context_mode
                changed = True
            parent_session = STORE.sessions.get(str(job.get("session_id") or ""))
            retired_provider = self._retired_provider(job)
            if retired_provider and job.get("disabled_reason") != "provider_removed":
                job["disabled_reason"] = "provider_removed"
                changed = True
            if (
                (retired_provider or (parent_session and parent_session.get("archived")))
                and (job.get("enabled") or job.get("manual_run_pending"))
            ):
                # Retired providers and archived chats cannot run. Persist the
                # pause before starting the scheduler; do not append recurring
                # failures to the saved chat on every occurrence.
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
                job["manual_run_pending"] = False
                job.pop("manual_run_requested_at", None)
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                clear_job_update_park(job)
                job["updated_at"] = now_iso()
                job["_revision"] = new_job_revision()
                changed = True
        if changed:
            await self.save()

    async def save(self) -> None:
        # Every mutator calls save while holding _lock. Detach every nested
        # value before the first await so a worker never traverses live state
        # after cancellation lets a later mutation begin.
        snapshot = json.loads(json.dumps(self.jobs, ensure_ascii=False))
        self._save_sequence += 1
        save_sequence = self._save_sequence

        async def persist_snapshot() -> None:
            async with self._save_lock:
                # Concurrent direct save calls can be scheduled out of order.
                # Once a newer snapshot exists, an older one must never become
                # the final durable registry.
                if save_sequence < self._save_sequence:
                    return
                await asyncio.to_thread(write_jobs_registry, snapshot)
                self._last_saved_sequence = save_sequence

        save_task = asyncio.create_task(persist_snapshot())
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            # to_thread cannot stop its filesystem write. Join through any
            # repeated cancellation before the caller releases the mutation
            # lock, then restore cancellation to the caller.
            await join_task_despite_caller_cancellation(save_task)
            raise

    async def snapshot(
        self,
        jid: str,
        *,
        expected_session_id: str | None = None,
        ensure_revision: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or (
                expected_session_id is not None
                and job.get("session_id") != expected_session_id
            ):
                raise HTTPException(status_code=404, detail="job not found")
            if ensure_revision and not str(job.get("_revision") or "").startswith(
                "job_rev_"
            ):
                job["_revision"] = new_job_revision()
            return dict(job)

    async def reconcile_admitted_runs_after_restart(self) -> int:
        """Advance crash-admitted occurrences before the scheduler starts."""

        async with self._lock:
            snapshot = {
                job_id: dict(job)
                for job_id, job in self.jobs.items()
            }
        admissions = await asyncio.to_thread(
            durable_scheduled_job_admissions,
            snapshot,
        )
        recovered = 0
        for job_id in sorted(admissions):
            event = admissions[job_id]
            manual = event.get("manual_run") is True
            marked = await self.mark_ran(
                job_id,
                manual=manual,
                expected_revision=str(event.get("job_revision") or ""),
                scheduled_run_at=float(event["job_scheduled_run_at"]),
                require_exact_match=True,
            )
            if marked is not None:
                recovered += 1
        return recovered

    async def assert_dispatch_revision(
        self,
        jid: str,
        session_id: str,
        expected_revision: str,
    ) -> None:
        async with self._lock:
            current = self.jobs.get(jid)
            if (
                not current
                or current.get("session_id") != session_id
                or current.get("_revision") != expected_revision
            ):
                raise ScheduledJobRevisionChanged()

    async def create(
        self,
        req: CreateJobRequest,
        *,
        emit_event: bool = True,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        validate_job_text_bounds(req.title, req.prompt)
        parent_session = STORE.sessions.get(req.session_id)
        if not parent_session:
            raise HTTPException(status_code=404, detail="session not found")
        if parent_session.get("archived"):
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before scheduling jobs",
            )
        context_mode, backend = resolve_job_context_contract(
            parent_session,
            req.context_mode,
            req.backend,
            context_mode_explicit="context_mode" in request_fields_set(req),
            enabled_backends=VALID_BACKENDS,
            default_backend=DEFAULT_BACKEND,
        )
        jid = f"job_{uuid.uuid4().hex[:16]}"
        now = now_iso()
        now_timestamp = time.time()
        schedule_kind = requested_job_schedule_kind(
            req.schedule_kind,
            req.interval_seconds,
            req.cron_expression,
            req.rrule,
        )
        timezone_name = normalize_job_timezone(req.timezone)
        interval_seconds = normalize_interval_seconds(req.interval_seconds) if schedule_kind == "interval" else None
        first_run_at = parse_job_timestamp(req.first_run_at, timezone_name)
        schedule_start_at = first_run_at if first_run_at is not None else now_timestamp
        if schedule_kind == "rrule" and first_run_at is None:
            # Keep DTSTART strictly after the request instant so COUNT includes
            # the first scheduled execution instead of consuming a past anchor.
            schedule_start_at = float(math.ceil(now_timestamp))
        cron_expression = None
        rrule_expression = None
        if schedule_kind == "cron":
            cron_expression = normalize_cron_expression(req.cron_expression, jid)
        elif schedule_kind == "rrule":
            rrule_expression = normalize_rrule_expression(req.rrule, timezone_name, schedule_start_at)

        recurring_calendar = schedule_kind in {"cron", "rrule"}
        loop = True if recurring_calendar else bool(req.loop)
        if recurring_calendar:
            max_runs = max(1, int(req.max_runs)) if req.max_runs is not None else None
        else:
            max_runs = None if loop else max(1, int(req.max_runs or 1))
        if schedule_kind == "interval" and first_run_at is None and interval_seconds:
            schedule_start_at = now_timestamp + interval_seconds
        job = {
            "id": jid,
            "session_id": req.session_id,
            "title": req.title,
            "prompt": req.prompt,
            "schedule_kind": schedule_kind,
            "interval_seconds": interval_seconds,
            "cron_expression": cron_expression,
            "rrule": rrule_expression,
            "timezone": timezone_name,
            "schedule_start_at": schedule_start_at,
            "scheduled_run_at": None,
            "loop": loop,
            "max_runs": max_runs,
            "enabled": req.enabled,
            "backend": backend,
            "context_mode": context_mode,
            "created_at": now,
            "updated_at": now,
            "last_run_at": None,
            "next_run_at": None,
            "run_count": 0,
            "manual_run_pending": False,
            "_revision": new_job_revision(),
        }
        if group_id:
            # Set only by the group jobs route; the job posts into that group.
            job["group_id"] = group_id
        if req.enabled:
            if first_run_at is not None:
                # An explicit first run is exact, even when it is off-rule.
                next_run_at = first_run_at
            elif schedule_kind == "interval":
                next_run_at = schedule_start_at if interval_seconds else None
            else:
                next_run_at = next_job_occurrence(job, now_timestamp, inclusive=False)
            job["next_run_at"] = next_run_at
            job["scheduled_run_at"] = next_run_at
            if next_run_at is None and schedule_kind in {"cron", "rrule"}:
                job["enabled"] = False
        async with self._lock:
            # Recheck inside the job lock. If archive won the race, its durable
            # session state is already visible; if create won, archive waits
            # for this lock and pauses the newly inserted job immediately.
            current_parent = STORE.sessions.get(req.session_id)
            if not current_parent:
                raise HTTPException(status_code=404, detail="session not found")
            if current_parent.get("archived"):
                raise HTTPException(
                    status_code=409,
                    detail="unarchive this chat before scheduling jobs",
                )
            self.jobs[jid] = job
            try:
                await self.save()
            except Exception:
                # A failed create must not leave a runnable in-memory job.
                # Cancellation is different:
                # save() joins its non-cancellable worker before re-raising,
                # so that snapshot is already durable and memory must retain
                # the same job instead of creating a restart split-brain.
                if self.jobs.get(jid) is job:
                    self.jobs.pop(jid, None)
                raise
        if emit_event:
            await append_event(req.session_id, "job_created", {
                "job": event_job(job),
                "job_id": jid,
                "message": f"Scheduled job created: {req.title}",
            })
        return job

    async def update(
        self,
        jid: str,
        patch: dict[str, Any],
        *,
        expected_session_id: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        updated_job: dict[str, Any]
        validate_job_text_bounds(
            patch.get("title", ""),
            patch.get("prompt", ""),
        )
        async with self._lock:
            stored_job = self.jobs.get(jid)
            if not stored_job or (expected_session_id is not None and stored_job.get("session_id") != expected_session_id):
                raise HTTPException(status_code=404, detail="job not found")
            job = dict(stored_job)
            parent_session = STORE.sessions.get(str(job.get("session_id") or ""))
            prospective_job = {**job, **({"backend": patch["backend"]} if patch.get("backend") else {})}
            if self._retired_provider(prospective_job):
                if patch.get("enabled") is True:
                    raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
                patch = {**patch, "enabled": False}
                job["disabled_reason"] = "provider_removed"
            elif job.get("disabled_reason") == "provider_removed":
                job.pop("disabled_reason")
            if parent_session and parent_session.get("archived"):
                if patch.get("enabled") is True:
                    raise HTTPException(
                        status_code=409,
                        detail="unarchive this chat before enabling scheduled jobs",
                    )
                # A metadata edit must not preserve a legacy enabled state for
                # an archived chat.
                patch = dict(patch)
                patch["enabled"] = False
            if "chat_references" in patch or patch.get("prompt") is not None:
                # Saved @Chat targets were removed with cross-chat messaging.
                # A legacy job keeps them, and stays paused at dispatch, until
                # the user clears them or replaces the prompt they annotate.
                job.pop("chat_references", None)
            was_enabled = bool(job.get("enabled"))
            if "backend" in patch and patch["backend"] is not None and patch["backend"] not in VALID_BACKENDS:
                raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
            if (
                "context_mode" in patch
                and patch["context_mode"] is not None
                and patch["context_mode"] not in VALID_JOB_CONTEXT_MODES
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "context_mode must be one of "
                        f"{sorted(VALID_JOB_CONTEXT_MODES)}"
                    ),
                )
            runtime_contract_changed = (
                ("backend" in patch and patch["backend"] is not None)
                or (
                    "context_mode" in patch
                    and patch["context_mode"] is not None
                )
            )
            if runtime_contract_changed:
                parent_session = STORE.sessions.get(
                    str(job.get("session_id") or "")
                )
                if not parent_session:
                    raise HTTPException(status_code=404, detail="session not found")
                context_mode_explicit = (
                    "context_mode" in patch
                    and patch["context_mode"] is not None
                )
                next_context_mode = (
                    patch["context_mode"]
                    if patch.get("context_mode") is not None
                    else job_context_mode(job)
                )
                next_backend = (
                    patch["backend"]
                    if patch.get("backend") is not None
                    else job.get("backend")
                )
                resolved_context_mode, normalized_backend = (
                    resolve_job_context_contract(
                        parent_session,
                        next_context_mode,
                        next_backend,
                        context_mode_explicit=context_mode_explicit,
                        enabled_backends=VALID_BACKENDS,
                        default_backend=DEFAULT_BACKEND,
                    )
                )
                patch = dict(patch)
                patch["context_mode"] = resolved_context_mode
                if "backend" in patch and patch["backend"] is not None:
                    patch["backend"] = normalized_backend
            current_kind = job_schedule_kind(job)
            requested_kind = str(patch.get("schedule_kind") or "").strip().lower()
            legacy_interval_patch = (
                current_kind in {"cron", "rrule"}
                and not requested_kind
                and patch.get("interval_seconds") is not None
                and not str(patch.get("cron_expression") or "").strip()
                and not str(patch.get("rrule") or "").strip()
            )
            if legacy_interval_patch:
                # Contract-v7 clients only understand intervals and include
                # interval/mode fields whenever they save a job. Preserve a
                # v8 calendar schedule unless the caller explicitly selects a
                # new schedule kind.
                patch = dict(patch)
                for key in ("interval_seconds", "loop", "max_runs"):
                    patch.pop(key, None)
            supplied_kinds: list[str] = []
            if patch.get("interval_seconds") is not None:
                supplied_kinds.append("interval")
            if str(patch.get("cron_expression") or "").strip():
                supplied_kinds.append("cron")
            if str(patch.get("rrule") or "").strip():
                supplied_kinds.append("rrule")
            if len(supplied_kinds) > 1:
                raise HTTPException(status_code=400, detail="choose only one schedule expression")
            if requested_kind and requested_kind not in VALID_JOB_SCHEDULE_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"schedule_kind must be one of {sorted(VALID_JOB_SCHEDULE_KINDS)}",
                )
            if requested_kind and supplied_kinds and requested_kind != supplied_kinds[0]:
                raise HTTPException(status_code=400, detail="schedule fields conflict with schedule_kind")
            next_kind = requested_kind or (supplied_kinds[0] if supplied_kinds else current_kind)

            has_next_run_field = "next_run_at" in patch
            has_exact_next_run_patch = has_next_run_field and patch["next_run_at"] is not None
            reset_next_run_to_schedule = has_next_run_field and patch["next_run_at"] is None
            timezone_name = normalize_job_timezone(
                patch.get("timezone") if patch.get("timezone") is not None else job.get("timezone")
            )
            schedule_changed = next_kind != current_kind or timezone_name != normalize_job_timezone(job.get("timezone"))
            if next_kind == "interval" and patch.get("interval_seconds") is not None:
                schedule_changed = schedule_changed or normalize_interval_seconds(patch["interval_seconds"]) != job.get("interval_seconds")
            elif next_kind == "cron" and str(patch.get("cron_expression") or "").strip():
                schedule_changed = schedule_changed or normalize_cron_expression(patch["cron_expression"], jid) != job.get("cron_expression")
            elif next_kind == "rrule" and str(patch.get("rrule") or "").strip():
                comparison_anchor = float(job.get("schedule_start_at") or time.time())
                schedule_changed = schedule_changed or normalize_rrule_expression(
                    patch["rrule"], timezone_name, comparison_anchor
                ) != job.get("rrule")
            next_run_at = (
                parse_job_timestamp(patch.get("next_run_at"), timezone_name)
                if has_exact_next_run_patch
                else None
            )
            for key in ("title", "prompt", "enabled", "backend", "context_mode"):
                if key in patch and patch[key] is not None:
                    job[key] = patch[key]
            job["schedule_kind"] = next_kind
            job["timezone"] = timezone_name

            if next_kind == "interval":
                interval_value = patch.get("interval_seconds") if "interval_seconds" in patch else job.get("interval_seconds")
                job["interval_seconds"] = normalize_interval_seconds(interval_value)
                job["cron_expression"] = None
                job["rrule"] = None
                if "loop" in patch and patch["loop"] is not None:
                    job["loop"] = bool(patch["loop"])
                elif current_kind != "interval":
                    job["loop"] = False
                if "max_runs" in patch:
                    if job.get("loop"):
                        job["max_runs"] = None
                    elif patch["max_runs"] is not None:
                        job["max_runs"] = max(1, int(patch["max_runs"]))
                elif "loop" in patch and job.get("loop"):
                    job["max_runs"] = None
                elif (current_kind != "interval" or "loop" in patch) and not job.get("loop"):
                    job["max_runs"] = max(
                        int(job.get("run_count") or 0) + 1,
                        int(job.get("max_runs") or 1),
                    )
            elif next_kind == "cron":
                expression = patch.get("cron_expression") if "cron_expression" in patch else job.get("cron_expression")
                job["cron_expression"] = normalize_cron_expression(expression, jid)
                job["interval_seconds"] = None
                job["rrule"] = None
                job["loop"] = True
                if "max_runs" in patch:
                    job["max_runs"] = max(1, int(patch["max_runs"])) if patch["max_runs"] is not None else None
            else:
                expression = patch.get("rrule") if "rrule" in patch else job.get("rrule")
                if current_kind != "rrule" or job.get("schedule_start_at") is None:
                    job["schedule_start_at"] = time.time()
                job["rrule"] = normalize_rrule_expression(
                    expression,
                    timezone_name,
                    float(job.get("schedule_start_at") or time.time()),
                )
                job["interval_seconds"] = None
                job["cron_expression"] = None
                job["loop"] = True
                if "max_runs" in patch:
                    job["max_runs"] = max(1, int(patch["max_runs"])) if patch["max_runs"] is not None else None

            if schedule_changed:
                schedule_reference = time.time()
                if next_kind == "interval" and job.get("interval_seconds"):
                    job["schedule_start_at"] = schedule_reference + int(job["interval_seconds"])
                elif next_kind == "rrule":
                    job["schedule_start_at"] = float(math.ceil(schedule_reference))
                else:
                    job["schedule_start_at"] = schedule_reference
            elif job.get("schedule_start_at") is None:
                job["schedule_start_at"] = job.get("scheduled_run_at") or job.get("next_run_at") or time.time()

            if next_kind == "interval" and has_exact_next_run_patch:
                # For interval schedules, the selected next run is the new
                # cadence anchor, just like an explicit first run at creation.
                # Treating it as a one-off makes every later run snap back to
                # the old wall-clock time.
                job["schedule_start_at"] = next_run_at

            max_runs = job.get("max_runs")
            exhausted = max_runs is not None and int(job.get("run_count") or 0) >= int(max_runs)
            if exhausted:
                job["enabled"] = False
            if job.get("enabled") and has_exact_next_run_patch:
                job["next_run_at"] = next_run_at
                job["scheduled_run_at"] = next_run_at
            elif job.get("enabled") and (
                reset_next_run_to_schedule
                or schedule_changed
                or patch.get("enabled") is True
                or not job.get("next_run_at")
            ):
                reference = time.time()
                if (
                    next_kind == "interval"
                    and job.get("interval_seconds")
                    and patch.get("enabled") is True
                    and not was_enabled
                ):
                    job["schedule_start_at"] = reference + int(job["interval_seconds"])
                next_occurrence = next_job_occurrence(job, reference, inclusive=False)
                job["next_run_at"] = next_occurrence
                job["scheduled_run_at"] = next_occurrence
                if next_occurrence is None:
                    job["enabled"] = False
            if not job.get("enabled"):
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
            # Any explicit edit owns a fresh revision and resets the bounded
            # unavailable-runtime budget. A stale scheduler snapshot is then
            # unable to consume or advance the edited occurrence.
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            self.jobs[jid] = job
            try:
                await self.save()
            except Exception:
                self.jobs[jid] = stored_job
                raise
            updated_job = dict(job)
        if emit_event:
            await append_event(str(updated_job.get("session_id") or ""), "job_updated", {
                "job": event_job(updated_job),
                "job_id": jid,
                "message": f"Scheduled job updated: {updated_job.get('title') or jid}",
            })
        return updated_job

    async def delete(self, jid: str, *, expected_session_id: str | None = None) -> bool:
        deleted_job: dict[str, Any] | None = None
        async with self._lock:
            current = self.jobs.get(jid)
            if current and (expected_session_id is None or current.get("session_id") == expected_session_id):
                deleted_job = self.jobs.pop(jid)
            elif expected_session_id is not None:
                raise HTTPException(status_code=404, detail="job not found")
            try:
                await self.save()
            except Exception:
                # An ordinary persistence failure did not durably commit the
                # removal, so keep the exact previously-owned object in
                # memory. Cancellation is intentionally different: save()
                # joins its shielded writer before propagating cancellation,
                # which means the deletion has committed and must stay gone.
                if deleted_job is not None:
                    self.jobs[jid] = deleted_job
                raise
        if deleted_job:
            await append_event(str(deleted_job.get("session_id") or ""), "job_deleted", {
                "job": event_job(deleted_job),
                "job_id": jid,
                "message": f"Scheduled job deleted: {deleted_job.get('title') or jid}",
            })
        return deleted_job is not None

    async def delete_for_session(self, session_id: str) -> int:
        async with self._lock:
            doomed = {
                jid: job
                for jid, job in self.jobs.items()
                if job.get("session_id") == session_id
            }
            for jid in doomed:
                self.jobs.pop(jid, None)
            try:
                await self.save()
            except Exception:
                # Keep failed cleanup retryable. The session tombstone prevents
                # these jobs from running while a later delete/scheduler pass
                # retries the durable removal.
                self.jobs.update(doomed)
                raise
            return len(doomed)

    async def pause_for_session(
        self,
        session_id: str,
        *,
        persist_unchanged: bool = False,
    ) -> int:
        paused_jobs: list[dict[str, Any]] = []
        async with self._lock:
            updated_at = now_iso()
            for job in self.jobs.values():
                if job.get("session_id") != session_id:
                    continue
                enabled = bool(job.get("enabled"))
                manual_pending = bool(job.get("manual_run_pending"))
                if not enabled and not manual_pending:
                    continue
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
                clear_job_runtime_unavailable_deferral(job)
                clear_job_update_park(job)
                if manual_pending:
                    job["manual_run_pending"] = False
                    job.pop("manual_run_requested_at", None)
                    job.pop("manual_run_defer_reason", None)
                    job.pop("_last_manual_defer_event_at", None)
                job["updated_at"] = updated_at
                job["_revision"] = new_job_revision()
                paused_jobs.append(dict(job))
            if not paused_jobs and not persist_unchanged:
                return 0
            # Deliberately keep the in-memory jobs paused if persistence fails.
            # The live scheduler must fail safe, and load() will retry the
            # durable reconciliation from the archived session on restart.
            await self.save()

        for job in paused_jobs:
            try:
                await append_event(session_id, "job_updated", {
                    "job": event_job(job),
                    "job_id": job.get("id"),
                    "message": (
                        "Scheduled job paused because chat was archived: "
                        f"{job.get('title') or job.get('id')}"
                    ),
                })
            except Exception as exc:
                # The pause is already durable. A timeline/audit projection
                # failure must never reactivate a schedule.
                logger.warning(
                    "could not append archived job pause event session=%s job=%s: %s",
                    session_id,
                    job.get("id"),
                    concise_error_message(exc),
                )
        return len(paused_jobs)

    async def pause_for_chat_reference_repair(
        self,
        jid: str,
        error: ScheduledJobChatReferenceRepairRequired,
        *,
        expected_revision: str,
    ) -> bool:
        paused_job: dict[str, Any] | None = None
        detail = str(error.detail or "saved chat target is unavailable").strip()
        async with self._lock:
            job = self.jobs.get(jid)
            if (
                job is None
                or job.get("_revision") != expected_revision
            ):
                return False
            job["enabled"] = False
            job["next_run_at"] = None
            job["scheduled_run_at"] = None
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["manual_run_pending"] = False
            job.pop("manual_run_requested_at", None)
            job.pop("manual_run_defer_reason", None)
            job.pop("_last_manual_defer_event_at", None)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            paused_job = dict(job)
            # Keep the in-memory schedule paused if persistence fails. It is
            # safer to require repair in this process than to keep dispatching
            # a known-invalid durable authority.
            await self.save()

        message = (
            f"Scheduled job paused: {paused_job.get('title') or jid} — "
            f"its saved @ chat target is invalid ({detail}). "
            "Edit or remove the target, then re-enable the job."
        )
        try:
            await append_event(
                str(paused_job.get("session_id") or ""),
                "job_error",
                {
                    "job": {
                        key: value
                        for key, value in provider_public_job(paused_job).items()
                        if key != "prompt"
                    },
                    "job_id": jid,
                    "error_code": "scheduled_chat_reference_invalid",
                    "repair_required": True,
                    "message": message,
                },
            )
        except Exception as exc:
            # The durable pause is authoritative even if its timeline
            # projection fails. Never reactivate or retry the invalid grant.
            logger.warning(
                "could not append scheduled chat-reference pause event job=%s: %s",
                jid,
                concise_error_message(exc),
            )
        return True

    async def mark_ran(
        self,
        jid: str,
        *,
        manual: bool = False,
        expected_revision: str | None = None,
        scheduled_run_at: float | None = None,
        require_exact_match: bool = False,
    ) -> dict[str, Any] | None:
        """Record an admitted run without advancing a replacement schedule.

        A user edit can win after ``turn_started`` is durable but before this
        bookkeeping acquires the job lock. Count that legitimate run, while
        advancing recurrence only when the stored job still points at the exact
        occurrence that was admitted. A manual run remains additive when a
        future scheduled occurrence is already pending.
        """
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                return None
            completed_at = time.time()
            admitted_scheduled_at = float(
                scheduled_run_at
                if scheduled_run_at is not None
                else job.get("scheduled_run_at")
                or job.get("next_run_at")
                or completed_at
            )
            current_scheduled_at = job.get("scheduled_run_at")
            if current_scheduled_at is None:
                current_scheduled_at = job.get("next_run_at")
            same_revision = (
                expected_revision is None
                or job.get("_revision") == expected_revision
            )
            same_occurrence = False
            if current_scheduled_at is not None:
                with suppress(TypeError, ValueError):
                    same_occurrence = (
                        float(current_scheduled_at) == admitted_scheduled_at
                    )
            if require_exact_match and (
                not same_revision
                or (manual and not job.get("manual_run_pending"))
                or (not manual and not same_occurrence)
            ):
                return None
            advance_admitted_occurrence = same_revision or same_occurrence
            job["last_run_at"] = now_iso()
            if manual:
                job["last_manual_run_at"] = job["last_run_at"]
                job["manual_run_pending"] = False
                job.pop("manual_run_requested_at", None)
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                job.pop("last_manual_run_error", None)
                job.pop("last_manual_run_error_at", None)
            else:
                job["last_scheduled_run_at"] = admitted_scheduled_at
            job["run_count"] = int(job.get("run_count") or 0) + 1
            run_count = int(job.get("run_count") or 0)
            max_runs = job.get("max_runs")
            limit_reached = max_runs is not None and run_count >= int(max_runs)
            recurring = (
                job_schedule_kind(job) in {"cron", "rrule"}
                or bool(job.get("loop"))
                or (job.get("interval_seconds") is not None and max_runs is not None and run_count < int(max_runs))
            )
            # A manual run requested before the next occurrence is additive:
            # it must not consume or move that future scheduled occurrence.
            preserve_future_schedule = False
            if (
                manual
                and bool(job.get("enabled"))
                and current_scheduled_at is not None
                and recurring
                and not limit_reached
            ):
                with suppress(TypeError, ValueError):
                    preserve_future_schedule = (
                        float(current_scheduled_at) > completed_at
                    )
            if preserve_future_schedule:
                pass
            elif (
                advance_admitted_occurrence
                and job.get("enabled")
                and recurring
                and not limit_reached
            ):
                next_occurrence = next_job_occurrence(
                    job,
                    max(admitted_scheduled_at, completed_at),
                    inclusive=False,
                )
                job["next_run_at"] = next_occurrence
                job["scheduled_run_at"] = next_occurrence
                if next_occurrence is None:
                    job["enabled"] = False
            elif limit_reached or (
                advance_admitted_occurrence
                and (not job.get("enabled") or not recurring)
            ):
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            await self.save()
            return dict(job)

    async def defer(
        self,
        jid: str,
        reason: str,
        delay_seconds: int | None = None,
        *,
        expected_revision: str | None = None,
        expected_next_run_at: float | None = None,
    ) -> bool:
        delay = int(delay_seconds or JOB_BUSY_RETRY_SECONDS)
        emit_event = False
        event_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or not job.get("enabled"):
                return False
            if (
                expected_revision is not None
                and job.get("_revision") != expected_revision
            ):
                return False
            if expected_next_run_at is not None:
                try:
                    same_occurrence = (
                        float(job.get("next_run_at"))
                        == float(expected_next_run_at)
                    )
                except (TypeError, ValueError):
                    same_occurrence = False
                if not same_occurrence:
                    return False
            now = time.time()
            clear_job_update_park(job)
            job["next_run_at"] = now + max(delay, 5)
            job["last_deferred_at"] = now_iso()
            job["last_defer_reason"] = reason
            last_emit = float(job.get("_last_defer_event_at") or 0)
            if JOB_DEFER_EVENT_MIN_SECONDS <= 0 or now - last_emit >= JOB_DEFER_EVENT_MIN_SECONDS:
                job["_last_defer_event_at"] = now
                emit_event = True
                event_job = public_job(job)
            job["updated_at"] = now_iso()
            revision = new_job_revision()
            job["_revision"] = revision
            await self.save()
        if emit_event and event_job and event_job.get("session_id"):
            await append_event(str(event_job["session_id"]), "job_deferred", {
                "job": event_job,
                "job_id": jid,
                "message": f"Scheduled job deferred: {event_job.get('title') or jid} — {reason}",
            })
        return True


    async def resume_legacy_update_parked(self) -> int:
        """Rearm unchanged occurrences parked by an ended idle reservation."""

        resumed = 0
        changed = False
        async with self._lock:
            now = time.time()
            updated_at = now_iso()
            for job in self.jobs.values():
                parked_schedule_id = str(
                    job.get("_update_park_schedule_id") or ""
                ).strip()
                if not parked_schedule_id:
                    continue
                changed = True
                parked_revision = str(
                    job.get("_update_park_revision") or ""
                )
                try:
                    parked_occurrence = float(
                        job.get("_update_park_occurrence")
                    )
                    current_occurrence = float(job.get("scheduled_run_at"))
                    original_next_run_at = float(
                        job.get("_update_park_original_next_run_at")
                    )
                except (TypeError, ValueError):
                    exact_park = False
                else:
                    exact_park = (
                        math.isfinite(parked_occurrence)
                        and math.isfinite(current_occurrence)
                        and math.isfinite(original_next_run_at)
                        and job.get("_revision") == parked_revision
                        and bool(job.get("enabled"))
                        and current_occurrence == parked_occurrence
                    )
                clear_job_update_park(job)
                if exact_park:
                    # Preserve the canonical occurrence, but make its retry
                    # immediately due. A future edit has already failed the
                    # revision/occurrence CAS above and remains authoritative.
                    job["next_run_at"] = min(original_next_run_at, now)
                    resumed += 1
                job["updated_at"] = updated_at
                job["_revision"] = new_job_revision()
            if changed:
                await self.save()
        return resumed

    async def defer_runtime_unavailable(
        self,
        jid: str,
        reason: str,
        *,
        expected_revision: str,
        expected_next_run_at: float,
    ) -> Literal["deferred", "exhausted", "stale"]:
        """Durably retry one unavailable-runtime occurrence under a hard cap."""

        event_snapshot: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if (
                job is None
                or not job.get("enabled")
                or job.get("_revision") != expected_revision
            ):
                return "stale"
            try:
                same_due_time = (
                    float(job.get("next_run_at"))
                    == float(expected_next_run_at)
                )
            except (TypeError, ValueError):
                same_due_time = False
            if not same_due_time:
                return "stale"
            try:
                occurrence = float(
                    job.get("scheduled_run_at")
                    if job.get("scheduled_run_at") is not None
                    else expected_next_run_at
                )
            except (TypeError, ValueError):
                return "stale"
            try:
                stored_occurrence = float(
                    job.get("_runtime_unavailable_occurrence")
                )
            except (TypeError, ValueError):
                stored_occurrence = math.nan
            try:
                defer_count = int(job.get("_runtime_unavailable_defer_count") or 0)
            except (TypeError, ValueError):
                defer_count = 0
            if stored_occurrence != occurrence:
                defer_count = 0
            defer_count = max(0, min(defer_count, JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS))
            if defer_count >= JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS:
                return "exhausted"

            now = time.time()
            clear_job_update_park(job)
            job["_runtime_unavailable_occurrence"] = occurrence
            job["_runtime_unavailable_defer_count"] = defer_count + 1
            job["next_run_at"] = now + max(JOB_BUSY_RETRY_SECONDS, 5)
            job["last_deferred_at"] = now_iso()
            job["last_defer_reason"] = reason
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            event_snapshot = event_job(job)
            await self.save()

        if event_snapshot and event_snapshot.get("session_id"):
            await append_event(
                str(event_snapshot["session_id"]),
                "job_deferred",
                {
                    "job": event_snapshot,
                    "job_id": jid,
                    "message": (
                        "Scheduled job deferred: "
                        f"{event_snapshot.get('title') or jid} — {reason}"
                    ),
                },
            )
        return "deferred"

    def _manual_run_deferred_result(
        self,
        jid: str,
        reason: str,
    ) -> dict[str, Any]:
        job = self.jobs.get(jid) or {}
        title = str(job.get("title") or jid)
        clean_reason = str(reason or "the chat is temporarily unavailable")
        return {
            "ok": True,
            "job_id": jid,
            "job": public_job(job),
            "run_id": None,
            "queued": True,
            "deferred": True,
            "manual_run_pending": True,
            "message": (
                f"Run now is queued for {title}: {clean_reason}. "
                "It will start automatically when the chat is available."
            ),
        }

    async def _record_manual_run_deferred(
        self,
        jid: str,
        reason: str,
    ) -> dict[str, Any]:
        deferred_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            if job.get("manual_run_pending"):
                clean_reason = str(reason or "the chat is temporarily unavailable")
                if job.get("manual_run_defer_reason") != clean_reason:
                    job["manual_run_defer_reason"] = clean_reason
                    job["updated_at"] = now_iso()
                    now = time.time()
                    last_emit = float(job.get("_last_manual_defer_event_at") or 0)
                    if (
                        JOB_DEFER_EVENT_MIN_SECONDS <= 0
                        or now - last_emit >= JOB_DEFER_EVENT_MIN_SECONDS
                    ):
                        job["_last_manual_defer_event_at"] = now
                        deferred_job = event_job(job)
                    job["_revision"] = new_job_revision()
                    await self.save()
        if deferred_job and deferred_job.get("session_id"):
            try:
                await append_event(str(deferred_job["session_id"]), "job_deferred", {
                    "job": deferred_job,
                    "job_id": jid,
                    "message": (
                        f"Manual run deferred: {deferred_job.get('title') or jid} "
                        f"— {reason}"
                    ),
                })
            except Exception as event_error:
                logger.warning(
                    "could not append manual job deferred event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )
        return self._manual_run_deferred_result(jid, reason)

    async def _clear_failed_manual_run(
        self,
        jid: str,
        error: Exception,
    ) -> None:
        failed_job: dict[str, Any] | None = None
        error_message = concise_error_message(error)
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or not job.get("manual_run_pending"):
                return
            job["manual_run_pending"] = False
            job.pop("manual_run_requested_at", None)
            job.pop("manual_run_defer_reason", None)
            job.pop("_last_manual_defer_event_at", None)
            job["last_manual_run_error"] = error_message
            job["last_manual_run_error_at"] = now_iso()
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            failed_job = event_job(job)
            await self.save()
        if failed_job and failed_job.get("session_id"):
            try:
                await append_event(str(failed_job["session_id"]), "job_error", {
                    "job": failed_job,
                    "job_id": jid,
                    "message": (
                        f"Manual run failed: {failed_job.get('title') or jid} "
                        f"— {error_message}"
                    ),
                })
            except Exception as event_error:
                logger.warning(
                    "could not append manual job error event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )

    async def _start_job_run(
        self,
        jid: str,
        *,
        manual: bool,
    ) -> dict[str, Any]:
        # Updates replace the stored dict under the job lock.  Hold one
        # immutable snapshot so prompt text and its structured grants cannot
        # come from different job revisions.
        job = await self.snapshot(jid, ensure_revision=True)
        if self._retired_provider(job):
            raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
        job_revision = str(job.get("_revision") or "")
        if not job_revision:
            raise ScheduledJobRevisionChanged()
        session_id = str(job.get("session_id") or "")
        parent_session = STORE.sessions.get(session_id)
        if parent_session and parent_session.get("archived"):
            await self.pause_for_session(session_id)
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before running scheduled jobs",
            )
        scheduled_run_at = (
            time.time()
            if manual
            else float(
                job.get("scheduled_run_at")
                or job.get("next_run_at")
                or time.time()
            )
        )
        if job.get("team_references") or job.get("chat_references"):
            # Saved Team and @Chat targets were retired. Running the prompt
            # without them would silently drop part of the job's routing.
            error = ScheduledJobChatReferenceRepairRequired(
                status_code=409,
                detail=(
                    "saved Team targets are no longer supported"
                    if job.get("team_references")
                    else "saved @Chat targets are no longer supported"
                ),
            )
            await self.pause_for_chat_reference_repair(jid, error, expected_revision=job_revision)
            raise error
        group_id = str(job.get("group_id") or "")
        if group_id:
            result = await start_group_job_run(group_id, job, jid)
            marked_job = await self.mark_ran(
                jid,
                manual=manual,
                expected_revision=job_revision,
                scheduled_run_at=scheduled_run_at,
            )
            ran_job = public_job(marked_job if isinstance(marked_job, dict) else job)
            await append_event(session_id, "job_ran", {
                "job": ran_job,
                "job_id": jid,
                "run_id": None,
                "group_id": group_id,
                "group_message_seq": result["group_message_seq"],
                "job_scheduled_run_at": scheduled_run_at,
                "manual_run": manual,
                "context_mode": "group",
                "message": (
                    f"Scheduled group job ran{' manually' if manual else ''}: "
                    f"{ran_job.get('title') or jid}"
                ),
            })
            return result
        req = TurnRequest(
            prompt=job["prompt"],
            file_ids=[],
            backend=job.get("backend"),
            purpose="scheduled_job",
            job_id=jid,
            job_title=str(job.get("title") or jid),
            job_scheduled_run_at=scheduled_run_at,
        )
        context_mode = job_context_mode(job)
        result = await start_turn(
            session_id,
            req,
            queue_if_busy=False,
            provider_context_mode=context_mode,
            scheduled_job_revision=job_revision,
            scheduled_job_manual_run=manual,
        )
        marked_job = await self.mark_ran(
            jid,
            manual=manual,
            expected_revision=job_revision,
            scheduled_run_at=scheduled_run_at,
        )
        # Deleting a job after its turn was admitted prevents recurrence but
        # cannot undo that run.  Keep its timeline marker without indexing the
        # now-absent mutable store entry.
        ran_job = public_job(marked_job if isinstance(marked_job, dict) else job)
        await append_event(session_id, "job_ran", {
            "job": ran_job,
            "job_id": jid,
            "run_id": result["run_id"],
            "job_scheduled_run_at": scheduled_run_at,
            "manual_run": manual,
            "context_mode": context_mode,
            "message": (
                f"Scheduled job ran{' manually' if manual else ''}: "
                f"{ran_job.get('title') or jid}"
            ),
        })
        return result

    async def run_job(self, jid: str) -> dict[str, Any]:
        return await self._start_job_run(jid, manual=False)

    async def _dispatch_pending_manual_run(
        self,
        jid: str,
        *,
        blocker_checked: bool = False,
    ) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job.get("manual_run_pending"):
            return {
                "ok": True,
                "job_id": jid,
                "job": public_job(job),
                "run_id": None,
                "queued": False,
                "deferred": False,
                "manual_run_pending": False,
                "message": "The pending manual run has already been admitted.",
            }
        if jid in self._manual_runs_in_flight:
            return self._manual_run_deferred_result(
                jid,
                "manual run admission is already in progress",
            )

        # No await occurs between the membership check and add, so this is an
        # atomic claim within the event loop. Persistence is the durable layer;
        # this claim only coalesces concurrent dispatch attempts.
        self._manual_runs_in_flight.add(jid)
        try:
            job = self.jobs.get(jid)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            session_id = str(job.get("session_id") or "")
            parent_session = STORE.sessions.get(session_id)
            if parent_session and parent_session.get("archived"):
                await self.pause_for_session(session_id)
                raise HTTPException(
                    status_code=409,
                    detail="unarchive this chat before running scheduled jobs",
                )

            try:
                if not blocker_checked:
                    blocker = await scheduled_job_blocker(
                        session_id,
                        manual=True,
                        **({"group": True} if job.get("group_id") else {}),
                    )
                    if blocker:
                        return await self._record_manual_run_deferred(jid, blocker)
                result = await self._start_job_run(jid, manual=True)
            except Exception as exc:
                if isinstance(exc, ScheduledJobChatReferenceRepairRequired):
                    raise
                parent_session = STORE.sessions.get(session_id)
                archived_rejection = (
                    isinstance(exc, HTTPException)
                    and exc.status_code == 409
                    and "archiv" in str(exc.detail).lower()
                )
                if (
                    (parent_session and parent_session.get("archived"))
                    or archived_rejection
                ):
                    await self.pause_for_session(session_id)
                    raise
                if (
                    isinstance(exc, HTTPException)
                    and exc.status_code in {409, 423, 429, 503}
                ):
                    return await self._record_manual_run_deferred(
                        jid,
                        str(exc.detail or "the chat is temporarily unavailable"),
                    )
                await self._clear_failed_manual_run(jid, exc)
                raise

            return {
                **result,
                "ok": True,
                "job_id": jid,
                "job": public_job(self.jobs[jid]),
                "queued": False,
                "deferred": False,
                "manual_run_pending": False,
                "message": f"Run now started for {job.get('title') or jid}.",
            }
        finally:
            self._manual_runs_in_flight.discard(jid)

    async def request_manual_run(self, jid: str, *, expected_session_id: str | None = None) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job or (expected_session_id is not None and job.get("session_id") != expected_session_id):
            raise HTTPException(status_code=404, detail="job not found")
        if self._retired_provider(job):
            raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
        session_id = str(job.get("session_id") or "")
        parent_session = STORE.sessions.get(session_id)
        if parent_session and parent_session.get("archived"):
            await self.pause_for_session(session_id)
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before running scheduled jobs",
            )

        pending_event_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or (expected_session_id is not None and job.get("session_id") != expected_session_id):
                raise HTTPException(status_code=404, detail="job not found")
            if not job.get("manual_run_pending"):
                job["manual_run_pending"] = True
                job["manual_run_requested_at"] = now_iso()
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                job.pop("last_manual_run_error", None)
                job.pop("last_manual_run_error_at", None)
                job["updated_at"] = now_iso()
                job["_revision"] = new_job_revision()
                pending_event_job = event_job(job)
                await self.save()

        if pending_event_job and pending_event_job.get("session_id"):
            try:
                await append_event(
                    str(pending_event_job["session_id"]),
                    "job_updated",
                    {
                        "job": pending_event_job,
                        "job_id": jid,
                        "message": (
                            "Manual run requested: "
                            f"{pending_event_job.get('title') or jid}"
                        ),
                    },
                )
            except Exception as event_error:
                logger.warning(
                    "could not append manual job pending event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )

        return await self._dispatch_pending_manual_run(jid)

    def start_scheduler(self) -> None:
        if (
            not SERVER.shutting_down
            and (self._scheduler_task is None or self._scheduler_task.done())
        ):
            self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    async def scheduler_loop(self) -> None:
        """Keep scheduling after an individual iteration escapes unexpectedly."""

        while True:
            try:
                await self._scheduler_loop_until_failure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "job scheduler iteration failed; continuing error=%s",
                    concise_error_message(exc),
                )

    async def _scheduler_loop_until_failure(self) -> None:
        logger.info("job scheduler started")
        while True:
            await asyncio.sleep(max(JOB_SCHEDULER_INTERVAL_SECONDS, 1.0))
            now = time.time()
            due = [
                (
                    job["id"],
                    job.get("_revision"),
                    (
                        float(job["next_run_at"])
                        if (
                            not job.get("manual_run_pending")
                            and job.get("enabled")
                            and job.get("next_run_at")
                        )
                        else None
                    ),
                )
                for job in list(self.jobs.values())
                if job.get("manual_run_pending")
                or (
                    job.get("enabled")
                    and job.get("next_run_at")
                    and float(job["next_run_at"]) <= now
                )
            ]
            for jid, due_revision, due_next_run_at in due:
                job = self.jobs.get(jid)
                if not job:
                    continue
                manual_run_pending = bool(job.get("manual_run_pending"))
                job_session_id = str(job.get("session_id") or "")
                if (
                    not job_session_id
                    or job_session_id not in STORE.sessions
                    or job_session_id in DELETING_SESSIONS
                    or job_session_id in DELETED_SESSION_TOMBSTONES
                ):
                    try:
                        await self.delete_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "scheduled job cleanup failed session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                if STORE.sessions[job_session_id].get("archived"):
                    try:
                        await self.pause_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "could not persist archived job pause session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                blocker = await scheduled_job_blocker(
                    job_session_id,
                    manual=manual_run_pending,
                    **({"group": True} if job.get("group_id") else {}),
                )
                if blocker:
                    if manual_run_pending:
                        await self._record_manual_run_deferred(jid, blocker)
                    else:
                        await self.defer(
                            jid,
                            blocker,
                            JOB_BUSY_RETRY_SECONDS,
                            expected_revision=due_revision,
                            expected_next_run_at=due_next_run_at,
                        )
                    continue
                # The blocker check yields. Archive/pause or a user edit may
                # have changed this due item while it was in flight; never
                # dispatch from the stale due-list snapshot.
                job = self.jobs.get(jid)
                if not job:
                    continue
                if job.get("_revision") != due_revision:
                    # Even when an edit leaves its replacement occurrence due,
                    # dispatch it only from a fresh scheduler snapshot. Error
                    # handling below is guarded by the dispatched revision;
                    # launching a newer revision from this stale iteration
                    # would make a permanent failure look stale and retry it.
                    continue
                manual_run_pending = bool(job.get("manual_run_pending"))
                scheduled_run_due = bool(
                    job.get("enabled")
                    and job.get("next_run_at")
                    and float(job["next_run_at"]) <= time.time()
                )
                if (
                    not manual_run_pending
                    and due_next_run_at is not None
                    and float(job.get("next_run_at") or 0) != due_next_run_at
                ):
                    continue
                if not manual_run_pending and not scheduled_run_due:
                    continue
                # ``scheduled_job_blocker`` awaits several locks. Session
                # deletion can commit during that yield before its jobs are
                # removed; revalidate the exact parent before dereferencing it.
                parent_session = STORE.sessions.get(job_session_id)
                if (
                    parent_session is None
                    or job_session_id in DELETING_SESSIONS
                    or job_session_id in DELETED_SESSION_TOMBSTONES
                ):
                    try:
                        await self.delete_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "scheduled job cleanup failed session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                if parent_session.get("archived"):
                    try:
                        await self.pause_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "could not persist archived job pause session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                try:
                    if manual_run_pending:
                        await self._dispatch_pending_manual_run(
                            jid,
                            blocker_checked=True,
                        )
                    else:
                        await self.run_job(jid)
                except Exception as e:
                    if isinstance(e, ScheduledJobRevisionChanged):
                        # An edit/revoke won before admission. The current job
                        # revision already owns its next-run state; never emit
                        # an error or reschedule from the stale due snapshot.
                        continue
                    if isinstance(
                        e,
                        ScheduledJobChatReferenceRepairRequired,
                    ):
                        logger.warning(
                            "scheduled job %s paused for chat-reference repair: %s",
                            jid,
                            str(e.detail or "invalid saved target"),
                        )
                        # run_job already committed the pause and its one clear
                        # repair event. Do not append a duplicate generic error
                        # or compute another recurrence from the stale due item.
                        continue
                    parent_session = STORE.sessions.get(job_session_id)
                    archived_rejection = (
                        isinstance(e, HTTPException)
                        and e.status_code == 409
                        and "archiv" in str(e.detail).lower()
                    )
                    if (
                        (parent_session and parent_session.get("archived"))
                        or archived_rejection
                    ):
                        try:
                            await self.pause_for_session(job_session_id)
                        except Exception as exc:
                            logger.warning(
                                "could not persist archived job pause session=%s: %s",
                                job_session_id,
                                concise_error_message(exc),
                            )
                        continue
                    runtime_unavailable_reason = (
                        scheduled_job_runtime_unavailable_reason(e)
                        if not manual_run_pending
                        else None
                    )
                    if (
                        runtime_unavailable_reason
                        and due_next_run_at is not None
                    ):
                        runtime_defer_result = await self.defer_runtime_unavailable(
                            jid,
                            runtime_unavailable_reason,
                            expected_revision=str(due_revision or ""),
                            expected_next_run_at=due_next_run_at,
                        )
                        if runtime_defer_result in {"deferred", "stale"}:
                            continue
                        # Exhaustion deliberately falls through to the normal
                        # terminal error/recurrence path for this occurrence.
                    lifecycle_defer_reason = (
                        scheduled_job_lifecycle_admission_defer_reason(e)
                    )
                    if lifecycle_defer_reason:
                        if manual_run_pending:
                            await self._record_manual_run_deferred(
                                jid,
                                lifecycle_defer_reason,
                            )
                        else:
                            # Preserve the canonical occurrence and use the due
                            # snapshot as a CAS guard. If a user edited/replaced
                            # the schedule while dispatch was in flight, that
                            # revision owns timing and defer cannot overwrite it.
                            await self.defer(
                                jid,
                                lifecycle_defer_reason,
                                JOB_BUSY_RETRY_SECONDS,
                                expected_revision=due_revision,
                                expected_next_run_at=due_next_run_at,
                            )
                        continue
                    if manual_run_pending:
                        logger.warning(
                            "pending manual job %s failed permanently: %s",
                            jid,
                            e,
                        )
                        await self._clear_failed_manual_run(jid, e)
                        continue
                    logger.warning("scheduled job %s failed: %s", jid, e)
                    async with self._lock:
                        current = self.jobs.get(jid)
                        if (
                            current is not job
                            or current.get("_revision") != due_revision
                        ):
                            # A user edit/revoke won while provider dispatch
                            # was in flight. Its replacement revision owns all
                            # timing; never serialize or emit an error from the
                            # stale due snapshot.
                            continue
                        failed_at = time.time()
                        scheduled_at = float(
                            job.get("scheduled_run_at")
                            or job.get("next_run_at")
                            or failed_at
                        )
                        run_count = int(job.get("run_count") or 0)
                        max_runs = job.get("max_runs")
                        limit_reached = (
                            max_runs is not None
                            and run_count >= int(max_runs)
                        )
                        recurring = (
                            job_schedule_kind(job) in {"cron", "rrule"}
                            or bool(job.get("loop"))
                            or (
                                job.get("interval_seconds") is not None
                                and max_runs is not None
                                and run_count < int(max_runs)
                            )
                        )
                        if (
                            job.get("enabled")
                            and recurring
                            and not limit_reached
                        ):
                            next_occurrence = next_job_occurrence(
                                job,
                                max(scheduled_at, failed_at),
                                inclusive=False,
                            )
                            job["next_run_at"] = next_occurrence
                            job["scheduled_run_at"] = next_occurrence
                            if next_occurrence is None:
                                job["enabled"] = False
                        else:
                            job["enabled"] = False
                            job["next_run_at"] = None
                            job["scheduled_run_at"] = None
                        clear_job_runtime_unavailable_deferral(job)
                        clear_job_update_park(job)
                        await self.save()
                        failed_job = dict(job)
                    if failed_job.get("session_id"):
                        await append_event(
                            failed_job["session_id"],
                            "job_error",
                            {
                                "job": public_job(failed_job),
                                "job_id": jid,
                                "message": (
                                    "Scheduled job failed: "
                                    f"{failed_job.get('title') or jid} — {e}"
                                ),
                            },
                        )

    async def stop_scheduler(self) -> None:
        """Cancel and join the scheduler before provider shutdown begins."""

        task = self._scheduler_task
        self._scheduler_task = None
        if task is None or task is asyncio.current_task() or task.done():
            if task is not None and not task.cancelled():
                with suppress(BaseException):
                    task.result()
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


JOBS = JobStore()
# The turn engine reaches the job store through WIRING (it sits below jobs).
WIRING.jobs = JOBS


async def append_provider_job_mutation_event(
    job: dict[str, Any],
    event_type: Literal["job_created", "job_updated"],
) -> None:
    """Best-effort timeline projection for an already committed job mutation."""

    job_id = str(job.get("id") or "")
    session_id = str(job.get("session_id") or "")
    verb = "created" if event_type == "job_created" else "updated"
    try:
        await append_event(session_id, event_type, {
            "job": event_job(job),
            "job_id": job_id,
            "message": (
                f"Scheduled job {verb}: {job.get('title') or job_id}"
            ),
        })
    except Exception as exc:
        # Persistence has already succeeded. An audit/timeline projection
        # failure must not make the provider retry or refund route authority.
        logger.warning(
            "could not append provider job mutation event job=%s type=%s: %s",
            job_id,
            event_type,
            concise_error_message(exc),
        )


def compact_job_interval(value: Any) -> str:
    try:
        seconds = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "unspecified"
    if seconds <= 0:
        return "unspecified"
    parts: list[str] = []
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, unit)
        if amount:
            parts.append(f"{amount}{suffix}")
        if len(parts) == 2:
            break
    return " ".join(parts)


def scheduled_job_resource_diagnostics(pressure: dict[str, Any], active_count: int) -> dict[str, Any]:
    """Explain resource admission from one snapshot, without taking run locks.

    Per-chat ownership, provider readiness and maintenance remain separate
    admission checks. Health must not wait for their locks to explain memory.
    """

    available = pressure.get("available_mem_mb")
    blocker = None
    for cap, minimum, action, message in (
        (MAX_ACTIVE_AGENT_RUNS, MIN_START_AVAILABLE_MEM_MB, "start an agent turn",
         f"server already has {active_count} active agent run(s)"),
        (JOB_MAX_ACTIVE_RUNS, JOB_MIN_AVAILABLE_MEM_MB, "start a scheduled job",
         f"{active_count} active agent run(s)"),
    ):
        if cap > 0 and active_count >= cap:
            blocker = message
            break
        if isinstance(available, int) and minimum > 0 and available < minimum:
            blocker = low_available_memory_message(available, minimum, action=action)
            break
    return {
        "effective_min_available_mem_mb": max(0, MIN_START_AVAILABLE_MEM_MB, JOB_MIN_AVAILABLE_MEM_MB),
        "resource_blocked": blocker is not None,
        "resource_blocking_reason": blocker,
    }


def scheduled_job_runtime_unavailable_reason(error: Exception) -> str | None:
    """Recognize the provider runtime probe's exact structured rejection."""

    if not isinstance(error, HTTPException) or error.status_code != 503:
        return None
    detail = error.detail
    if not isinstance(detail, dict) or detail.get("code") != "runtime_unavailable":
        return None
    message = str(detail.get("message") or "").strip()
    return message or "The selected provider runtime is unavailable."


async def ensure_discardable_empty_session(session_id: str, expected_updated_at: str) -> None:
    """Fail closed while the caller holds the lifecycle lock/deletion fence.

    Automatic placeholder cleanup must never behave like an explicit Delete:
    it may not interrupt a turn, cancel queued work or remove uploaded content.
    Local drafts are additionally checked by the originating desktop client.
    """
    def untouched() -> bool:
        session = STORE.sessions.get(session_id)
        return bool(
            session and expected_updated_at
            and session.get("updated_at") == expected_updated_at
            and session.get("title") == "New chat"
            and not session_backend_locked(session)
            and not any(session.get(key) for key in (
                "parent_id", "pinned", "archived", "manual_unread",
                "codex_goal", "claude_goal", "emergency_alert",
            ))
            and session_id not in RUNS.active and session_id not in RUNS.busy
            and session_id not in RUNS.current_turns
            and not RUNS.queued.get(session_id)
            and session_id not in RUNS.run_now
            and session_id not in RUNS.run_now_requests
            and not any(not task.done() for task in RUNS.turn_tasks.get(session_id, ()))
            and not any(job.get("session_id") == session_id for job in JOBS.jobs.values())
        )

    def only_creation_event() -> bool:
        # Unlike timeline projection, do not skip malformed/hidden records.
        # Any extra byte or uncertain history preserves the chat.
        with events_path(session_id).open("rb") as stream:
            line = stream.readline(65537)
            if not line or len(line) > 65536 or stream.read(1):
                return False
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return False
        return isinstance(event, dict) and event.get("type") == "session_created" and event.get("seq") == 1

    if not untouched():
        raise HTTPException(status_code=409, detail="Chat is no longer an unused placeholder; it was preserved.")
    history_empty, files, terminal = await asyncio.gather(
        asyncio.to_thread(only_creation_event),
        asyncio.to_thread(list_session_file_records, session_id),
        asyncio.to_thread(terminal_windows_snapshot, session_id),
    )
    if not untouched() or not history_empty or files or terminal.get("exists"):
        raise HTTPException(status_code=409, detail="Chat has content or resources; it was preserved.")


async def list_session_jobs(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    jobs = sorted(
        (public_job(job) for job in JOBS.jobs.values() if job.get("session_id") == session_id),
        key=lambda job: job.get("updated_at") or "",
        reverse=True,
    )
    return {"jobs": jobs}


async def get_session_job_runs(
    session_id: str,
    job_id: str,
    timeline_group_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=320,
    ),
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await asyncio.to_thread(
            read_scheduled_job_runs,
            session_id,
            job_id,
            timeline_group_id=timeline_group_id,
            before_seq=before_seq,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
