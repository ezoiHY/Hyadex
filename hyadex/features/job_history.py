"""Job run history: run records, semantic job events and their timeline form."""
from __future__ import annotations

from collections import deque, OrderedDict
from contextlib import suppress
from typing import Any

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
from hyadex.core.event_seq import timeline_index_session_lock
from hyadex.core.paths import now_iso
from hyadex.core.state import TIMELINE_INDEX_CACHE_LOCK

SEMANTIC_TIMELINE_JOB_RUN_LIMIT = 7
SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT = 64


def scheduled_job_history_response_event(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Combine one run's useful output with its authoritative latest status."""
    output = (
        dict(record["output_event"])
        if isinstance(record.get("output_event"), dict)
        else {}
    )
    status_event = (
        dict(record["status_event"])
        if isinstance(record.get("status_event"), dict)
        else {}
    )
    response = {
        **(output or status_event),
        "id": f"job_run:{record['key']}",
        "session_id": str(record.get("session_id") or ""),
        # Run history is ordered/cursored by the final event associated with
        # the attempt, while the authoritative lifecycle seq remains explicit.
        "seq": int(record.get("end_seq") or status_event.get("seq") or 0),
        "type": str(
            status_event.get("type")
            or output.get("type")
            or "job_finished"
        ),
        "ts": (
            status_event.get("ts")
            or output.get("ts")
            or record.get("timestamp")
        ),
        "purpose": "scheduled_job",
        "job_id": str(record.get("job_id") or ""),
        "job_title": str(record.get("job_title") or "Scheduled job"),
        "job_run_status": str(record.get("status") or "unknown"),
        "job_run_status_seq": int(
            status_event.get("seq") or record.get("end_seq") or 0
        ),
        "job_run_status_type": str(status_event.get("type") or ""),
    }
    run_id = str(record.get("run_id") or "").strip()
    if run_id:
        response["run_id"] = run_id
    response["job_status"] = response["job_run_status"]
    response["job_status_seq"] = response["job_run_status_seq"]
    response["job_status_type"] = response["job_run_status_type"]
    response["job_status_run_id"] = run_id or None
    # Status fields come from the latest lifecycle event; output text stays
    # sourced from the best assistant/final event for this same run.
    for field in (
        "message",
        "error",
        "exit_code",
        "is_error",
        "stopped",
        "native_interrupt",
    ):
        if status_event.get(field) is not None:
            response[field] = status_event[field]
    if record.get("status") == "failed":
        response["is_error"] = True
        if not str(response.get("error") or "").strip():
            response["error"] = (
                status_event.get("message")
                or "Scheduled job failed."
            )
    elif record.get("status") == "stopped":
        response["stopped"] = True
    return client_safe_event(response)


def semantic_job_event_priority(event: dict[str, Any]) -> int:
    event_type = str(event.get("type") or "")
    if (
        event_type == "error"
        or event_type.endswith("_error")
        or event_type == "turn_stopped"
        or event.get("is_error") is True
        or bool(event.get("error"))
    ) and event_type not in {"tool_started", "tool_finished", "raw_event"}:
        return 5
    if event_type == "turn_finished" and str(event.get("result_text") or "").strip():
        return 4
    if event_type == "assistant_text" and str(event.get("text") or "").strip():
        return 3
    if event_type == "job_finished":
        return 3
    if event_type in {"job_ran", "job_started"}:
        return 2
    if event_type == "turn_started":
        return 1
    return 0


def semantic_job_event_is_result(event: dict[str, Any]) -> bool:
    """Return whether an event carries a result worth preserving in the summary."""
    event_type = str(event.get("type") or "")
    if event_type == "turn_finished":
        return bool(str(event.get("result_text") or "").strip())
    if event_type == "assistant_text":
        return bool(str(event.get("text") or "").strip())
    if event_type == "job_finished":
        return any(
            str(event.get(field) or "").strip()
            for field in ("result_text", "text", "message")
        )
    if timeline_index_is_error(event):
        return any(
            str(event.get(field) or "").strip()
            for field in ("result_text", "text", "message", "error", "output")
        )
    return False


def read_scheduled_job_runs(
    session_id: str,
    job_id: str,
    *,
    timeline_group_id: str | None = None,
    before_seq: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return one useful representative per scheduled run, newest first.

    The normal timeline-index scan builds one bounded record per run, including
    useful output, authoritative status, and byte/seq bounds. History paging
    therefore performs no second JSONL scan, even for very large chats.
    """
    clean_job_id = str(job_id or "").strip()
    if not clean_job_id:
        raise KeyError("scheduled job not found")
    clean_timeline_group_id = (
        timeline_group_id.strip()
        if isinstance(timeline_group_id, str)
        else ""
    )
    bounded_limit = max(1, min(int(limit or 20), 100))
    normalized_before = (
        max(1, int(before_seq))
        if before_seq is not None
        else None
    )
    lock = timeline_index_session_lock(session_id)
    with lock:
        _build_timeline_index_locked(session_id)
        timeline_cache = timeline_index_cached_entry(session_id, touch=True) or {}
        job_landmark_exists = any(
            candidate.get("kind") == "job"
            and str(candidate.get("job_id") or "") == clean_job_id
            for candidate in timeline_cache.get("records") or []
        )
        if not job_landmark_exists:
            raise KeyError("scheduled job not found")
        run_records = timeline_cache.get("run_history_records") or {}
        if clean_timeline_group_id:
            segment = (timeline_cache.get("by_key") or {}).get(
                clean_timeline_group_id
            )
            if (
                not isinstance(segment, dict)
                or segment.get("kind") != "job"
                or str(segment.get("job_id") or "") != clean_job_id
            ):
                raise KeyError("scheduled job timeline group not found")
            run_keys = (
                (timeline_cache.get("job_timeline_group_run_keys") or {}).get(
                    clean_timeline_group_id
                )
                or []
            )
        else:
            run_keys = (
                (timeline_cache.get("job_run_keys") or {}).get(clean_job_id) or []
            )
        total = len(run_keys)
        # A run key is registered when the run first becomes associated with
        # the job, while its end_seq can advance later. For example, a run can
        # start, a subsequent tick can be deferred, and then the original run
        # can finish. Sort only the lightweight cached record references by
        # their final event sequence so cursor paging cannot skip that finish.
        ordered_records = sorted(
            (
                record
                for key in run_keys
                if isinstance(
                    (record := run_records.get(key)),
                    dict,
                )
            ),
            key=lambda record: (
                int(record.get("end_seq") or 0),
                int(record.get("start_seq") or 0),
                str(record.get("key") or ""),
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        has_more = False
        for record in ordered_records:
            record_seq = int(record.get("end_seq") or 0)
            if (
                normalized_before is not None
                and record_seq >= normalized_before
            ):
                continue
            if len(selected) >= bounded_limit:
                has_more = True
                break
            response_event = scheduled_job_history_response_event(record)
            if clean_timeline_group_id:
                response_event["job_timeline_group_id"] = (
                    clean_timeline_group_id
                )
            selected.append(response_event)
    with TIMELINE_INDEX_CACHE_LOCK:
        evict_timeline_index_cache_locked()
    next_before = (
        min(int(event.get("seq") or 0) for event in selected)
        if has_more and selected
        else None
    )
    return {
        "session_id": session_id,
        "job_id": clean_job_id,
        "timeline_group_id": clean_timeline_group_id or None,
        "runs": selected,
        "total": total,
        "has_more": has_more,
        "next_before": next_before,
    }


def new_semantic_job_state(landmark: dict[str, Any]) -> dict[str, Any]:
    return {
        "landmark": landmark,
        "event_count": 0,
        "run_ids": set(),
        "attempt_ids": set(),
        "occurrence_ids": set(),
        "reported_run_count": 0,
        "representatives": OrderedDict(),
        "standalone": deque(maxlen=SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT),
        "latest_event": None,
        "latest_result_event": None,
        "latest_output_event": None,
        "latest_status_event": None,
        "latest_status": None,
        "latest_run_id": None,
        "latest_attempt_id": None,
        "latest_run_seq": 0,
        "latest_run_extras": deque(maxlen=SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT),
        # Keep one lightweight reasoning and tool anchor for the latest run.
        # The complete trace remains lazy-loaded through read_indexed_run_trace,
        # but these anchors make an interrupted job's retained reasoning
        # visibly discoverable immediately after a chat reload.
        "latest_run_reasoning": None,
        "latest_run_tool": None,
        "title": str(landmark.get("title") or "Scheduled job"),
        "job": None,
    }


def add_semantic_job_event(
    state: dict[str, Any],
    event: dict[str, Any],
    *,
    attempt_id: str | None = None,
) -> None:
    state["event_count"] = int(state["event_count"]) + 1
    seq = int(event.get("seq") or 0)
    latest = state.get("latest_event")
    if not isinstance(latest, dict) or seq >= int(latest.get("seq") or 0):
        state["latest_event"] = event
    latest_result = state.get("latest_result_event")
    if semantic_job_event_is_result(event) and (
        not isinstance(latest_result, dict)
        or seq >= int(latest_result.get("seq") or 0)
    ):
        # Keep one canonical result outside the bounded representative/history
        # collections. A later marker-only or stopped run must not erase the
        # last useful output merely because optional page detail was exhausted.
        state["latest_result_event"] = event
    latest_output = state.get("latest_output_event")
    output_rank = scheduled_job_output_rank(event)
    if output_rank[0] > 0 and (
        not isinstance(latest_output, dict)
        or output_rank > scheduled_job_output_rank(latest_output)
    ):
        state["latest_output_event"] = event
    status = scheduled_job_run_status(event)
    latest_status = state.get("latest_status_event")
    if status is not None and scheduled_job_status_should_replace(
        latest_status if isinstance(latest_status, dict) else None,
        str(state.get("latest_status") or "") or None,
        event,
        status,
    ):
        state["latest_status_event"] = event
        state["latest_status"] = status

    title = str(event.get("job_title") or "").strip()
    job = event.get("job") if isinstance(event.get("job"), dict) else None
    if job:
        state["job"] = job
        title = title or str(job.get("title") or "").strip()
        with suppress(TypeError, ValueError):
            state["reported_run_count"] = max(
                int(state["reported_run_count"]),
                int(job.get("run_count") or 0),
            )
    if title:
        state["title"] = title

    run_id = str(event.get("run_id") or "").strip()
    if not run_id:
        occurrence = scheduled_job_occurrence_key(event)
        if occurrence:
            state["occurrence_ids"].add(occurrence)
        if str(event.get("type") or "") in TIMELINE_INDEX_JOB_TYPES or timeline_index_is_error(event):
            state["standalone"].append(event)
        return
    state["run_ids"].add(run_id)
    clean_attempt_id = str(attempt_id or run_id).strip() or run_id
    state["attempt_ids"].add(clean_attempt_id)
    if seq >= int(state["latest_run_seq"]):
        if clean_attempt_id != state.get("latest_attempt_id"):
            state["latest_attempt_id"] = clean_attempt_id
            state["latest_run_id"] = run_id
            state["latest_run_extras"] = deque(maxlen=SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT)
            state["latest_run_reasoning"] = None
            state["latest_run_tool"] = None
        state["latest_run_seq"] = seq
    if run_id == state.get("latest_run_id"):
        event_type = str(event.get("type") or "")
        if event_type == "reasoning_summary" or (event_type == "reasoning_text" and state["latest_run_reasoning"] is None):
            state["latest_run_reasoning"] = event
        elif event_type in {"tool_started", "tool_finished"}:
            state["latest_run_tool"] = event
    if run_id == state.get("latest_run_id") and (
        event.get("file")
        or event.get("artifact")
        or str(event.get("type") or "") == "code_diff"
    ):
        state["latest_run_extras"].append(event)

    priority = semantic_job_event_priority(event)
    if priority <= 0:
        return
    representatives: OrderedDict[str, dict[str, Any]] = state["representatives"]
    current = representatives.get(clean_attempt_id)
    current_priority = semantic_job_event_priority(current) if current else 0
    if current is None or priority > current_priority or (
        priority == current_priority and seq >= int(current.get("seq") or 0)
    ):
        representatives[clean_attempt_id] = event
    representatives.move_to_end(clean_attempt_id)
    while len(representatives) > SEMANTIC_TIMELINE_JOB_RUN_LIMIT:
        representatives.popitem(last=False)


def semantic_job_summary_event(
    session_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    landmark = state["landmark"]
    anchor_seq = int(landmark.get("start_seq") or landmark.get("end_seq") or 0)
    end_seq = int(landmark.get("end_seq") or anchor_seq)
    timeline_group_id = str(
        landmark.get("job_timeline_group_id")
        or landmark.get("key")
        or "job"
    )
    logical_job_id = str(landmark.get("job_id") or "").strip()
    if not logical_job_id:
        logical_job_id = timeline_group_id.removeprefix("job:").split(
            ":segment:",
            1,
        )[0]
    representatives: list[dict[str, Any]] = list(state["representatives"].values())
    standalone: list[dict[str, Any]] = list(state["standalone"])
    latest_fallback = max(
        [*representatives, *standalone],
        key=lambda event: int(event.get("seq") or 0),
        default=state.get("latest_event") if isinstance(state.get("latest_event"), dict) else {},
    )
    latest_result = (
        state.get("latest_output_event")
        if isinstance(state.get("latest_output_event"), dict)
        else (
            state.get("latest_result_event")
            if isinstance(state.get("latest_result_event"), dict)
            else None
        )
    )
    latest_status_event = (
        state.get("latest_status_event")
        if isinstance(state.get("latest_status_event"), dict)
        else None
    )
    standalone_status_candidates = [
        event
        for event in standalone
        if str(event.get("type") or "") == "job_deferred"
        or timeline_index_is_error(event)
    ]
    latest_standalone = max(
        standalone_status_candidates,
        key=lambda event: int(event.get("seq") or 0),
        default=None,
    )
    result_seq = int(latest_result.get("seq") or 0) if latest_result else 0
    latest_run_seq = int(state.get("latest_run_seq") or 0)
    # A runless error/defer after the most recent run is the current status.
    # Otherwise retain the newest real assistant/final result, even when a
    # marker-only or stopped run is newer and bounded detail omits that result.
    if (
        latest_standalone is not None
        and int(latest_standalone.get("seq") or 0) > max(result_seq, latest_run_seq)
    ):
        latest_display = latest_standalone
    elif latest_result is not None:
        latest_display = latest_result
    else:
        latest_display = latest_fallback
    latest_event = state.get("latest_event") if isinstance(state.get("latest_event"), dict) else latest_display
    title = str(state.get("title") or landmark.get("title") or "Scheduled job")
    run_count = len(state["attempt_ids"])
    if not run_count:
        run_count = len(state.get("occurrence_ids") or ())
    summary: dict[str, Any] = {
        "seq": anchor_seq,
        "id": f"job_summary:{timeline_group_id.removeprefix('job:')}",
        "session_id": session_id,
        "type": "job_summary",
        # The card is anchored by seq/job_start_seq, while its header time
        # continues to describe the latest status represented by the summary.
        "ts": latest_event.get("ts") or landmark.get("timestamp") or now_iso(),
        "purpose": "scheduled_job",
        "job_id": logical_job_id,
        "job_timeline_group_id": timeline_group_id,
        "job_title": title,
        "job_run_count": run_count,
        "job_event_count": int(state.get("event_count") or 0),
        "job_start_seq": int(
            landmark.get("_semantic_original_start_seq")
            or landmark.get("start_seq")
            or anchor_seq
        ),
        "job_end_seq": end_seq,
        "job_history_truncated": run_count > len(representatives),
        "job_latest_run_id": (
            str(state.get("latest_run_id") or "").strip() or None
        ),
        "job_latest_status": str(state.get("latest_status") or "unknown"),
        "job_latest_status_seq": int(
            latest_status_event.get("seq") or 0
        ) if latest_status_event else 0,
        "job_latest_status_type": str(
            latest_status_event.get("type") or ""
        ) if latest_status_event else "",
        "job_latest_status_run_id": (
            str(latest_status_event.get("run_id") or "").strip() or None
        ) if latest_status_event else None,
    }
    # Compact aliases are consumed by current desktop clients. Keep the
    # explicit ``job_latest_*`` names as the durable, self-describing contract.
    summary["job_status"] = summary["job_latest_status"]
    summary["job_status_seq"] = summary["job_latest_status_seq"]
    summary["job_status_type"] = summary["job_latest_status_type"]
    summary["job_status_run_id"] = summary["job_latest_status_run_id"]
    job = dict(state["job"]) if isinstance(state.get("job"), dict) else {
        "id": summary["job_id"],
        "title": title,
    }
    job["run_count"] = max(int(job.get("run_count") or 0), run_count)
    summary["job"] = job
    for field in (
        "backend",
        "result_text",
        "text",
        "output",
        "message",
        "error",
        "exit_code",
        "is_error",
        "stopped",
    ):
        if latest_display.get(field) is not None:
            summary[field] = latest_display[field]
    if latest_status_event is not None:
        for field in (
            "message",
            "error",
            "exit_code",
            "is_error",
            "stopped",
            "native_interrupt",
        ):
            if latest_status_event.get(field) is not None:
                summary[field] = latest_status_event[field]
    if summary["job_latest_status"] == "failed":
        summary["is_error"] = True
        if not str(summary.get("error") or "").strip():
            summary["error"] = (
                (latest_status_event or {}).get("message")
                or "Scheduled job failed."
            )
    elif summary["job_latest_status"] == "stopped":
        summary["stopped"] = True
    if str(latest_display.get("type") or "") == "job_error":
        summary["is_error"] = True
        summary["error"] = (
            latest_display.get("error")
            or latest_display.get("message")
            or "Scheduled job failed."
        )
    if not any(str(summary.get(field) or "").strip() for field in ("result_text", "text", "message")):
        summary["message"] = "Scheduled job status updated."
    return client_safe_event(summary)
