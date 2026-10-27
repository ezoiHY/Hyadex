#!/usr/bin/env python3
"""HyadexServer.

FastAPI service for a native Mac frontend. The server owns agent execution on
the agent host and streams normalized events from Claude Code / Codex CLI runs.

This intentionally mirrors the newest Slack bot's runner shape while removing
Slack-specific transport, formatting, and upload constraints.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import copy
import ctypes
import errno
import fcntl
import glob
import hashlib
import hmac
import ipaddress
import importlib.util
import inspect
import io
import json
from hyadex.store.transcripts import jsonl_records, scan_check, SCAN_SECONDS
import logging
import logging.handlers
import math
import mmap
import os
import pwd
import pty
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import types
import termios
import tempfile
import threading
import time
import unicodedata
import uuid
import weakref
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, AsyncIterator, Callable, Iterable, Iterator, Literal
from urllib.parse import quote, unquote, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from hyadex.app import ApplicationBindings, create_app
from hyadex.providers.runtime import (
    MAX_PROVIDER_RUNTIME_CONTEXT_BYTES,
    MAX_PROVIDER_RUNTIME_ENV_VARS,
    PROVIDER_SECRET_ENV_NAMES,
    ProviderRuntimeContextError,
    ProviderTurnPayload,
    assert_provider_user_message_unchanged,
    is_scrubbed_provider_runtime_env_name,
    scrub_provider_runtime_environment,
    validate_provider_runtime_context,
    validate_provider_runtime_env,
)
from hyadex.jobs.schedules import (
    VALID_JOB_SCHEDULE_KINDS,
    job_schedule_kind,
    next_job_occurrence,
    normalize_cron_expression,
    normalize_interval_seconds,
    normalize_job_timezone,
    normalize_rrule_expression,
    parse_job_timestamp,
    requested_job_schedule_kind,
)
from hyadex.jobs.context import (
    VALID_JOB_CONTEXT_MODES, job_context_mode, resolve_job_context_contract,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.routing import Match
from starlette.datastructures import Headers
import uvicorn
import websockets
import codex_auth
import server_instances
import local_session_ownership
import provider_usage

from hyadex.providers.codex.app_server import (
    CodexAppServerDisconnected,
    CodexAppServerError,
    CodexAppServerManager,
    CodexAppServerProtocolError,
    CodexAppServerRequestError,
    CodexAppServerSubscriptionClosed,
    CodexAppServerTimeout,
    decline_server_request,
)
from hyadex.providers.claude.sdk import (
    CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
    CLAUDE_PROVIDER_MCP_SERVER_NAME,
    CLAUDE_PROVIDER_MCP_TOOL_NAME,
    CLAUDE_SDK_MCP_STATUS_TRUNCATED_KEY,
    ClaudeSDKConfigurationConflict,
    ClaudeSDKControlTimeout,
    ClaudeSDKGenerationChanged,
    ClaudeSDKMCPServerNotFound,
    ClaudeSDKQueryError,
    ClaudeSDKRunActive,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorError,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
    canonical_claude_mcp_identifier,
    claude_background_tracking_hooks,
    claude_nondurable_scheduler_reason,
    create_claude_agent_options,
    create_claude_sdk_mcp_server,
    default_claude_sdk_client_factory,
)
from provider_commands import (
    MAX_PROVIDER_COMMANDS,
    ProviderCommandInventory,
    ProviderCommandRecord,
    claude_provider_command_inventory,
    codex_provider_command_inventory,
    empty_provider_command_inventory,
)
from hyadex.store.files import atomic_json
from claude_history_repair import ClaudeMetadataRepairCache, enrich_interruption_origins, filter_native_claude_mailbox_wake_items
from codex_history_repair import (
    CodexGoalHistoryRepairCache, CodexNativeHistoryRepairCache, CodexNativeHistoryProofUnavailable,
    codex_public_item_origin, filter_native_codex_history_items,
)
from claude_history_provenance import ClaudeInterruptionTracker, normalize_claude_interruption_context
from claude_goals import ClaudeGoalProjection
from claude_background_reconciliation import (
    CONSUMED_EVENT as CLAUDE_BACKGROUND_CONSUMED_EVENT,
    RECONCILED_EVENT as CLAUDE_BACKGROUND_RECONCILED_EVENT,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
    normalize_task_receipts,
    pending_reconciliation_state,
    pending_task_reconciliations,
    reconciliation_envelope,
)
from hyadex import autopilot
from hyadex.routes.autopilot import create_autopilot_router
from hyadex import groups as group_chat
from hyadex.events import EventBus
from hyadex.providers.state import PROVIDERS, ProviderState
from hyadex.core.server import SERVER, ServerState
from hyadex.core.wiring import WIRING
from hyadex.core.runs import RUNS, RunState
from hyadex.routes.groups import create_group_router
import side_questions
import title_generation

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 agent hosts
    tomllib = None

logger = logging.getLogger("hyadex-server")

# --- Extracted modules ---
from hyadex.api import providers as providers_routes
from hyadex.api.providers import (
    delete_claude_goal,
    delete_codex_goal,
    get_claude_runtime,
    get_codex_goal,
    get_codex_runtime,
    post_claude_context_usage_refresh,
    post_claude_interaction_response,
    post_codex_compact,
    post_codex_interaction_response,
    put_claude_goal,
    put_codex_goal,
)
from hyadex.api import search as search_routes
from hyadex.api.search import (
    search_all_session_timelines,
    search_session_timeline,
)
from hyadex.api import sessions as sessions_routes
from hyadex.api.sessions import (
    create_session,
    delete_session,
    fork_session,
    get_session,
    get_session_provider_commands,
    get_session_run_trace,
    get_session_subagents,
    get_timeline_index,
    get_turn_code_diff,
    list_sessions,
    mark_session_read,
    mark_session_unread,
    session_events,
    update_session,
)
from hyadex.api import terminal as terminal_routes
from hyadex.api.terminal import (
    capture_session_tmux_pane,
    delete_session_terminal,
    exit_terminal_auto_scroll,
    get_session_terminal,
    get_session_terminal_windows,
    get_session_tmux_panes,
    input_session_terminal,
    open_session_terminal,
    resize_session_terminal,
    resize_terminal_pane,
    run_session_terminal_action,
    session_terminal,
)
from hyadex.api import admin as admin_routes
from hyadex.api.admin import (
    get_codex_goals_admin_endpoint,
    get_codex_subagents_admin_endpoint,
    put_codex_goals_admin_endpoint,
    put_codex_subagents_admin_endpoint,
    restart_server_endpoint,
    server_restart_status_endpoint,
)
from hyadex.api import health as health_routes
from hyadex.api.health import (
    complete_working_directory,
    connection_addresses,
    health,
    HYADEX_SERVER_DISPLAY_NAME,
    runtime_catalog,
    runtime_provider_usage,
)
from hyadex.api.common import (
    require_native_admin_control,
)
from hyadex.api import provider_mcp as provider_mcp_routes
from hyadex.api.provider_mcp import (
    codex_provider_mcp,
)
from hyadex.api import mount_routes
from hyadex.ops.lifecycle import (
    _restart_server_endpoint_impl,
    accept_forced_server_restart,
    AUTOPILOT,
    bounded_shutdown_phase,
    configured_uvicorn_graceful_shutdown_seconds,
    current_server_restart_blocker_snapshot,
    DEFAULT_UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
    detect_managed_server_service_kind,
    execution_maintenance_control,
    force_kill_managed_server_after_deadline,
    FORCED_RESTART_AUDIT_FLAGS,
    host_health_record,
    host_monitor_loop,
    lifespan,
    log_server_restart_denial,
    macos_launchd_owns_current_process,
    managed_server_service_kind,
    MAX_UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
    official_server_release_tree,
    public_forced_restart_audit,
    public_server_restart_status,
    public_server_restart_work_counts,
    purge_provider_authority_files_after_restart,
    reconcile_server_restart_status_after_startup,
    require_server_restart_control,
    restart_blocking_queued_turn_count_locked,
    server_lifecycle_blocker_counts,
    SERVER_LIFECYCLE_OPERATION_LOCK,
    server_restart_blocker_snapshot_locked,
    SERVER_RESTART_BLOCKER_SNAPSHOT_VERSION,
    server_restart_capability,
    SERVER_RESTART_COOLDOWN_SECONDS,
    SERVER_RESTART_COUNT_LIMIT,
    SERVER_RESTART_FORCE_KILL_DELAY_SECONDS,
    SERVER_RESTART_FORCE_LOCK_TIMEOUT_SECONDS,
    SERVER_RESTART_FORCE_PROBE_TIMEOUT_SECONDS,
    SERVER_RESTART_GRACEFUL_KILL_DELAY_SECONDS,
    server_restart_request_fingerprint,
    server_restart_safety_work_message,
    SERVER_RESTART_SIGNAL_DELAY_SECONDS,
    SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS,
    server_restart_work_message,
    SERVER_SHUTDOWN_PHASE_COUNT,
    SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS,
    SERVER_SHUTDOWN_STRAGGLERS,
    ServerRestartRequest,
    signal_forced_server_restart,
    signal_managed_server_restart,
    start_forced_server_restart_signal_thread,
    trim_process_args,
    uvicorn_graceful_shutdown_seconds,
    write_bounded_jsonl,
)
from hyadex.features.search import (
    active_history_search_session_ids,
    collect_semantic_timeline_events,
    history_search_connection,
    HISTORY_SEARCH_DB,
    history_search_event_record,
    HISTORY_SEARCH_EVENT_TYPES,
    HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS,
    history_search_index_loop,
    HISTORY_SEARCH_INDEX_VERSION,
    HISTORY_SEARCH_INITIAL_DELAY_SECONDS,
    HISTORY_SEARCH_LINE_MARKERS,
    HISTORY_SEARCH_LOCK,
    HISTORY_SEARCH_SYNC_INTERVAL_SECONDS,
    read_semantic_timeline_page,
    run_history_search_sync,
    search_all_timelines,
    search_timeline_index,
    SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES,
    SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM,
    SEMANTIC_TIMELINE_ESSENTIAL_PAGE_OVERFLOW_LIMIT,
    SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM,
    semantic_timeline_event_identity,
    semantic_timeline_event_is_completed_commentary,
    semantic_timeline_event_is_display,
    semantic_timeline_event_is_trace_anchor,
    semantic_timeline_landmark_anchor,
    semantic_timeline_ordinary_candidates,
    SEMANTIC_TIMELINE_REPAIR_PAGE_OVERFLOW_LIMIT,
    SEMANTIC_TIMELINE_TRACE_ANCHOR_PAGE_OVERFLOW_LIMIT,
    sync_history_search_index,
    timeline_search_event_text,
    timeline_search_fts_query,
    timeline_search_snippet,
    timeline_search_tokens,
    timeline_search_value,
)
from hyadex.features.jobs import (
    add_semantic_job_event,
    append_provider_job_mutation_event,
    authorize_provider_jobs_operation,
    clear_job_runtime_unavailable_deferral,
    clear_job_update_park,
    compact_job_interval,
    durable_scheduled_job_admissions,
    ensure_discardable_empty_session,
    event_job,
    get_session_job_runs,
    JOBS,
    JobStore,
    list_session_jobs,
    new_semantic_job_state,
    provider_public_job,
    provider_public_job_run,
    public_job,
    read_scheduled_job_runs,
    scheduled_job_blocker,
    scheduled_job_history_response_event,
    scheduled_job_lifecycle_admission_defer_reason,
    scheduled_job_resource_diagnostics,
    scheduled_job_runtime_unavailable_reason,
    ScheduledJobRevisionChanged,
    semantic_job_event_is_result,
    semantic_job_event_priority,
    semantic_job_summary_event,
    SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT,
    SEMANTIC_TIMELINE_JOB_RUN_LIMIT,
    validate_job_text_bounds,
    write_jobs_registry,
)
from hyadex.features.group_runtime import (
    GROUPS,
    HyadexGroupRuntime,
    new_job_revision,
    start_group_job_run,
)
from hyadex.features.forks import (
    _fork_session_locked,
    cleanup_abandoned_fork_provider_threads,
    cleanup_aborted_session_fork,
    clear_staged_fork_provider_reference,
    completed_fork_events,
    ForkSessionRequest,
    save_staged_fork_provider_reference,
)
from hyadex.core.stop import (
    _run_queued_turn_now_and_release,
    _run_queued_turn_now_once,
    abandoned_turn_after_restart,
    join_cancelled_tasks,
    recover_abandoned_turns_after_start,
    request_explicit_stop,
    RESTART_ORPHAN_ACTIVITY_TYPES,
    RESTART_ORPHAN_TERMINAL_TYPES,
    run_explicit_stop_operation,
    run_queued_turn_now,
    stop_turn,
    tail_jsonl_file,
)
from hyadex.core.turns import (
    _delete_codex_goal_locked,
    _post_codex_compact_locked,
    _put_codex_goal_locked,
    _recover_queued_turns_for_sessions,
    _start_next_queued_turn_locked,
    _start_turn_locked,
    active_run_summaries,
    append_active_stdout,
    append_turn_finished_event,
    bind_active_turn,
    broadcast_reasoning_summary_stream,
    build_memory_augmented_prompt,
    clear_active_process,
    clear_reasoning_summary_stream,
    consume_codex_native_turn,
    enqueue_turn,
    execute_receipted_turn,
    explicit_stop_session_ids,
    finalize_owned_turn_finished,
    finish_cancelled_claude_sdk_start,
    finish_claude_sdk_start_failure,
    finish_codex_control_terminal_publication,
    finish_reasoning_summary_stream,
    flush_reasoning_summary_stream,
    interactive_client_capabilities,
    jsonable_active_run,
    managed_server_restart_is_planned,
    persist_reasoning_summary,
    post_turn_with_receipt,
    post_turn_without_receipt,
    provider_turn_may_manage_jobs,
    read_indexed_run_trace,
    reasoning_summary_stream_item,
    reconcile_idle_queue_session,
    reconcile_idle_queue_session_from_snapshot,
    reconcile_idle_queued_turns,
    reconcile_idle_queued_turns_from_health_poll,
    reconcile_provider_task_exit,
    recover_queued_turns_after_start,
    recover_turn_receipt,
    release_codex_control_thread,
    release_turn_slot,
    retry_claude_stop_fence,
    retry_next_queued_turn_later,
    run_claude,
    run_claude_print,
    run_claude_sdk,
    run_codex,
    run_codex_app_server,
    run_codex_exec,
    schedule_claude_stop_fence_retry,
    schedule_next_queued_turn,
    schedule_queued_turn_retry,
    schedule_steered_turn_slot_waiter,
    server_lifecycle_active_session_ids_locked,
    set_claude_goal,
    start_claude_goal_command,
    start_codex_goal_resume_consumer,
    start_next_queued_turn,
    start_turn,
    start_turn_durably,
    supervise_provider_turn_task,
    turn_receipt_path,
    update_reasoning_summary_stream,
    wait_for_steered_turn_slot,
    write_turn_receipt,
)
from hyadex.features.emergency import (
    AcknowledgeEmergencyRequest,
    broadcast_committed_emergency_removal,
    broadcast_emergency_session,
    broadcast_emergency_session_removed,
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
from hyadex.core.http import (
    agent_helper_browser_request_forbidden,
    AgentCreateScopedJobRequest,
    AgentNotificationRequest,
    AgentUpdateJobRequest,
    bearer_token,
    bounded_request_content_length,
    CreateJobRequest,
    CreateScopedJobRequest,
    declared_auth_routes,
    declared_route_auth,
    JobCreateFields,
    live_unsafe_http_mutation_ids_locked,
    prebuffer_bounded_request_body,
    prebuffer_declared_body,
    privileged_native_browser_request_forbidden,
    privileged_native_json_transport,
    register_unsafe_http_mutation_locked,
    release_unsafe_http_mutation,
    request_authorized,
    request_exact_bearer_authorized,
    request_exact_native_token_header_authorized,
    request_exact_restart_header_authorized,
    request_route_parses_body,
    route_auth,
    router,
    server_restart_browser_request_forbidden,
    server_restart_error_detail,
    SERVER_RESTART_MAX_BODY_BYTES,
    server_restart_post_transport_error,
    TerminalActionRequest,
    TerminalInputRequest,
    TerminalOpenRequest,
    TerminalResizeRequest,
    token_matches,
    unsafe_http_mutation_blocked_response,
    unsafe_http_mutation_count_locked,
    UnsafeMutationAdmissionRejected,
    UpdateJobRequest,
    validate_listener_auth,
    websocket_authorized,
    websocket_endpoint_subprotocol,
    websocket_requested_protocols,
    websocket_token_subprotocol,
)
from hyadex.providers.codex.runtime import (
    _CODEX_CANCEL_INTERACTION_FUTURE,
    ABANDONED_FORK_PROVIDER_THREADS_LOCK,
    acquire_codex_control_thread,
    acquire_codex_interactive_control_lease,
    acquire_codex_run_thread,
    active_codex_approval_policy,
    active_codex_work_labels,
    active_provider_background_work_labels,
    active_provider_background_work_labels_async,
    apply_codex_goal_time_budget_limit,
    archive_rejected_codex_session,
    bind_codex_control_reservation,
    bind_forked_codex_thread,
    broadcast_codex_usage_changed,
    build_claude_sdk_options,
    build_codex_cmd,
    build_fork_memory,
    cache_codex_approval_item,
    cancel_codex_interactions,
    cancel_codex_native_actions,
    claim_codex_control_terminal_publication,
    close_codex_app_server_manager,
    codex_app_server_changed_paths,
    CODEX_APP_SERVER_EVICTING_THREADS,
    CODEX_APP_SERVER_INVALIDATED_THREADS,
    codex_app_server_manager,
    codex_app_server_manager_for_thread,
    CODEX_APP_SERVER_MANAGER_LOCK,
    CODEX_APP_SERVER_PINNED_THREADS,
    codex_app_server_reasoning_plaintext,
    codex_app_server_reasoning_summary,
    CODEX_APP_SERVER_THREAD_LRU,
    CODEX_APP_SERVER_THREAD_LRU_LOCK,
    CODEX_APP_SERVER_THREAD_PIN_COUNTS,
    codex_app_server_tool,
    codex_app_server_tool_output,
    CODEX_APPROVAL_ITEM_CACHE,
    codex_binary_identity,
    CODEX_BINARY_PROBE_LOCK,
    codex_call_tool,
    codex_compaction_error_message,
    codex_compaction_retry_mode,
    CODEX_COMPACTION_WARN_COUNT,
    codex_control_http_error,
    CODEX_CONTROL_TERMINAL_FENCES,
    codex_exec_agent_message,
    codex_exec_raw_event_text,
    codex_goal_api_is_unsupported,
    CODEX_GOAL_SYNC_GENERATIONS,
    CODEX_GOALS_ADMIN_MAX_BODY_BYTES,
    codex_goals_admin_status,
    codex_goals_cli_args,
    CODEX_HYGIENE_TASKS,
    CODEX_HYGIENE_WARNING_RANK,
    CODEX_INTERACTION_HANDLER_TASKS,
    CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS,
    CODEX_INTERACTIVE_CONTROL_THREADS,
    CODEX_LOGIN_PROBE_LOCK,
    codex_manager_has_callbacks,
    codex_manager_has_callers,
    codex_manager_owns_notification,
    codex_manager_session_busy,
    codex_manager_work_belongs_to_session,
    CODEX_NATIVE_ACTION_TASKS,
    codex_native_archive_rejection,
    codex_native_compaction_was_terminal,
    codex_native_goal_owner_matches,
    codex_notification_run_id,
    codex_output_exit_code,
    CODEX_PENDING_INTERACTIONS,
    CODEX_PENDING_INTERACTIONS_LOCK,
    codex_permission_value_is_subset,
    codex_provider_command_turn_input,
    codex_provider_mcp_config,
    codex_provider_mcp_jsonrpc_error,
    codex_provider_mcp_run_proof,
    codex_provider_mcp_tool_definition,
    CODEX_QUARANTINED_GOAL_THREADS,
    codex_raw_developer_message,
    codex_reasoning_text,
    codex_request_is_interactive,
    codex_result_error,
    codex_runtime_snapshot,
    codex_session_has_active_run,
    codex_session_id_for_thread,
    codex_thread_has_local_run_owner,
    codex_thread_hygiene,
    codex_thread_hygiene_message,
    codex_thread_hygiene_public,
    codex_thread_hygiene_warning,
    codex_thread_instruction_hash,
    codex_thread_instructions,
    codex_thread_params,
    codex_thread_status_message,
    CODEX_TOKEN_USAGE_PERSIST_INTERVAL_SECONDS,
    codex_token_usage_snapshot,
    codex_turn_matches_client_user_message,
    codex_user_developer_instructions,
    CodexExecMessageBuffer,
    CodexForkCleanupError,
    CodexGoalRequest,
    CodexGoalsAdminRequest,
    CodexInteractionResponseRequest,
    commit_codex_goal_steer,
    create_native_side_chat,
    current_codex_token_usage_snapshot,
    discover_session_provider_commands,
    drain_retired_codex_managers,
    effective_codex_approval_policy,
    ensure_codex_app_server_thread,
    evict_codex_app_server_thread,
    execute_provider_tool,
    execute_provider_tool_once,
    existing_codex_app_server_manager_for_thread,
    expire_codex_login_diagnostic,
    finish_codex_interaction_locked,
    forget_abandoned_fork_provider_thread,
    fork_codex_thread,
    format_byte_size,
    get_codex_goals_admin,
    handle_codex_server_request,
    invalidated_codex_thread_error,
    is_codex_app_server_retry_notice,
    is_codex_compaction_failure,
    is_codex_reconnect_notice,
    is_codex_resume_failure,
    is_silent_codex_completion,
    mark_codex_exec_context_usage_unavailable,
    migrate_retired_codex_selection,
    parse_codex_call_arguments,
    pause_active_codex_goal_for_stop,
    pause_idle_codex_goals_before_disable,
    persist_abandoned_fork_provider_thread,
    persist_reconciled_codex_goal,
    pin_codex_app_server_thread,
    prepare_codex_app_server_process,
    prepare_codex_login_turn,
    project_codex_notification,
    project_codex_usage_notification,
    provider_background_work_labels_from_snapshot,
    provider_tool_capability_snapshot,
    public_codex_interaction,
    put_codex_goals_admin,
    quarantine_codex_goal_thread,
    queued_codex_runtime_matches_active,
    reconcile_codex_thread_goal,
    record_codex_token_usage,
    recover_abandoned_codex_compactions_after_start,
    refresh_codex_app_server_binary,
    refresh_codex_app_server_login,
    register_codex_app_server_child,
    register_codex_native_action,
    release_codex_control_slot,
    release_codex_interactive_control_lease,
    release_idle_codex_manager_session,
    request_client_is_codex_provider_mcp_local,
    request_exact_codex_provider_mcp_secret,
    reset_codex_ephemeral_runtime_metadata,
    resolve_codex_interaction,
    resolve_provider_command_selection,
    retain_codex_goal_run_owner,
    retain_codex_manager_caller,
    retire_failed_codex_fork,
    retire_invalidated_codex_app_server_thread,
    retire_or_record_failed_codex_fork,
    rollover_codex_provider_session,
    schedule_codex_manager_drain,
    schedule_codex_thread_hygiene_check,
    send_codex_goal_steer,
    session_references_codex_thread,
    settle_idle_codex_goal_for_stop,
    should_recover_codex_resume,
    SIDE_QUESTIONS,
    start_standalone_codex_app_server_thread,
    stop_codex_goal_resume,
    terminal_codex_token_usage_run_id,
    touch_codex_app_server_thread,
    turn_start_blocker,
    unpin_codex_app_server_thread,
    unregister_codex_app_server_child,
    update_codex_pending_session_metadata,
    validate_codex_goal_resume_budget,
    validate_codex_interaction_response,
    validate_inherited_codex_goal,
    warn_codex_thread_hygiene,
)
from hyadex.core.queue import (
    _publish_artifact_entries_unlocked,
    append_claude_history_event,
    append_codex_history_event,
    append_durable_event,
    append_durable_event_batch,
    append_durable_event_batch_locked,
    append_imported_history,
    append_manifest_artifact_error,
    await_native_steer_result,
    claude_completed_fork_boundary,
    claude_history_event_item,
    claude_imported_fork_boundary,
    codex_goal_followup_requires_native,
    codex_goal_steer_selection_is_plain,
    codex_history_event_item,
    codex_history_user_event_item,
    codex_history_user_item,
    collect_manifest,
    collect_recent_leftover_manifests,
    commit_staged_upload,
    create_session_with_history,
    deferred_force_send_outcome,
    fence_native_steer_delivery,
    filter_codex_history_for_import,
    force_send_conflict_detail,
    force_send_task_owner,
    import_session_history,
    legacy_delivery_identity,
    load_provider_history_with_cursor,
    log_queue_promotion_fence,
    mark_native_steer_accepted,
    move_queued_turn,
    MoveQueuedTurnRequest,
    native_steer_requeue_event_payload,
    NonNativeForceSendRequiresLifecycleLock,
    parse_claude_history,
    parse_claude_history_events,
    parse_codex_history,
    parse_codex_history_events,
    parse_legacy_steering_lineage,
    parse_provider_history_delta,
    pause_queued_turns_after_explicit_stop,
    persist_artifact_records,
    prepare_claude_history_metadata_repair,
    prepare_codex_goal_history_repair,
    prepare_codex_native_history_repair,
    prepare_imported_history_items,
    prepare_provider_history_metadata_repair,
    prepare_steered_turn,
    provider_history,
    public_queued_turn,
    publish_artifact_entries,
    queue_checkpoint_anchor,
    queue_checkpoint_loop,
    queue_checkpoint_seed,
    queue_positions,
    queue_promotion_fence,
    queue_recovery_readiness,
    queued_event_lines,
    queued_turn_from_event,
    queued_turn_run_metadata,
    queued_turn_unqueued_payload,
    queued_turns_snapshot,
    rebuild_queued_turns_from_events,
    reject_promoted_queue_mutation,
    request_fields_set,
    requeue_native_steer_after_safe_rejection,
    requeue_turn_front,
    restore_missing_durable_force_send_row,
    run_provider_history_sync,
    RunQueuedTurnNowRequest,
    save_queue_checkpoint,
    scan_queued_turns_from_events,
    schedule_provider_history_sync,
    should_schedule_queue_after_finish,
    stop_idle_claude_background_subagents,
    stop_idle_claude_background_subagents_bounded,
    strip_hyadex_generated_user_text,
    sync_provider_history,
    terminally_discard_queued_turn,
    turn_steering_lineage,
    unqueue_turn,
    update_queued_turn,
    UpdateQueuedTurnRequest,
    wait_for_queue_recovery_admission,
    watch_manifest_artifacts,
    withdraw_unaccepted_native_steer,
)
from hyadex.providers.registry import (
    _provider_child_int,
    auth_failure_text,
    authorize_provider_action,
    build_turn_provider_prompt,
    build_user_provider_prompt,
    discover_codex_catalog,
    discover_runtime_backend_catalog,
    discover_runtime_catalog,
    discover_runtime_catalog_within_budget,
    ensure_runtime_available,
    interactive_backend_supported,
    interactive_supported_backends,
    issue_native_steer_provider_authority,
    kill_registered_provider_children,
    native_steer_provider_actions,
    network_host_is_loopback,
    normalize_steering_lineage,
    other_local_instance_provider_keys,
    persist_run_provider_session,
    prepare_provider_background_work_snapshot,
    probe_runtime,
    provider_child_command_fingerprint,
    provider_child_command_line,
    provider_child_executable_identity,
    provider_child_process_exists,
    provider_child_process_start_identity,
    provider_child_registry_entry_is_owned,
    PROVIDER_CHILDREN_LOCK,
    PROVIDER_COMMAND_EMPTY_FALLBACK_SECRET,
    provider_command_invocation_token,
    provider_command_prompt_matches,
    PROVIDER_COMMAND_SELECTOR_SECRET,
    provider_command_selector_secret,
    provider_command_snapshot,
    provider_command_support,
    provider_context_goal_is_exhausted,
    provider_host_boot_identity,
    provider_turn_may_raise_emergency,
    ProviderCommandSelectionInvalid,
    ProviderCommandSelectionUnavailable,
    public_runtime_diagnostic,
    queued_turn_uses_retired_routing,
    read_provider_children_registry,
    record_runtime_failure,
    record_runtime_success,
    redacted_provider_argv,
    refresh_runtime_diagnostics,
    register_provider_child,
    request_client_is_loopback,
    request_client_is_provider_helper_local,
    request_has_agent_helper_proxy_headers,
    runtime_action,
    runtime_diagnostic,
    RUNTIME_DIAGNOSTIC_GENERATIONS,
    runtime_diagnostic_payload,
    runtime_diagnostics_snapshot,
    runtime_display_name,
    runtime_executable,
    runtime_priority,
    safe_runtime_version,
    standalone_provider_session,
    store_runtime_diagnostic,
    strip_hyadex_provider_context,
    sweep_orphaned_provider_children,
    turn_slot_is_owned,
    unregister_provider_child,
    write_provider_children_registry,
)
from hyadex.features.side_questions import (
    codex_app_server_service_tier,
    CODEX_APP_SERVER_SERVICE_TIER_ALIASES,
    CODEX_GOALS_RECONFIGURING,
    flatten_codex_config_overrides,
    require_side_question_admission,
)
from hyadex.providers.claude.runtime import (
    acknowledge_claude_background_reconciliation,
    active_claude_permission_mode,
    build_claude_cmd,
    cancel_claude_interactions,
    cancel_claude_stop_fence_retry,
    CLAUDE_ACCOUNT_USAGE,
    CLAUDE_ACCOUNT_USAGE_FAILURE_TTL_SECONDS,
    CLAUDE_ACCOUNT_USAGE_LOCK,
    CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS,
    CLAUDE_ACCOUNT_USAGE_TTL_SECONDS,
    claude_background_work_labels_from_events,
    claude_context_usage_snapshot,
    CLAUDE_EMPTY_TURN_ERROR,
    claude_empty_turn_failure_message,
    claude_event_file_fingerprints,
    claude_fallback_model_options,
    claude_fork_has_conversation,
    CLAUDE_GOAL_LOCKS,
    CLAUDE_GOAL_PATHS,
    CLAUDE_GOAL_PENDING,
    CLAUDE_GOAL_PROJECTIONS,
    CLAUDE_INTERACTION_HANDLER_TASKS,
    claude_interaction_method,
    claude_models_api_base_url_allowed,
    CLAUDE_PENDING_INTERACTIONS,
    CLAUDE_PENDING_INTERACTIONS_LOCK,
    claude_permission_context_value,
    claude_permission_update_public_value,
    claude_project_dir_for_cwd,
    claude_provider_id_for_session,
    CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL,
    claude_question_params,
    claude_result_error,
    claude_resume_file_for_cwd,
    claude_run_has_pending_interaction,
    claude_runtime_snapshot,
    claude_sdk_cli_path,
    claude_sdk_configuration_key,
    claude_sdk_dependency_available,
    claude_sdk_manager,
    CLAUDE_SDK_MANAGER_LOCK,
    claude_sdk_result_details,
    CLAUDE_STOP_FENCE_RETRY_DELAYS_SECONDS,
    CLAUDE_STOP_FENCE_RETRY_TASKS,
    claude_supports_effort,
    claude_supports_no_session_persistence,
    CLAUDE_TASK_NOTIFICATION_CLOSE,
    CLAUDE_TASK_NOTIFICATION_OPEN,
    ClaudeGoalRequest,
    ClaudeInteractionResponseRequest,
    close_claude_sdk_manager,
    detached_stop_in_progress,
    discover_claude_native_models,
    discover_claude_provider_models,
    ensure_provider_manager_factory_admission,
    evict_failed_idle_claude_context_sample,
    explicit_stop_in_progress,
    handle_claude_tool_permission,
    interrupt_claude_sdk_run_bounded,
    invalidate_failed_claude_context_usage_sample,
    is_claude_internal_diagnostic,
    is_claude_task_notification_text,
    is_expected_claude_interruption_result,
    loaded_claude_background_session_state,
    mark_claude_sdk_turn_ready_after_ack,
    mark_provider_turn_ready,
    MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES,
    observe_claude_provider_usage,
    parse_claude_help_catalog,
    parse_claude_help_model_option_groups,
    parse_claude_help_model_options,
    parse_claude_models_api_payload,
    persist_claude_background_task_receipts,
    probe_claude_account_usage,
    project_claude_sdk_message,
    PROVIDER_WORK_LABEL_LIMIT,
    public_claude_interaction,
    queued_claude_runtime_matches_active,
    read_claude_account_usage,
    record_claude_context_usage,
    refresh_claude_goal,
    refresh_idle_claude_context_usage,
    require_claude_goal_session,
    resolve_claude_interaction,
    resolve_claude_resume_provider,
    run_catalog_command,
    runtime_catalog_budget_expired,
    runtime_command,
    runtime_option,
    runtime_probe_timeout,
    RuntimeCatalogBudgetExpired,
    sample_claude_context_usage,
    server_default_runtime_option,
    stop_cleanup_in_progress,
    title_effort_label,
    title_model_label,
    tool_changed_paths,
    TurnRequest,
    unique_runtime_options,
    update_claude_pending_session_metadata,
    validate_claude_interaction_response,
    validated_claude_fork_provider_id,
)
from hyadex.providers.subagents import (
    _emit_codex_subagent_state_once,
    build_claude_subagent_snapshot,
    build_subagent_snapshot,
    cache_codex_subagent_native_statuses,
    cached_codex_subagent_native_statuses,
    claude_sdk_field,
    claude_sdk_subagent_lifecycle_raw,
    claude_sdk_type,
    claude_stop_fence_attempt_lock,
    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
    claude_subagent_child_activity,
    claude_subagent_progress_activity,
    codex_child_status_from_thread,
    codex_child_status_from_turn,
    codex_collaboration_states,
    codex_collaboration_thread_ids,
    CODEX_GOALS_CONFIG_LOCK,
    codex_session_has_live_subagents,
    CODEX_SUBAGENT_FINALIZE_TASKS,
    CODEX_SUBAGENT_FINALIZE_TIMEOUT_SECONDS,
    codex_subagent_has_live_owner,
    CODEX_SUBAGENT_NATIVE_STATUS_CACHES,
    codex_subagent_native_turn_candidates,
    codex_subagent_native_turn_scopes,
    CODEX_SUBAGENT_SCAN_LIMIT,
    CODEX_SUBAGENT_SCAN_TIMEOUT_SECONDS,
    codex_subagent_thread_identity,
    CODEX_SUBAGENT_TRANSITION_LOCKS,
    codex_subagents_admin_status,
    CodexSubagentsAdminRequest,
    compact_subagent_text,
    discard_unused_claude_stop_fence_attempt_lock,
    emit_codex_subagent_state,
    empty_subagent_stop_result,
    evict_claude_sdk_chat,
    finalize_codex_subagents_after_run,
    get_codex_subagents_admin,
    is_codex_subagent_notification_user_item,
    normalize_subagent_status,
    prepare_codex_subagent_terminal_snapshot,
    project_codex_subagent_item,
    put_codex_subagents_admin,
    read_codex_admin_settings,
    reconcile_codex_subagents,
    record_codex_subagent_limit_application,
    schedule_codex_subagent_finalization,
    stop_codex_descendant_subagents,
    useful_subagent_identity_text,
)
from hyadex.features.files import (
    _capture_git_tree,
    _code_diff_git_env,
    _copy_private_bounded_file,
    _discover_git_snapshot_paths,
    _git_command,
    _git_stream_stdout,
    _normalize_changed_paths,
    _open_private_exclusive,
    _parse_git_numstat,
    _write_nul_pathspec,
    _write_turn_code_diff,
    active_artifact_publication_run,
    artifact_entries_digest,
    artifact_event_receipt,
    artifact_publication_id,
    artifact_publication_lock,
    ARTIFACT_PUBLICATION_LOCK_STRIPES,
    artifact_record,
    capture_git_baseline,
    discard_git_baseline,
    discard_owned_git_baselines,
    file_attachment_prompt_lines,
    file_signature,
    GIT_BASELINES_BY_TASK,
    legacy_attachment_paths,
    legacy_file_ids_for_paths,
    live_manifest_batch_ready,
    live_manifest_entry_ready,
    load_file_meta,
    normalize_artifact_entries,
    normalize_artifact_entry_shapes,
    normalized_artifact_request,
    normalized_link_target,
    path_is_relative_to,
    prepare_artifact_records,
    publish_turn_code_diff,
    PublishArtifactsRequest,
    read_artifact_manifest,
    remove_artifact_records,
    remove_artifact_records_async,
    remove_upload_tree,
    require_session_file_meta,
    run_event_metadata,
    run_upload_worker,
    session_file_for_link,
    split_legacy_attached_files,
    split_legacy_scoped_attachments,
    stage_upload_file_sync,
    unlink_manifest_if_unchanged,
    validate_session_file_ids,
    write_upload_metadata_sync,
)
from hyadex.features.history import (
    append_imported_events,
    append_imported_events_sync,
    clear_imported_active_runs,
    clone_fork_code_diffs,
    clone_fork_code_diffs_async,
    clone_fork_file_record,
    clone_fork_file_record_async,
    committed_history_sync_checkpoint,
    copy_fork_file_data,
    copy_fork_history,
    delete_cloned_fork_code_diffs,
    delete_cloned_fork_files,
    fork_event_file_ids,
    fork_file_source_path,
    FORK_HISTORY_EVENT_TYPES,
    fork_source_file_records,
    history_commit_guard,
    history_cursor_matches_source_stamp,
    history_file_stamp,
    history_item_cursor_digest,
    history_maintenance_key,
    HISTORY_MAINTENANCE_SLOTS,
    history_maintenance_state,
    HISTORY_MAINTENANCE_TASKS,
    history_message_match_details,
    history_message_match_tokens,
    history_messages_match,
    history_projection_notice,
    HISTORY_PROJECTION_REVISIONS,
    history_sync_checkpoint,
    HISTORY_SYNC_CHECKPOINT_VERSION,
    HISTORY_SYNC_SCHEDULED,
    history_timeline_message_keys,
    HistoryPreparationStale,
    imported_history_terminal_event,
    local_history_import_guard,
    persist_history_sync_cursor,
    provider_history_path,
    provider_history_prefix_digest,
    provider_history_source_snapshot,
    provider_history_source_stamp,
    reconcile_cursor_history_items,
    save_history_maintenance_state,
    seed_claude_interruption_context,
    unsynced_history_items,
)
from hyadex.features.terminal import (
    _ensure_terminal_session_locked,
    _kill_terminal_session_locked,
    _terminal_action_locked,
    _update_session,
    await_terminal_task_after_cancellation,
    bootstrap_isolated_tmux_server,
    capture_tmux_pane,
    configure_terminal_session_shell,
    ensure_terminal_session,
    kill_terminal_session,
    path_is_within,
    read_terminal_output,
    require_existing_terminal_session,
    resize_terminal_window,
    resolve_terminal_login_shell,
    scroll_terminal_history,
    scrub_tmux_global_secret_environment,
    send_terminal_input,
    set_pty_dimensions,
    spawn_terminal_client,
    stop_terminal_client,
    terminal_action,
    TERMINAL_ATTACHMENTS,
    TERMINAL_DISABLED_SHELL_NAMES,
    TERMINAL_FALLBACK_SHELLS,
    TERMINAL_INPUT_MAX_BYTES,
    TERMINAL_INPUT_WRITE_TIMEOUT_SECONDS,
    terminal_login_command,
    TERMINAL_READ_BYTES,
    terminal_session_lock,
    TERMINAL_SESSION_LOCK_STRIPES,
    terminal_session_path,
    terminal_snapshot,
    terminal_windows_snapshot,
    TerminalAttachmentAdmissionClosed,
    TerminalAttachmentRegistry,
    tmux_capability,
    TMUX_CAPTURE_MAX_LINES,
    TMUX_CHAT_MATCH_LABELS,
    tmux_explicit_chat_targets,
    TMUX_NOISE_TOKENS,
    tmux_panes_snapshot,
    tmux_session_exists,
    TMUX_SUBMITTER_KEYWORDS,
    TMUX_TARGET_RE,
    tmux_target_token,
    valid_terminal_login_shell,
    write_terminal_input,
)
from hyadex.core.event_log import (
    _build_timeline_index_locked,
    _snapshot_object_dir_is_owned,
    append_durable_event_batch_sync,
    ArtifactPublicationError,
    bounded_job_history_text,
    bounded_jsonl_events_range,
    build_timeline_index,
    client_safe_event,
    codex_subagent_ownership_snapshot,
    compact_timeline_index_text,
    darwin_available_memory_mb,
    deliver_event_to_live_clients,
    event_index_resume_offset,
    EventCatchupContinuation,
    evict_timeline_index_cache_locked,
    find_agent_notification_event,
    FORK_INTERNAL_RUN_CACHE_MAX,
    fork_internal_run_ids,
    host_pressure_snapshot,
    is_client_visible_event,
    is_fork_internal_event,
    is_native_goal_steer_event,
    is_visible_timeline_event,
    job_run_history_event_snapshot,
    JOB_RUN_HISTORY_TEXT_LIMIT,
    legacy_session_event_file_ids,
    load_publication_events,
    normalized_event_catchup_continuation,
    parse_vm_stat_available_mb,
    project_legacy_imported_provider_event,
    project_provider_history_event_for_egress,
    publication_receipt_path,
    read_client_events_page,
    read_event_catchup_batch,
    read_events,
    read_visible_events_after_page,
    read_visible_events_page,
    reasoning_summary_stream_snapshot,
    rebuild_event_index,
    recover_publication_events_from_tail,
    scheduled_job_occurrence_key,
    scheduled_job_output_rank,
    scheduled_job_run_status,
    scheduled_job_status_should_replace,
    send_event_catchup,
    send_event_stream_status,
    store_publication_events,
    TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD,
    timeline_index_async_cross_chat_key,
    timeline_index_cached_entry,
    timeline_index_cross_chat_key,
    timeline_index_event_is_hidden,
    timeline_index_event_text,
    timeline_index_is_error,
    timeline_index_is_native_steer_transition_stop,
    TIMELINE_INDEX_JOB_TYPES,
    timeline_index_retire_native_steer_turn,
    update_async_cross_chat_timeline_record,
    verified_publication_events,
)
from hyadex.providers.authority import (
    agent_runner_env,
    issue_provider_authority,
    issued_provider_capability_snapshot,
    legacy_compact_provider_authority_block_is_generated,
    LEGACY_PROVIDER_AUTHORITY_BINDING,
    LEGACY_PROVIDER_AUTHORITY_FOOTER,
    LEGACY_PROVIDER_AUTHORITY_HEADER,
    LEGACY_PROVIDER_AUTHORITY_LABELS,
    LEGACY_PROVIDER_AUTHORITY_OPTIONAL_PREFIXES,
    legacy_provider_authority_run_id,
    LEGACY_PROVIDER_AUTHORITY_VERBOSE_LEAD,
    LEGACY_PROVIDER_AUTHORITY_VERBOSE_TAIL,
    LEGACY_PROVIDER_FINAL_RESULT_HANDOFF,
    legacy_verbose_provider_authority_block_is_generated,
    NativeSteerHandoffError,
    PROVIDER_AUTHORITY_ACTIONS,
    provider_authority_path,
    provider_authority_runtime_env,
    PROVIDER_AUTHORITY_TTL_SECONDS,
    provider_capability_header,
    provider_capability_is_attached_to_live_run,
    provider_tool_active_matches,
    provider_tool_authority_path,
    provider_tool_call_is_read_only,
    ProviderToolError,
    redact_provider_tool_output,
    request_exact_registered_provider_capability,
    resolve_provider_tool_arguments,
    shell_word_after,
    strip_all_legacy_hyadex_provider_authority_suffixes,
    strip_legacy_hyadex_provider_authority_suffix,
    terminal_session_name,
    validate_provider_tool_input,
)
from hyadex.providers.codex.history import (
    CODEX_GOAL_HISTORY_REPAIR_CACHE,
    codex_history_assistant_item,
    codex_history_assistant_metadata,
    codex_history_user_record,
    codex_hygiene_int_setting,
    CODEX_NATIVE_HISTORY_REPAIR_CACHE,
    CODEX_ROLLOUT_MISS_TTL_SECONDS,
    CODEX_ROLLOUT_OVERSIZED_BYTES,
    codex_rollout_path,
    CODEX_ROLLOUT_PATH_CACHE,
    CODEX_ROLLOUT_PATH_MISSES,
    codex_rollout_stats,
    CODEX_ROLLOUT_WARN_BYTES,
    codex_runtime_user_item_kind,
    codex_session_meta_is_subagent,
    codex_transcript_meta,
    codex_transcript_metadata_records,
    CODEX_TRANSCRIPT_SCAN_LINES,
    codex_user_item_has_human_provenance,
    find_codex_history,
    is_codex_goal_runtime_user_item,
    merge_codex_history_duplicate,
)
from hyadex.core.sessions import (
    _revoke_provider_authority,
    _write_sessions_json_text_locked,
    ABANDONED_FORK_PROVIDER_THREADS,
    active_emergency_alerts,
    add_provider_no_proxy_environment,
    append_event,
    apply_emergency_lifecycle_event,
    bounded_codex_output_text,
    cancel_generated_session_title,
    clamp_codex_runtime_effort,
    CLAUDE_STOP_FENCE_SESSIONS,
    clean_emergency_message,
    clean_session_system_prompt,
    close_generated_session_titles,
    codex_app_server_env,
    codex_app_server_managers,
    codex_available_model,
    codex_compaction_count,
    CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT,
    codex_default_service_tier,
    codex_goal_time_budget_is_exhausted,
    codex_goal_time_budget_remaining,
    codex_output_text,
    codex_provider_mcp_connection_host,
    CODEX_RETIRED_APP_SERVER_MANAGERS,
    codex_retired_model_replacements,
    codex_runtime_settings,
    CODEX_SESSION_APP_SERVER_MANAGERS,
    codex_session_index_thread_names,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    codex_user_config_defaults,
    codex_user_config_path,
    CreateSessionRequest,
    delete_session_local_roots,
    delete_session_owned_file_records,
    durable_event_seq,
    effective_claude_permission_mode,
    effective_content_type,
    effective_provider_jobs_access,
    EMERGENCY_EVENT_FIELDS,
    emergency_lifecycle_events_after,
    emergency_summary,
    empty_chat_dismissed_for,
    empty_chat_revision,
    encode_sessions_json,
    ensure_backend_update_allowed,
    ensure_claude_permission_mode_update_allowed,
    ensure_codex_thread_not_pending_fork_cleanup,
    ensure_session_not_deleting,
    ensure_session_not_initializing,
    event_delivery_lock,
    event_establishes_session_file_origin,
    event_files_belong_to_session,
    event_output_text,
    events_index_path,
    EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
    execution_maintenance_blocks_work,
    existing_codex_app_server_manager,
    file_record_belongs_to_session,
    forget_event_seq,
    FORK_INTERNAL_RUN_CACHE,
    FORK_INTERNAL_RUN_LOCKS,
    generate_session_title,
    guess_content_type,
    HISTORY_MAINTENANCE_STATE,
    is_agent_visible_event,
    iter_session_events,
    last_event_seq_from_file,
    legacy_session_sort_key,
    list_session_file_records,
    load_codex_user_config,
    MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    managed_server_restart_blocks_work,
    mark_provider_context_usage_unavailable,
    meaningful_chat_cwd,
    native_codex_compaction_id,
    native_codex_compaction_terminal_aliases,
    native_session_available,
    next_event_seq,
    normalize_runtime_effort,
    normalize_runtime_effort_for_model,
    normalized_emergency_alert,
    normalized_local_session_cwd,
    notification_events_after,
    PendingSessionSave,
    preview_session_runtime_update,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    PROVIDER_CALLBACK_ORIGIN,
    provider_helper_server_origin,
    public_session,
    read_abandoned_fork_thread_ids,
    read_event_index,
    read_file_meta_record,
    read_native_session_title,
    read_server_restart_status,
    ReadSessionRequest,
    rebuild_codex_subagent_indexes,
    reconcile_event_seq_after_failed_write,
    reconcile_session_agent_notifications,
    reconcile_session_emergency_alerts,
    reconcile_stale_server_restart_acceptance,
    record_event_index_entry,
    refresh_native_session_title,
    register_session_task,
    remember_codex_native_compaction_terminal,
    REMOVED_ENDPOINT_MESSAGE,
    REMOVED_PROVIDER_MESSAGE,
    repair_event_log_tail,
    retired_session_runtime_message,
    reversed_jsonl_events,
    revoke_provider_authority,
    revoke_terminal_run_authority,
    RUNTIME_DIAGNOSTICS,
    RUNTIME_DIAGNOSTICS_LOCK,
    schedule_generated_session_title,
    ScheduledJobChatReferenceRepairRequired,
    server_lifecycle_admission_blocker,
    SERVER_RESTART_ACCEPTED_STALE_SECONDS,
    SERVER_RESTART_ACTIVE_PHASES,
    SERVER_RESTART_PHASES,
    SERVER_RESTART_SIGNAL_LOCK,
    server_restart_status_age_seconds,
    session_backend_locked,
    session_codex_subagent_states,
    session_codex_thread_id,
    session_folder,
    session_lifecycle_lock,
    session_order_value,
    session_prompt_addendum,
    session_section_key,
    session_subagent_limit_control,
    session_system_prompt,
    SessionStore,
    should_bump_session_updated_at,
    sorted_sessions,
    STORE,
    TIMELINE_INDEX_CODEX_COMPACTION_TYPES,
    TIMELINE_INDEX_CODEX_GOAL_TYPES,
    timeline_index_codex_lifecycle_key,
    timeline_index_session_lock,
    title_runtime_key,
    update_session_event_metadata,
    UpdateSessionRequest,
    validate_session_subagent_limit,
    wait_for_session_tasks,
    write_abandoned_fork_thread_ids,
    write_event_index,
    write_server_restart_status,
    write_sessions_json_text,
)
from hyadex.core.processes import (
    _provider_child_ps,
    best_process_label,
    bounded_restart_tmux_cgroup_state,
    cgroup_is_within,
    DARWIN_PS_COMMAND,
    descendant_processes,
    hyadex_server_systemd_cgroup,
    hyadex_server_systemd_cgroup_from_paths,
    HYADEX_SERVER_SYSTEMD_UNIT,
    ordered_process_tree,
    parse_darwin_ps_rows,
    parse_elapsed_seconds,
    parse_ps_rows,
    proc_cwd,
    process_cgroup_paths,
    process_depth,
    process_group_for_pid,
    procfs_process_rows,
    provider_child_parent_pid,
    PROVIDER_CHILD_PROBE_TIMEOUT_SECONDS,
    ps_process_rows,
    run_tmux,
    server_instance_environment,
    server_restart_tmux_cgroup_state,
    terminate_process_tree,
    tmux_bin,
    tmux_bootstrap_environment,
    TMUX_COMMAND_TIMEOUT_SECONDS,
    TMUX_PROBE_CACHE,
    TMUX_PROBE_CACHE_SECONDS,
    tmux_server_pid,
    top_process_rows,
    working_tmux_bin,
)
from hyadex.features.usage import (
    bounded_codex_interaction_value,
    broadcast_provider_runtime_changed,
    broadcast_provider_usage_changed,
    clear_provider_context_usage_locked,
    close_event_websocket_after_delivery_failure,
    close_event_websocket_over_capacity,
    context_usage_runtime_fields,
    current_provider_context_usage_generation,
    HUB,
    join_task_despite_caller_cancellation,
    next_provider_context_usage_generation,
    provider_context_usage_signal,
    PROVIDER_USAGE,
    SubscriberHub,
)
from hyadex.features.titles import (
    active_generated_title_work_labels,
    generated_title_eligible,
    GENERATED_TITLE_SLOTS,
    GENERATED_TITLE_TASKS,
    session_accepts_auto_title,
)
from hyadex.providers.claude.history import (
    add_history_item,
    bounded_claude_transcript_regions,
    bounded_jsonl_events,
    bounded_jsonl_paths,
    claude_history_candidates,
    claude_history_event_is_task_notification,
    CLAUDE_METADATA_REPAIR_CACHE,
    CLAUDE_TASK_NOTIFICATION_ORIGIN,
    CLAUDE_TASK_NOTIFICATION_PROMPT_SOURCES,
    CLAUDE_TASK_NOTIFICATION_RE,
    CLAUDE_TRANSCRIPT_CWD_LINE_BYTES,
    CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES,
    claude_transcript_matches_cwd,
    claude_transcript_title,
    compact_import_text,
    find_claude_history,
    history_dedup_key,
    HISTORY_SEARCH_DIRTY,
    HISTORY_SEARCH_REPAIR_DIRTY,
    HISTORY_SYNC_CURSOR_VERSION,
    is_claude_task_notification_history_event,
    is_import_boilerplate,
    message_text,
    native_session_title,
    normalized_history_import_limit,
    normalized_history_item,
    normalized_history_provider_origin,
    normalized_history_sync_cursor,
    path_if_jsonl,
    provider_session_identifier,
    PROVIDER_SESSION_IDENTIFIER_RE,
    session_provider_id,
    text_from_content,
)
from hyadex.core.state import (
    AGENT_HELPER_PROXY_HEADERS,
    COMPACT_TIMELINE_HIDDEN_TYPES,
    CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES,
    DARWIN_MEMORY_CACHE,
    DARWIN_MEMORY_CACHE_SECONDS,
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_DELIVERY_LOCKS,
    EVENT_INDEX_MIN_FILE_BYTES,
    EVENT_INDEX_STRIDE,
    EVENT_INDEX_VERSION,
    EVENT_LINE_SEQ_RE,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
    EVENT_SEQ_REPAIR_LOCKS,
    EVENT_STREAM_HEARTBEAT_SECONDS,
    EVENTS,
    HEALTH_QUEUE_RECONCILE_STATE,
    LEGACY_STEERING_PREFIX,
    LIVE_STDOUT_MAX_LINE_CHARS,
    LIVE_STDOUT_MAX_LINES,
    NOTIFICATION_EVENT_FIELDS,
    QUEUE_CHECKPOINT_DIRTY,
    QUEUE_FENCE_LOGGED_AT,
    QUEUE_RECOVERY_ADMISSION_WAIT_SECONDS,
    QUEUE_RECOVERY_READY,
    QUEUE_RETRY_TASKS,
    REASONING_SUMMARY_STREAM_LAST_SENT,
    REASONING_SUMMARY_STREAM_PENDING,
    REASONING_SUMMARY_STREAMS,
    ROUTE_AUTH_INDEX,
    RouteAuth,
    RouteAuthKind,
    RUN_NOW_COMPLETED_MAX,
    RUN_NOW_COMPLETED_RESULTS,
    RUN_NOW_COMPLETED_TTL_SECONDS,
    RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS,
    RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS,
    RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
    RUN_TRACE_EVENT_TYPES,
    SESSION_LIFECYCLE_LOCKS,
    SESSION_LIFECYCLE_UPDATE_FIELDS,
    SESSION_SNAPSHOT_RECONCILE_AT,
    SESSION_STORE_SAVE_DEBOUNCE_SECONDS,
    SESSION_STORE_SAVE_MAX_LATENCY_SECONDS,
    SESSIONS_WRITE_LOCK,
    TIMELINE_INDEX_CACHE,
    TIMELINE_INDEX_CACHE_LOCK,
    TIMELINE_INDEX_CACHE_MAX,
    TIMELINE_INDEX_HIDDEN_TYPES,
    TIMELINE_INDEX_LOCK_STRIPES,
    TIMELINE_INDEX_LOCKS,
    TIMELINE_INDEX_PROJECTION_VERSION,
    TIMELINE_INDEX_TRACE_TYPES,
    TURN_RECEIPT_TASKS,
    UNSAFE_HTTP_MUTATION_ADMISSION_LOCK,
    UNSAFE_HTTP_MUTATION_METHODS,
    UNSAFE_HTTP_MUTATION_TASKS,
    USER_ROUTE,
)
from hyadex.core.server_logging import (
    ACCESS_LOG_MESSAGE_RE,
    ACCESS_LOG_TOKEN_QUERY_RE,
    command_log_hints,
    concise_error_message,
    configure_server_logging,
    fd_log_hints,
    is_polling_access_log_request,
    LOG_PATH_SUFFIXES,
    POLLING_ACCESS_LOG_EXACT_PATHS,
    POLLING_ACCESS_LOG_PATH_RE,
    PollingAccessLogFilter,
    redact_access_log_token,
    REPEATED_LOG_SUPPRESSION_SECONDS,
    RepeatedMessageFilter,
    RETIRED_SHARE_PATH_RE,
    SERVER_LOG_DEFAULT_BACKUPS,
    SERVER_LOG_DEFAULT_MAX_BYTES,
    SERVER_LOG_DIR_NAME,
    SERVER_LOG_FILE_NAME,
    SERVER_LOG_FORMAT,
    server_log_rotation_settings,
    ServerLoggingHandlers,
    unique_log_hints,
)
from hyadex.core.util import (
    _patch_changed_paths,
    _safe_config_parent,
    bounded_jsonl_records_range,
    bounded_lock,
    clean_assistant_text,
    compact_memory_text,
    contained_jsonl_path,
    decoded_exact_header_secret,
    ensure_dirs,
    LEADING_DECORATION_RE,
    low_available_memory_message,
    merged_file_ids,
    normalized_file_id,
    normalized_network_address,
    normalized_tool_name,
    runner_env,
    server_identity,
    SkillSelection,
    strip_hyadex_memory_context,
    terminal_dimensions,
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_MIN_COLUMNS,
    TERMINAL_MIN_ROWS,
    TransientAdmissionWait,
    utc_timestamp,
)
from hyadex.providers.instructions import (
    CLAUDE_PROMPT_PRELUDE,
    CLAUDE_SDK_CONFIGURATION_VERSION,
    CODEX_PROMPT_PRELUDE,
    CODEX_THREAD_POLICY_VERSION,
    HYADEX_CONTEXT_END_MARKER,
    MAX_JOB_PROMPT_CHARS,
    MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS,
    MAX_SESSION_SYSTEM_PROMPT_CHARS,
    PROVIDER_AUTHORITY_USAGE_INSTRUCTIONS,
    PROVIDER_THREAD_INSTRUCTION_ADDENDUM,
    PROVIDER_TOOL_DESCRIPTION,
    PROVIDER_TOOL_HELPERS,
    PROVIDER_TOOL_INPUT_SCHEMA,
    PROVIDER_TOOL_MAX_ARGUMENT_BYTES,
    PROVIDER_TOOL_MAX_ARGUMENT_CHARS,
    PROVIDER_TOOL_MAX_ARGUMENTS,
    PROVIDER_TOOL_MAX_BODY_BYTES,
    PROVIDER_TOOL_MAX_CONCURRENT_PER_CLAUDE_CHAT,
    PROVIDER_TOOL_MAX_OUTPUT_BYTES,
    PROVIDER_TOOL_MAX_STDIN_BYTES,
    PROVIDER_TOOL_MAX_STDIN_CHARS,
    PROVIDER_TOOL_READ_ONLY_COMMANDS,
    PROVIDER_TOOL_REPLAY,
    PROVIDER_TOOL_REPLAY_LIMIT,
    PROVIDER_TOOL_REPLAY_LOCK,
    PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT,
    PROVIDER_TOOL_REPLAY_TOMBSTONES,
    PROVIDER_TOOL_TIMEOUT_SECONDS,
    SYSTEM_PROMPT,
)
from hyadex.providers.codex.config import (
    _codex_config_non_empty_str,
    _codex_config_positive_int,
    _CODEX_THREAD_CONFIG_WARNED_KEYS,
    _warn_codex_thread_config_once,
    codex_effective_thread_config,
    codex_session_config_overrides,
    CODEX_THREAD_CONFIG_AGENT_VALIDATORS,
    CODEX_THREAD_CONFIG_DEFAULTS,
    CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY,
    CODEX_THREAD_CONFIG_TOP_LEVEL_VALIDATORS,
    merge_codex_thread_config,
    read_codex_thread_config_overrides,
    sanitize_codex_thread_config,
)
from hyadex.core.identity import (
    legacy_server_identity,
    read_server_identity_file,
    SERVER_IDENTITY_CACHE,
    SERVER_IDENTITY_FILE,
    SERVER_IDENTITY_LOCK,
    SERVER_IDENTITY_RE,
    write_server_identity_file,
)
from hyadex.core.paths import (
    artifact_publications_dir,
    code_diffs_dir,
    codex_manifest_path,
    complete_working_directory_sync,
    events_path,
    existing_cwd,
    iso_from_timestamp,
    manifests_dir,
    now_iso,
    safe_name,
    session_dir,
    uploads_dir,
    validated_fork_cwd,
    write_all,
)
from hyadex.core.config import (
    _configured_codex_effort,
    ABANDONED_FORK_THREADS_FILE,
    AGENT_TOKEN,
    API_CONTRACT_VERSION,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    canonical_server_display_name,
    CLAUDE_BIN,
    CLAUDE_CLI_MODEL_ALIASES,
    CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS,
    CLAUDE_CURRENT_MODEL_FALLBACKS,
    CLAUDE_DEFAULT_PERMISSION_MODE,
    CLAUDE_LEGACY_MODEL_FALLBACKS,
    CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS,
    CLAUDE_MODEL_VALUE_RE,
    CLAUDE_MODELS_API_MAX_BYTES,
    CLAUDE_PERMISSION_MODE_OPTIONS,
    CLAUDE_PERMISSION_MODES,
    CLAUDE_PROJECTS_ROOT,
    CLAUDE_PROVIDER_SECRET_ENV_NAMES,
    CLAUDE_SDK_CONNECT_TIMEOUT_SECONDS,
    CLAUDE_SDK_IDLE_TIMEOUT_SECONDS,
    CLAUDE_SDK_IDLE_TTL_SECONDS,
    CLAUDE_SDK_IDLE_WARN_SECONDS,
    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
    CLAUDE_SDK_MAX_LOADED_CHATS,
    CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS,
    CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS,
    CLAUDE_SDK_TURN_TIMEOUT_SECONDS,
    CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_AUTO,
    CLAUDE_TRANSPORT_PRINT,
    CODE_DIFF_DISCOVERY_MAX_BYTES,
    CODE_DIFF_INDEX_MAX_BYTES,
    CODE_DIFF_NUMSTAT_MAX_BYTES,
    CODE_DIFF_PATCH_MAX_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_FILE_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_PATHS,
    CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS,
    CODE_DIFFS_ROOT,
    CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS,
    CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_JSONL_LIMIT_BYTES,
    CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_MAX_LOADED_THREADS,
    CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT,
    CODEX_APP_SERVER_TIMEOUT_SECONDS,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    CODEX_APPROVAL_POLICIES,
    CODEX_APPROVAL_REVIEWERS,
    CODEX_BIN,
    CODEX_CONTEXT_WINDOW_RESERVE_TOKENS,
    CODEX_DEFAULT_APPROVAL_POLICY,
    CODEX_DEFAULT_APPROVALS_REVIEWER,
    CODEX_DEFAULT_EFFORT,
    CODEX_DEFAULT_MODEL,
    CODEX_DEFAULT_PERMISSION_PROFILE,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_EFFORT_ALIASES,
    CODEX_EFFORT_ORDER,
    CODEX_EFFORTS,
    CODEX_EXEC_RAW_EVENT_MAX_CHARS,
    CODEX_FALLBACK_EFFORTS,
    CODEX_FALLBACK_MODEL_EFFORTS,
    CODEX_FALLBACK_MODELS,
    CODEX_FALLBACK_SERVICE_TIERS,
    CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
    CODEX_GOAL_STATUSES,
    CODEX_GOALS_DEFAULT_ENABLED,
    CODEX_GOALS_ENABLED,
    CODEX_INTERACTION_METHODS,
    CODEX_INTERACTIVE_CLIENT_CAPABILITY,
    CODEX_NONINTERACTIVE_APPROVAL_POLICY,
    CODEX_PROVIDER_MCP_HEADER_NAME,
    CODEX_PROVIDER_MCP_HEADER_SECRET,
    CODEX_PROVIDER_MCP_NAME,
    CODEX_PROVIDER_MCP_PATH,
    CODEX_PROVIDER_MCP_PROOF_KEY,
    CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_SANDBOX_MODES,
    CODEX_SANDBOX_POLICY_TYPES,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CODEX_SESSION_INDEX_PATH,
    CODEX_SESSIONS_ROOT,
    CODEX_SETTINGS_FILE,
    CODEX_SUBAGENT_RECONCILE_LIMIT,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_AUTO,
    CODEX_TRANSPORT_EXEC,
    CONFIG_ENV_APPLIED,
    CONFIG_ENV_FILE,
    CONFIG_ENV_NAME_RE,
    CONFIG_ENV_RUNTIME_OVERRIDE_NAMES,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    DEFAULT_SESSION_EVENT_LIMIT,
    EMERGENCY_ACTIVE_ALERT_LIMIT,
    EMERGENCY_AUTHORITY_DENIED_PURPOSES,
    EMERGENCY_MESSAGE_MAX_CHARS,
    EMERGENCY_REQUESTS_PER_RUN,
    EMERGENCY_WEBSOCKET_PROTOCOL,
    enabled_backends,
    env_setting,
    EVENT_WEBSOCKET_DELIVERY_FAILURE_CLOSE_CODE,
    EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL,
    EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION,
    EVENT_WEBSOCKET_OVER_CAP_CLOSE_CODE,
    EVENTS_WEBSOCKET_PROTOCOL,
    EXPLICIT_STOP_FENCE_RELEASE_TIMEOUT_SECONDS,
    EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS,
    FILES_ROOT,
    FORK_INTERNAL_PURPOSES,
    HARD_TERMINALIZABLE_TURN_TRANSPORTS,
    HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS,
    HISTORY_SYNC_BATCH_BYTES,
    HISTORY_SYNC_EVENT_SCAN_LIMIT,
    HOST_HEALTH_FILE,
    HOST_HEALTH_MAX_BYTES,
    HOST_MONITOR_INTERVAL_SECONDS,
    hyadex_setting,
    IDLE_KILL_SECONDS,
    IDLE_WARN_SECONDS,
    JOB_BUSY_RETRY_SECONDS,
    JOB_DEFER_EVENT_MIN_SECONDS,
    JOB_MAX_ACTIVE_RUNS,
    JOB_MIN_AVAILABLE_MEM_MB,
    JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS,
    JOB_SCHEDULER_INTERVAL_SECONDS,
    JOBS_FILE,
    LEGACY_DELIVERY_IDENTITY_FIELDS,
    load_config_env_file,
    MAX_ACTIVE_AGENT_RUNS,
    MAX_ARTIFACT_PUBLISH_FILES,
    MAX_ARTIFACT_TEXT_CHARS,
    MAX_ARTIFACT_TITLE_CHARS,
    MAX_CODEX_APPROVAL_ITEM_CACHE,
    MAX_CODEX_GOAL_CHARS,
    MAX_CODEX_INTERACTION_TEXT_CHARS,
    MAX_CODEX_PENDING_INTERACTIONS,
    MAX_CODEX_SESSION_INDEX_BYTES,
    MAX_CODEX_SESSION_INDEX_LINES,
    MAX_EVENT_RESPONSE_LIMIT,
    MAX_FORK_MEMORY_CHARS,
    MAX_FORK_MEMORY_ITEM_CHARS,
    MAX_IMPORT_MESSAGES,
    MAX_IMPORTED_TEXT_CHARS,
    MAX_JOB_TITLE_CHARS,
    MAX_LOCAL_SESSION_SCAN_FILES,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
    MAX_UPLOAD_BYTES,
    MAX_WORKING_DIRECTORY_COMPLETIONS,
    MAX_WORKING_DIRECTORY_SCAN_ENTRIES,
    MAX_WORKSPACE_PATH_CHARS,
    MIN_START_AVAILABLE_MEM_MB,
    NOTIFICATION_REQUESTS_PER_RUN,
    parse_config_env_file,
    PROCESS_STREAM_LIMIT,
    PROVIDER_AUTHORITY_ROOT,
    PROVIDER_CHILD_PROC_ROOT,
    PROVIDER_CHILDREN_FILE,
    PROVIDER_JOBS_ACCESS_DEFAULT,
    PROVIDER_JOBS_ACCESS_MODE_SET,
    PROVIDER_JOBS_ACCESS_MODES,
    QUEUE_FENCE_LOG_INTERVAL_SECONDS,
    read_codex_goals_enabled,
    REQUEST_TIMEOUT_SECONDS,
    require_enabled_backend,
    resolve_state_dir,
    RETIRED_QUEUED_TURN_FIELDS,
    RETIRED_TURN_PURPOSES,
    RUNTIME_CATALOG_BUDGET_SECONDS,
    RUNTIME_CATALOG_DEADLINE,
    RUNTIME_CATALOG_TIMEOUT_SECONDS,
    RUNTIME_DIAGNOSTIC_TTL_SECONDS,
    scrub_server_secret_environment,
    SCRUBBED_SERVER_SECRET_ENV_NAMES,
    SERVER_ADMIN_ROOT,
    SERVER_DISPLAY_NAME_ERROR,
    SERVER_DISPLAY_NAME_MAX_BYTES,
    SERVER_INSTANCE_ID,
    SERVER_INSTANCE_NAME,
    SERVER_RESTART_STATUS_FILE,
    SERVER_ROOT,
    SERVER_VERSION,
    SERVER_VERSION_FILE,
    SESSION_ORDER_STEP,
    SESSIONS_FILE,
    STATE_DIR,
    STOP_CONFIRM_TIMEOUT_SECONDS,
    STOP_GRACE_SECONDS,
    SUBAGENT_SNAPSHOT_LOG_LIMIT,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
    SUBAGENT_SNAPSHOT_TEXT_LIMIT,
    TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS,
    TERMINAL_WEBSOCKET_PROTOCOL,
    TMUX_INSTANCE_ARGS,
    VALID_BACKENDS,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)

# Fail before reading another instance's config or resolving/migrating state.
server_instances.validate_runtime_environment(os.environ, Path.home())
if len(PROVIDER_THREAD_INSTRUCTION_ADDENDUM) > MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS:
    raise RuntimeError("Hyadex static provider instructions exceed their safe limit")

WIRING.jobs = JOBS


def deferred_force_send_response_for_client(
    result: dict[str, Any],
    request: RunQueuedTurnNowRequest | None,
) -> dict[str, Any]:
    """Expose the additive deferred contract only to clients that opt in."""
    if request is not None and request.accept_deferred_queue_response:
        return result
    raise HTTPException(
        status_code=409,
        detail={
            "code": "force_send_deferred",
            "message": str(
                result.get("message")
                or "Force Send was deferred and the message remains queued."
            ),
            "action": "Refresh the queue before trying Force Send again.",
            "retryable": True,
            "delivery_uncertain": False,
            "queued_id": result.get("queued_id"),
        },
    )


def file_response_media_type(meta: dict[str, Any]) -> str:
    filename = str(meta.get("filename") or Path(str(meta.get("path") or "")).name)
    return effective_content_type(filename, str(meta.get("content_type") or ""))


def route_auth_for_scope(app: Any, method: str, path: str, *, any_method: bool = False) -> RouteAuth:
    """The declaration of the route a request would reach (default ``user``).

    Only the few declared routes are matched, so ordinary requests pay one
    short scan. ``any_method`` also accepts a path match with another method,
    for preflight requests.
    """
    index = ROUTE_AUTH_INDEX.get(app)
    if index is None:
        index = ROUTE_AUTH_INDEX[app] = declared_auth_routes(app.router.routes)
    scope = {"type": "http", "method": str(method or "").upper(), "path": path, "root_path": ""}
    for route, auth in index:
        match, _child = route.matches(scope)
        if match == Match.FULL or (any_method and match == Match.PARTIAL):
            return auth
    return USER_ROUTE


def agent_helper_route_body_limit(method: str, path: str) -> int | None:
    auth = route_auth_for_scope(app, method, path)
    return auth.body_limit if auth.kind == "agent_helper" else None


def is_agent_helper_route(method: str, path: str) -> bool:
    return route_auth_for_scope(app, method, path).kind == "agent_helper"


async def require_agent_token(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        # Preflight never reaches native controls or the provider channel.
        preflight = route_auth_for_scope(request.app, "OPTIONS", path, any_method=True)
        if preflight.kind in {"native_admin", "provider_mcp"}:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    auth = route_auth_for_scope(request.app, request.method, path)

    if auth.kind == "provider_mcp":
        if (
            request.method.upper() != "POST"
            or agent_helper_browser_request_forbidden(request)
            or not request_client_is_codex_provider_mcp_local(request)
            or not request_exact_codex_provider_mcp_secret(request)
        ):
            return JSONResponse(
                {"detail": "Codex provider MCP transport is forbidden"},
                status_code=403,
            )
        if (rejected := await prebuffer_declared_body(request, auth.body_limit)) is not None:
            return rejected
        request.state.codex_provider_mcp_authenticated = True
    elif auth.kind == "agent_helper":
        # Helpers use a per-run provider capability and validate their action
        # and session in-route. The client bearer is never a substitute.
        if (
            agent_helper_browser_request_forbidden(request)
            or not request_client_is_provider_helper_local(request)
        ):
            return JSONResponse(
                {"detail": "agent helper route is restricted to local provider clients"},
                status_code=403,
            )
        if not await request_exact_registered_provider_capability(request):
            return JSONResponse(
                {"detail": "provider capability is invalid"},
                status_code=403,
            )
        if request.method.upper() in UNSAFE_HTTP_MUTATION_METHODS:
            if (rejected := await prebuffer_declared_body(request, auth.body_limit)) is not None:
                return rejected
    elif auth.kind == "native_admin":
        if privileged_native_browser_request_forbidden(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if not AGENT_TOKEN:
            return JSONResponse(
                {
                    "detail": (
                        "This administrative control requires authenticated "
                        "HyadexServer mode"
                    )
                },
                status_code=503,
            )
        if not request_exact_native_token_header_authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if request.method.upper() == "PUT" and auth.body_limit is not None:
            declared_size, transport_error = privileged_native_json_transport(
                request,
                max_body_bytes=auth.body_limit,
                label="Codex settings",
                require_content_length=True,
            )
            if transport_error is not None:
                status_code, detail = transport_error
                return JSONResponse({"detail": detail}, status_code=status_code)
            body_error = await prebuffer_bounded_request_body(
                request,
                max_body_bytes=auth.body_limit,
                declared_size=declared_size,
            )
            if body_error is not None:
                status_code, detail = body_error
                return JSONResponse({"detail": detail}, status_code=status_code)

    # Restart has a stricter header-only check below, including its structured
    # error contract; URL credentials must reach that rejection.
    if auth.kind in {"user", "native_admin"} and not request_authorized(request):
        logger.warning("unauthorized request method=%s path=%s host=%s", request.method, path, request.client.host if request.client else "-")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    if auth.kind == "restart":
        if server_restart_browser_request_forbidden(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        # Enforce the privileged route's header-only authentication before
        # FastAPI parses a request body. The route repeats these checks for
        # direct invocation safety and performs the managed-service proof.
        if not AGENT_TOKEN:
            return JSONResponse(
                {
                    "detail": server_restart_error_detail(
                        "server_restart_auth_required",
                        "Server restart requires authenticated HyadexServer mode.",
                        action=(
                            "Install or configure HyadexServer with an access token."
                        ),
                    )
                },
                status_code=503,
            )
        if not request_exact_restart_header_authorized(request):
            return JSONResponse(
                {
                    "detail": server_restart_error_detail(
                        "server_restart_unauthorized",
                        (
                            "Server restart requires an access token in an "
                            "authorization header."
                        ),
                    )
                },
                status_code=401,
            )
        if request.method.upper() == "POST":
            transport_error = server_restart_post_transport_error(request)
            if transport_error is not None:
                status_code, detail = transport_error
                return JSONResponse({"detail": detail}, status_code=status_code)

    # Restart drives the lifecycle fence itself, so it is not admitted as an
    # ordinary mutation.
    unsafe_mutation = (
        request.method.upper() in UNSAFE_HTTP_MUTATION_METHODS
        and auth.kind != "restart"
    )
    if not unsafe_mutation:
        return await call_next(request)
    mutation_id = ""
    mutation_task: asyncio.Task[Any] | None = None
    mutation_registry = UNSAFE_HTTP_MUTATION_TASKS
    admission_rejected_response: JSONResponse | None = None
    downstream_task: asyncio.Task[Response]
    original_receive = getattr(request, "_receive", None)
    defer_admission_until_body_received = (
        original_receive is not None
        and request_route_parses_body(request)
        and not getattr(request.state, "bounded_body_prebuffered", False)
    )

    async def admit_unsafe_http_mutation(
        task: asyncio.Task[Any],
    ) -> None:
        nonlocal admission_rejected_response, mutation_id
        nonlocal mutation_task, mutation_registry
        if mutation_id:
            return
        async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
            if mutation_id:
                return
            blocked = unsafe_http_mutation_blocked_response()
            if blocked is not None:
                admission_rejected_response = blocked
                raise UnsafeMutationAdmissionRejected(blocked)
            (
                mutation_id,
                mutation_task,
                mutation_registry,
            ) = register_unsafe_http_mutation_locked(task)

    try:
        if defer_admission_until_body_received:
            # Authentication and transport validation above are header-only.
            # Do not let a client reserve the global lifecycle fence merely
            # by opening a request and withholding its body. FastAPI always
            # consumes a route's declared body before invoking the handler, so
            # atomically acquire the lease at the final request-body frame.
            async def receive_after_mutation_admission() -> dict[str, Any]:
                message = await original_receive()
                if (
                    message.get("type") == "http.request"
                    and not message.get("more_body", False)
                ):
                    # Starlette reads each frame in a short-lived task used to
                    # race the request against disconnect. Registering that
                    # receiver would release the lease as soon as it returns,
                    # before validation or the handler can mutate anything.
                    # The enclosing call_next task instead owns the complete
                    # downstream request through its response boundary.
                    await admit_unsafe_http_mutation(downstream_task)
                return message

            request._receive = receive_after_mutation_admission
            downstream_task = asyncio.create_task(call_next(request))
        else:
            async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
                blocked = unsafe_http_mutation_blocked_response()
                if blocked is not None:
                    return blocked
                # Own the downstream operation independently from the HTTP
                # caller. Cancelling an await on asyncio.to_thread only cancels
                # its Future; the worker keeps mutating state. Shielding this
                # child means the lease remains authoritative until the actual
                # handler settles.
                downstream_task = asyncio.create_task(call_next(request))
                (
                    mutation_id,
                    mutation_task,
                    mutation_registry,
                ) = register_unsafe_http_mutation_locked(downstream_task)
        try:
            response = await asyncio.shield(downstream_task)
            # FastAPI deliberately normalizes exceptions raised while parsing
            # a declared request body into a 400 response. Preserve the exact
            # admission result recorded before the final body frame instead of
            # exposing that parser fallback to the client.
            if admission_rejected_response is not None:
                return admission_rejected_response
            return response
        except UnsafeMutationAdmissionRejected as exc:
            return exc.response
        except asyncio.CancelledError:
            # A disconnected/re-cancelled request cannot release admission
            # while its downstream thread or coroutine still owns a mutation.
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(downstream_task)
            raise
    finally:
        if defer_admission_until_body_received:
            request._receive = original_receive
        if mutation_id:
            release_unsafe_http_mutation(
                mutation_id,
                mutation_task,
                mutation_registry,
            )


router.include_router(side_questions.create_side_question_router(
    authorize=require_native_admin_control,
    session_exists=native_session_available,
    runtime=SIDE_QUESTIONS,
))


# Group discussions follow their member chats' turns; observing only schedules
# the engine's own work and never delays the chat's event path.
EVENTS.on_observe(lambda session_id, event: GROUPS.observe_member_event(session_id, event),
                  types=group_chat.OBSERVED_EVENT_TYPES)
router.include_router(create_autopilot_router(AUTOPILOT))


@router.post("/api/groups/{group_id}/jobs")
async def create_group_job(group_id: str, req: CreateScopedJobRequest) -> dict[str, Any]:
    """Schedule a message into a group. It is anchored to the default responder's chat."""

    group = GROUPS.require(group_id, writable=True)
    if group.get("kind") == "autopilot":
        raise HTTPException(status_code=409, detail="this group belongs to an autopilot run")
    handle = GROUPS.default_responder(group)
    participant = GROUPS.participant(group, handle) if handle else None
    if participant is None:
        raise HTTPException(status_code=409, detail="the group has no agents")
    fields = req.model_dump(exclude_unset=True)
    if fields.get("chat_references") or fields.get("team_references"):
        raise HTTPException(status_code=400, detail="group jobs do not support chat or team references")
    session_id = participant["session_id"]
    async with session_lifecycle_lock(session_id):
        job = await JOBS.create(CreateJobRequest(session_id=session_id, **fields), group_id=group_id)
    return {"job": public_job(job)}


