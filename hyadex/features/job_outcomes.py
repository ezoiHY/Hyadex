"""How each scheduled job's latest run ended.

``last_run_at`` records when the scheduler admitted a run; whether that run
then succeeded is only known when its turn ends. This module follows the turns
that scheduled jobs start and reports their outcome, which ``JobStore`` keeps
on the job as ``last_run_status``, ``last_run_error``, ``last_run_id`` and
``last_run_finished_at``.

Statuses: ``running`` while the turn is in progress; ``completed``, ``failed``
or ``stopped`` once it ends; ``dispatched`` for group jobs, whose run is a
group message handed to the members. A run that could not start at all is
recorded as ``failed`` by the scheduler itself.

A job's ``turn_started`` carries ``purpose="scheduled_job"`` and ``job_id``;
later events of the same run are matched by ``run_id`` (a Stop's terminal
event carries only the run id).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

OBSERVED_EVENT_TYPES = frozenset({"turn_started", "error", "turn_finished", "turn_stopped"})
TERMINAL_EVENT_TYPES = frozenset({"turn_finished", "turn_stopped"})
RUN_STATUSES = frozenset({"running", "completed", "failed", "stopped", "dispatched"})
MAX_ERROR_CHARS = 500
_TRACKED_RUN_LIMIT = 512


@dataclass(frozen=True)
class RunOutcome:
    job_id: str
    run_id: str
    status: str
    error: str | None = None


class JobRunTracker:
    """Bounded in-memory map of scheduled-job runs to their outcome."""

    def __init__(self, limit: int = _TRACKED_RUN_LIMIT) -> None:
        self._limit = limit
        self._jobs: OrderedDict[str, str] = OrderedDict()
        self._errors: dict[str, str] = {}
        self._finished: OrderedDict[str, RunOutcome] = OrderedDict()

    def track(self, run_id: str, job_id: str) -> None:
        if run_id and job_id:
            self._jobs[run_id] = job_id
            self._jobs.move_to_end(run_id)
            self._trim()

    def finished(self, run_id: str) -> RunOutcome | None:
        return self._finished.get(run_id)

    def observe(self, event: dict[str, Any]) -> RunOutcome | None:
        """Return the run's outcome when ``event`` ends a scheduled-job turn."""
        run_id = str(event.get("run_id") or "")
        if not run_id:
            return None
        event_type = str(event.get("type") or "")
        if event_type == "turn_started":
            if event.get("purpose") == "scheduled_job":
                self.track(run_id, str(event.get("job_id") or ""))
            return None
        job_id = self._jobs.get(run_id)
        if not job_id and event.get("purpose") == "scheduled_job":
            job_id = str(event.get("job_id") or "")
            self.track(run_id, job_id)
        if not job_id:
            return None
        if event_type == "error":
            message = str(event.get("message") or event.get("error") or "").strip()
            self._errors.setdefault(run_id, message[:MAX_ERROR_CHARS] or "The run failed.")
            return None
        if event_type not in TERMINAL_EVENT_TYPES:
            return None
        error = self._errors.pop(run_id, None)
        if event_type == "turn_stopped" or event.get("stopped") is True:
            status = "stopped"
        elif error is not None or event.get("is_error") is True or event.get("exit_code") not in (None, 0):
            status = "failed"
            error = error or str(event.get("error") or "").strip()[:MAX_ERROR_CHARS] or "The run failed."
        else:
            status = "completed"
        outcome = RunOutcome(job_id=job_id, run_id=run_id, status=status,
                             error=error if status == "failed" else None)
        self._jobs.pop(run_id, None)
        self._finished[run_id] = outcome
        self._trim()
        return outcome

    def _trim(self) -> None:
        while len(self._jobs) > self._limit:
            stale, _ = self._jobs.popitem(last=False)
            self._errors.pop(stale, None)
        while len(self._finished) > self._limit:
            self._finished.popitem(last=False)


def apply_outcome(job: dict[str, Any], status: str, *, run_id: str | None, error: str | None,
                  at: str) -> None:
    """Store one outcome on a job record (the caller holds the job lock)."""
    job["last_run_status"] = status
    if run_id:
        job["last_run_id"] = run_id
    else:
        job.pop("last_run_id", None)
    if status == "failed":
        job["last_run_error"] = (error or "The run failed.")[:MAX_ERROR_CHARS]
    else:
        job.pop("last_run_error", None)
    if status == "running":
        job.pop("last_run_finished_at", None)
    else:
        job["last_run_finished_at"] = at
