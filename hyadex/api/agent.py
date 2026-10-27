"""Agent helper endpoints: artifacts, emergency alerts and notifications, plus emergency acknowledgement and its WebSocket."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging.handlers
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from hyadex.core.config import (
    EMERGENCY_ACTIVE_ALERT_LIMIT,
    EMERGENCY_WEBSOCKET_PROTOCOL,
    NOTIFICATION_REQUESTS_PER_RUN,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from hyadex.core.event_log import (
    ArtifactPublicationError,
    find_agent_notification_event,
    load_publication_events,
    recover_publication_events_from_tail,
    store_publication_events,
    verified_publication_events,
)
from hyadex.core.http import (
    AgentNotificationRequest,
    route_auth,
    router,
    websocket_authorized,
    websocket_endpoint_subprotocol,
    websocket_requested_protocols,
)
from hyadex.core.paths import now_iso
from hyadex.core.artifacts import publish_artifact_entries
from hyadex.core.history_sync import append_durable_event, append_durable_event_batch
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ensure_session_not_deleting,
    ensure_session_not_initializing,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    public_session,
    session_lifecycle_lock,
    sorted_sessions,
    STORE,
)
from hyadex.core.session_alerts import active_emergency_alerts, clean_emergency_message
from hyadex.core.util import server_identity
from hyadex.features.emergency import (
    AcknowledgeEmergencyRequest,
    emergency_alert_batch_through,
    emergency_alert_id,
    emergency_alert_response,
    emergency_alert_was_acknowledged,
    EMERGENCY_HUB_KEY,
    emergency_request_digest,
    EmergencyAlertRequest,
    find_emergency_acknowledgement_event,
    find_emergency_request_event,
    project_committed_emergency_event,
    public_emergency_session,
    reserve_provider_emergency_request,
)
from hyadex.features.files import (
    active_artifact_publication_run,
    artifact_event_receipt,
    normalize_artifact_entries,
    normalized_artifact_request,
    PublishArtifactsRequest,
)
from hyadex.features.usage import (
    close_event_websocket_over_capacity,
    HUB,
    join_task_despite_caller_cancellation,
)
from hyadex.providers.authority import provider_capability_header
from hyadex.providers.registry import authorize_provider_action

logger = logging.getLogger("hyadex-server")

router = APIRouter()


@router.post("/api/sessions/{session_id}/emergency/acknowledge")
async def acknowledge_session_emergency(
    session_id: str,
    req: AcknowledgeEmergencyRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_initializing(session_id)
        sess = STORE.sessions.get(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="session not found")
        if req.through_alert_id is not None:
            boundary, alert_ids = await asyncio.to_thread(
                emergency_alert_batch_through, session_id, req.through_alert_id,
            )
            if boundary is None:
                raise HTTPException(status_code=409, detail="that emergency alert boundary was not found")
            if alert_ids:
                events = await append_durable_event_batch(session_id, [
                    ("emergency_alert_acknowledged", {
                        "emergency_alert_id": alert_id,
                        "message": "Emergency acknowledged by the user.",
                    })
                    for alert_id in alert_ids
                ])
                committed = events[-1]
            else:
                committed = boundary
            sess = await project_committed_emergency_event(session_id, committed)
            return {"session": public_session(sess), "acknowledged": bool(alert_ids),
                    "acknowledged_count": len(alert_ids)}
        alert_id = req.expected_alert_id
        acknowledged_event = await asyncio.to_thread(
            find_emergency_acknowledgement_event,
            session_id,
            alert_id,
        )
        if acknowledged_event is not None:
            sess = await project_committed_emergency_event(
                session_id,
                acknowledged_event,
            )
            return {"session": public_session(sess), "acknowledged": False}
        active = {
            str(alert.get("id") or ""): alert
            for alert in active_emergency_alerts(sess)
        }
        if alert_id not in active:
            raise HTTPException(
                status_code=409,
                detail="that emergency alert is no longer active",
            )
        event = await append_durable_event(
            session_id,
            "emergency_alert_acknowledged",
            {
                "emergency_alert_id": alert_id,
                "message": "Emergency acknowledged by the user.",
            },
        )
        sess = await project_committed_emergency_event(session_id, event)
        return {"session": public_session(sess), "acknowledged": True}


@router.websocket("/api/emergency-alerts/events")
async def emergency_alert_events(ws: WebSocket) -> None:
    selected_subprotocol = websocket_endpoint_subprotocol(
        ws,
        EMERGENCY_WEBSOCKET_PROTOCOL,
    )
    if not websocket_authorized(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4401)
        return
    if EMERGENCY_WEBSOCKET_PROTOCOL not in websocket_requested_protocols(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4406)
        return
    await ws.accept(subprotocol=EMERGENCY_WEBSOCKET_PROTOCOL)
    try:
        if not await HUB.reserve(EMERGENCY_HUB_KEY, ws):
            await close_event_websocket_over_capacity(ws)
            return
        # State writes use the same lock. Sending the snapshot before
        # registration while holding it closes the snapshot/live-update race.
        async with STORE.transaction():
            sessions = [
                public_emergency_session(sess)
                for sess in sorted_sessions(list(STORE.sessions.values()))
                if not sess.get("_fork_initializing")
                and active_emergency_alerts(sess)
            ]
            await asyncio.wait_for(
                ws.send_json({
                    "type": "emergency_snapshot",
                    "server_identity": server_identity(),
                    "sessions": sessions,
                }),
                timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
            )
            activated = await HUB.register_accepted(EMERGENCY_HUB_KEY, ws)
        if not activated:
            await close_event_websocket_over_capacity(ws)
            return
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.unsubscribe(EMERGENCY_HUB_KEY, ws)


def publication_response(
    session_id: str,
    publication_id: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "chat_id": session_id,
        "publication_id": publication_id,
        "run_id": events[0].get("run_id") if events else None,
        "receipts": [artifact_event_receipt(event) for event in events],
    }


@router.post("/api/agent/sessions/{session_id}/artifacts", dependencies=route_auth("agent_helper", 16 * 1024 * 1024))
async def publish_agent_artifacts(
    request: Request,
    session_id: str,
    req: PublishArtifactsRequest,
) -> dict[str, Any]:
    await authorize_provider_action(request, action="publish", session_id=session_id)
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_deleting(session_id)

    try:
        publication_id, entries, digest = await asyncio.to_thread(
            normalized_artifact_request,
            req.publication_id,
            req.files,
        )
    except ArtifactPublicationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # O(1) idempotency lookup precedes the active-run requirement. A retry
    # after a lost response must retrieve its original durable receipts even if
    # the turn has since finished or another turn has started.
    try:
        existing = await asyncio.to_thread(
            load_publication_events,
            session_id,
            publication_id,
        )
        receipt_sidecar_exists = bool(existing)
        if not existing and request.headers.get("x-hyadex-publication-retry") == "1":
            existing = await asyncio.to_thread(
                recover_publication_events_from_tail,
                session_id,
                publication_id,
            )
        if existing:
            original_run_id = str(existing[0].get("run_id") or "")
            verified = await asyncio.to_thread(
                verified_publication_events,
                existing,
                run_id=original_run_id,
                publication_id=publication_id,
                publication_digest=digest,
                expected_count=len(entries),
            )
            if not receipt_sidecar_exists:
                try:
                    await asyncio.to_thread(
                        store_publication_events,
                        session_id,
                        publication_id,
                        verified,
                    )
                except BaseException as exc:
                    logger.warning(
                        "recovered publication sidecar repair failed session=%s publication=%s: %s",
                        session_id,
                        publication_id,
                        concise_error_message(exc),
                    )
            return publication_response(session_id, publication_id, verified)
    except ArtifactPublicationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    run_id, metadata = await active_artifact_publication_run(session_id)
    try:
        entries = await asyncio.to_thread(normalize_artifact_entries, entries)
    except ArtifactPublicationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    publication_task = asyncio.create_task(publish_artifact_entries(
        session_id,
        run_id,
        entries,
        publication_id=publication_id,
        publication_digest=digest,
        metadata=metadata,
    ))
    try:
        events = await asyncio.shield(publication_task)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        try:
            events = await join_task_despite_caller_cancellation(publication_task)
        except ArtifactPublicationError as publication_exc:
            raise HTTPException(status_code=409, detail=str(publication_exc)) from publication_exc
        except BaseException as publication_exc:
            if isinstance(publication_exc, (KeyboardInterrupt, SystemExit)):
                raise
            logger.exception(
                "agent artifact publication failed session=%s run=%s publication=%s",
                session_id,
                run_id,
                publication_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"artifact publication failed: {concise_error_message(publication_exc)}",
            ) from publication_exc
    return publication_response(session_id, publication_id, events)


@router.post("/api/agent/sessions/{session_id}/emergency-alerts", dependencies=route_auth("agent_helper", 8 * 1024))
async def raise_agent_emergency_alert(
    request: Request,
    session_id: str,
    req: EmergencyAlertRequest,
) -> dict[str, Any]:
    # Authenticate before looking at any chat state so a loopback caller with
    # no/foreign authority cannot use response codes as a hidden-chat oracle.
    await authorize_provider_action(
        request,
        action="emergency",
        session_id=session_id,
    )
    message = clean_emergency_message(req.message)
    if not message:
        raise HTTPException(status_code=422, detail="emergency message is empty")
    request_digest = emergency_request_digest(message)
    async with session_lifecycle_lock(session_id):
        sess = STORE.sessions.get(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="session not found")
        ensure_session_not_deleting(session_id)
        if sess.get("archived"):
            raise HTTPException(
                status_code=409,
                detail="an archived chat cannot raise an emergency alert",
            )
        capability = await reserve_provider_emergency_request(
            request,
            session_id=session_id,
            request_id=req.request_id,
            request_digest=request_digest,
        )
        source_run_id = str(capability.get("source_run_id") or "")
        alert_id = emergency_alert_id(session_id, source_run_id, req.request_id)
        existing = await asyncio.to_thread(
            find_emergency_request_event,
            session_id,
            req.request_id,
            source_run_id,
        )
        if existing is not None:
            if (
                existing.get("emergency_request_digest") != request_digest
                or existing.get("run_id") != source_run_id
                or (existing.get("emergency_alert") or {}).get("id") != alert_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="emergency request receipt does not match this live turn",
                )
            acknowledged = await asyncio.to_thread(
                emergency_alert_was_acknowledged,
                session_id,
                alert_id,
                int(existing.get("seq") or 0),
            )
            if acknowledged:
                acknowledged_event = await asyncio.to_thread(
                    find_emergency_acknowledgement_event,
                    session_id,
                    alert_id,
                )
                if acknowledged_event is not None:
                    sess = await project_committed_emergency_event(
                        session_id,
                        acknowledged_event,
                    )
            else:
                sess = await project_committed_emergency_event(
                    session_id,
                    existing,
                )
            return emergency_alert_response(
                session_id,
                existing,
                sess,
                duplicate=True,
            )

        if len(active_emergency_alerts(sess)) >= EMERGENCY_ACTIVE_ALERT_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="acknowledge an existing emergency before raising another",
            )

        raised_at = now_iso()
        alert = {
            "id": alert_id,
            "status": "active",
            "severity": "critical",
            "message": message,
            "raised_at": raised_at,
            "source_run_id": source_run_id,
        }
        event = await append_durable_event(session_id, "emergency_alert_raised", {
            "run_id": source_run_id,
            "message": message,
            "emergency_alert": alert,
            "emergency_alert_id": alert_id,
            "emergency_request_id": req.request_id,
            "emergency_request_digest": request_digest,
        })
        sess = await project_committed_emergency_event(session_id, event)
        return emergency_alert_response(
            session_id,
            event,
            sess,
            duplicate=False,
        )


@router.post("/api/agent/sessions/{session_id}/notifications", dependencies=route_auth("agent_helper", 8 * 1024))
async def raise_agent_notification(
    request: Request,
    session_id: str,
    req: AgentNotificationRequest,
) -> dict[str, Any]:
    capability = await authorize_provider_action(request, action="notify", session_id=session_id)
    message = clean_emergency_message(req.message)
    if not message:
        raise HTTPException(status_code=422, detail="notification message is empty")
    if req.level == "emergency":
        receipt = await raise_agent_emergency_alert(
            request,
            session_id,
            EmergencyAlertRequest(request_id=req.request_id, message=message),
        )
        return {
            "ok": True, "chat_id": session_id, "level": req.level,
            "event_id": receipt["event_id"], "event_seq": receipt["event_seq"],
            "duplicate": receipt["duplicate"],
        }

    request_digest = hashlib.sha256(
        json.dumps({"level": req.level, "message": message}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    async with session_lifecycle_lock(session_id):
        sess = STORE.sessions.get(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="session not found")
        ensure_session_not_deleting(session_id)
        if sess.get("archived"):
            raise HTTPException(status_code=409, detail="an archived chat cannot send a notification")
        token = provider_capability_header(request)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with PROVIDER_AUTHORITY_LOCK:
            live = PROVIDER_AUTHORITIES.get(token_hash)
            if live is None or live.get("source_run_id") != capability.get("source_run_id"):
                raise HTTPException(status_code=403, detail="provider capability is no longer attached to this turn")
            reservations = live.setdefault("notification_requests", {})
            previous_digest = reservations.get(req.request_id)
            if previous_digest is not None and previous_digest != request_digest:
                raise HTTPException(status_code=409, detail="notification request ID was already used with different content")
            if previous_digest is None:
                if len(reservations) >= NOTIFICATION_REQUESTS_PER_RUN:
                    raise HTTPException(status_code=429, detail="this turn already used its notification limit")
                reservations[req.request_id] = request_digest
        source_run_id = str(capability.get("source_run_id") or "")
        existing = await asyncio.to_thread(
            find_agent_notification_event, session_id, req.request_id, source_run_id,
        )
        if existing is not None:
            if existing.get("notification_request_digest") != request_digest:
                raise HTTPException(status_code=409, detail="notification receipt does not match this live turn")
            return {"ok": True, "chat_id": session_id, "level": req.level,
                    "event_id": existing["id"], "event_seq": existing["seq"], "duplicate": True}
        event = await append_durable_event(session_id, "agent_notification", {
            "run_id": source_run_id,
            "message": message,
            "notification_level": req.level,
            "notification_request_id": req.request_id,
            "notification_request_digest": request_digest,
        })
        return {"ok": True, "chat_id": session_id, "level": req.level,
                "event_id": event["id"], "event_seq": event["seq"], "duplicate": False}