@router.get("/api/groups/{group_id}/jobs")
async def list_group_jobs(group_id: str) -> dict[str, Any]:
    GROUPS.require(group_id)
    return {"jobs": [public_job(job) for job in JOBS.jobs.values() if job.get("group_id") == group_id]}


@router.delete("/api/groups/{group_id}/jobs/{job_id}")
async def delete_group_job(group_id: str, job_id: str) -> dict[str, Any]:
    job = JOBS.jobs.get(job_id)
    if not job or job.get("group_id") != group_id:
        raise HTTPException(status_code=404, detail="job not found")
    session_id = str(job.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        deleted = await JOBS.delete(job_id, expected_session_id=session_id)
    return {"deleted": bool(deleted), "id": job_id}
router.include_router(create_group_router(
    GROUPS,
    websocket_authorized=websocket_authorized,
    reserve=HUB.reserve,
    register=HUB.register_accepted,
    unsubscribe=HUB.unsubscribe,
    subprotocol=lambda ws: websocket_endpoint_subprotocol(ws, EVENTS_WEBSOCKET_PROTOCOL),
    send_timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
))


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


@router.post("/api/sessions/{session_id}/turns")
async def post_turn(session_id: str, req: TurnRequest) -> Any:
    if req.shared_chat_id is not None or req.shared_chat_request_id is not None:
        raise HTTPException(status_code=422, detail="Browser sharing attribution is no longer accepted")
    if req.client_request_id is not None:
        return await post_turn_with_receipt(session_id, req)
    return await post_turn_without_receipt(session_id, req)


@router.delete("/api/sessions/{session_id}/queue/{queued_id}")
async def delete_queued_turn(session_id: str, queued_id: str) -> dict[str, Any]:
    return await unqueue_turn(session_id, queued_id)


@router.patch("/api/sessions/{session_id}/queue/{queued_id}")
async def patch_queued_turn(session_id: str, queued_id: str, req: UpdateQueuedTurnRequest) -> dict[str, Any]:
    return await update_queued_turn(session_id, queued_id, req)


@router.post("/api/sessions/{session_id}/queue/{queued_id}/move")
async def post_move_queued_turn(session_id: str, queued_id: str, req: MoveQueuedTurnRequest) -> dict[str, Any]:
    return await move_queued_turn(session_id, queued_id, req)


@router.post("/api/sessions/{session_id}/queue/{queued_id}/run-now")
async def post_run_queued_turn_now(
    session_id: str,
    queued_id: str,
    req: RunQueuedTurnNowRequest | None = None,
) -> dict[str, Any]:
    try:
        result = await run_queued_turn_now(session_id, queued_id)
        if result.get("deferred"):
            return deferred_force_send_response_for_client(result, req)
        return result
    except NativeSteerHandoffError as exc:
        # Retry-safe races normally become deferred outcomes in the coalesced
        # operation above. Keep this defensive boundary so no future handoff
        # path can leak an expected provider-completion race as a raw 500.
        if exc.safe_to_requeue:
            result = await deferred_force_send_outcome(session_id, queued_id, exc)
            return deferred_force_send_response_for_client(result, req)
        delivery_uncertain = bool(exc.delivery_uncertain)
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "force_send_delivery_uncertain"
                    if delivery_uncertain
                    else "force_send_not_retryable"
                ),
                "message": (
                    "Force Send delivery could not be confirmed: "
                    f"{concise_error_message(exc)}"
                ),
                "action": (
                    "Do not retry automatically. Refresh the chat and verify "
                    "whether the message appeared."
                ),
                "retryable": False,
                "delivery_uncertain": delivery_uncertain,
                "queued_id": queued_id,
            },
        ) from exc


