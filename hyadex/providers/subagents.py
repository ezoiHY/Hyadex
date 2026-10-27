"""Provider subagents: native status, limits and their projection into chats."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import inspect
import json
import logging.handlers
import re
from typing import Any
import uuid
import weakref

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.providers.claude.background_reconciliation import normalize_task_receipts
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CODEX_SETTINGS_FILE,
    CODEX_SUBAGENT_RECONCILE_LIMIT,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_EXEC,
    hyadex_setting,
    SERVER_INSTANCE_ID,
    STOP_CONFIRM_TIMEOUT_SECONDS,
    SUBAGENT_SNAPSHOT_LOG_LIMIT,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
    SUBAGENT_SNAPSHOT_TEXT_LIMIT,
)
from hyadex.core.event_seq import durable_event_seq
from hyadex.core.paths import events_path, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_codex import (
    codex_app_server_managers,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    existing_codex_app_server_manager,
    session_codex_thread_id,
)
from hyadex.core.sessions import append_event, STORE
from hyadex.core.util import utc_timestamp
from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager
from hyadex.providers.claude.subagents import (
    build_claude_subagent_snapshot,
    compact_subagent_text,
    normalize_subagent_status,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.config import (
    _codex_config_positive_int,
    codex_effective_thread_config,
    CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY,
    sanitize_codex_thread_config,
)
from hyadex.providers.codex.history import codex_runtime_user_item_kind
from hyadex.providers.state import PROVIDERS
from hyadex.core.file_store import atomic_json

logger = logging.getLogger("hyadex-server")


def build_subagent_snapshot(session_id: str, limit: int = 64) -> dict[str, Any]:
    """Merge provider-native snapshots without relying on transcript inference."""

    limit = max(1, min(int(limit or 64), SUBAGENT_SNAPSHOT_STATE_LIMIT))
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    session = STORE.sessions.get(session_id) or {}
    if str(session.get("backend") or "") != BACKEND_CODEX:
        claude = build_claude_subagent_snapshot(
            session_id,
            SUBAGENT_SNAPSHOT_STATE_LIMIT,
        )
        for state in claude.get("subagents") or []:
            key = f"{str(state.get('backend') or BACKEND_CLAUDE)}:{state.get('subagent_id')}"
            merged[key] = dict(state)
    with CODEX_SUBAGENT_INDEX_LOCK:
        codex_states = [dict(state) for state in CODEX_SUBAGENT_STATE.values()]
    for state in codex_states:
        if str(state.get("session_id") or "") != session_id:
            continue
        key = f"{str(state.get('backend') or '')}:{state.get('subagent_id')}"
        merged[key] = dict(state)

    active_statuses = {"starting", "running"}
    retained = list(merged.values())
    retained.sort(key=lambda state: (
        0 if normalize_subagent_status(state.get("subagent_status")) in active_statuses else 1,
        -(durable_event_seq(state) or 0),
    ))
    # The desktop persists this array as timeline events. Reconciliation can
    # intentionally learn an old terminal child without appending a duplicate
    # event; that in-memory state has no durable event ID or sequence and must
    # not be advertised as an Event. Older responses that leaked those states
    # caused SQLite binding failures in desktop clients.
    persistable = [
        state
        for state in retained
        if durable_event_seq(state) is not None
        and isinstance(state.get("id"), str)
        and bool(str(state.get("id") or "").strip())
        and str(state.get("session_id") or "") == session_id
        and str(state.get("type") or "") == "subagent_state"
    ]
    selected = persistable[:limit]
    sequences = [seq for state in retained if (seq := durable_event_seq(state)) is not None]
    return {
        "session_id": session_id,
        "subagents": selected,
        "count": len(retained),
        "active_count": sum(
            normalize_subagent_status(state.get("subagent_status")) in active_statuses
            for state in retained
        ),
        "latest_seq": max(sequences, default=0),
    }


def empty_subagent_stop_result() -> dict[str, Any]:
    return {
        "descendants": 0,
        "requested": [],
        "interrupted": [],
        "pending": [],
        "errors": [],
        "fence_committed": True,
    }


