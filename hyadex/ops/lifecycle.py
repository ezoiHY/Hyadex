"""Server lifecycle: startup, shutdown, managed restarts and maintenance holds."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import hmac
import json
import logging.handlers
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Literal
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from hyadex.core.abandoned import recover_abandoned_turns_after_start
from hyadex.core.config import (
    AGENT_TOKEN,
    CONFIG_ENV_APPLIED,
    CONFIG_ENV_FILE,
    HOST_HEALTH_FILE,
    HOST_HEALTH_MAX_BYTES,
    HOST_MONITOR_INTERVAL_SECONDS,
    hyadex_setting,
    PROVIDER_AUTHORITY_ROOT,
    SERVER_INSTANCE_ID,
    SERVER_INSTANCE_NAME,
    SERVER_ROOT,
    SERVER_VERSION,
    STATE_DIR,
)
from hyadex.core.host import host_pressure_snapshot
from hyadex.core.http import (
    live_unsafe_http_mutation_ids_locked,
    request_exact_restart_header_authorized,
    server_restart_browser_request_forbidden,
    server_restart_error_detail,
    unsafe_http_mutation_count_locked,
    validate_listener_auth,
)
from hyadex.core.lifecycle_admission import execution_maintenance_blocks_work
from hyadex.core.paths import now_iso
from hyadex.core.processes import (
    bounded_restart_tmux_cgroup_state,
    hyadex_server_systemd_cgroup,
    server_restart_tmux_cgroup_state,
    top_process_rows,
)
from hyadex.core.queue_recovery import queue_checkpoint_loop
from hyadex.core.restart_status import (
    managed_server_restart_blocks_work,
    read_server_restart_status,
    reconcile_stale_server_restart_acceptance,
    SERVER_RESTART_ACTIVE_PHASES,
    SERVER_RESTART_PHASES,
    SERVER_RESTART_SIGNAL_LOCK,
    server_restart_status_age_seconds,
    write_server_restart_status,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_codex import ABANDONED_FORK_PROVIDER_THREADS
from hyadex.core.session_titles import close_generated_session_titles
from hyadex.core.sessions import PROVIDER_AUTHORITIES, PROVIDER_AUTHORITY_LOCK, STORE
from hyadex.core.state import (
    DELETING_SESSIONS,
    HEALTH_QUEUE_RECONCILE_STATE,
    TURN_RECEIPT_TASKS,
    UNSAFE_HTTP_MUTATION_ADMISSION_LOCK,
)
from hyadex.core.stop import join_cancelled_tasks
from hyadex.core.turns import (
    active_run_summaries,
    explicit_stop_session_ids,
    recover_queued_turns_after_start,
    server_lifecycle_active_session_ids_locked,
)
from hyadex.core.util import bounded_lock, ensure_dirs, server_identity, utc_timestamp
from hyadex.features.forks import cleanup_abandoned_fork_provider_threads
from hyadex.features.group_runtime import AUTOPILOT, GROUPS
from hyadex.features.history import clear_imported_active_runs, HISTORY_MAINTENANCE_TASKS
from hyadex.features.jobs import JOBS
from hyadex.features.search import history_search_index_loop
from hyadex.features.side_questions import CODEX_GOALS_RECONFIGURING
from hyadex.features.terminal import scrub_tmux_global_secret_environment, TERMINAL_ATTACHMENTS
from hyadex.ops import instances as server_instances
from hyadex.providers.claude.background_state import PROVIDER_WORK_LABEL_LIMIT
from hyadex.providers.claude.runtime import CLAUDE_STOP_FENCE_RETRY_TASKS, close_claude_sdk_manager
from hyadex.providers.claude.subagents import CLAUDE_STOP_FENCE_ATTEMPT_TASKS
from hyadex.providers.codex.commands import SIDE_QUESTIONS
from hyadex.providers.codex.interactions import close_codex_app_server_manager
from hyadex.providers.codex.runtime import (
    active_provider_background_work_labels,
    provider_background_work_labels_from_snapshot,
)
from hyadex.providers.codex.threads import recover_abandoned_codex_compactions_after_start
from hyadex.providers.registry import (
    kill_registered_provider_children,
    prepare_provider_background_work_snapshot,
    refresh_runtime_diagnostics,
    sweep_orphaned_provider_children,
)

logger = logging.getLogger("hyadex-server")


class ServerRestartRequest(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: uuid.UUID
    expected_server_identity: str = Field(min_length=1, max_length=128)
    expected_server_instance_id: str = Field(min_length=1, max_length=128)
    confirmed: Literal[True]
    force: Literal[True] | None = None
    force_confirmed: Literal[True] | None = None
    expected_blocker_revision: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_literal_boolean_true(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("confirmed must be the JSON boolean true")
        return value

    @field_validator("force", "force_confirmed", mode="before")
    @classmethod
    def require_optional_literal_boolean_true(cls, value: Any) -> Any:
        if value is not None and value is not True:
            raise ValueError("force controls must be omitted or the JSON boolean true")
        return value

    @model_validator(mode="after")
    def require_explicit_force_confirmation(self) -> "ServerRestartRequest":
        if (self.force is True) != (self.force_confirmed is True):
            raise ValueError(
                "force and force_confirmed must both be the JSON boolean true"
            )
        # A forced restart is the emergency path. The blocker revision is an
        # optional audit hint: when it is stale the restart still proceeds and
        # the mismatch is recorded, because an operator who cannot obtain a
        # fresh snapshot from a wedged server must still be able to restart it.
        if self.expected_blocker_revision is not None and self.force is not True:
            raise ValueError(
                "expected_blocker_revision requires a forced restart"
            )
        return self


async def purge_provider_authority_files_after_restart() -> int:
    """Fail closed after restart without following or broadly globbing paths.

    Provider grants and live-run ownership are intentionally memory-only, and
    startup terminalizes every provider turn orphaned with the old process.
    Rehydrating a token from its provider-facing file would lose its exact
    action, route, consumption, and transition fences. Clear the registry and
    unlink only canonical authority-file names inside the private root.
    """

    async with PROVIDER_AUTHORITY_LOCK:
        PROVIDER_AUTHORITIES.clear()
    removed_files = 0
    try:
        candidates = list(PROVIDER_AUTHORITY_ROOT.iterdir())
    except OSError:
        candidates = []
    for path in candidates:
        if not re.fullmatch(
            r"run_[A-Za-z0-9_-]+(?:-[0-9a-f]{32})?\.json",
            path.name,
        ):
            continue
        try:
            # unlink() removes a matching symlink itself and never follows it.
            path.unlink()
        except OSError:
            continue
        removed_files += 1
    return removed_files


def trim_process_args(row: dict[str, Any], max_chars: int = 500) -> dict[str, Any]:
    out = dict(row)
    args = str(out.get("args") or "")
    if len(args) > max_chars:
        out["args"] = args[:max_chars].rstrip() + "... [trimmed]"
    return out


def write_bounded_jsonl(path: Path, record: dict[str, Any], max_bytes: int) -> None:
    ensure_dirs()
    with suppress(Exception):
        if path.exists() and path.stat().st_size > max_bytes:
            rotated = path.with_suffix(path.suffix + ".1")
            with suppress(FileNotFoundError):
                rotated.unlink()
            path.replace(rotated)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


async def host_health_record() -> dict[str, Any]:
    active_runs = await active_run_summaries()
    active_pgids = {int(run["pgid"]) for run in active_runs if run.get("pgid") is not None}
    top_rows = await asyncio.to_thread(top_process_rows, 20)
    top_processes = []
    for row in top_rows:
        clean = trim_process_args(row)
        row_pgid = int(clean.get("pgid") or -1)
        clean["tracked_by_hyadex"] = row_pgid in active_pgids
        top_processes.append(clean)
    return {
        "ts": now_iso(),
        "pressure": host_pressure_snapshot(),
        "active_runs": active_runs,
        "top_processes": top_processes,
        "jobs": len(JOBS.jobs),
    }


async def host_monitor_loop() -> None:
    logger.info("host health monitor started path=%s", HOST_HEALTH_FILE)
    while True:
        try:
            record = await host_health_record()
            await asyncio.to_thread(write_bounded_jsonl, HOST_HEALTH_FILE, record, HOST_HEALTH_MAX_BYTES)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("host health monitor failed: %s", exc)
        await asyncio.sleep(max(HOST_MONITOR_INTERVAL_SECONDS, 5.0))


SERVER_LIFECYCLE_OPERATION_LOCK = asyncio.Lock()
SERVER_RESTART_SIGNAL_DELAY_SECONDS = 0.5
SERVER_RESTART_FORCE_KILL_DELAY_SECONDS = 3.0
# Every lifespan shutdown phase runs under this deadline (bounded_shutdown_phase)
# so one wedged join cannot starve the provider teardown that follows it.
SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS = 5.0
# Number of bounded_shutdown_phase calls in the lifespan teardown; keep in sync
# so the cooperative watchdog budget below honours every phase.
SERVER_SHUTDOWN_PHASE_COUNT = 10
DEFAULT_UVICORN_GRACEFUL_SHUTDOWN_SECONDS = 20.0
MAX_UVICORN_GRACEFUL_SHUTDOWN_SECONDS = 60.0
SERVER_SHUTDOWN_STRAGGLERS: set[asyncio.Task[Any]] = set()


def configured_uvicorn_graceful_shutdown_seconds() -> float:
    try:
        configured = float(
            hyadex_setting(
                "UVICORN_GRACEFUL_SHUTDOWN_SECONDS",
                str(int(DEFAULT_UVICORN_GRACEFUL_SHUTDOWN_SECONDS)),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_UVICORN_GRACEFUL_SHUTDOWN_SECONDS
    if not math.isfinite(configured):
        return DEFAULT_UVICORN_GRACEFUL_SHUTDOWN_SECONDS
    return min(
        MAX_UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
        max(1.0, configured),
    )


# A cooperative restart still arms the hard-kill watchdog: uvicorn waits on
# background tasks/websockets and a Codex child may ignore SIGTERM, so without
# a deadline the port can stay closed forever. The budget is derived from the
# uvicorn graceful window plus every bounded lifespan shutdown phase, so the
# watchdog can only fire after the phases it protects had their chance.
SERVER_RESTART_GRACEFUL_KILL_DELAY_SECONDS = (
    configured_uvicorn_graceful_shutdown_seconds()
    + SERVER_SHUTDOWN_PHASE_COUNT * SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS
    + 5.0
)
SERVER_RESTART_COOLDOWN_SECONDS = 30.0
# A forced (emergency) restart must never wait on server state that may itself
# be the reason the operator is restarting. Every lock, probe, and snapshot on
# that path is bounded by these timeouts and degrades to an audited best-effort
# snapshot instead of refusing or hanging.
SERVER_RESTART_FORCE_LOCK_TIMEOUT_SECONDS = 2.0
SERVER_RESTART_FORCE_PROBE_TIMEOUT_SECONDS = 2.0
SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS = 2.0
SERVER_RESTART_BLOCKER_SNAPSHOT_VERSION = 2
SERVER_RESTART_COUNT_LIMIT = 1_000_000
MANAGED_SERVER_SERVICE_KIND_CACHE: str | None = None


async def execution_maintenance_control(
    action: str | None = None,
    operation_id: str | None = None,
    lease_id: str | None = None,
    lifetime_seconds: int = 120,
) -> dict[str, Any]:
    """Hold actual turn/mutation admission before authorizing worker retirement."""

    if SERVER.execution_maintenance is None:
        raise RuntimeError("This process is not an independent execution worker")
    provider_snapshot = await prepare_provider_background_work_snapshot(timeout_seconds=5.0)
    async with RUNS.active_lock:
        async with RUNS.queue_lock:
            async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
                blockers = server_lifecycle_blocker_counts(
                    server_lifecycle_active_session_ids_locked(),
                    restart_blocking_queued_turn_count_locked(),
                    provider_background_work_labels_from_snapshot(provider_snapshot),
                    unsafe_http_mutation_count_locked(),
                )
                blockers.update({
                    "session_deletions": len(DELETING_SESSIONS),
                    "goals_reconfiguration": int(CODEX_GOALS_RECONFIGURING),
                    "managed_restart": int(managed_server_restart_blocks_work()),
                })
                if action is None:
                    return SERVER.execution_maintenance.status(blockers)
                return SERVER.execution_maintenance.apply(
                    action, operation_id, lease_id, lifetime_seconds, blockers,
                )


def official_server_release_tree() -> bool:
    """Prove an installed runtime, including a worker retained across API updates."""

    configured = str(os.environ.get("HYADEX_SERVER_INSTALL_DIR") or "").strip()
    if not configured:
        return False
    install_root = Path(configured).expanduser()
    try:
        current = (install_root / "current").resolve(strict=True)
        if SERVER.execution_maintenance is not None:
            from hyadex.ops.execution.install import active_worker_release

            # Only the owned installation manifest can authorize the older
            # execution runtime once the public gateway advances. Service
            # ownership must still be proven separately below.
            retained = active_worker_release(install_root)
            if retained is not None:
                return retained == SERVER_ROOT
    except (OSError, RuntimeError, ValueError):
        return False
    return current == SERVER_ROOT


def macos_launchd_owns_current_process() -> bool | None:
    """Prove that the official launchd job owns this exact process.

    Returns ``None`` when the probe could not decide (launchctl timed out).
    """

    if sys.platform != "darwin" or not Path("/bin/launchctl").is_file():
        return False
    try:
        result = subprocess.run(
            [
                "/bin/launchctl",
                "print",
                f"gui/{os.getuid()}/{server_instances.launchd_label(SERVER_INSTANCE_NAME)}",
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        # A slow launchctl under host load is not evidence that the job does
        # not own this process. Report "unknown" so a prior positive proof
        # for this immutable PID keeps standing instead of turning the
        # emergency restart control into a 503.
        logger.warning("launchctl ownership probe timed out; ownership unknown")
        return None
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    launchd_pids = re.findall(
        r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$",
        str(result.stdout or ""),
    )
    return launchd_pids == [str(os.getpid())]


def detect_managed_server_service_kind() -> str | None:
    """Prove that an official user service will relaunch this process."""

    if not official_server_release_tree():
        return None
    if sys.platform.startswith("linux"):
        return "systemd-user" if hyadex_server_systemd_cgroup() else None
    if sys.platform == "darwin":
        owned = macos_launchd_owns_current_process()
        if owned is None:
            # Unknown: fall back to the cached positive proof, if any.
            return MANAGED_SERVER_SERVICE_KIND_CACHE
        return "launch-agent" if owned else None
    return None


def managed_server_service_kind() -> str | None:
    """Cache only positive ownership proofs for this immutable process PID."""

    global MANAGED_SERVER_SERVICE_KIND_CACHE
    if MANAGED_SERVER_SERVICE_KIND_CACHE is not None:
        return MANAGED_SERVER_SERVICE_KIND_CACHE
    detected = detect_managed_server_service_kind()
    if detected is not None:
        MANAGED_SERVER_SERVICE_KIND_CACHE = detected
    return detected


def server_restart_capability(
    blocker_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reconcile_stale_server_restart_acceptance()
    if not AGENT_TOKEN:
        capability = {
            "available": False,
            "required": False,
            "message": "Server restart requires authenticated HyadexServer mode.",
            "action": "Install or configure HyadexServer with an access token.",
            "version": 2,
            "force_restart": False,
        }
        if blocker_snapshot is not None:
            capability["blocker_snapshot"] = blocker_snapshot
        return capability
    if managed_server_service_kind() is None:
        capability = {
            "available": False,
            "required": False,
            "message": "Server restart is unavailable for an unmanaged server process.",
            "action": "Install HyadexServer as its supported user service, then reconnect.",
            "version": 2,
            "force_restart": False,
        }
        if blocker_snapshot is not None:
            capability["blocker_snapshot"] = blocker_snapshot
        return capability
    capability = {
        "available": True,
        "required": False,
        "message": "Authenticated managed server restart is available.",
        "action": None,
        "version": 2,
        "force_restart": True,
        "force_confirmation_required": True,
    }
    if blocker_snapshot is not None:
        capability["blocker_snapshot"] = blocker_snapshot
    return capability


def public_server_restart_status(
    status: dict[str, Any] | None = None,
    *,
    blocker_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = (
        status
        if status is not None
        else reconcile_stale_server_restart_acceptance()
    )
    phase = str(current.get("phase") or "idle")
    if phase not in SERVER_RESTART_PHASES:
        phase = "idle"
    result: dict[str, Any] = {
        "phase": phase,
        "current_version": SERVER_VERSION,
        "server_identity": server_identity(),
        "server_instance_id": SERVER_INSTANCE_ID,
        "message": str(current.get("message") or (
            "HyadexServer is ready."
            if phase == "idle"
            else "HyadexServer restart status is available."
        ))[:500],
        "forced": current.get("_forced") is True,
    }
    if result["forced"]:
        raw_snapshot = current.get("_forced_work_snapshot")
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        result["interrupted_work"] = public_server_restart_work_counts(snapshot)
        raw_audit = current.get("_forced_audit")
        if isinstance(raw_audit, dict):
            result["forced_audit"] = public_forced_restart_audit(raw_audit)
    if blocker_snapshot is not None:
        result["blocker_snapshot"] = blocker_snapshot
    for name in (
        "request_id",
        "requested_at",
        "updated_at",
        "completed_at",
        "failed_at",
    ):
        value = str(current.get(name) or "").strip()
        if value:
            result[name] = value[:128]
    return result


def reconcile_server_restart_status_after_startup() -> dict[str, Any]:
    """Complete an accepted restart only after a different process starts."""

    status = read_server_restart_status()
    if str(status.get("phase") or "") not in SERVER_RESTART_ACTIVE_PHASES:
        return status
    source_instance_id = str(status.get("_source_instance_id") or "")
    if not source_instance_id:
        return write_server_restart_status(
            phase="failed",
            message="The previous server restart record was incomplete.",
            failed_at=utc_timestamp(),
        )
    if source_instance_id == SERVER_INSTANCE_ID:
        return status
    return write_server_restart_status(
        phase="complete",
        message="HyadexServer restarted successfully.",
        completed_at=utc_timestamp(),
    )


def public_server_restart_work_counts(snapshot: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "codex_goals_reconfiguring": (
            snapshot.get("codex_goals_reconfiguring") is True
        ),
        "tmux_server_in_service_cgroup": (
            snapshot.get("tmux_server_in_service_cgroup") is True
        ),
        "tmux_server_cgroup_unknown": (
            snapshot.get("tmux_server_cgroup_unknown") is True
        ),
    }
    for name in (
        "active_count",
        "restart_blocking_queued_count",
        "provider_background_count",
        "server_maintenance_count",
        "mutation_count",
        "deleting_session_count",
    ):
        value = snapshot.get(name)
        result[name] = (
            min(SERVER_RESTART_COUNT_LIMIT, max(0, value))
            if isinstance(value, int) and not isinstance(value, bool)
            else 0
        )
    return result


FORCED_RESTART_AUDIT_FLAGS = (
    "snapshot_degraded",
    "blockers_changed_after_confirmation",
    "blocker_revision_omitted",
    "safety_blockers_overridden",
    "cooldown_overridden",
    "superseded_pending_restart",
    "execution_maintenance_overridden",
)


def public_forced_restart_audit(audit: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded, boolean-only public view of a forced restart audit."""

    result: dict[str, Any] = {
        name: audit.get(name) is True for name in FORCED_RESTART_AUDIT_FLAGS
    }
    reasons = audit.get("snapshot_degraded_reasons")
    if isinstance(reasons, list):
        result["snapshot_degraded_reasons"] = [
            str(reason)[:64] for reason in reasons[:8] if str(reason).strip()
        ]
    return result


