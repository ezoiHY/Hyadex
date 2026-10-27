"""History search and the semantic timeline."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging.handlers
import re
import sqlite3
import stat
import threading
import time
from typing import Any
import uuid

from hyadex.core.config import hyadex_setting, MAX_EVENT_RESPONSE_LIMIT, STATE_DIR
from hyadex.core.event_log import client_safe_event
from hyadex.core.timeline_index import (
    _build_timeline_index_locked,
    evict_timeline_index_cache_locked,
    fork_internal_run_ids,
    is_client_visible_event,
    is_fork_internal_event,
    is_native_goal_steer_event,
    project_legacy_imported_provider_event,
    scheduled_job_occurrence_key,
    TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD,
    timeline_index_cached_entry,
    timeline_index_cross_chat_key,
    timeline_index_event_is_hidden,
    timeline_index_is_error,
    timeline_index_is_native_steer_transition_stop,
    TIMELINE_INDEX_JOB_TYPES,
    timeline_index_retire_native_steer_turn,
)
from hyadex.core.paths import events_path
from hyadex.core.history_sync import (
    prepare_claude_history_metadata_repair,
    prepare_codex_goal_history_repair,
)
from hyadex.core.sessions import STORE
from hyadex.core.session_files import event_files_belong_to_session
from hyadex.core.event_seq import timeline_index_session_lock
from hyadex.core.session_codex import timeline_index_codex_lifecycle_key
from hyadex.core.state import (
    RUN_TRACE_EVENT_TYPES,
    TIMELINE_INDEX_CACHE_LOCK,
    TIMELINE_INDEX_TRACE_TYPES,
)
from hyadex.core.util import ensure_dirs
from hyadex.features.job_history import (
    add_semantic_job_event,
    new_semantic_job_state,
    semantic_job_summary_event,
    SEMANTIC_TIMELINE_JOB_RUN_LIMIT,
)
from hyadex.providers.claude.history import HISTORY_SEARCH_DIRTY, HISTORY_SEARCH_REPAIR_DIRTY

logger = logging.getLogger("hyadex-server")

HISTORY_SEARCH_DB = STATE_DIR / "history_search.sqlite3"
HISTORY_SEARCH_LOCK = threading.Lock()
HISTORY_SEARCH_INDEX_VERSION = "8"
HISTORY_SEARCH_SYNC_INTERVAL_SECONDS = max(
    0.25, float(hyadex_setting("HISTORY_SEARCH_SYNC_INTERVAL_SECONDS", "1.0"))
)
HISTORY_SEARCH_INITIAL_DELAY_SECONDS = max(
    0.0, float(hyadex_setting("HISTORY_SEARCH_INITIAL_DELAY_SECONDS", "30.0"))
)
HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS = max(
    30.0, float(hyadex_setting("HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS", "300"))
)


SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM = 4
# Media and diffs are first-class output, but a corrupt/noisy run must not turn
# a one-item semantic page into a thousand-event response. Thirty-two keeps
# the known 13-video review workflow intact, while the page cap prevents many
# selected media-heavy turns from multiplying that allowance without bound.
SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM = 32
SEMANTIC_TIMELINE_ESSENTIAL_PAGE_OVERFLOW_LIMIT = 128
SEMANTIC_TIMELINE_TRACE_ANCHOR_PAGE_OVERFLOW_LIMIT = 128
# Same-ID history corrections clear previously cached replay bubbles. They
# need their own bounded allowance, not the slots reserved for chat content.
SEMANTIC_TIMELINE_REPAIR_PAGE_OVERFLOW_LIMIT = 256
SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES = {
    "artifact_created",
    "file_uploaded",
    "code_diff",
}


def timeline_search_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            text = timeline_search_value(value.get(key))
            if text:
                return text
    try:
        return re.sub(r"\s+", " ", json.dumps(value, ensure_ascii=False)).strip()
    except Exception:
        return re.sub(r"\s+", " ", str(value)).strip()


def timeline_search_event_text(event: dict[str, Any]) -> str:
    for field in ("result_text", "text", "prompt", "digest", "message", "error"):
        text = timeline_search_value(event.get(field))
        if text:
            return text
    return ""


def timeline_search_snippet(text: str, tokens: list[str], limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    folded = compact.casefold()
    positions = [folded.find(token) for token in tokens if token and folded.find(token) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(compact), start + limit)
    start = max(0, end - limit)
    return ("…" if start else "") + compact[start:end].strip() + ("…" if end < len(compact) else "")


def timeline_search_tokens(query: str) -> list[str]:
    matches = re.findall(r'"([^"]+)"|(\S+)', query.strip())
    return [(phrase or word).casefold() for phrase, word in matches if phrase or word]


def timeline_search_fts_query(query: str) -> str:
    matches = re.findall(r'"([^"]+)"|(\S+)', query.strip())
    terms: list[str] = []
    for phrase, word in matches:
        value = (phrase or word).casefold()
        if not value:
            continue
        escaped = value.replace('"', '""')
        terms.append(f'"{escaped}"' if phrase else f'"{escaped}"*')
    return " AND ".join(terms)


def semantic_timeline_event_identity(event: dict[str, Any]) -> str:
    return str(event.get("id") or f"seq:{event.get('seq')}:{event.get('type')}")


def semantic_timeline_event_is_display(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if timeline_index_is_error(event):
        return True
    if event_type == "assistant_text":
        return bool(str(event.get("text") or "").strip())
    if event_type == "turn_finished":
        return bool(str(event.get("result_text") or "").strip())
    if event_type in {"artifact_created", "file_uploaded", "code_diff"}:
        return True
    if event_type.startswith("handoff_digest_"):
        return True
    return (
        event_type not in TIMELINE_INDEX_TRACE_TYPES
        and event_type != "turn_started"
        and not is_native_goal_steer_event(event)
    )


def semantic_timeline_event_is_completed_commentary(
    event: dict[str, Any],
) -> bool:
    """Return whether an event is durable completed agent commentary."""
    return (
        str(event.get("type") or "") == "reasoning_summary"
        and str(event.get("phase") or "") == "commentary"
        and bool(str(event.get("text") or "").strip())
    )


def semantic_timeline_event_is_trace_anchor(event: dict[str, Any]) -> bool:
    """Keep one lightweight handle so completed trace detail stays discoverable."""
    event_type = str(event.get("type") or "")
    if event_type not in RUN_TRACE_EVENT_TYPES:
        return False
    if not str(event.get("run_id") or "").strip():
        return False
    if event_type == "reasoning_summary":
        return bool(str(event.get("text") or "").strip())
    return True


def semantic_timeline_ordinary_candidates(
    events: list[dict[str, Any]],
    *,
    preserve_completed_commentary: bool = False,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    ordered = sorted(events, key=lambda event: int(event.get("seq") or 0))
    if not ordered:
        return [], [], [], []
    first_turn = next(
        (event for event in ordered if str(event.get("type") or "") == "turn_started"),
        None,
    )
    latest_display = next(
        (event for event in reversed(ordered) if semantic_timeline_event_is_display(event)),
        ordered[-1],
    )
    primary = [latest_display]
    secondary_event = first_turn
    if secondary_event is None and ordered[0] is not latest_display:
        secondary_event = ordered[0]
    secondary_candidates = [secondary_event] if secondary_event is not None else []
    trace_anchor = next(
        (event for event in reversed(ordered) if semantic_timeline_event_is_trace_anchor(event)
            and event.get("type") != "reasoning_text"),
        next((event for event in reversed(ordered) if semantic_timeline_event_is_trace_anchor(event)), None),
    )
    primary_id = semantic_timeline_event_identity(latest_display)
    secondary_by_id = {
        semantic_timeline_event_identity(event): event
        for event in secondary_candidates
        if semantic_timeline_event_identity(event) != primary_id
    }
    secondary = list(secondary_by_id.values())
    core_ids = {
        semantic_timeline_event_identity(event)
        for event in [*primary, *secondary]
    }
    remaining = [
        event
        for event in reversed(ordered)
        if semantic_timeline_event_identity(event) not in core_ids
    ]
    detail_essential = [
        event
        for event in remaining
        if str(event.get("type") or "") in SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES
    ]
    commentary_essential = [
        event
        for event in remaining
        if preserve_completed_commentary
        and semantic_timeline_event_is_completed_commentary(event)
    ]
    essential = [*detail_essential, *commentary_essential]
    essential_ids = {
        semantic_timeline_event_identity(event)
        for event in essential
    }
    optional = [
        event
        for event in remaining
        if semantic_timeline_event_identity(event) not in essential_ids
    ]
    if trace_anchor is not None:
        trace_anchor_id = semantic_timeline_event_identity(trace_anchor)
        optional = [
            trace_anchor,
            *(event for event in optional if semantic_timeline_event_identity(event) != trace_anchor_id),
        ]
    return primary, secondary, essential, optional


def collect_semantic_timeline_events(
    session_id: str,
    selected: list[dict[str, Any]],
    *,
    job_by_run: dict[str, str],
    job_timeline_group_by_run: dict[str, str],
    job_timeline_group_by_occurrence: dict[str, str],
    job_timeline_group_by_run_start_seq: dict[int, str],
    job_timeline_group_by_runless_event_seq: dict[int, str],
    fork_internal_run_ids: set[str],
    internal_status_run_ids: set[str],
    event_limit: int,
    history_repair_window=None,
    repair_seq_bounds: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    if not selected:
        return []
    path = events_path(session_id)
    if not path.exists():
        return []

    selected_by_key = {str(landmark.get("key") or ""): landmark for landmark in selected}
    selected_jobs = {
        key: new_semantic_job_state(landmark)
        for key, landmark in selected_by_key.items()
        if str(landmark.get("kind") or "") == "job"
    }
    events_by_key: dict[str, list[dict[str, Any]]] = {
        key: [] for key in selected_by_key if key not in selected_jobs
    }
    page_repairs: list[dict[str, Any]] = []
    native_steer_retired_keys: set[str] = set()
    stopped_turn_keys: set[str] = set()
    current_turn_by_run: dict[str, str] = {}
    current_job_timeline_group_by_run: dict[str, str] = {}
    current_job_attempt_by_run: dict[str, str] = {}
    active_turn_key: str | None = None
    seen_user_turn_keys: set[str] = set()
    scan_offset = min(
        (
            max(0, int(landmark.get("_semantic_start_offset") or 0))
            for landmark in selected
        ),
        default=0,
    )
    with suppress(OSError):
        if scan_offset >= path.stat().st_size:
            scan_offset = 0

    with path.open("rb") as source:
        source.seek(scan_offset)
        for raw_line in source:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq") or 0)
            if seq <= 0:
                continue
            run_id = str(event.get("run_id") or "").strip()
            if event.get("cross_chat_exchange_status") is True and run_id:
                internal_status_run_ids.add(run_id)
            if is_fork_internal_event(event, fork_internal_run_ids):
                continue
            if not is_client_visible_event(event):
                continue
            page_repaired = False
            if history_repair_window is not None:
                repaired = history_repair_window.project_event(event)
                if repaired is not None:
                    event = repaired
                    page_repaired = True
                elif history_repair_window.is_hidden(event):
                    event = {
                        **event, "prompt": "",
                        "provider_history_repair": "source_proven_import",
                        TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD: True,
                    }
                    page_repaired = True
            before_projection = event
            event = project_legacy_imported_provider_event(event, session_id)
            page_repaired = page_repaired or (
                event is not before_projection
                and event.get("provider_history_repair") in {
                    "source_proven_import", "source_proven_assistant_replay", "source_proven_native_replay",
                }
            )
            hidden_imported_prompt = bool(
                event.get(TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD)
            )
            event_type = str(event.get("type") or "")
            if (
                timeline_index_event_is_hidden(event)
                or run_id in internal_status_run_ids
            ):
                continue
            job_payload = (
                event.get("job")
                if isinstance(event.get("job"), dict)
                else {}
            )
            explicit_job_id = str(
                event.get("job_id") or job_payload.get("id") or ""
            ).strip()
            transition_job_id = explicit_job_id or (
                str(job_by_run.get(run_id) or "").strip()
                if run_id
                else ""
            ) or (run_id if event.get("purpose") == "scheduled_job" else "")
            if (
                timeline_index_is_native_steer_transition_stop(event)
                and not transition_job_id
            ):
                retired_key = (
                    current_turn_by_run.get(run_id)
                    if run_id
                    else active_turn_key
                )
                if retired_key:
                    native_steer_retired_keys.add(retired_key)
                active_turn_key = timeline_index_retire_native_steer_turn(
                    event,
                    current_turn_by_run,
                    active_turn_key,
                )
                continue
            key: str | None = None
            job_attempt_id: str | None = None

            digest_id = str(event.get("digest_job_id") or "").strip()
            cross_chat_key = timeline_index_cross_chat_key(event)
            if event_type == "provider_session_reset":
                key = f"event:{event.get('id') or seq}"
            elif cross_chat_key:
                key = cross_chat_key
                is_target_delivery_event = (
                    event.get("purpose") == "cross_chat_handoff_delivery"
                )
                if run_id and is_target_delivery_event:
                    current_turn_by_run[run_id] = key
                    active_turn_key = key
                if is_target_delivery_event and event_type in {"turn_finished", "turn_stopped"}:
                    if active_turn_key == key:
                        active_turn_key = None
                    if run_id:
                        current_turn_by_run.pop(run_id, None)
            elif digest_id and (
                event_type.startswith("handoff_digest_")
                or event.get("purpose") == "handoff_digest"
            ):
                key = f"digest:{digest_id}"
                if run_id and event.get("purpose") == "handoff_digest":
                    current_turn_by_run[run_id] = key
                    active_turn_key = key
                if event_type == "turn_finished":
                    if active_turn_key == key:
                        active_turn_key = None
                    if run_id:
                        current_turn_by_run.pop(run_id, None)
            elif event_type == "emergency_alert_raised":
                key = f"event:{event.get('id') or seq}"
            else:
                codex_lifecycle_key = timeline_index_codex_lifecycle_key(event)
                job_id = explicit_job_id or (
                    job_by_run.get(run_id) if run_id else None
                )
                if not job_id and (
                    event_type in TIMELINE_INDEX_JOB_TYPES
                    or event.get("purpose") == "scheduled_job"
                    or explicit_job_id
                ):
                    job_id = explicit_job_id or run_id or f"job-{seq}"
                if codex_lifecycle_key:
                    key = codex_lifecycle_key
                elif job_id:
                    occurrence = scheduled_job_occurrence_key(event)
                    occurrence_map_key = (
                        f"{job_id}\0{occurrence}"
                        if job_id and occurrence
                        else ""
                    )
                    occurrence_group = (
                        job_timeline_group_by_occurrence.get(
                            occurrence_map_key
                        )
                        if occurrence_map_key
                        else None
                    )
                    if occurrence_map_key:
                        job_attempt_id = f"occurrence:{occurrence_map_key}"
                    if run_id and event_type == "turn_started":
                        started_group = (
                            occurrence_group
                            or job_timeline_group_by_run_start_seq.get(seq)
                        )
                        if started_group:
                            current_job_timeline_group_by_run[run_id] = (
                                started_group
                            )
                        current_job_attempt_by_run[run_id] = (
                            job_attempt_id or f"run:{run_id}:start:{seq}"
                        )
                    key = (
                        occurrence_group
                        or (
                            current_job_timeline_group_by_run.get(run_id)
                            if run_id
                            else job_timeline_group_by_runless_event_seq.get(seq)
                        )
                    ) or (
                        job_timeline_group_by_run.get(run_id)
                        if run_id
                        else None
                    )
                    if not key and f"job:{job_id}" in selected_by_key:
                        # Compatibility fallback for an index built before
                        # timeline groups were introduced.
                        key = f"job:{job_id}"
                elif timeline_index_is_error(event):
                    key = f"event:{event.get('id') or seq}"
                elif event_type == "turn_started" or is_native_goal_steer_event(event):
                    run_key = run_id or f"seq-{seq}"
                    key = f"turn:{run_key}"
                    if is_native_goal_steer_event(event) or key in seen_user_turn_keys:
                        key = f"turn:{run_key}:start-{seq}"
                    seen_user_turn_keys.add(key)
                    active_turn_key = key
                    if run_id:
                        current_turn_by_run[run_id] = key
                else:
                    key = current_turn_by_run.get(run_id) if run_id else active_turn_key
                    if run_id and not key:
                        key = f"turn:{run_id}"
                    if event_type in {"assistant_text", "turn_finished"}:
                        key = key or f"turn:seq-{seq}"
                        if event_type == "turn_finished":
                            if active_turn_key == key:
                                active_turn_key = None
                            if run_id:
                                current_turn_by_run.pop(run_id, None)
                    elif event_type in {"artifact_created", "file_uploaded"}:
                        file_payload = event.get("artifact") or event.get("file")
                        if not isinstance(file_payload, dict) or (
                            event_type == "file_uploaded" and not key
                        ):
                            key = None
                        else:
                            key = key or f"turn:seq-{seq}"
                    elif event_type in TIMELINE_INDEX_TRACE_TYPES or key:
                        key = key or f"turn:seq-{seq}"
                    else:
                        key = f"event:{event.get('id') or seq}"

            if page_repaired and (
                key in selected_by_key
                or (repair_seq_bounds is not None and repair_seq_bounds[0] < seq <= repair_seq_bounds[1])
            ) and event_files_belong_to_session(event, session_id):
                # Same-ID empty records replace already-cached bogus bubbles.
                # A repaired row can have lost its old semantic landmark.
                # Retain its correction within this page's existing scan.
                page_repairs.append(client_safe_event(event))
            if not key or key not in selected_by_key:
                continue
            if hidden_imported_prompt:
                # The event already updated logical-turn routing above. Its
                # provider-only prompt is not part of the semantic response.
                continue
            if event_type == "turn_stopped" or (
                event_type == "turn_finished"
                and event.get("stopped") is True
            ):
                stopped_turn_keys.add(key)
            if not event_files_belong_to_session(event, session_id):
                continue
            event = client_safe_event(event)
            if key in selected_jobs:
                resolved_job_id = str(
                    selected_by_key[key].get("job_id") or explicit_job_id or ""
                ).strip()
                if not resolved_job_id:
                    resolved_job_id = key.removeprefix("job:").split(
                        ":segment:",
                        1,
                    )[0]
                annotated_event = dict(event)
                annotated_event["job_id"] = resolved_job_id
                annotated_event["job_timeline_group_id"] = key
                if not str(annotated_event.get("purpose") or "").strip():
                    annotated_event["purpose"] = "scheduled_job"
                if not str(annotated_event.get("job_title") or "").strip():
                    annotated_event["job_title"] = str(
                        selected_by_key[key].get("title") or "Scheduled job"
                    )
                add_semantic_job_event(
                    selected_jobs[key],
                    annotated_event,
                    attempt_id=(
                        job_attempt_id
                        or current_job_attempt_by_run.get(run_id)
                        if run_id
                        else None
                    ),
                )
            else:
                events_by_key[key].append(event)

    bundles: list[tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]] = []
    for landmark in selected:
        key = str(landmark.get("key") or "")
        if key in selected_jobs:
            state = selected_jobs[key]
            representatives = list(state["representatives"].values())
            standalone = list(state["standalone"])
            retain_interrupted_trace_anchor = (
                str(state.get("latest_status") or "") == "stopped"
            )
            extras = [
                *list(state["latest_run_extras"]),
                *(
                    [state["latest_run_reasoning"]]
                    if retain_interrupted_trace_anchor
                    and isinstance(state.get("latest_run_reasoning"), dict)
                    else []
                ),
                *(
                    [state["latest_run_tool"]]
                    if retain_interrupted_trace_anchor
                    and isinstance(state.get("latest_run_tool"), dict)
                    else []
                ),
            ]
            optional_by_id = {
                semantic_timeline_event_identity(event): event
                for event in [*standalone, *representatives, *extras]
            }
            optional = sorted(
                optional_by_id.values(),
                key=lambda event: int(event.get("seq") or 0),
                reverse=True,
            )
            bundles.append((
                [semantic_job_summary_event(session_id, state)],
                [],
                [
                    event
                    for event in optional
                    if str(event.get("type") or "") in SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES
                ],
                [
                    event
                    for event in optional
                    if str(event.get("type") or "") not in SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES
                ],
            ))
        elif str(landmark.get("kind") or "") == "codex_lifecycle":
            lifecycle_events = events_by_key.get(key, [])
            started = min(
                (
                    event
                    for event in lifecycle_events
                    if str(event.get("type") or "")
                    == "codex_compaction_started"
                ),
                key=lambda event: int(event.get("seq") or 0),
                default=None,
            )
            completed = max(
                (
                    event
                    for event in lifecycle_events
                    if str(event.get("type") or "")
                    == "codex_compaction_completed"
                ),
                key=lambda event: int(event.get("seq") or 0),
                default=None,
            )
            latest = max(
                lifecycle_events,
                key=lambda event: int(event.get("seq") or 0),
                default=None,
            )
            anchor = started or completed or latest
            completion = (
                completed
                if completed is not None and completed is not anchor
                else None
            )
            # Preserve the arrival anchor and the latest state as one semantic
            # unit. The renderer can update the row in place after completion,
            # while history reload still receives its original position.
            bundles.append((
                [anchor] if anchor is not None else [],
                [],
                [completion] if completion is not None else [],
                [],
            ))
        elif str(landmark.get("kind") or "") == "cross_chat" or key.startswith("cross-chat:handoff:"):
            handoff_events = events_by_key.get(key, [])
            lifecycle_events = [
                event
                for event in handoff_events
                if str(event.get("type") or "").startswith(("cross_chat_", "chat_conversation_message_"))
            ]
            mailbox = any(event.get("delivery_mode") == "mailbox" for event in lifecycle_events)
            arrival_events = [
                event for event in lifecycle_events
                if not (
                    event.get("conversation_mode") == "async_route_v1"
                    and event.get("target_session_id") == session_id
                ) or event.get("delivery_mode") == "mailbox" or (mailbox and event.get("type") == "chat_conversation_message_received") or event.get("type") in {"chat_conversation_message_started", "chat_conversation_message_delivered"}
            ]
            anchor = min(
                arrival_events,
                key=lambda event: int(event.get("seq") or 0),
                default=None,
            )
            latest = max(
                lifecycle_events,
                key=lambda event: int(event.get("seq") or 0),
                default=anchor,
            )
            delivery_events = [
                event
                for event in handoff_events
                if not str(event.get("type") or "").startswith(("cross_chat_", "chat_conversation_message_"))
                and str(event.get("type") or "") != "turn_started"
            ]
            delivery_tool_events = [
                event
                for event in delivery_events
                if str(event.get("type") or "") in {"tool_started", "tool_finished"}
            ]
            (
                delivery_primary,
                delivery_secondary,
                delivery_essential,
                delivery_optional,
            ) = semantic_timeline_ordinary_candidates(
                delivery_events,
                preserve_completed_commentary=True,
            )
            bundles.append((
                (
                    [anchor or latest]
                    if (anchor or latest) is not None
                    else delivery_primary
                ),
                [latest] if latest is not None and latest is not anchor else [],
                [*delivery_primary, *delivery_essential, *delivery_tool_events],
                [*delivery_secondary, *delivery_optional],
            ))
        else:
            bundles.append(semantic_timeline_ordinary_candidates(
                events_by_key.get(key, []),
                preserve_completed_commentary=(
                    key in native_steer_retired_keys
                    or key in stopped_turn_keys
                    # A semantic full-tail refresh replaces the mobile
                    # snapshot. Completed commentary from the unfinished
                    # active turn is therefore durable display content, not
                    # optional trace sampling.
                    or key == active_turn_key
                ),
            ))

    deduplicated: dict[str, dict[str, Any]] = {}

    def append_with_limit(event: dict[str, Any], limit: int) -> bool:
        identity = semantic_timeline_event_identity(event)
        if identity in deduplicated:
            return False
        if len(deduplicated) >= limit:
            return False
        deduplicated[identity] = event
        return True

    # Every selected semantic item gets one representative before any item
    # receives detail. The page-size clamp in read_semantic_timeline_page()
    # guarantees that this baseline always fits.
    for primary, _secondary, _essential, _optional in bundles:
        if primary:
            append_with_limit(primary[0], event_limit)

    # Preserve both sides of ordinary chat turns whenever the response budget
    # permits it. Newer units win only when a caller requests more semantic
    # items than can fit with two events apiece.
    for _primary, secondary, _essential, _optional in reversed(bundles):
        for event in secondary:
            append_with_limit(event, event_limit)

    # Reserve both sides of ordinary turns before emitting repair receipts.
    # A single imported run can contain hundreds of corrected boundaries:
    # charging them to the ordinary budget used to leave only trace anchors
    # for recent turns. Extend that budget only by receipts actually added,
    # retaining its remaining detail capacity and the absolute response cap.
    repair_allowance = min(
        SEMANTIC_TIMELINE_REPAIR_PAGE_OVERFLOW_LIMIT,
        MAX_EVENT_RESPONSE_LIMIT - event_limit,
    )
    repair_limit = len(deduplicated) + repair_allowance
    repair_count = 0
    for event in page_repairs:
        if append_with_limit(event, repair_limit):
            repair_count += 1
    event_limit += repair_count

    # Files and reviewable diffs are semantic output, not optional trace
    # detail. Give each selected item a generous but bounded overflow and also
    # cap total page overflow. This preserves normal multi-video workflows
    # without allowing one pathological turn to return 1,000 media records.
    essential_queues = [
        deque(essential[:SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM])
        for _primary, _secondary, essential, _optional in bundles
    ]
    essential_response_limit = min(
        MAX_EVENT_RESPONSE_LIMIT,
        event_limit + min(
            SEMANTIC_TIMELINE_ESSENTIAL_PAGE_OVERFLOW_LIMIT,
            len(bundles) * SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM,
        ),
    )
    while len(deduplicated) < essential_response_limit:
        made_progress = False
        for queue in reversed(essential_queues):
            while queue:
                event = queue.popleft()
                if append_with_limit(event, essential_response_limit):
                    made_progress = True
                    break
            if len(deduplicated) >= essential_response_limit:
                break
        if not made_progress:
            break

    # Essential media and reviewable diffs always win their pass above. Give
    # each ordinary turn one separately bounded trace handle afterward, even
    # when essentials consumed the base event budget, so lazy reasoning detail
    # remains discoverable without displacing first-class output.
    anchor_queues = [
        deque(
            [anchor]
            if (anchor := next(
                (
                    event
                    for event in optional
                    if semantic_timeline_event_is_trace_anchor(event)
                ),
                None,
            )) is not None
            else []
        )
        for _primary, _secondary, _essential, optional in bundles
    ]
    anchor_response_limit = min(
        MAX_EVENT_RESPONSE_LIMIT,
        max(event_limit, len(deduplicated))
        + min(
            SEMANTIC_TIMELINE_TRACE_ANCHOR_PAGE_OVERFLOW_LIMIT,
            sum(bool(queue) for queue in anchor_queues),
        ),
    )
    while len(deduplicated) < anchor_response_limit:
        made_progress = False
        for queue in reversed(anchor_queues):
            while queue:
                event = queue.popleft()
                if append_with_limit(event, anchor_response_limit):
                    made_progress = True
                    break
            if len(deduplicated) >= anchor_response_limit:
                break
        if not made_progress:
            break

    # Fill remaining space round-robin so one trace-heavy turn cannot crowd
    # every other selected item out of the response.
    optional_queues = [
        deque(optional)
        for _primary, _secondary, _essential, optional in bundles
    ]
    while len(deduplicated) < event_limit:
        made_progress = False
        for queue in reversed(optional_queues):
            while queue:
                event = queue.popleft()
                if append_with_limit(event, event_limit):
                    made_progress = True
                    break
            if len(deduplicated) >= event_limit:
                break
        if not made_progress:
            break

    return sorted(
        deduplicated.values(),
        key=lambda event: (
            int(event.get("seq") or 0),
            1 if event.get("type") == "job_summary" else 0,
            str(event.get("id") or ""),
        ),
    )


def semantic_timeline_landmark_anchor(landmark: dict[str, Any]) -> int:
    # Every landmark, including a scheduled-job segment, stays anchored where
    # it first appeared. Later status/output only advances end_seq.
    return int(landmark.get("start_seq") or landmark.get("end_seq") or 0)


def read_semantic_timeline_page(
    session_id: str,
    *,
    after: int = 0,
    semantic_before: int | None = None,
    limit: int = 500,
    tail: bool = True,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    lock = timeline_index_session_lock(session_id)
    with lock:
        index = _build_timeline_index_locked(session_id)
        cached = timeline_index_cached_entry(session_id, touch=True) or {}
        job_by_run = dict(cached.get("job_by_run") or {})
        job_timeline_group_by_run = dict(
            cached.get("job_timeline_group_by_run") or {}
        )
        job_timeline_group_by_occurrence = dict(
            cached.get("job_timeline_group_by_occurrence") or {}
        )
        job_timeline_group_by_run_start_seq = dict(
            cached.get("job_timeline_group_by_run_start_seq") or {}
        )
        job_timeline_group_by_runless_event_seq = dict(
            cached.get("job_timeline_group_by_runless_event_seq") or {}
        )
        fork_internal_run_ids = set(cached.get("fork_internal_run_ids") or ())
        internal_status_run_ids = set(
            cached.get("internal_status_run_ids") or ()
        )
        records_by_key = cached.get("by_key") or {}
        landmarks = index.get("landmarks") or []
        total = len(landmarks)
        boundary = (
            int(semantic_before)
            if semantic_before is not None
            else None
        )
        eligible_count = 0
        selected_landmarks: deque[dict[str, Any]] = deque(
            maxlen=limit if tail else None
        )
        for landmark in landmarks:
            anchor = semantic_timeline_landmark_anchor(landmark)
            if anchor <= after or (
                boundary is not None and anchor >= boundary
            ):
                continue
            eligible_count += 1
            if tail or len(selected_landmarks) < limit:
                selected_landmarks.append(landmark)
        selected = []
        import_window_ends = []
        run_history_records = cached.get("run_history_records") or {}
        run_history_keys = cached.get("run_history_keys_by_run_id") or {}
        for landmark in selected_landmarks:
            key = str(landmark.get("key") or "")
            record = records_by_key.get(key) or {}
            if key.startswith("turn:import_"):
                # Repeated user rows in a single import share one run. Its last
                # indexed occurrence includes the terminal after the checkpoint.
                # Reuse those byte bounds; never scan from the start to find it.
                run_id = key.removeprefix("turn:").split(":start-", 1)[0]
                for history_key in (run_history_keys.get(run_id) or [])[-1:]:
                    history = run_history_records.get(history_key) or {}
                    end_offset = history.get("end_offset")
                    if type(end_offset) is int and end_offset > 0:
                        import_window_ends.append(end_offset)
            selected.append({
                **dict(landmark),
                "_semantic_original_start_seq": int(
                    record.get("start_seq")
                    or landmark.get("start_seq")
                    or 0
                ),
                "_semantic_start_offset": max(
                    0,
                    int(record.get("start_offset") or 0),
                ),
            })
        if tail:
            omitted_before = max(0, eligible_count - len(selected))
            omitted_after = 0
        else:
            omitted_before = 0
            omitted_after = max(0, eligible_count - len(selected))
    with TIMELINE_INDEX_CACHE_LOCK:
        evict_timeline_index_cache_locked()
    # One bounded, cached proof on an explicit older-page read. No timer,
    # background poll, full-log repair pass, or provider-history mutation.
    history_repair_window = (
        prepare_claude_history_metadata_repair(
            session_id, event_window_end=max(import_window_ends),
        )
        if import_window_ends else None
    )
    events = collect_semantic_timeline_events(
        session_id,
        selected,
        job_by_run=job_by_run,
        job_timeline_group_by_run=job_timeline_group_by_run,
        job_timeline_group_by_occurrence=(
            job_timeline_group_by_occurrence
        ),
        job_timeline_group_by_run_start_seq=(
            job_timeline_group_by_run_start_seq
        ),
        job_timeline_group_by_runless_event_seq=(
            job_timeline_group_by_runless_event_seq
        ),
        fork_internal_run_ids=fork_internal_run_ids,
        internal_status_run_ids=internal_status_run_ids,
        history_repair_window=history_repair_window,
        repair_seq_bounds=(
            max(after, min((int(item.get("_semantic_original_start_seq") or 0)
                            for item in selected), default=after + 1) - 1),
            (boundary - 1 if boundary is not None else int(index.get("latest_seq") or 0))
            if tail else max((int(item.get("end_seq") or 0) for item in selected), default=0),
        ),
        event_limit=min(
            MAX_EVENT_RESPONSE_LIMIT,
            sum(
                max(
                    SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM,
                    SEMANTIC_TIMELINE_JOB_RUN_LIMIT + 1,
                )
                if str(landmark.get("kind") or "") == "job"
                else SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM
                for landmark in selected
            ),
        ),
    )
    earliest_anchor = min(
        (
            semantic_timeline_landmark_anchor(landmark)
            for landmark in selected
        ),
        default=0,
    )
    return {
        "events": events,
        "latest_seq": int(index.get("latest_seq") or 0),
        "semantic_item_count": len(selected),
        "semantic_total": total,
        "semantic_omitted_before": omitted_before,
        "semantic_omitted_after": omitted_after,
        "next_semantic_before": earliest_anchor if omitted_before > 0 and earliest_anchor > 0 else None,
    }


def search_timeline_index(session_id: str, query: str, limit: int = 40) -> dict[str, Any]:
    tokens = timeline_search_tokens(query)
    session = STORE.sessions.get(session_id)
    if not tokens or (
        isinstance(session, dict) and session.get("_fork_initializing")
    ):
        return {"session_id": session_id, "query": query, "results": []}
    connection = history_search_connection()
    try:
        state = connection.execute(
            "SELECT offset FROM history_search_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        rows = connection.execute("""
            SELECT event_id, seq, ts, role, text
            FROM history_search
            WHERE history_search MATCH ? AND session_id = ?
            ORDER BY CAST(seq AS INTEGER) DESC
            LIMIT ?
        """, (timeline_search_fts_query(query), session_id, max(1, min(100, limit)))).fetchall()
    finally:
        connection.close()
    with suppress(OSError):
        if not state or int(state[0]) < events_path(session_id).stat().st_size:
            HISTORY_SEARCH_DIRTY.add(session_id)
    results = [{
        "session_id": session_id,
        "event_id": row[0],
        "seq": int(row[1] or 0),
        "ts": row[2],
        "role": row[3] or "system",
        "snippet": timeline_search_snippet(str(row[4] or ""), tokens),
    } for row in rows]
    return {"session_id": session_id, "query": query, "results": results}


HISTORY_SEARCH_EVENT_TYPES = {
    "turn_started", "turn_steered", "assistant_text", "turn_finished", "reasoning_summary", "error",
    "job_created", "job_ran", "job_started", "job_deferred", "job_finished", "job_error",
    "artifact_created", "artifact_error", "file_uploaded",
    "handoff_digest_started", "handoff_digest_ready", "handoff_digest_received", "handoff_digest_submitted", "handoff_digest_sent",
}
HISTORY_SEARCH_LINE_MARKERS = tuple(
    f'"type":"{event_type}"'.encode("utf-8") for event_type in sorted(HISTORY_SEARCH_EVENT_TYPES)
) + (b'_error"',)


def history_search_event_record(
    event: dict[str, Any],
    internal_run_ids: set[str] | None = None,
    *,
    expected_session_id: str | None = None,
) -> tuple[str, str] | None:
    if is_fork_internal_event(event, internal_run_ids):
        return None
    if not is_client_visible_event(event):
        return None
    projection_session_id = (
        str(expected_session_id or "").strip()
        or str(event.get("session_id") or "").strip()
    )
    if projection_session_id:
        event = project_legacy_imported_provider_event(
            event,
            projection_session_id,
        )
        if event.get(TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD):
            return None
    event_type = str(event.get("type") or "")
    if event_type == "turn_steered" and not is_native_goal_steer_event(event):
        return None
    if event_type not in HISTORY_SEARCH_EVENT_TYPES and not event_type.endswith("_error"):
        return None
    if event_type in TIMELINE_INDEX_JOB_TYPES or event.get("job_id"):
        role = "job"
    elif timeline_index_is_error(event):
        role = "error"
    elif event_type == "turn_started" or is_native_goal_steer_event(event):
        role = "user"
    elif event_type in {"assistant_text", "turn_finished"}:
        role = "assistant"
    elif event_type == "reasoning_summary":
        role = "trace"
    elif event_type in {"artifact_created", "file_uploaded"}:
        role = "file"
    else:
        role = "system"

    values = [timeline_search_event_text(event)]
    job = event.get("job") if isinstance(event.get("job"), dict) else {}
    values.extend((timeline_search_value(job.get("title")), timeline_search_value(job.get("prompt"))))
    file_payload = event.get("artifact") or event.get("file")
    if isinstance(file_payload, dict):
        values.extend((timeline_search_value(file_payload.get("title")), timeline_search_value(file_payload.get("filename"))))
    text = " ".join(dict.fromkeys(value for value in values if value)).strip()
    return (role, text) if text else None


def history_search_connection() -> sqlite3.Connection:
    ensure_dirs()
    connection = sqlite3.connect(HISTORY_SEARCH_DB, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS history_search USING fts5(
            text,
            session_id UNINDEXED,
            event_id UNINDEXED,
            seq UNINDEXED,
            ts UNINDEXED,
            role UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS history_search_state (
            session_id TEXT PRIMARY KEY,
            inode INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history_search_sessions (
            session_id TEXT PRIMARY KEY,
            active INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history_search_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    version = connection.execute(
        "SELECT value FROM history_search_meta WHERE key = 'index_version'"
    ).fetchone()
    if not version or str(version[0]) != HISTORY_SEARCH_INDEX_VERSION:
        connection.execute("DELETE FROM history_search")
        connection.execute("DELETE FROM history_search_state")
        connection.execute(
            "INSERT INTO history_search_meta(key, value) VALUES ('index_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (HISTORY_SEARCH_INDEX_VERSION,),
        )
        connection.commit()
    return connection


def sync_history_search_index(
    connection: sqlite3.Connection,
    target_session_ids: set[str],
    active_session_ids: set[str],
    *,
    prune: bool = False,
) -> tuple[int, int]:
    indexed_events = 0
    indexed_sessions = 0
    if prune:
        connection.execute("DELETE FROM history_search_sessions")
        connection.executemany(
            "INSERT INTO history_search_sessions(session_id, active) VALUES (?, 1)",
            ((session_id,) for session_id in active_session_ids),
        )
        state_rows = connection.execute("SELECT session_id FROM history_search_state").fetchall()
        for (stale_id,) in state_rows:
            if stale_id in active_session_ids:
                continue
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (stale_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (stale_id,))
    else:
        for session_id in target_session_ids:
            active = session_id in active_session_ids
            connection.execute(
                "INSERT INTO history_search_sessions(session_id, active) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET active = excluded.active",
                (session_id, int(active)),
            )
            if not active:
                connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
                connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
                HISTORY_SEARCH_REPAIR_DIRTY.discard(session_id)
    connection.commit()

    insert = "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) VALUES (?, ?, ?, ?, ?, ?)"
    update_state = """
        INSERT INTO history_search_state(session_id, inode, offset, mtime_ns) VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            inode = excluded.inode, offset = excluded.offset, mtime_ns = excluded.mtime_ns
    """
    for session_id in target_session_ids & active_session_ids:
        repair_projection = session_id in HISTORY_SEARCH_REPAIR_DIRTY
        if repair_projection:
            # Refresh an evicted proof only for a repair explicitly requested
            # by a chat read. Ordinary global indexing never discovers logs.
            prepare_codex_goal_history_repair(session_id)
        path = events_path(session_id)
        if not path.exists():
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
            connection.commit()
            HISTORY_SEARCH_REPAIR_DIRTY.discard(session_id)
            continue
        stat = path.stat()
        state = connection.execute(
            "SELECT inode, offset FROM history_search_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        offset = int(state[1]) if state else 0
        if repair_projection or (state and (int(state[0]) != stat.st_ino or stat.st_size < offset)):
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
            connection.commit()
            offset = 0
            HISTORY_SEARCH_REPAIR_DIRTY.discard(session_id)
        if stat.st_size <= offset:
            continue

        indexed_sessions += 1
        internal_run_ids = fork_internal_run_ids(session_id)
        pending: list[tuple[str, str, str, int, str | None, str]] = []
        committed_offset = offset
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                line_start = source.tell()
                raw_line = source.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    source.seek(line_start)
                    break
                committed_offset = source.tell()
                if not any(marker in raw_line for marker in HISTORY_SEARCH_LINE_MARKERS):
                    continue
                with suppress(Exception):
                    event = json.loads(raw_line.decode("utf-8", "replace"))
                    if not event_files_belong_to_session(event, session_id):
                        continue
                    record = history_search_event_record(
                        event,
                        internal_run_ids,
                        expected_session_id=session_id,
                    )
                    if record:
                        role, text = record
                        pending.append((
                            text,
                            session_id,
                            str(event.get("id") or event.get("seq") or uuid.uuid4().hex),
                            int(event.get("seq") or 0),
                            event.get("ts"),
                            role,
                        ))
                if len(pending) >= 1000:
                    connection.executemany(insert, pending)
                    indexed_events += len(pending)
                    pending.clear()
                    connection.execute(update_state, (session_id, stat.st_ino, committed_offset, stat.st_mtime_ns))
                    connection.commit()
        if pending:
            connection.executemany(insert, pending)
            indexed_events += len(pending)
        connection.execute(update_state, (session_id, stat.st_ino, committed_offset, stat.st_mtime_ns))
        connection.commit()
    return indexed_sessions, indexed_events


def run_history_search_sync(
    target_session_ids: set[str],
    active_session_ids: set[str],
    *,
    prune: bool = False,
) -> tuple[int, int]:
    with HISTORY_SEARCH_LOCK:
        connection = history_search_connection()
        try:
            return sync_history_search_index(
                connection,
                target_session_ids,
                active_session_ids,
                prune=prune,
            )
        finally:
            connection.close()


def active_history_search_session_ids() -> set[str]:
    """Return committed, non-archived chats eligible for global indexing."""

    return {
        session_id
        for session_id, session in STORE.sessions.items()
        if isinstance(session, dict)
        and not bool(session.get("archived"))
        and not bool(session.get("_fork_initializing"))
        and not session.get("group_member")
    }


async def history_search_index_loop() -> None:
    if HISTORY_SEARCH_INITIAL_DELAY_SECONDS:
        await asyncio.sleep(HISTORY_SEARCH_INITIAL_DELAY_SECONDS)
    last_full_sync: float | None = None
    while True:
        active_session_ids = active_history_search_session_ids()
        now = time.monotonic()
        full_sync = last_full_sync is None or now - last_full_sync >= HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS
        if full_sync:
            target_session_ids = set(active_session_ids)
            HISTORY_SEARCH_DIRTY.difference_update(target_session_ids)
        else:
            target_session_ids = set(HISTORY_SEARCH_DIRTY)
            HISTORY_SEARCH_DIRTY.difference_update(target_session_ids)
        if target_session_ids or full_sync:
            started = time.monotonic()
            try:
                indexed_sessions, indexed_events = await asyncio.to_thread(
                    run_history_search_sync,
                    target_session_ids,
                    active_session_ids,
                    prune=full_sync,
                )
                if indexed_sessions or indexed_events:
                    logger.info(
                        "history search index synced sessions=%s events=%s elapsed=%.2fs",
                        indexed_sessions,
                        indexed_events,
                        time.monotonic() - started,
                    )
                if full_sync:
                    last_full_sync = now
            except asyncio.CancelledError:
                raise
            except Exception:
                HISTORY_SEARCH_DIRTY.update(target_session_ids)
                logger.exception("history search background sync failed")
        await asyncio.sleep(HISTORY_SEARCH_SYNC_INTERVAL_SECONDS)


def search_all_timelines(
    query: str,
    limit: int = 40,
    eligible_session_ids: set[str] | None = None,
) -> dict[str, Any]:
    tokens = timeline_search_tokens(query)
    if not tokens:
        return {"query": query, "results": []}
    fts_query = timeline_search_fts_query(query)
    # Restrict against the authoritative in-memory session set, not only the
    # asynchronously maintained SQLite active bit. This prevents staged or
    # just-removed fork rows from leaking during background-index latency.
    eligible = sorted(
        active_history_search_session_ids()
        if eligible_session_ids is None
        else eligible_session_ids
    )
    if not eligible:
        return {"query": query, "results": []}
    placeholders = ", ".join("?" for _ in eligible)
    eligibility_clause = f" AND history_search.session_id IN ({placeholders})"
    parameters: list[Any] = [fts_query, *eligible]
    parameters.append(max(1, min(100, limit)))
    connection = history_search_connection()
    try:
        rows = connection.execute(f"""
            WITH ranked AS (
                SELECT history_search.session_id, event_id, seq, ts, role, text,
                       ROW_NUMBER() OVER (
                           PARTITION BY history_search.session_id
                           ORDER BY COALESCE(ts, '') DESC, CAST(seq AS INTEGER) DESC
                       ) AS match_rank,
                       COUNT(*) OVER (PARTITION BY history_search.session_id) AS match_count
                FROM history_search
                JOIN history_search_sessions
                  ON history_search_sessions.session_id = history_search.session_id
                 AND history_search_sessions.active = 1
                WHERE history_search MATCH ?
                {eligibility_clause}
            )
            SELECT session_id, event_id, seq, ts, role, text, match_count
            FROM ranked
            WHERE match_rank = 1
            ORDER BY COALESCE(ts, '') DESC, CAST(seq AS INTEGER) DESC
            LIMIT ?
        """, parameters).fetchall()
    finally:
        connection.close()
    return {
        "query": query,
        "results": [{
            "session_id": row[0],
            "event_id": row[1],
            "seq": int(row[2] or 0),
            "ts": row[3],
            "role": row[4] or "system",
            "snippet": timeline_search_snippet(str(row[5] or ""), tokens),
            "match_count": int(row[6] or 1),
        } for row in rows],
    }