@router.post("/api/sessions/{session_id}/stop")
async def stop_turn_endpoint(session_id: str) -> dict[str, Any]:
    return await request_explicit_stop(session_id)


@router.get("/api/jobs")
async def list_jobs(session_id: str | None = None) -> dict[str, Any]:
    jobs = sorted(
        (
            public_job(job)
            for job in JOBS.jobs.values()
            if session_id is None or job.get("session_id") == session_id
        ),
        key=lambda j: j.get("updated_at") or "",
        reverse=True,
    )
    return {"jobs": jobs}


@router.get("/api/agent/sessions/{session_id}/jobs", dependencies=route_auth("agent_helper", 0))
async def list_agent_session_jobs(request: Request, session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="read",
        )
        response = await list_session_jobs(session_id)
        return {
            **response,
            "jobs": [
                provider_public_job(job)
                for job in response.get("jobs") or []
                if isinstance(job, dict)
            ],
        }


@router.get("/api/agent/sessions/{session_id}/jobs/{job_id}/runs", dependencies=route_auth("agent_helper", 0))
async def get_agent_session_job_runs(
    request: Request,
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
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="read",
        )
        response = await get_session_job_runs(
            session_id,
            job_id,
            timeline_group_id=timeline_group_id,
            before_seq=before_seq,
            limit=limit,
        )
        return {
            **response,
            "runs": [
                provider_public_job_run(run)
                for run in response.get("runs") or []
                if isinstance(run, dict)
            ],
        }


