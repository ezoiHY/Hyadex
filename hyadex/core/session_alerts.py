"""Agent notifications and emergency alerts as recorded in a chat's events."""
from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator
import unicodedata

from hyadex.core.config import EMERGENCY_MESSAGE_MAX_CHARS
from hyadex.core.paths import events_path
from hyadex.core.state import NOTIFICATION_EVENT_FIELDS
from hyadex.core.transcripts import jsonl_records


def clean_emergency_message(value: Any) -> str:
    """Return bounded, one-line plain text suitable for high-priority UI."""

    characters: list[str] = []
    for character in str(value or "")[: EMERGENCY_MESSAGE_MAX_CHARS * 4]:
        category = unicodedata.category(character)
        characters.append(" " if category.startswith("C") else character)
    return " ".join("".join(characters).split())[:EMERGENCY_MESSAGE_MAX_CHARS]


def normalized_emergency_alert(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    alert_id = str(value.get("id") or "").strip()
    message = clean_emergency_message(value.get("message"))
    raised_at = str(value.get("raised_at") or "").strip()
    source_run_id = str(value.get("source_run_id") or "").strip()
    if (
        not re.fullmatch(r"emergency_[0-9a-f]{32}", alert_id)
        or not message
        or not raised_at
    ):
        return None
    return {
        "id": alert_id,
        "status": "active",
        "severity": "critical",
        "message": message,
        "raised_at": raised_at[:80],
        "source_run_id": source_run_id[:160] or None,
    }


def reversed_jsonl_events(
    path: Path, *, retained_fields: frozenset[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Read newest-first in linear work, without accumulating a giant line.

    Find offsets in fixed blocks. Emergency readers only retain lifecycle
    fields; unrelated text/media is validated and skipped in bounded chunks.
    Other consumers retain the full event contract.
    """

    if not path.exists():
        return
    chunk_size = 256 * 1024
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = end = source.tell()

        def read_event(start: int, stop: int) -> dict[str, Any] | None:
            if stop <= start:
                return None
            source.seek(start)
            if retained_fields is not None:
                for event, _, _ in jsonl_records(
                    source, end=stop, project=False, require_newline=False,
                    retained_fields=retained_fields, deadline=float("inf"),
                    # This is a server-owned journal, not a native import.
                    # A tool payload can legitimately contain many values.
                    # Keep retained metadata bounded; scan omitted values in
                    # linear time without the importer's value-count ceiling.
                    max_nodes=None, max_depth=512,
                ):
                    return event
                return None
            with suppress(ValueError, UnicodeError):
                event = json.loads(source.read(stop - start).decode("utf-8", "replace"))
                if isinstance(event, dict):
                    return event
            return None

        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            source.seek(position)
            block = source.read(read_size)
            offset = len(block)
            while (offset := block.rfind(b"\n", 0, offset)) >= 0:
                start = position + offset + 1
                event = read_event(start, end)
                if event is not None:
                    yield event
                end = start - 1
        event = read_event(0, end)
        if event is not None:
            yield event


EMERGENCY_EVENT_FIELDS = frozenset({
    "id", "seq", "type", "ts", "session_id", "run_id",
    "emergency_alert", "emergency_alert_id", "emergency_request_id", "emergency_request_digest",
})


def notification_events_after(
    session_id: str,
    after_seq: int,
) -> tuple[list[dict[str, Any]], int]:
    newest_seq = max(0, int(after_seq or 0))
    events: list[dict[str, Any]] = []
    for event in reversed_jsonl_events(events_path(session_id), retained_fields=NOTIFICATION_EVENT_FIELDS):
        seq = int(event.get("seq") or 0)
        newest_seq = max(newest_seq, seq)
        if seq <= after_seq:
            break
        if event.get("type") == "agent_notification":
            events.append(event)
    events.reverse()
    return events, newest_seq


def reconcile_session_agent_notifications(session_id: str, sess: dict[str, Any]) -> bool:
    through = max(0, int(sess.get("_notification_reconciled_through_seq") or 0))
    checkpoint = through or max(0, int(sess.get("latest_event_seq") or 0))
    events, newest_seq = notification_events_after(session_id, checkpoint)
    if not events and not through:
        return False
    before = (
        sess.get("latest_notification_level"), sess.get("latest_notification_message"),
        sess.get("unread_notification_level"), sess.get("unread_notification_seq"), through,
    )
    if not through and events:
        sess.pop("unread_notification_level", None)
        sess.pop("unread_notification_seq", None)
    read_seq = int(sess.get("last_read_agent_event_seq") or 0)
    ranks = {"normal": 1, "important": 2, "emergency": 3}
    for event in events:
        level = event.get("notification_level")
        seq = int(event.get("seq") or 0)
        if level not in ranks or seq <= 0:
            continue
        sess["latest_notification_level"] = level
        sess["latest_notification_message"] = event.get("message")
        if seq > int(sess.get("latest_agent_event_seq") or 0):
            sess["latest_agent_event_seq"] = seq
            sess["latest_agent_event_at"] = event.get("ts")
            sess["latest_agent_event_type"] = "agent_notification"
        if seq > int(sess.get("latest_event_seq") or 0):
            sess["latest_event_seq"] = seq
            sess["latest_event_at"] = event.get("ts")
            sess["latest_event_type"] = "agent_notification"
        if seq > read_seq and ranks[level] >= ranks.get(sess.get("unread_notification_level"), 0):
            sess["unread_notification_level"] = level
            sess["unread_notification_seq"] = seq
    if int(sess.get("unread_notification_seq") or 0) <= read_seq:
        sess.pop("unread_notification_level", None)
        sess.pop("unread_notification_seq", None)
    if newest_seq:
        sess["_notification_reconciled_through_seq"] = newest_seq
    after = (
        sess.get("latest_notification_level"), sess.get("latest_notification_message"),
        sess.get("unread_notification_level"), sess.get("unread_notification_seq"),
        int(sess.get("_notification_reconciled_through_seq") or 0),
    )
    return before != after


def emergency_lifecycle_events_after(
    session_id: str,
    after_seq: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return every emergency lifecycle record after a durable projection."""

    newest_seq = max(0, int(after_seq or 0))
    events: list[dict[str, Any]] = []
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        seq = int(event.get("seq") or 0)
        newest_seq = max(newest_seq, seq)
        if seq <= after_seq:
            break
        if str(event.get("type") or "") in {
            "emergency_alert_raised",
            "emergency_alert_acknowledged",
        }:
            events.append(event)
    events.reverse()
    return events, newest_seq


def active_emergency_alerts(sess: dict[str, Any]) -> list[dict[str, Any]]:
    values = sess.get("_emergency_alerts")
    if not isinstance(values, list):
        values = []
    by_id: dict[str, dict[str, Any]] = {}
    for value in values:
        alert = normalized_emergency_alert(value)
        if alert:
            by_id[alert["id"]] = alert
    return list(by_id.values())


def apply_emergency_lifecycle_event(
    alerts: list[dict[str, Any]],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {str(alert["id"]): dict(alert) for alert in alerts}
    event_type = str(event.get("type") or "")
    if event_type == "emergency_alert_raised":
        alert = normalized_emergency_alert(event.get("emergency_alert"))
        if alert:
            by_id[alert["id"]] = alert
    elif event_type == "emergency_alert_acknowledged":
        alert_id = str(event.get("emergency_alert_id") or "").strip()
        by_id.pop(alert_id, None)
    return list(by_id.values())


def reconcile_session_emergency_alerts(
    session_id: str,
    sess: dict[str, Any],
) -> bool:
    """Repair summary state from the durable tail after an interrupted save."""

    before = active_emergency_alerts(sess)
    alerts = before
    previous_through = max(
        0,
        int(sess.get("_emergency_reconciled_through_seq") or 0),
    )
    lifecycle_events, reconciled_through = emergency_lifecycle_events_after(
        session_id,
        previous_through,
    )
    for event in lifecycle_events:
        alerts = apply_emergency_lifecycle_event(alerts, event)
    if alerts:
        sess["_emergency_alerts"] = alerts
    else:
        sess.pop("_emergency_alerts", None)
    if reconciled_through:
        sess["_emergency_reconciled_through_seq"] = reconciled_through
    return alerts != before or reconciled_through != previous_through


def emergency_summary(sess: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    alerts = active_emergency_alerts(sess)
    return (dict(alerts[-1]) if alerts else None, len(alerts))
