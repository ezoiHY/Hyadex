"""Emergency alerts raised by agents and their lifecycle."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from hyadex.core.config import EMERGENCY_MESSAGE_MAX_CHARS, EMERGENCY_REQUESTS_PER_RUN
from hyadex.core.paths import events_path
from hyadex.core.sessions import (
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    public_session,
    STORE,
)
from hyadex.core.session_alerts import (
    EMERGENCY_EVENT_FIELDS,
    emergency_summary,
    normalized_emergency_alert,
    reversed_jsonl_events,
)
from hyadex.core.util import server_identity
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.providers.authority import provider_capability_header
from hyadex.providers.registry import authorize_provider_action


class EmergencyAlertRequest(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    message: str = Field(min_length=1, max_length=EMERGENCY_MESSAGE_MAX_CHARS)


class AcknowledgeEmergencyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expected_alert_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=80,
        pattern=r"^emergency_[0-9a-f]{32}$",
    )
    through_alert_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=80,
        pattern=r"^emergency_[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def exactly_one_alert_boundary(self):
        if (self.expected_alert_id is None) == (self.through_alert_id is None):
            raise ValueError("provide exactly one of expected_alert_id or through_alert_id")
        return self


EMERGENCY_HUB_KEY = "__hyadex_emergency_alerts__"


async def broadcast_emergency_session(sess: dict[str, Any]) -> None:
    await HUB.broadcast(EMERGENCY_HUB_KEY, {
        "type": "emergency_changed",
        "server_identity": server_identity(),
        "session": public_emergency_session(sess),
    })


async def broadcast_emergency_session_removed(session_id: str) -> None:
    await HUB.broadcast(EMERGENCY_HUB_KEY, {
        "type": "emergency_removed",
        "server_identity": server_identity(),
        "session_id": session_id,
    })


async def project_committed_emergency_event(
    session_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Finish projection + live delivery after the lifecycle event is fsynced."""

    async def complete() -> dict[str, Any]:
        sess = await STORE.apply_emergency_event(session_id, event)
        await broadcast_emergency_session(sess)
        return sess

    task = asyncio.create_task(complete())
    try:
        return await asyncio.shield(task)
    except BaseException:
        # Once the lifecycle event is durable, caller cancellation must not
        # strand the red summary or its global notification signal.
        return await join_task_despite_caller_cancellation(task)


async def broadcast_committed_emergency_removal(session_id: str) -> None:
    task = asyncio.create_task(broadcast_emergency_session_removed(session_id))
    try:
        await asyncio.shield(task)
    except BaseException:
        await join_task_despite_caller_cancellation(task)


def emergency_request_digest(message: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"message": message},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def emergency_alert_id(
    session_id: str,
    source_run_id: str,
    request_id: str,
) -> str:
    digest = hashlib.sha256(
        "\0".join((server_identity(), session_id, source_run_id, request_id)).encode(
            "utf-8"
        )
    ).hexdigest()
    return "emergency_" + digest[:32]


async def reserve_provider_emergency_request(
    request: Request,
    *,
    session_id: str,
    request_id: str,
    request_digest: str,
) -> dict[str, Any]:
    capability = await authorize_provider_action(
        request,
        action="emergency",
        session_id=session_id,
    )
    token = provider_capability_header(request)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with PROVIDER_AUTHORITY_LOCK:
        current = PROVIDER_AUTHORITIES.get(token_hash)
        if (
            current is None
            or current.get("source_session_id") != capability.get("source_session_id")
            or current.get("source_run_id") != capability.get("source_run_id")
        ):
            raise HTTPException(
                status_code=403,
                detail="provider capability is no longer attached to this turn",
            )
        reservations = current.setdefault("emergency_requests", {})
        previous_digest = reservations.get(request_id)
        if previous_digest is not None and previous_digest != request_digest:
            raise HTTPException(
                status_code=409,
                detail="emergency request ID was already used with different content",
            )
        if previous_digest is None:
            if len(reservations) >= EMERGENCY_REQUESTS_PER_RUN:
                raise HTTPException(
                    status_code=429,
                    detail="this turn already used its emergency contact limit",
                )
            reservations[request_id] = request_digest
        return dict(current)


def find_emergency_request_event(
    session_id: str,
    request_id: str,
    source_run_id: str,
) -> dict[str, Any] | None:
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        if (
            event.get("type") == "emergency_alert_raised"
            and event.get("emergency_request_id") == request_id
            and event.get("run_id") == source_run_id
        ):
            return event
    return None


def emergency_alert_was_acknowledged(
    session_id: str,
    alert_id: str,
    raised_seq: int,
) -> bool:
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        seq = int(event.get("seq") or 0)
        if seq <= raised_seq:
            return False
        if (
            event.get("type") == "emergency_alert_acknowledged"
            and event.get("emergency_alert_id") == alert_id
        ):
            return True
    return False


def find_emergency_acknowledgement_event(
    session_id: str,
    alert_id: str,
) -> dict[str, Any] | None:
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        if (
            event.get("type") == "emergency_alert_acknowledged"
            and event.get("emergency_alert_id") == alert_id
        ):
            return event
    return None


def emergency_alert_batch_through(
    session_id: str,
    through_alert_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Freeze active IDs at an exact durable raise boundary, including retries.

    Read lifecycle facts rather than a potentially interrupted summary save.
    Reverse order lets each alert's newest fact determine whether it is active.
    A previously acknowledged boundary remains a valid, immutable cut-off.
    """
    settled: set[str] = set()
    active: list[tuple[int, str]] = []
    boundary = None
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        if event.get("type") == "emergency_alert_acknowledged":
            settled.add(str(event.get("emergency_alert_id") or ""))
        elif event.get("type") == "emergency_alert_raised":
            alert = normalized_emergency_alert(event.get("emergency_alert"))
            seq = int(event.get("seq") or 0)
            if alert is None or seq <= 0:
                continue
            alert_id = alert["id"]
            if alert_id == through_alert_id:
                boundary = event
            if alert_id not in settled:
                active.append((seq, alert_id))
                settled.add(alert_id)
    if boundary is None:
        return None, []
    boundary_seq = int(boundary["seq"])
    return boundary, [alert_id for seq, alert_id in reversed(active) if seq <= boundary_seq]


def emergency_alert_response(
    session_id: str,
    event: dict[str, Any],
    sess: dict[str, Any],
    *,
    duplicate: bool,
) -> dict[str, Any]:
    alert = normalized_emergency_alert(event.get("emergency_alert"))
    if alert is None:
        raise HTTPException(status_code=500, detail="emergency alert receipt is invalid")
    return {
        "ok": True,
        "chat_id": session_id,
        "alert": alert,
        "event_id": event.get("id"),
        "event_seq": event.get("seq"),
        "duplicate": duplicate,
        # Keep the provider receipt deliberately narrow. The authenticated
        # Desktop receives the full session summary over its own channel.
        "unacknowledged_emergency_count": emergency_summary(sess)[1],
    }


def public_emergency_session(sess: dict[str, Any]) -> dict[str, Any]:
    public = public_session(sess, summary=True)
    emergency_alert, emergency_count = emergency_summary(sess)
    public["emergency_alert"] = emergency_alert
    public["unacknowledged_emergency_count"] = emergency_count
    return public