@router.post("/api/jobs")
async def create_job(req: CreateJobRequest) -> dict[str, Any]:
    async with session_lifecycle_lock(req.session_id):
        job = await JOBS.create(req)
    return {"job": public_job(job)}


@router.post("/api/agent/sessions/{session_id}/jobs", dependencies=route_auth("agent_helper", 512 * 1024))
async def create_agent_session_job(
    request: Request,
    session_id: str,
    req: AgentCreateScopedJobRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        if "team_references" in request_fields_set(req):
            raise HTTPException(
                status_code=403,
                detail=(
                    "provider jobs access cannot create durable team grants"
                ),
            )
        fields = req.model_dump(exclude_unset=True)
        # Both are retired and validated empty; neither has an effect.
        fields.pop("chat_routes", None)
        fields.pop("chat_references", None)
        job = await JOBS.create(CreateJobRequest(
            session_id=session_id,
            **fields,
        ), emit_event=False)
        await append_provider_job_mutation_event(job, "job_created")
        return {"job": provider_public_job(job)}


@router.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, req: UpdateJobRequest) -> dict[str, Any]:
    snapshot = await JOBS.snapshot(job_id)
    session_id = str(snapshot.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        job = await JOBS.update(
            job_id,
            req.model_dump(exclude_unset=True),
            expected_session_id=session_id,
        )
    return {"job": public_job(job)}


@router.patch("/api/agent/sessions/{session_id}/jobs/{job_id}", dependencies=route_auth("agent_helper", 512 * 1024))
async def update_agent_session_job(
    request: Request,
    session_id: str,
    job_id: str,
    req: AgentUpdateJobRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        if (
            "team_references" in request_fields_set(req)
            and req.team_references
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "provider jobs access cannot add or expand durable team grants"
                ),
            )
        fields = req.model_dump(exclude_unset=True)
        # Retired and validated empty. An explicit empty chat_references
        # still clears a legacy job's saved @Chat targets.
        fields.pop("chat_routes", None)
        job = await JOBS.update(
            job_id,
            fields,
            expected_session_id=session_id,
            emit_event=False,
        )
        await append_provider_job_mutation_event(job, "job_updated")
        return {"job": provider_public_job(job)}


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    snapshot = await JOBS.snapshot(job_id)
    session_id = str(snapshot.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        deleted = await JOBS.delete(
            job_id,
            expected_session_id=session_id,
        )
    return {"ok": True, "deleted": deleted}


@router.delete("/api/agent/sessions/{session_id}/jobs/{job_id}", dependencies=route_auth("agent_helper", 0))
async def delete_agent_session_job(
    request: Request,
    session_id: str,
    job_id: str,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        deleted = await JOBS.delete(job_id, expected_session_id=session_id)
        return {"ok": True, "deleted": deleted}


@router.post("/api/jobs/{job_id}/run")
async def run_job(job_id: str) -> dict[str, Any]:
    return await JOBS.request_manual_run(job_id)


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


@router.post("/api/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_deleting(session_id)
    if bool(session.get("archived")):
        raise HTTPException(status_code=409, detail="archived chats cannot accept uploads")

    ensure_dirs()
    file_id = f"file_{uuid.uuid4().hex[:16]}"
    filename = safe_name(file.filename or "upload")
    final_root = FILES_ROOT / file_id
    staging_root = Path(tempfile.mkdtemp(
        prefix=f".{file_id}-upload-",
        dir=str(FILES_ROOT),
    ))
    staged_file = staging_root / filename
    copy_task = asyncio.create_task(asyncio.to_thread(
        stage_upload_file_sync,
        file.file,
        staged_file,
    ))
    try:
        size = await asyncio.shield(copy_task)
    except BaseException:
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(copy_task)
        with suppress(BaseException):
            await remove_upload_tree(staging_root)
        raise

    dest = final_root / filename
    meta = {
        "id": file_id,
        "session_id": session_id,
        "kind": "upload",
        "filename": filename,
        "path": str(dest),
        "size": size,
        "content_type": effective_content_type(filename, file.content_type),
        "created_at": now_iso(),
    }
    try:
        await run_upload_worker(
            write_upload_metadata_sync,
            staging_root / "meta.json",
            meta,
        )
        commit_task = asyncio.create_task(commit_staged_upload(
            session_id,
            staging_root,
            final_root,
            meta,
        ))
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(commit_task)
            raise
        return {"file": meta}
    finally:
        await remove_upload_tree(staging_root)


@router.get("/api/sessions/{session_id}/files")
async def list_session_files(
    session_id: str,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    content_prefix: str | None = Query(default=None),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    records = await asyncio.to_thread(list_session_file_records, session_id)
    if content_prefix:
        prefix = content_prefix.strip().lower()
        records = [
            rec for rec in records
            if str(rec.get("content_type") or "").lower().startswith(prefix)
        ]
    total = len(records)
    if limit is None:
        return {
            "files": records,
            "total": total,
            "offset": 0,
            "limit": total,
            "has_more": False,
        }
    page = records[offset:offset + limit]
    return {
        "files": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }


@router.get("/api/sessions/{session_id}/links/file")
@router.head("/api/sessions/{session_id}/links/file")
async def get_session_linked_file(session_id: str, target: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await asyncio.to_thread(session_file_for_link, session_id, target)
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )


@router.get("/api/sessions/{session_id}/files/{file_id}")
@router.head("/api/sessions/{session_id}/files/{file_id}")
async def get_session_file(session_id: str, file_id: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await asyncio.to_thread(
        require_session_file_meta,
        session_id,
        file_id,
    )
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )


def main() -> int:
    configure_server_logging(STATE_DIR)
    parser = argparse.ArgumentParser(description="HyadexServer")
    parser.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--bind", default=hyadex_setting("AGENT_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(hyadex_setting("AGENT_PORT", "7870")))
    args = parser.parse_args()
    # Downstream helper environments and the required Codex MCP transport
    # must target the socket selected by the CLI, not the import-time default.
    SERVER.bind_address = str(args.bind)
    SERVER.port = int(args.port)
    try:
        validate_listener_auth()
    except RuntimeError as exc:
        parser.error(str(exc))
    uvicorn.run(
        app,
        host=args.bind,
        port=args.port,
        log_level="info",
        # Route uvicorn's own loggers through the root handlers above (stderr
        # plus the rotating file) instead of its private stdout/stderr config,
        # so access and error lines are rotated and filtered as well.
        log_config=None,
        # The local-only Hub gate relies on the actual socket peer. This
        # preview does not support header-trusting proxy operation.
        proxy_headers=False,
        server_header=False,
        # Without this uvicorn waits forever for open websockets/background
        # work after SIGTERM, and the restart watchdog is the only way out.
        timeout_graceful_shutdown=uvicorn_graceful_shutdown_seconds(),
    )
    return 0


# --- Route modules ---
mount_routes(router, providers_routes.router)
mount_routes(router, search_routes.router)
mount_routes(router, sessions_routes.router)
mount_routes(router, terminal_routes.router)
mount_routes(router, admin_routes.router)
mount_routes(router, health_routes.router)
mount_routes(router, provider_mcp_routes.router)

app = create_app(ApplicationBindings(
    router=router, authorize_http=require_agent_token, lifespan=lifespan,
))


# --- Compatibility facade (temporary) ---------------------------------------
# Code is moving from this module into hyadex/ packages; this module re-exports
# what moved. Tests and launch code still assign attributes here
# (``patch.object(agent_server, ...)``, ``module.SERVER_PORT = ...``). Each such
# assignment is forwarded to the extracted modules that hold the same object
# under that name, so the code that now lives there sees it too. Removed once
# callers address the owning modules directly.
EXTRACTED_MODULES: tuple[types.ModuleType, ...] = (sys.modules['hyadex.core.config'], sys.modules['hyadex.core.paths'], sys.modules['hyadex.core.identity'], sys.modules['hyadex.providers.codex.config'], sys.modules['hyadex.providers.instructions'], sys.modules['hyadex.core.util'], sys.modules['hyadex.core.server_logging'], sys.modules['hyadex.core.state'], sys.modules['hyadex.providers.claude.history'], sys.modules['hyadex.features.titles'], sys.modules['hyadex.features.usage'], sys.modules['hyadex.core.processes'], sys.modules['hyadex.core.sessions'], sys.modules['hyadex.providers.codex.history'], sys.modules['hyadex.providers.authority'], sys.modules['hyadex.core.event_log'], sys.modules['hyadex.features.terminal'], sys.modules['hyadex.features.history'], sys.modules['hyadex.features.files'], sys.modules['hyadex.providers.subagents'], sys.modules['hyadex.providers.claude.runtime'], sys.modules['hyadex.features.side_questions'], sys.modules['hyadex.providers.registry'], sys.modules['hyadex.core.queue'], sys.modules['hyadex.providers.codex.runtime'], sys.modules['hyadex.core.http'], sys.modules['hyadex.features.emergency'], sys.modules['hyadex.core.turns'], sys.modules['hyadex.core.stop'], sys.modules['hyadex.features.forks'], sys.modules['hyadex.features.group_runtime'], sys.modules['hyadex.features.jobs'], sys.modules['hyadex.features.search'], sys.modules['hyadex.ops.lifecycle'], sys.modules['hyadex.api.provider_mcp'], sys.modules['hyadex.api.common'], sys.modules['hyadex.api.health'], sys.modules['hyadex.api.admin'], sys.modules['hyadex.api.terminal'], sys.modules['hyadex.api.sessions'], sys.modules['hyadex.api.search'], sys.modules['hyadex.api.providers'], )


# Module globals that core code reaches through WIRING.
WIRED_NAMES = {"JOBS": "jobs"}


class _CompatibilityFacade(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        missing = object()
        previous = self.__dict__.get(name, missing)
        if previous is not missing:
            for module in EXTRACTED_MODULES:
                if module.__dict__.get(name, missing) is previous:
                    module.__dict__[name] = value
            if name in WIRED_NAMES and getattr(WIRING, WIRED_NAMES[name]) is previous:
                setattr(WIRING, WIRED_NAMES[name], value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CompatibilityFacade

if __name__ == "__main__":
    raise SystemExit(main())