def server_restart_blocker_snapshot_locked(
    *,
    tmux_cgroup_state: dict[str, Any] | None = None,
    provider_background_work_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Snapshot restart blockers while admission locks are held by the caller."""

    explicit_stop_ids = explicit_stop_session_ids()
    maintenance_session_ids = sorted(
        str(session_id)
        for session_id in RUNS.maintenance | explicit_stop_ids
    )
    active_session_ids = sorted(
        str(session_id)
        for session_id in RUNS.busy
        - RUNS.maintenance
        - explicit_stop_ids
    )
    deleting_session_ids = sorted(str(session_id) for session_id in DELETING_SESSIONS)
    queued_tokens: list[tuple[str, str, int, str, bool, bool]] = []
    queued_count = 0
    for session_id, queue in sorted(
        RUNS.queued.items(),
        key=lambda entry: str(entry[0]),
    ):
        for index, item in enumerate(queue):
            durable = item.get("_durable") is True
            transitioning = item.get("_update_transitioning") is True
            if durable and not transitioning:
                continue
            queued_count += 1
            queued_tokens.append((
                "queued",
                str(session_id)[:240],
                index,
                str(item.get("queued_id") or "")[:240],
                durable,
                transitioning,
            ))
    for session_id, item in sorted(
        RUNS.run_now.items(),
        key=lambda entry: str(entry[0]),
    ):
        durable = item.get("_durable") is True
        transitioning = item.get("_update_transitioning") is True
        if durable and not transitioning:
            continue
        queued_count += 1
        queued_tokens.append((
            "run_now",
            str(session_id)[:240],
            0,
            str(item.get("queued_id") or "")[:240],
            durable,
            transitioning,
        ))
    duplicate_provider_labels = {
        *(f"active chat {session_id}" for session_id in active_session_ids),
        *(
            f"Codex maintenance for {session_id}"
            for session_id in maintenance_session_ids
        ),
    }
    provider_work_labels = [
        str(label)[:200]
        for label in (
            provider_background_work_labels
            if provider_background_work_labels is not None
            else active_provider_background_work_labels()
        )
        if str(label) not in duplicate_provider_labels
    ][:PROVIDER_WORK_LABEL_LIMIT]
    live_mutation_ids = live_unsafe_http_mutation_ids_locked()
    mutation_count = len(live_mutation_ids)
    codex_goals_reconfiguring = bool(CODEX_GOALS_RECONFIGURING)
    tmux_state = (
        tmux_cgroup_state
        if isinstance(tmux_cgroup_state, dict)
        else server_restart_tmux_cgroup_state()
    )
    tmux_server_in_service_cgroup = (
        tmux_state.get("tmux_server_in_service_cgroup") is True
    )
    tmux_server_cgroup_unknown = (
        tmux_state.get("tmux_server_cgroup_unknown") is True
    )
    active_work_tokens: list[tuple[str, str, str, str, str]] = []
    for session_id in active_session_ids:
        current = RUNS.current_turns.get(session_id) or {}
        active = RUNS.active.get(session_id) or {}
        active_work_tokens.append((
            session_id,
            str(current.get("_server_restart_admission_id") or "")[:128],
            str(
                current.get("codex_control_reservation_id")
                or active.get("codex_control_reservation_id")
                or ""
            )[:128],
            str(current.get("run_id") or "")[:240],
            str(active.get("run_id") or "")[:240],
        ))
    digest_payload = {
        "version": SERVER_RESTART_BLOCKER_SNAPSHOT_VERSION,
        "active_session_ids": active_session_ids,
        # These private tokens make a force confirmation stale when different
        # work replaces the reviewed run in the same chat. They are hashed into
        # the revision only and are never returned by the public snapshot.
        "active_work_tokens": active_work_tokens,
        "maintenance_session_ids": maintenance_session_ids,
        "queued_tokens": queued_tokens,
        "provider_work_labels": provider_work_labels,
        "mutation_count": mutation_count,
        # IDs distinguish replacement requests in the private revision without
        # exposing request identity through the public restart snapshot.
        "mutation_ids": live_mutation_ids,
        "deleting_session_ids": deleting_session_ids,
        "codex_goals_reconfiguring": codex_goals_reconfiguring,
        "tmux_server": {
            "in_service_cgroup": tmux_server_in_service_cgroup,
            "cgroup_unknown": tmux_server_cgroup_unknown,
            "pid": tmux_state.get("_tmux_server_pid"),
            "cgroup_paths": list(
                tmux_state.get("_tmux_server_cgroup_paths") or ()
            ),
            "service_cgroup": tmux_state.get("_server_service_cgroup"),
            "inspection": str(
                tmux_state.get("_tmux_server_inspection") or "unknown"
            )[:160],
            "probe_revision": tmux_state.get("_tmux_server_probe_revision"),
        },
    }
    revision = hashlib.sha256(json.dumps(
        digest_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    counts = public_server_restart_work_counts({
        "active_count": len(active_session_ids),
        "restart_blocking_queued_count": queued_count,
        "provider_background_count": len(provider_work_labels),
        "server_maintenance_count": len(maintenance_session_ids),
        "mutation_count": mutation_count,
        "deleting_session_count": len(deleting_session_ids),
        "codex_goals_reconfiguring": codex_goals_reconfiguring,
        "tmux_server_in_service_cgroup": tmux_server_in_service_cgroup,
        "tmux_server_cgroup_unknown": tmux_server_cgroup_unknown,
    })
    forceable_blockers = any(
        counts[name] > 0
        for name in (
            "active_count",
            "restart_blocking_queued_count",
            "provider_background_count",
        )
    ) or counts["tmux_server_in_service_cgroup"] is True or (
        counts["tmux_server_cgroup_unknown"] is True
    )
    safety_blockers = any(
        counts[name] > 0
        for name in (
            "server_maintenance_count",
            "mutation_count",
            "deleting_session_count",
        )
    ) or counts["codex_goals_reconfiguring"] is True
    return {
        "version": SERVER_RESTART_BLOCKER_SNAPSHOT_VERSION,
        "revision": revision,
        **counts,
        "has_forceable_blockers": forceable_blockers,
        "has_safety_blockers": safety_blockers,
        "has_blockers": forceable_blockers or safety_blockers,
        # A forced restart overrides every blocker above (see
        # accept_forced_server_restart). Clients must never disable their
        # emergency restart control based on the counts in this snapshot.
        "force_restart_available": True,
    }


async def current_server_restart_blocker_snapshot(
    *,
    lock_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Snapshot restart blockers, degrading instead of hanging on wedged locks.

    ``lock_timeout_seconds`` defaults to
    ``SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS``. When any admission lock or
    the tmux probe cannot be obtained in time the snapshot is still computed
    from the current in-memory state (the loop is single-threaded, so the
    read is consistent) and marked ``snapshot_degraded`` so clients can show
    that the counts are best-effort.
    """

    timeout = (
        SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS
        if lock_timeout_seconds is None
        else float(lock_timeout_seconds)
    )
    provider_work_snapshot = await prepare_provider_background_work_snapshot(
        timeout_seconds=timeout,
    )
    degraded: list[str] = []
    async with bounded_lock(RUNS.active_lock, timeout) as active_held:
        if not active_held:
            degraded.append("active_lock")
        async with bounded_lock(RUNS.queue_lock, timeout) as queue_held:
            if not queue_held:
                degraded.append("queue_lock")
            async with bounded_lock(
                UNSAFE_HTTP_MUTATION_ADMISSION_LOCK,
                timeout,
            ) as mutation_held:
                if not mutation_held:
                    degraded.append("mutation_admission_lock")
                tmux_cgroup_state, probe_timed_out = (
                    await bounded_restart_tmux_cgroup_state(
                        SERVER_RESTART_FORCE_PROBE_TIMEOUT_SECONDS
                    )
                )
                if probe_timed_out:
                    degraded.append("tmux_probe")
                provider_work_labels = (
                    provider_background_work_labels_from_snapshot(
                        provider_work_snapshot
                    )
                )
                snapshot = server_restart_blocker_snapshot_locked(
                    tmux_cgroup_state=tmux_cgroup_state,
                    provider_background_work_labels=provider_work_labels,
                )
    if (
        provider_work_snapshot.get("consistent") is not True
        or provider_work_snapshot.get("stale") is True
    ):
        degraded.append("provider_scan")
    if degraded:
        snapshot["snapshot_degraded"] = True
        snapshot["snapshot_degraded_reasons"] = degraded
    return snapshot


def server_restart_request_fingerprint(body: ServerRestartRequest) -> str:
    """Bind an idempotency key to one exact restart target and force mode."""

    canonical = json.dumps(
        {
            "contract": 3,
            "expected_server_identity": body.expected_server_identity,
            "expected_server_instance_id": body.expected_server_instance_id,
            "forced": body.force is True and body.force_confirmed is True,
            "expected_blocker_revision": body.expected_blocker_revision,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def server_restart_work_message(
    active_count: int,
    queued_count: int,
    provider_work_count: int,
    mutation_count: int,
    *,
    deleting_count: int = 0,
    codex_goals_reconfiguring: bool = False,
    tmux_server_in_service_cgroup: bool = False,
    tmux_server_cgroup_unknown: bool = False,
) -> str:
    parts: list[str] = []
    if active_count:
        parts.append(
            f"{active_count} active agent run{'s' if active_count != 1 else ''}"
        )
    if queued_count:
        parts.append(
            f"{queued_count} restart-blocking queued turn{'s' if queued_count != 1 else ''}"
        )
    if provider_work_count:
        parts.append(
            f"{provider_work_count} provider background task{'s' if provider_work_count != 1 else ''}"
        )
    if mutation_count:
        parts.append(
            f"{mutation_count} in-flight server change{'s' if mutation_count != 1 else ''}"
        )
    if deleting_count:
        parts.append(
            f"{deleting_count} session deletion{'s' if deleting_count != 1 else ''}"
        )
    if codex_goals_reconfiguring:
        parts.append("Codex goals reconfiguration")
    if tmux_server_in_service_cgroup:
        parts.append(
            "a persistent tmux server inside hyadex-server.service"
        )
    if tmux_server_cgroup_unknown:
        parts.append("an unverified persistent tmux server cgroup")
    detail = " and ".join(parts) or "server work"
    return f"HyadexServer cannot restart while {detail} remains active."


def server_restart_safety_work_message(
    maintenance_count: int,
    mutation_count: int,
    deleting_count: int,
    codex_goals_reconfiguring: bool,
) -> str:
    parts: list[str] = []
    if maintenance_count:
        parts.append(
            f"{maintenance_count} server maintenance operation"
            f"{'s' if maintenance_count != 1 else ''}"
        )
    if mutation_count:
        parts.append(
            f"{mutation_count} in-flight server change"
            f"{'s' if mutation_count != 1 else ''}"
        )
    if deleting_count:
        parts.append(
            f"{deleting_count} session deletion"
            f"{'s' if deleting_count != 1 else ''}"
        )
    if codex_goals_reconfiguring:
        parts.append("Codex goals reconfiguration")
    detail = " and ".join(parts) or "safety-critical server work"
    return (
        f"HyadexServer cannot restart while {detail} remains active; "
        "a cooperative restart waits for it; force restart overrides it."
    )


def force_kill_managed_server_after_deadline(
    request_id: str,
    pid: int,
    delay_seconds: float | None = None,
) -> None:
    """Guarantee a managed restart cannot drain forever.

    This watchdog runs inside the process that was asked to restart. If that
    process is still alive when the deadline passes, graceful shutdown has
    hung (background tasks, provider children, wedged locks), so the process
    is killed unconditionally. Forced restarts use the short
    ``SERVER_RESTART_FORCE_KILL_DELAY_SECONDS``; cooperative restarts pass the
    longer ``SERVER_RESTART_GRACEFUL_KILL_DELAY_SECONDS`` so a healthy drain
    can finish first. It deliberately does not re-read the restart journal: a
    concurrent status write, a superseding request, or a journal that could
    not be persisted must not be able to cancel an emergency restart.

    Registered provider children are killed by process group first: they run
    in their own session, so SIGKILL of this pid alone would orphan them.
    """

    time.sleep(
        SERVER_RESTART_FORCE_KILL_DELAY_SECONDS
        if delay_seconds is None
        else max(0.0, float(delay_seconds))
    )
    if pid != os.getpid():
        # Never signal a process other than the one that armed this watchdog.
        return
    # The journal write is best effort and must not delay the kill: a signal
    # worker stalled on filesystem I/O may hold this lock indefinitely.
    journal_locked = SERVER_RESTART_SIGNAL_LOCK.acquire(timeout=2.0)
    try:
        if journal_locked:
            with suppress(Exception):
                write_server_restart_status(
                    phase="signaling",
                    request_id=request_id,
                    message=(
                        "HyadexServer did not stop after SIGTERM; forcing the "
                        "managed process to exit so its user service relaunches."
                    ),
                )
    finally:
        if journal_locked:
            SERVER_RESTART_SIGNAL_LOCK.release()
    with suppress(Exception):
        kill_registered_provider_children()
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def signal_managed_server_restart(request_id: str) -> None:
    """Signal this process only after FastAPI has sent the accepted response."""

    time.sleep(SERVER_RESTART_SIGNAL_DELAY_SECONDS)
    with SERVER_RESTART_SIGNAL_LOCK:
        status = read_server_restart_status()
        if (
            str(status.get("request_id") or "") != request_id
            or str(status.get("phase") or "") != "accepted"
            or str(status.get("_source_instance_id") or "")
            != SERVER_INSTANCE_ID
        ):
            return
        if managed_server_service_kind() is None:
            write_server_restart_status(
                phase="failed",
                message="Managed service ownership changed before restart.",
                failed_at=utc_timestamp(),
            )
            return
        write_server_restart_status(
            phase="signaling",
            message=(
                "HyadexServer is restarting; reconnecting clients may briefly "
                "go offline."
            ),
        )
        try:
            pid = os.getpid()
            # Always arm the watchdog. A cooperative drain that wedges (uvicorn
            # waiting on tasks/websockets, a Codex child ignoring SIGTERM)
            # would otherwise leave the port closed forever; only the deadline
            # differs between a forced and a cooperative restart.
            threading.Thread(
                target=force_kill_managed_server_after_deadline,
                args=(
                    request_id,
                    pid,
                    (
                        SERVER_RESTART_FORCE_KILL_DELAY_SECONDS
                        if status.get("_forced") is True
                        else SERVER_RESTART_GRACEFUL_KILL_DELAY_SECONDS
                    ),
                ),
                daemon=True,
                name="hyadex-server-force-restart",
            ).start()
            os.kill(pid, signal.SIGTERM)
        except Exception:
            write_server_restart_status(
                phase="failed",
                message=(
                    "HyadexServer could not signal its managed process to restart."
                ),
                failed_at=utc_timestamp(),
            )


def server_lifecycle_blocker_counts(
    active_session_ids: list[str],
    queued_turn_count: int,
    provider_work_labels: list[str] | None = None,
    mutation_count: int = 0,
) -> dict[str, int]:
    """Return the bounded public blocker summary for a lifecycle operation."""

    return {
        "active_runs": max(0, len(active_session_ids)),
        "queued_turns": max(0, int(queued_turn_count)),
        "provider_background_tasks": max(
            0,
            len(provider_work_labels or []),
        ),
        "in_flight_server_changes": max(0, int(mutation_count)),
    }


def restart_blocking_queued_turn_count_locked() -> int:
    """Count only queue entries that are not yet restart-durable.

    Callers hold QUEUE_LOCK. Durable turns are reconstructed from events after
    replacement, so treating them as active work can defer worker retirement forever
    after their parent was explicitly stopped.
    """

    return sum(
        item.get("_durable") is False
        or item.get("_update_transitioning") is True
        for queue in RUNS.queued.values()
        for item in queue
    ) + sum(
        item.get("_durable") is False
        or item.get("_update_transitioning") is True
        for item in RUNS.run_now.values()
    )


async def bounded_shutdown_phase(
    name: str,
    awaitable: Any,
    *,
    timeout: float | None = None,
) -> bool:
    """Run one lifespan shutdown phase under a deadline; never raise.

    A single wedged join (a provider that ignores SIGTERM, a lock held by a
    dead task) must not prevent the phases after it - in particular provider
    teardown - from running. Returns True when the phase completed in time.
    """

    budget = (
        SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS
        if timeout is None
        else max(0.0, float(timeout))
    )
    try:
        phase_task = asyncio.ensure_future(awaitable)
    except Exception as exc:
        logger.warning(
            "shutdown phase %s failed error=%s",
            name,
            concise_error_message(exc),
        )
        return False

    def consume_shutdown_straggler(completed: asyncio.Task[Any]) -> None:
        SERVER_SHUTDOWN_STRAGGLERS.discard(completed)
        with suppress(BaseException):
            completed.result()

    try:
        done, _pending = await asyncio.wait({phase_task}, timeout=budget)
    except asyncio.CancelledError:
        phase_task.cancel()
        SERVER_SHUTDOWN_STRAGGLERS.add(phase_task)
        phase_task.add_done_callback(consume_shutdown_straggler)
        # Matches the previous per-await suppress: a cancelled join never
        # aborts the remaining teardown.
        return False
    if phase_task not in done:
        # asyncio.wait returns at the deadline without waiting for cancellation
        # acknowledgement. A provider coroutine may suppress CancelledError;
        # retain and consume it later, but never let that defeat this bound or
        # starve the remaining shutdown phases.
        phase_task.cancel()
        SERVER_SHUTDOWN_STRAGGLERS.add(phase_task)
        phase_task.add_done_callback(consume_shutdown_straggler)
        logger.warning(
            "shutdown phase %s did not finish within %.0fs; continuing",
            name,
            budget,
        )
        return False
    try:
        phase_task.result()
    except asyncio.CancelledError:
        return False
    except Exception as exc:
        logger.warning(
            "shutdown phase %s failed error=%s",
            name,
            concise_error_message(exc),
        )
        return False
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    from hyadex.ops.execution.ownership import acquire_state_ownership

    validate_listener_auth()
    # Both the maintained legacy entrypoint and the split worker pass here.
    # Retain the descriptor until process exit: bounded shutdown can leave
    # finalizers alive after this lifespan returns.
    acquire_state_ownership(STATE_DIR)
    install_root = os.environ.get("HYADEX_SERVER_INSTALL_DIR")
    if SERVER.execution_maintenance is None and install_root:
        # Never admit work over a journal the retired in-app updater left.
        from hyadex.ops.execution.install import refuse_retired_activation
        refuse_retired_activation(Path(install_root).expanduser().resolve())
    if SERVER.state_process_lock is None:
        SERVER.state_process_lock = server_instances.acquire_state_lock(STATE_DIR)
    SERVER.shutting_down = False
    await STORE.load()
    # Prime the managed-service ownership proof once, off the loop, so the
    # restart controls never depend on a fresh launchctl/cgroup probe later.
    with suppress(Exception):
        await asyncio.to_thread(managed_server_service_kind)
    await JOBS.load()
    await GROUPS.load()
    await AUTOPILOT.load()
    ensure_dirs()
    # A forced kill of the previous instance cannot reach provider children in
    # their own sessions; reap anything the registry says was left behind.
    with suppress(Exception):
        await asyncio.to_thread(sweep_orphaned_provider_children)
    await asyncio.to_thread(scrub_tmux_global_secret_environment)
    startup_restart_status = read_server_restart_status()
    forced_restart_request_id = (
        str(startup_restart_status.get("request_id") or "")[:128]
        if (
            str(startup_restart_status.get("phase") or "")
            in SERVER_RESTART_ACTIVE_PHASES
            and startup_restart_status.get("_forced") is True
            and str(startup_restart_status.get("_source_instance_id") or "")
            not in {"", SERVER_INSTANCE_ID}
        )
        else ""
    )
    reconcile_server_restart_status_after_startup()
    clear_imported_active_runs()
    abandoned_compaction_count = (
        await recover_abandoned_codex_compactions_after_start(
            forced_restart_request_id=(forced_restart_request_id or None),
        )
    )
    abandoned_turn_count = await recover_abandoned_turns_after_start(
        forced_restart_request_id=(forced_restart_request_id or None),
    )
    # Retire old update reservations without losing their due job occurrences.
    await JOBS.resume_legacy_update_parked()
    removed_authority_files = (
        await purge_provider_authority_files_after_restart()
    )
    recovered_scheduled_run_count = (
        await JOBS.reconcile_admitted_runs_after_restart()
    )
    JOBS.start_scheduler()
    async def recover_queue_before_admission() -> tuple[int, int]:
        succeeded = False
        try:
            result = await recover_queued_turns_after_start()
            succeeded = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the failed task installed as a fail-closed admission fence;
            # health and administrative recovery remain available.
            logger.exception(
                "queued turn startup recovery failed; turn admission remains closed"
            )
            raise
        finally:
            owner = asyncio.current_task()
            if succeeded and SERVER.queue_recovery_task is owner:
                SERVER.queue_recovery_task = None

    queue_recovery_task = asyncio.create_task(
        recover_queue_before_admission(),
        name="queued-turn-startup-recovery",
    )
    SERVER.queue_recovery_task = queue_recovery_task
    startup_abandoned_fork_threads = set(ABANDONED_FORK_PROVIDER_THREADS)
    abandoned_fork_cleanup_task = asyncio.create_task(
        cleanup_abandoned_fork_provider_threads(startup_abandoned_fork_threads)
    )
    host_monitor_task = asyncio.create_task(host_monitor_loop())
    queue_checkpoint_task = asyncio.create_task(queue_checkpoint_loop())
    history_search_task = asyncio.create_task(history_search_index_loop())
    async def recover_groups_and_autopilot() -> None:
        # Autopilot continues from the group rounds its runs were waiting for.
        await GROUPS.recover()
        await AUTOPILOT.recover()

    group_recovery_task = asyncio.create_task(recover_groups_and_autopilot())
    runtime_probe_task = asyncio.create_task(asyncio.to_thread(refresh_runtime_diagnostics, force=True))
    if CONFIG_ENV_APPLIED:
        # Names only - these can be provider credentials.
        logger.info(
            "loaded %d config-file environment variable(s) from %s: %s",
            len(CONFIG_ENV_APPLIED),
            CONFIG_ENV_FILE,
            ", ".join(CONFIG_ENV_APPLIED),
        )
    logger.info(
        "agent server ready state=%s sessions=%d jobs=%d recovered_scheduled_runs=%d abandoned_turns=%d abandoned_compactions=%d authority_removed=%d queue_recovery=background",
        STATE_DIR,
        len(STORE.sessions),
        len(JOBS.jobs),
        recovered_scheduled_run_count,
        abandoned_turn_count,
        abandoned_compaction_count,
        removed_authority_files,
    )
    try:
        yield
    finally:
        SERVER.shutting_down = True
        await bounded_shutdown_phase("side-questions", asyncio.gather(
            SIDE_QUESTIONS.close(), close_generated_session_titles(),
        ))
        # Every phase below is bounded by SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS
        # (see bounded_shutdown_phase) so one stuck join cannot starve the
        # provider teardown that follows it. The order is load-bearing.
        #
        # The scheduler can admit provider work and mutate both registries.
        # Retire it before flushing state or closing either provider manager.
        await bounded_shutdown_phase(
            "job-scheduler",
            JOBS.stop_scheduler(),
        )
        # Land the coalesced sessions.json write first: a restart watchdog can
        # kill later phases, and the trailing flush covers marks they make.
        await bounded_shutdown_phase(
            "sessions-flush-early",
            STORE.flush_pending_save(),
        )
        explicit_stop_tasks = tuple({
            *RUNS.explicit_stops.values(),
            *RUNS.detached_stop_tasks,
        })
        for explicit_stop_task in explicit_stop_tasks:
            explicit_stop_task.cancel()
        await bounded_shutdown_phase(
            "explicit-stop-operations",
            join_cancelled_tasks(*explicit_stop_tasks),
        )
        health_queue_reconcile_task = HEALTH_QUEUE_RECONCILE_STATE.get("task")
        if isinstance(health_queue_reconcile_task, asyncio.Task):
            health_queue_reconcile_task.cancel()
        claude_stop_fence_retry_tasks = tuple(
            CLAUDE_STOP_FENCE_RETRY_TASKS.values()
        )
        claude_stop_fence_attempt_tasks = tuple(
            task
            for tasks in CLAUDE_STOP_FENCE_ATTEMPT_TASKS.values()
            for task in tasks
        )
        for claude_stop_fence_retry_task in claude_stop_fence_retry_tasks:
            claude_stop_fence_retry_task.cancel()
        for claude_stop_fence_attempt_task in claude_stop_fence_attempt_tasks:
            claude_stop_fence_attempt_task.cancel()
        queue_recovery_task.cancel()
        queue_checkpoint_task.cancel()
        history_tasks = (*HISTORY_MAINTENANCE_TASKS.values(), *(item[1] for item in TURN_RECEIPT_TASKS.values()))
        for task in history_tasks:
            task.cancel()
        await bounded_shutdown_phase(
            "startup-reconciliation",
            join_cancelled_tasks(
                queue_recovery_task,
                queue_checkpoint_task,
                *history_tasks,
                *(
                    (health_queue_reconcile_task,)
                    if isinstance(health_queue_reconcile_task, asyncio.Task)
                    else ()
                ),
                *claude_stop_fence_retry_tasks,
                *claude_stop_fence_attempt_tasks,
            ),
        )
        if SERVER.queue_recovery_task is queue_recovery_task:
            SERVER.queue_recovery_task = None
        await bounded_shutdown_phase(
            "terminal-attachments",
            TERMINAL_ATTACHMENTS.close_admission_and_all(permanent=True),
        )
        await bounded_shutdown_phase("claude-sdk", close_claude_sdk_manager())
        await bounded_shutdown_phase(
            "codex-app-server",
            close_codex_app_server_manager(),
        )
        abandoned_fork_cleanup_task.cancel()
        host_monitor_task.cancel()
        history_search_task.cancel()
        runtime_probe_task.cancel()
        group_recovery_task.cancel()
        await bounded_shutdown_phase(
            "background-loops",
            join_cancelled_tasks(
                abandoned_fork_cleanup_task,
                host_monitor_task,
                history_search_task,
                runtime_probe_task,
                group_recovery_task,
            ),
        )
        # Every session mutator above is joined; land the last coalesced
        # sessions.json write before the loop stops.
        await bounded_shutdown_phase(
            "sessions-flush",
            STORE.flush_pending_save(),
        )




def require_server_restart_control(request: Request) -> None:
    if server_restart_browser_request_forbidden(request):
        raise HTTPException(status_code=403, detail="forbidden")
    if not AGENT_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=server_restart_error_detail(
                "server_restart_auth_required",
                "Server restart requires authenticated HyadexServer mode.",
                action="Install or configure HyadexServer with an access token.",
            ),
        )
    if not request_exact_restart_header_authorized(request):
        raise HTTPException(
            status_code=401,
            detail=server_restart_error_detail(
                "server_restart_unauthorized",
                "Server restart requires an access token in an authorization header.",
            ),
        )
    # Cached positive proof: the managed-service probe runs a subprocess and
    # must not be repeated (or time out into a refusal) on the emergency path.
    if managed_server_service_kind() is None:
        raise HTTPException(
            status_code=503,
            detail=server_restart_error_detail(
                "server_restart_unmanaged",
                "Server restart is unavailable for an unmanaged server process.",
                action="Install HyadexServer as its supported user service, then reconnect.",
            ),
        )


def signal_forced_server_restart(request_id: str) -> None:
    """Signal an emergency restart without depending on the restart journal.

    Runs on a dedicated thread after the accepted response has had time to
    flush. Unlike the cooperative path, a journal that cannot be read or
    written, a superseding record, or a wedged event loop cannot stop this
    worker: it arms the hard-kill watchdog first and then sends SIGTERM to
    this process so the managed user service relaunches it.
    """

    time.sleep(SERVER_RESTART_SIGNAL_DELAY_SECONDS)
    pid = os.getpid()
    with SERVER_RESTART_SIGNAL_LOCK:
        try:
            status = read_server_restart_status()
            if (
                str(status.get("request_id") or "") == request_id
                and str(status.get("phase") or "") == "signaling"
            ):
                # A duplicate worker for the same request already signaled;
                # its watchdog owns the hard kill.
                return
        except Exception:
            status = {}
        with suppress(Exception):
            write_server_restart_status(
                phase="signaling",
                request_id=request_id,
                _source_instance_id=SERVER_INSTANCE_ID,
                _forced=True,
                message=(
                    "HyadexServer is force restarting; reconnecting clients "
                    "will briefly go offline."
                ),
            )
        threading.Thread(
            target=force_kill_managed_server_after_deadline,
            args=(request_id, pid),
            daemon=True,
            name="hyadex-server-force-restart",
        ).start()
        with suppress(Exception):
            os.kill(pid, signal.SIGTERM)


def start_forced_server_restart_signal_thread(request_id: str) -> None:
    threading.Thread(
        target=signal_forced_server_restart,
        args=(request_id,),
        daemon=True,
        name="hyadex-server-forced-restart-signal",
    ).start()


async def accept_forced_server_restart(
    body: ServerRestartRequest,
    *,
    request_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    """Accept an audited emergency restart.

    Guarantees for ``force: true`` + ``force_confirmed: true``:

    * Only header authentication, the managed-service proof (both already
      checked by ``require_server_restart_control``) and the exact target
      identity/instance are required.
    * Everything the cooperative path refuses on is audited instead: the
      restart cooldown, a pending or stale restart record, worker maintenance,
      safety-critical work (session maintenance, deletions, HTTP mutations,
      goal reconfiguration), and a stale or omitted blocker revision.
    * No lock or probe is awaited beyond its bounded timeout. An emergency
      restart degrades a wedged lock into an audited snapshot.
    * The journal is best effort: if it cannot be written the restart still
      proceeds, because the journal exists to explain the restart, not to
      authorize it.
    * SIGTERM is sent from a dedicated thread and a hard-kill watchdog
      guarantees the process exits even when graceful shutdown hangs.
    """

    if (
        body.expected_server_identity != server_identity()
        or body.expected_server_instance_id != SERVER_INSTANCE_ID
    ):
        raise HTTPException(
            status_code=409,
            detail=server_restart_error_detail(
                "server_restart_target_changed",
                "The connected HyadexServer instance changed before restart confirmation.",
                action="Refresh the server connection and confirm the restart again.",
                retryable=True,
            ),
        )

    audit: dict[str, Any] = {}
    degraded_reasons: list[str] = []
    async with bounded_lock(
        SERVER_LIFECYCLE_OPERATION_LOCK,
        SERVER_RESTART_FORCE_LOCK_TIMEOUT_SECONDS,
    ) as operation_lock_held:
        if not operation_lock_held:
            degraded_reasons.append("lifecycle_operation_lock")
        try:
            restart_status = reconcile_stale_server_restart_acceptance()
        except Exception:
            restart_status = {"phase": "idle"}
            degraded_reasons.append("restart_journal_unreadable")
        if str(restart_status.get("request_id") or "") == request_id:
            accepted_fingerprint = str(
                restart_status.get("_request_fingerprint") or ""
            )
            if not accepted_fingerprint or not hmac.compare_digest(
                accepted_fingerprint,
                request_fingerprint,
            ):
                raise HTTPException(
                    status_code=409,
                    detail=server_restart_error_detail(
                        "server_restart_request_changed",
                        "The restart request ID was already used with different confirmation details.",
                        action=(
                            "Refresh the server connection and use a new request ID "
                            "for a newly confirmed restart."
                        ),
                    ),
                )
            if (
                str(restart_status.get("phase") or "") == "accepted"
                and str(restart_status.get("_source_instance_id") or "")
                == SERVER_INSTANCE_ID
            ):
                start_forced_server_restart_signal_thread(request_id)
            return public_server_restart_status(restart_status)


        phase = str(restart_status.get("phase") or "")
        if phase in SERVER_RESTART_ACTIVE_PHASES:
            audit["superseded_pending_restart"] = True
        elif (
            phase in {"complete", "failed"}
            and server_restart_status_age_seconds(restart_status)
            < SERVER_RESTART_COOLDOWN_SECONDS
        ):
            audit["cooldown_overridden"] = True
        if execution_maintenance_blocks_work():
            audit["execution_maintenance_overridden"] = True

        snapshot = await current_server_restart_blocker_snapshot(
            lock_timeout_seconds=SERVER_RESTART_FORCE_LOCK_TIMEOUT_SECONDS,
        )
        if snapshot.get("snapshot_degraded") is True:
            degraded_reasons.extend(
                str(reason)
                for reason in snapshot.get("snapshot_degraded_reasons") or ()
            )
        if body.expected_blocker_revision is None:
            audit["blocker_revision_omitted"] = True
        elif not hmac.compare_digest(
            str(body.expected_blocker_revision),
            str(snapshot.get("revision") or ""),
        ):
            audit["blockers_changed_after_confirmation"] = True
        if snapshot.get("has_safety_blockers") is True:
            audit["safety_blockers_overridden"] = True
        if degraded_reasons:
            audit["snapshot_degraded"] = True
            audit["snapshot_degraded_reasons"] = degraded_reasons

        now = utc_timestamp()
        journal_changes: dict[str, Any] = dict(
            phase="accepted",
            request_id=request_id,
            _source_instance_id=SERVER_INSTANCE_ID,
            _request_fingerprint=request_fingerprint,
            _forced=True,
            _forced_work_snapshot=public_server_restart_work_counts(snapshot),
            _forced_audit=audit,
            message=(
                "HyadexServer accepted the forced restart; any active work "
                "will be interrupted while its user service relaunches."
            ),
            requested_at=now,
            completed_at=None,
            failed_at=None,
        )
        try:
            restart_status = write_server_restart_status(**journal_changes)
        except Exception as exc:
            logger.error(
                "forced restart journal could not be written; restarting anyway "
                "request_id=%s error_type=%s",
                request_id,
                type(exc).__name__,
            )
            restart_status = {**journal_changes, "updated_at": now}
    logger.warning(
        "forced server restart accepted request_id=%s audit=%s",
        request_id,
        json.dumps(public_forced_restart_audit(audit), sort_keys=True),
    )
    start_forced_server_restart_signal_thread(request_id)
    return public_server_restart_status(restart_status)


def log_server_restart_denial(
    body: ServerRestartRequest,
    exc: HTTPException,
) -> None:
    """Record why a restart was refused; clients rarely surface the detail."""

    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    snapshot = detail.get("blocker_snapshot")
    counts = (
        public_server_restart_work_counts(snapshot)
        if isinstance(snapshot, dict)
        else {}
    )
    logger.warning(
        "server restart denied status=%s code=%s forced=%s message=%s blockers=%s",
        exc.status_code,
        str(detail.get("code") or "")[:64],
        body.force is True and body.force_confirmed is True,
        str(detail.get("message") or "")[:200],
        json.dumps(counts, sort_keys=True),
    )


async def _restart_server_endpoint_impl(
    body: ServerRestartRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    require_server_restart_control(request)
    request_id = str(body.request_id)
    forced = body.force is True and body.force_confirmed is True
    request_fingerprint = server_restart_request_fingerprint(body)

    if forced:
        # Emergency path: never refuse or hang on server state. See
        # accept_forced_server_restart for the exact guarantees.
        return await accept_forced_server_restart(
            body,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
        )

    async with SERVER_LIFECYCLE_OPERATION_LOCK:
        restart_status = reconcile_stale_server_restart_acceptance()
        if str(restart_status.get("request_id") or "") == request_id:
            accepted_fingerprint = str(
                restart_status.get("_request_fingerprint") or ""
            )
            if not accepted_fingerprint or not hmac.compare_digest(
                accepted_fingerprint,
                request_fingerprint,
            ):
                raise HTTPException(
                    status_code=409,
                    detail=server_restart_error_detail(
                        "server_restart_request_changed",
                        "The restart request ID was already used with different confirmation details.",
                        action=(
                            "Refresh the server connection and use a new request ID "
                            "for a newly confirmed restart."
                        ),
                    ),
                )
            if (
                str(restart_status.get("phase") or "") == "accepted"
                and str(restart_status.get("_source_instance_id") or "")
                == SERVER_INSTANCE_ID
            ):
                background_tasks.add_task(
                    signal_managed_server_restart,
                    request_id,
                )
            return public_server_restart_status(restart_status)
        if managed_server_restart_blocks_work(restart_status):
            raise HTTPException(
                status_code=409,
                detail=server_restart_error_detail(
                    "server_restart_in_progress",
                    "Another HyadexServer restart is already in progress.",
                    action="Wait for the server to reconnect before retrying.",
                    retryable=True,
                ),
            )
        terminal_restart = str(restart_status.get("phase") or "") in {
            "complete",
            "failed",
        }
        cooldown_remaining = (
            max(
                0,
                math.ceil(
                    SERVER_RESTART_COOLDOWN_SECONDS
                    - server_restart_status_age_seconds(restart_status)
                ),
            )
            if terminal_restart
            else 0
        )
        if terminal_restart and cooldown_remaining > 0:
            raise HTTPException(
                status_code=409,
                detail=server_restart_error_detail(
                    "server_restart_cooldown",
                    "HyadexServer was restarted recently.",
                    action="Wait briefly before requesting another restart.",
                    retryable=True,
                    retry_after_seconds=cooldown_remaining,
                ),
            )

        current_server_identity = server_identity()
        if (
            body.expected_server_identity != current_server_identity
            or body.expected_server_instance_id != SERVER_INSTANCE_ID
        ):
            raise HTTPException(
                status_code=409,
                detail=server_restart_error_detail(
                    "server_restart_target_changed",
                    "The connected HyadexServer instance changed before restart confirmation.",
                    action="Refresh the server connection and confirm the restart again.",
                    retryable=True,
                ),
            )
        if managed_server_service_kind() is None:
            raise HTTPException(
                status_code=503,
                detail=server_restart_error_detail(
                    "server_restart_unmanaged",
                    "Managed service ownership changed before restart acceptance.",
                    action="Reconnect or reinstall the supported HyadexServer user service.",
                ),
            )
        if execution_maintenance_blocks_work():
            raise HTTPException(
                status_code=409,
                detail=server_restart_error_detail(
                    "execution_maintenance_in_progress",
                    "HyadexServer cannot restart while its execution worker is in maintenance.",
                    action="Wait for worker maintenance to finish, then retry.",
                    retryable=True,
                ),
            )

        # Folding a long Claude event history is disk/JSON work. Complete it
        # before closing global admission, then validate cheap file metadata
        # under the locks so a concurrent provider change fails closed.
        provider_work_snapshot = (
            await prepare_provider_background_work_snapshot()
        )
        async with RUNS.active_lock:
            async with RUNS.queue_lock:
                async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
                    # Recheck every fence while holding the same locks used by
                    # turn/queue and HTTP-mutation admission. Once the journal
                    # is accepted, no new work can enter before SIGTERM.
                    restart_status = reconcile_stale_server_restart_acceptance()
                    if managed_server_restart_blocks_work(restart_status):
                        raise HTTPException(
                            status_code=409,
                            detail=server_restart_error_detail(
                                "server_restart_in_progress",
                                "Another HyadexServer restart is already in progress.",
                                action="Wait for the server to reconnect before retrying.",
                                retryable=True,
                            ),
                        )
                    if execution_maintenance_blocks_work():
                        raise HTTPException(
                            status_code=409,
                            detail=server_restart_error_detail(
                                "execution_maintenance_in_progress",
                                "HyadexServer cannot restart while its execution worker is in maintenance.",
                                action="Wait for worker maintenance to finish, then retry.",
                                retryable=True,
                            ),
                        )
                    tmux_cgroup_state = await asyncio.to_thread(
                        server_restart_tmux_cgroup_state
                    )
                    provider_work_labels = (
                        provider_background_work_labels_from_snapshot(
                            provider_work_snapshot
                        )
                    )
                    blocker_snapshot = server_restart_blocker_snapshot_locked(
                        tmux_cgroup_state=tmux_cgroup_state,
                        provider_background_work_labels=provider_work_labels,
                    )
                    active_count = int(blocker_snapshot["active_count"])
                    queued_count = int(
                        blocker_snapshot["restart_blocking_queued_count"]
                    )
                    provider_count = int(
                        blocker_snapshot["provider_background_count"]
                    )
                    maintenance_count = int(
                        blocker_snapshot["server_maintenance_count"]
                    )
                    mutation_count = int(blocker_snapshot["mutation_count"])
                    deleting_count = int(
                        blocker_snapshot["deleting_session_count"]
                    )
                    codex_goals_reconfiguring = bool(
                        blocker_snapshot["codex_goals_reconfiguring"]
                    )
                    if forced and not hmac.compare_digest(
                        str(body.expected_blocker_revision or ""),
                        str(blocker_snapshot["revision"]),
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=server_restart_error_detail(
                                "server_restart_blockers_changed",
                                "Server work changed after force restart was confirmed.",
                                action=(
                                    "Review the refreshed blocker counts and confirm "
                                    "force restart again."
                                ),
                                retryable=True,
                                blocker_snapshot=blocker_snapshot,
                            ),
                        )
                    if blocker_snapshot["has_safety_blockers"]:
                        raise HTTPException(
                            status_code=409,
                            detail=server_restart_error_detail(
                                "server_restart_unsafe_busy",
                                server_restart_safety_work_message(
                                    maintenance_count,
                                    mutation_count,
                                    deleting_count,
                                    codex_goals_reconfiguring,
                                ),
                                action=(
                                    "Wait for safety-critical server work to finish, "
                                    "then refresh restart status."
                                ),
                                retryable=True,
                                server_maintenance_count=maintenance_count,
                                mutation_count=mutation_count,
                                deleting_session_count=deleting_count,
                                codex_goals_reconfiguring=(
                                    codex_goals_reconfiguring
                                ),
                                blocker_snapshot=blocker_snapshot,
                            ),
                        )
                    if blocker_snapshot["has_forceable_blockers"] and not forced:
                        raise HTTPException(
                            status_code=409,
                            detail=server_restart_error_detail(
                                "server_restart_busy",
                                server_restart_work_message(
                                    active_count,
                                    queued_count,
                                    provider_count,
                                    0,
                                    tmux_server_in_service_cgroup=bool(
                                        blocker_snapshot[
                                            "tmux_server_in_service_cgroup"
                                        ]
                                    ),
                                    tmux_server_cgroup_unknown=bool(
                                        blocker_snapshot[
                                            "tmux_server_cgroup_unknown"
                                        ]
                                    ),
                                ),
                                action="Wait for current work to finish, then retry.",
                                retryable=True,
                                active_count=active_count,
                                restart_blocking_queued_count=queued_count,
                                provider_background_count=provider_count,
                                tmux_server_in_service_cgroup=bool(
                                    blocker_snapshot[
                                        "tmux_server_in_service_cgroup"
                                    ]
                                ),
                                tmux_server_cgroup_unknown=bool(
                                    blocker_snapshot[
                                        "tmux_server_cgroup_unknown"
                                    ]
                                ),
                                blocker_snapshot=blocker_snapshot,
                            ),
                        )
                    now = utc_timestamp()
                    forced_work_snapshot = public_server_restart_work_counts(
                        blocker_snapshot
                    )
                    try:
                        restart_status = write_server_restart_status(
                            phase="accepted",
                            request_id=request_id,
                            _source_instance_id=SERVER_INSTANCE_ID,
                            _request_fingerprint=request_fingerprint,
                            _forced=forced,
                            _forced_work_snapshot=(
                                forced_work_snapshot if forced else None
                            ),
                            message=(
                                "HyadexServer accepted the forced restart; any active "
                                "work will be interrupted while its user service "
                                "relaunches."
                                if forced
                                else "HyadexServer accepted the restart and will briefly "
                                "disconnect while its user service relaunches."
                            ),
                            requested_at=now,
                            completed_at=None,
                            failed_at=None,
                        )
                    except BaseException:
                        raise

        background_tasks.add_task(signal_managed_server_restart, request_id)
        return public_server_restart_status(restart_status)


def uvicorn_graceful_shutdown_seconds() -> int:
    return max(1, int(configured_uvicorn_graceful_shutdown_seconds()))
