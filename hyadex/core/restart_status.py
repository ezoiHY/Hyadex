"""Durable managed-restart status."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
from typing import Any

from hyadex.core.config import SERVER_INSTANCE_ID, SERVER_RESTART_STATUS_FILE
from hyadex.core.util import utc_timestamp
from hyadex.store.files import atomic_json

SERVER_RESTART_PHASES = {"idle", "accepted", "signaling", "complete", "failed"}
SERVER_RESTART_ACTIVE_PHASES = {"accepted", "signaling"}


SERVER_RESTART_ACCEPTED_STALE_SECONDS = 10.0


MANAGED_SERVER_RESTART_ACTIVE_DETAIL = "HyadexServer is restarting"


SERVER_RESTART_SIGNAL_LOCK = threading.Lock()


def read_server_restart_status() -> dict[str, Any]:
    try:
        value = json.loads(SERVER_RESTART_STATUS_FILE.read_text())
        if not isinstance(value, dict):
            value = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}
    phase = str(value.get("phase") or "idle")
    value["phase"] = phase if phase in SERVER_RESTART_PHASES else "idle"
    return value


def write_server_restart_status(**changes: Any) -> dict[str, Any]:
    value = read_server_restart_status()
    value.update(changes)
    value["updated_at"] = utc_timestamp()
    atomic_json(SERVER_RESTART_STATUS_FILE, value)
    return value


def managed_server_restart_blocks_work(
    status: dict[str, Any] | None = None,
) -> bool:
    current = (
        status
        if status is not None
        else reconcile_stale_server_restart_acceptance()
    )
    return str(current.get("phase") or "") in SERVER_RESTART_ACTIVE_PHASES


def server_restart_status_age_seconds(status: dict[str, Any]) -> float:
    timestamp: datetime | None = None
    for name in ("completed_at", "failed_at", "updated_at", "requested_at"):
        try:
            candidate = datetime.fromisoformat(
                str(status.get(name) or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        timestamp = (
            candidate
            if candidate.tzinfo is not None
            else candidate.replace(tzinfo=timezone.utc)
        )
        break
    if timestamp is None:
        return float("inf")
    return max(
        0.0,
        (datetime.now(timezone.utc) - timestamp).total_seconds(),
    )


def reconcile_stale_server_restart_acceptance() -> dict[str, Any]:
    """Fail a same-process acceptance whose response worker never started."""

    status = read_server_restart_status()
    if (
        str(status.get("phase") or "") != "accepted"
        or str(status.get("_source_instance_id") or "") != SERVER_INSTANCE_ID
        or server_restart_status_age_seconds(status)
        < SERVER_RESTART_ACCEPTED_STALE_SECONDS
    ):
        return status
    with SERVER_RESTART_SIGNAL_LOCK:
        status = read_server_restart_status()
        if (
            str(status.get("phase") or "") == "accepted"
            and str(status.get("_source_instance_id") or "")
            == SERVER_INSTANCE_ID
            and server_restart_status_age_seconds(status)
            >= SERVER_RESTART_ACCEPTED_STALE_SECONDS
        ):
            status = write_server_restart_status(
                phase="failed",
                message=(
                    "HyadexServer could not begin the accepted restart. "
                    "Retry after refreshing the server connection."
                ),
                failed_at=utc_timestamp(),
            )
    return status
