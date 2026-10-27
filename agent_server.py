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
    CLAUDE_BACKGROUND_WORK_CACHE_KEY,
    CLAUDE_BACKGROUND_WORK_CACHE_LABELS,
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
    CLAUDE_NO_SESSION_PERSISTENCE_SUPPORTED,
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
    CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK,
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
    REASONING_SUMMARY_STREAM_REVISION,
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

WIRING.jobs = JOBS


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


def resize_terminal_pane(session_id: str, columns: int, rows: int) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        require_existing_terminal_session(session_id)
        _cols, line_count = resize_terminal_window(session_id, columns, rows)
        return terminal_snapshot(session_id, lines=line_count)


def exit_terminal_auto_scroll(session_id: str) -> None:
    if shutil.which("tmux") is None:
        return
    name = terminal_session_name(session_id)
    if tmux_session_exists(name):
        run_tmux(["send-keys", "-X", "-t", name, "cancel"], check=False)


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


def file_response_media_type(meta: dict[str, Any]) -> str:
    filename = str(meta.get("filename") or Path(str(meta.get("path") or "")).name)
    return effective_content_type(filename, str(meta.get("content_type") or ""))


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
            from execution_install import active_worker_release

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


HYADEX_SERVER_DISPLAY_NAME = canonical_server_display_name(
    os.environ.get("HYADEX_SERVER_NAME")
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
    from execution_ownership import acquire_state_ownership

    validate_listener_auth()
    # Both the maintained legacy entrypoint and the split worker pass here.
    # Retain the descriptor until process exit: bounded shutdown can leave
    # finalizers alive after this lifespan returns.
    acquire_state_ownership(STATE_DIR)
    install_root = os.environ.get("HYADEX_SERVER_INSTALL_DIR")
    if SERVER.execution_maintenance is None and install_root:
        # Never admit work over a journal the retired in-app updater left.
        from execution_install import refuse_retired_activation
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


@router.post(CODEX_PROVIDER_MCP_PATH, dependencies=route_auth("provider_mcp", PROVIDER_TOOL_MAX_BODY_BYTES))
async def codex_provider_mcp(request: Request) -> Response:
    """Minimal Streamable-HTTP MCP endpoint for the static Codex tool."""

    if getattr(request.state, "codex_provider_mcp_authenticated", False) is not True:
        raise HTTPException(status_code=403, detail="Codex provider MCP is forbidden")
    try:
        message = await request.json()
    except Exception:
        return codex_provider_mcp_jsonrpc_error(None, -32700, "Parse error")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return codex_provider_mcp_jsonrpc_error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid params")
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or "2025-06-18")
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Hyadex", "version": SERVER_VERSION},
            },
        })
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [codex_provider_mcp_tool_definition()]},
        })
    if method != "tools/call":
        return codex_provider_mcp_jsonrpc_error(request_id, -32601, "Method not found")
    if params.get("name") != "run" or not isinstance(params.get("arguments"), dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid params")
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Missing turn metadata")
    turn_meta = meta.get("x-codex-turn-metadata")
    if not isinstance(turn_meta, dict):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Missing turn metadata")
    if any(
        turn_meta.get(name) is not None
        for name in (
            "parent_thread_id",
            "parent_turn_id",
            "root_turn_id",
            "subagent_kind",
        )
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Subagent tool calls are forbidden")
    thread_id = str(turn_meta.get("thread_id") or "")
    turn_id = str(turn_meta.get("turn_id") or "")
    client_meta = turn_meta.get("responsesapi_client_metadata")
    if not isinstance(client_meta, dict):
        client_meta = turn_meta.get("responsesapiClientMetadata")
    if not isinstance(client_meta, dict):
        client_meta = turn_meta
    run_id = str(client_meta.get("hyadex_run_id") or "")
    proof = str(client_meta.get("hyadex_run_proof") or "")
    core_call_id = str(meta.get("callId") or "")
    if (
        not thread_id
        or len(thread_id) > 256
        or not turn_id
        or len(turn_id) > 256
        or re.fullmatch(r"run_[A-Za-z0-9_-]{1,128}", run_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", proof) is None
        or not core_call_id
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Incomplete turn metadata")
    if len(core_call_id) > 512:
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid call identity")
    standalone_active_owner = False
    async with RUNS.active_lock:
        owners = [
            session_id
            for session_id, active in RUNS.active.items()
            if (
                active.get("backend") == BACKEND_CODEX
                and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                and str(active.get("provider_thread_id") or "") == thread_id
                and str(active.get("run_id") or "") == run_id
                and session_id in RUNS.busy
            )
        ]
        if len(owners) == 1:
            standalone_active_owner = bool(
                (RUNS.active.get(owners[0]) or {}).get(
                    "standalone_provider_context"
                )
            )
    if len(owners) != 1:
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Stale turn metadata")
    session_id = owners[0]
    stored_owners = [
        stored_session_id
        for stored_session_id, stored in STORE.sessions.items()
        if (
            stored_session_id not in DELETING_SESSIONS
            and stored_session_id not in DELETED_SESSION_TOMBSTONES
            and session_references_codex_thread(stored, thread_id)
        )
    ]
    if stored_owners != [session_id] and not (
        not stored_owners and standalone_active_owner
    ):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Ambiguous thread owner")
    expected_proof = codex_provider_mcp_run_proof(session_id, thread_id, run_id)
    if not hmac.compare_digest(proof, expected_proof):
        return codex_provider_mcp_jsonrpc_error(request_id, -32602, "Invalid turn proof")
    try:
        text, is_error = await execute_provider_tool_once(
            session_id,
            run_id,
            params["arguments"],
            replay_key="codex:" + core_call_id,
            backend=BACKEND_CODEX,
            provider_thread_id=thread_id,
            provider_turn_id=turn_id,
        )
    except ProviderToolError as exc:
        text, is_error = str(exc), True
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    })


@router.get("/api/connection-addresses")
async def connection_addresses(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    # Discover only on explicit QR sharing, never on the frequently polled health path.
    from server_instances import candidate_addresses, connection_choices, tailscale_status

    def discover() -> list[str]:
        choices = connection_choices(
            SERVER.bind_address, SERVER.port,
            candidate_addresses(SERVER.bind_address, SERVER.port), tailscale_status(),
        )
        return list(dict.fromkeys(filter(None, [choices["tailscale"], *choices["lan"]])))[:6]

    return {"version": 1, "server_identity": server_identity(), "urls": await asyncio.to_thread(discover)}


@router.get("/api/health")
async def health() -> dict[str, Any]:
    await reconcile_idle_queued_turns_from_health_poll()
    # Health must answer even when an admission lock is wedged; the reads
    # below are consistent without the lock on a single-threaded loop.
    async with bounded_lock(RUNS.active_lock, SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS):
        active = sorted(RUNS.busy)
    async with bounded_lock(RUNS.queue_lock, SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS):
        queued = {sid: len(queue) for sid, queue in RUNS.queued.items() if queue}
    restart_blocker_snapshot = await current_server_restart_blocker_snapshot()
    pressure = host_pressure_snapshot()
    tmux = tmux_capability(use_cache=True)
    return {
        "ok": True,
        "chat_readiness": queue_recovery_readiness(),
        "server_version": SERVER_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
        "server_identity": server_identity(),
        "server_instance_id": SERVER_INSTANCE_ID,
        "server_name": HYADEX_SERVER_DISPLAY_NAME,
        "state_dir": str(STATE_DIR),
        "default_backend": DEFAULT_BACKEND,
        "default_cwd": existing_cwd(DEFAULT_CWD),
        "auth_required": bool(AGENT_TOKEN),
        "capabilities": {
            "group_discussions_v1": {"version": 1, "modes": ["sequential", "parallel"], "jobs": True,
                                     "goals": True, "enabled_backends": sorted(enabled_backends())},
            "autopilot_v1": {"version": 1, "approval": ["plan", "step", "none"],
                             "isolation": ["worktree", "in_place"], "reviewer": ["planner", "fresh", "none"]},
            "subagent_limit_v1": {"version": 1, "backends": ["codex", "claude"]},
            "provider_usage": {"available": bool(AGENT_TOKEN), "version": 1, "backends": ["codex", "claude"]},
            "side_questions": side_questions.capability(),
            "websocket_auth_v1": {
                "available": True,
                "required": False,
                "version": 1,
                "message": (
                    "Event and terminal sockets support URL-free token "
                    "authentication."
                ),
                "action": None,
                "event_protocol": EVENTS_WEBSOCKET_PROTOCOL,
                "terminal_protocol": TERMINAL_WEBSOCKET_PROTOCOL,
            },
            "event_stream_liveness_v1": {"available": True, "version": 1},
            "session_fork_completed_prefix_v1": {
                "available": True,
                "version": 1,
                "supported_backends": [BACKEND_CLAUDE, BACKEND_CODEX],
            },
            "local_provider_commands_v1": {
                "available": True,
                "required": False,
                "version": 1,
                "endpoint": "/api/sessions/{session_id}/provider-commands",
                "supported_backends": [BACKEND_CODEX, BACKEND_CLAUDE],
                "client_capability_by_backend": {
                    BACKEND_CODEX: CODEX_INTERACTIVE_CLIENT_CAPABILITY,
                    BACKEND_CLAUDE: CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY,
                },
                "supported_kinds_by_backend": {
                    BACKEND_CODEX: ["skill"],
                    BACKEND_CLAUDE: ["command"],
                },
                "max_items": MAX_PROVIDER_COMMANDS,
                "message": (
                    "Session-scoped Claude commands and Codex skills are available."
                ),
                "action": None,
            },
            "server_restart": server_restart_capability(
                restart_blocker_snapshot
            ),
            "tmux": tmux,
            "working_directory_completion": {
                "available": True,
                "required": False,
                "message": "Working-directory suggestions are available from this server.",
                "action": None,
                "version": 1,
                "max_results": MAX_WORKING_DIRECTORY_COMPLETIONS,
            },
            "scheduled_jobs": {
                "available": True,
                "required": False,
                "message": (
                    "Scheduled jobs support parent-chat and standalone "
                    "provider contexts."
                ),
                "action": None,
                "version": 6,
                "context_modes": ["chat", "standalone"],
                "default_context_mode": "chat",
                "features": {
                    "next_run_reset": True,
                    "interval_next_run_reanchors": True,
                },
            },
            "provider_jobs_access_control_v1": {
                "available": bool(AGENT_TOKEN),
                "required": False,
                "message": (
                    "Per-chat provider scheduled-jobs access control is available."
                    if AGENT_TOKEN
                    else "Provider scheduled-jobs access control requires an authenticated HyadexServer configuration."
                ),
                "action": None if AGENT_TOKEN else "Configure an HyadexServer client token.",
                "version": 1,
                "modes": list(PROVIDER_JOBS_ACCESS_MODES),
                "default": PROVIDER_JOBS_ACCESS_DEFAULT,
            },
            "agent_emergency_alerts_v1": {
                "available": True,
                "required": False,
                "message": "Agents can raise explicit, durable emergency alerts for immediate user attention.",
                "action": None,
                "version": 1,
                "max_message_chars": EMERGENCY_MESSAGE_MAX_CHARS,
                "max_requests_per_run": EMERGENCY_REQUESTS_PER_RUN,
                "max_active_alerts": EMERGENCY_ACTIVE_ALERT_LIMIT,
                "stream_path": "/api/emergency-alerts/events",
                "stream_protocol": EMERGENCY_WEBSOCKET_PROTOCOL,
                "acknowledge_through_alert_id": True,
            },
            "agent_notification_levels_v1": {
                "available": bool(AGENT_TOKEN),
                "required": False,
                "message": "Agents can choose durable normal, important, or emergency notification levels.",
                "action": None if AGENT_TOKEN else "Configure an HyadexServer client token.",
                "version": 1,
                "levels": ["normal", "important", "emergency"],
                "max_message_chars": EMERGENCY_MESSAGE_MAX_CHARS,
                "max_requests_per_run": NOTIFICATION_REQUESTS_PER_RUN,
            },
            "codex_controls": {
                "available": CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC,
                "required": False,
                "message": (
                    "Interactive Codex app-server controls are available to opted-in clients."
                    if CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC
                    else "Codex controls require the app-server transport."
                ),
                "action": (
                    None
                    if CODEX_TRANSPORT != CODEX_TRANSPORT_EXEC
                    else "Set HYADEX_CODEX_TRANSPORT=app-server or auto."
                ),
                "version": 2,
                "interactive_client_capability": CODEX_INTERACTIVE_CLIENT_CAPABILITY,
                "features": {
                    "approvals": True,
                    "questions": True,
                    "goals": CODEX_GOALS_ENABLED,
                    "compaction": True,
                    "thread_status": True,
                },
            },
            "claude_controls": {
                "available": (
                    CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
                    and claude_sdk_dependency_available()
                ),
                "required": False,
                "message": (
                    "Per-chat Claude Agent SDK controls are available to opted-in clients."
                    if (
                        CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
                        and claude_sdk_dependency_available()
                    )
                    else "Claude uses the compatible print transport."
                ),
                "action": (
                    None
                    if claude_sdk_dependency_available()
                    else "Install the server release with Claude Agent SDK support."
                ),
                "version": 3,
                "interactive_client_capability": (
                    CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY
                ),
                "permission_modes": list(CLAUDE_PERMISSION_MODE_OPTIONS),
                "features": {
                    # Every real turn now carries run-scoped out-of-band
                    # authority. In-place provider steering cannot prove that
                    # a delayed pre-steer tool call belongs to the successor
                    # run, so Force Send deliberately uses Stop -> fresh turn.
                    "native_steer": False,
                    "force_send": True,
                    "interrupt": True,
                    "approvals": True,
                    "questions": True,
                    "permission_mode_control": True,
                    "permission_modes": True,
                    "context_usage_refresh": True,
                    "resume": True,
                    "fork": True,
                },
                "fallback_transport": CLAUDE_TRANSPORT_PRINT,
            },
        },
        "websocket_runtime": True,
        "websocket_runtime_version": websockets.__version__,
        "active": active,
        "active_count": len(active),
        "claude_stop_fence_count": len(CLAUDE_STOP_FENCE_SESSIONS),
        "queued": queued,
        "jobs": len(JOBS.jobs),
        "job_guard": {**pressure, **scheduled_job_resource_diagnostics(pressure, len(active))},
        "host_health_log": str(HOST_HEALTH_FILE),
        "runtimes": runtime_diagnostics_snapshot(),
    }


def require_native_admin_control(request: Request) -> None:
    """Repeat privileged native auth at the endpoint invocation boundary."""

    if privileged_native_browser_request_forbidden(request):
        raise HTTPException(status_code=403, detail="forbidden")
    if not AGENT_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "This administrative control requires authenticated "
                "HyadexServer mode"
            ),
        )
    if not request_exact_native_token_header_authorized(request):
        raise HTTPException(status_code=401, detail="unauthorized")


router.include_router(side_questions.create_side_question_router(
    authorize=require_native_admin_control,
    session_exists=native_session_available,
    runtime=SIDE_QUESTIONS,
))


# Group discussions follow their member chats' turns; observing only schedules
# the engine's own work and never delays the chat's event path.
EVENTS.on_observe(lambda session_id, event: GROUPS.observe_member_event(session_id, event),
                  types=group_chat.OBSERVED_EVENT_TYPES)
AUTOPILOT = autopilot.AutopilotEngine(STATE_DIR / "autopilot", GROUPS)
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


@router.get("/api/admin/codex/goals", dependencies=route_auth("native_admin"))
async def get_codex_goals_admin_endpoint(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    return await get_codex_goals_admin()


@router.put("/api/admin/codex/goals", dependencies=route_auth("native_admin", CODEX_GOALS_ADMIN_MAX_BODY_BYTES))
async def put_codex_goals_admin_endpoint(
    req: CodexGoalsAdminRequest,
    request: Request,
) -> dict[str, Any]:
    require_native_admin_control(request)
    return await put_codex_goals_admin(req)


@router.get("/api/admin/codex/subagents", dependencies=route_auth("native_admin"))
async def get_codex_subagents_admin_endpoint(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    return await get_codex_subagents_admin()


@router.put("/api/admin/codex/subagents", dependencies=route_auth("native_admin", CODEX_GOALS_ADMIN_MAX_BODY_BYTES))
async def put_codex_subagents_admin_endpoint(
    req: CodexSubagentsAdminRequest,
    request: Request,
) -> dict[str, Any]:
    require_native_admin_control(request)
    return await put_codex_subagents_admin(req)


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


@router.get("/api/admin/restart", dependencies=route_auth("restart"))
async def server_restart_status_endpoint(request: Request) -> dict[str, Any]:
    require_server_restart_control(request)
    blocker_snapshot = await current_server_restart_blocker_snapshot()
    return public_server_restart_status(
        blocker_snapshot=blocker_snapshot,
    )


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


@router.post("/api/admin/restart", status_code=202, dependencies=route_auth("restart"))
async def restart_server_endpoint(
    body: ServerRestartRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    try:
        return await _restart_server_endpoint_impl(
            body,
            request,
            background_tasks,
        )
    except HTTPException as exc:
        log_server_restart_denial(body, exc)
        raise


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


@router.get("/api/runtime/catalog")
async def runtime_catalog(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        await refresh_codex_app_server_binary(force=True)
        await refresh_codex_app_server_login(source="catalog_refresh")
    return await asyncio.to_thread(discover_runtime_catalog, force_runtime_probe=refresh)


@router.get("/api/runtime/usage")
async def runtime_provider_usage(
    request: Request, backend: str, session_id: str | None = None,
    refresh: bool = False,
) -> JSONResponse:
    require_native_admin_control(request)
    session = STORE.sessions.get(session_id) if session_id else None
    if session_id and session is None:
        raise HTTPException(404, "session not found")
    if backend not in {BACKEND_CODEX, BACKEND_CLAUDE}:
        result = provider_usage.unavailable(backend, "unsupported_provider")
    elif session is not None and str(session.get("backend") or DEFAULT_BACKEND).lower() != backend:
        result = provider_usage.unavailable(backend, "selection_changed")
    elif backend == BACKEND_CODEX:
        if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
            result = provider_usage.unavailable(backend, "unsupported_transport")
        else:
            try:
                manager = await codex_app_server_manager(session, allow_retired_login=True)
                result = await PROVIDER_USAGE.read_codex(manager, refresh=refresh)
            except Exception:
                result = provider_usage.unavailable(backend, "temporarily_unavailable")
    else:
        result = await read_claude_account_usage(refresh=refresh)
        if result.get("status") != "available":
            generation = (PROVIDERS.claude_sdk_manager.usage_generation(session_id)
                          if session_id and PROVIDERS.claude_sdk_manager is not None else None)
            observed = PROVIDER_USAGE.read_claude(session_id or "", generation)
            if observed.get("status") == "available":
                result = observed
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{session_id}/terminal")
async def get_session_terminal(session_id: str, lines: int = 240) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_snapshot, session_id, lines=lines)


@router.post("/api/sessions/{session_id}/terminal/open")
async def open_session_terminal(session_id: str, req: TerminalOpenRequest) -> dict[str, Any]:
    return await asyncio.to_thread(ensure_terminal_session, session_id, req.cwd)


@router.post("/api/sessions/{session_id}/terminal/input")
async def input_session_terminal(session_id: str, req: TerminalInputRequest) -> dict[str, Any]:
    return await asyncio.to_thread(send_terminal_input, session_id, req.text, enter=req.enter, key=req.key)


@router.post("/api/sessions/{session_id}/terminal/resize")
async def resize_session_terminal(session_id: str, req: TerminalResizeRequest) -> dict[str, Any]:
    return await asyncio.to_thread(resize_terminal_pane, session_id, req.columns, req.rows)


@router.get("/api/sessions/{session_id}/terminal/windows")
async def get_session_terminal_windows(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_windows_snapshot, session_id)


@router.post("/api/sessions/{session_id}/terminal/action")
async def run_session_terminal_action(session_id: str, req: TerminalActionRequest) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_action, session_id, req.action, req.target)


@router.delete("/api/sessions/{session_id}/terminal")
async def delete_session_terminal(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(kill_terminal_session, session_id)


@router.get("/api/sessions/{session_id}/tmux")
async def get_session_tmux_panes(session_id: str, include_all: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(tmux_panes_snapshot, session_id, include_all=include_all)


@router.get("/api/sessions/{session_id}/tmux/capture")
async def capture_session_tmux_pane(session_id: str, pane_id: str, lines: int = 500) -> dict[str, Any]:
    return await asyncio.to_thread(capture_tmux_pane, session_id, pane_id, lines=lines)


@router.get("/api/sessions")
async def list_sessions(summary: bool = False, placeholder_client_id: str = "") -> dict[str, Any]:
    await STORE.ensure_sort_orders()
    sessions = [
        public_session(session, summary=summary)
        for session in sorted_sessions(list(STORE.sessions.values()))
        if not session.get("_fork_initializing")
        and not session.get("group_member")
        and not empty_chat_dismissed_for(session, placeholder_client_id)
    ]
    return {"sessions": sessions}


@router.get("/api/search")
async def search_all_session_timelines(
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    eligible_session_ids = active_history_search_session_ids()
    return await asyncio.to_thread(
        search_all_timelines,
        q,
        limit,
        eligible_session_ids,
    )


@router.get("/api/working-directories/complete")
async def complete_working_directory(
    path: str = Query(default="", max_length=MAX_WORKSPACE_PATH_CHARS),
    limit: int = Query(default=24, ge=1, le=MAX_WORKING_DIRECTORY_COMPLETIONS),
) -> dict[str, Any]:
    return await asyncio.to_thread(complete_working_directory_sync, path, limit)


@router.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    require_enabled_backend(req.backend)
    provider_keys = local_session_ownership.provider_session_keys(req.model_dump(), DEFAULT_BACKEND)
    if provider_keys:
        with local_history_import_guard():
            foreign_keys = await asyncio.to_thread(other_local_instance_provider_keys)
            if provider_keys & foreign_keys:
                raise HTTPException(
                    status_code=409,
                    detail="This provider conversation is already used by another local HyadexServer instance. Open it there instead.",
                )
            return await create_session_with_history(req)
    return await create_session_with_history(req)


@router.get("/api/sessions/{session_id}/timeline-index")
async def get_timeline_index(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    return await asyncio.to_thread(build_timeline_index, session_id)


@router.get("/api/sessions/{session_id}/subagents")
async def get_session_subagents(
    session_id: str,
    limit: int = Query(default=64, ge=1, le=SUBAGENT_SNAPSHOT_STATE_LIMIT),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    session = STORE.sessions[session_id]
    if str(session.get("backend") or "") == BACKEND_CODEX and session_provider_id(session):
        try:
            # Inspect the process which still owns the children during login
            # handoff. This read must not be rejected as a new model turn.
            manager = await codex_app_server_manager(session, allow_retired_login=True)
            await reconcile_codex_subagents(session_id, manager)
        except Exception as exc:
            logger.warning(
                "Codex subagent snapshot reconciliation failed session=%s error=%s",
                session_id,
                concise_error_message(exc),
            )
    return await asyncio.to_thread(build_subagent_snapshot, session_id, limit)


@router.get("/api/sessions/{session_id}/search")
async def search_session_timeline(
    session_id: str,
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_initializing(session_id)
    return await asyncio.to_thread(search_timeline_index, session_id, q, limit)


@router.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = DEFAULT_SESSION_EVENT_LIMIT,
    tail: bool = True,
    visible: bool = False,
    compact: bool = False,
    page_mode: str | None = None,
    semantic_before: int | None = None,
) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    # Opening a chat is the moment to notice that its provider transcript grew
    # somewhere else (the provider's own CLI, or another machine sharing it).
    # Scheduled rather than awaited so the open stays fast; the client's event
    # stream delivers whatever it recovers.
    schedule_provider_history_sync(sess)
    normalized_page_mode = str(page_mode or "").strip().lower()
    if normalized_page_mode not in {"", "semantic"}:
        raise HTTPException(status_code=400, detail="page_mode must be semantic")
    page_tail = tail and after <= 0
    semantic_page: dict[str, Any] | None = None
    if normalized_page_mode == "semantic":
        semantic_page = await asyncio.to_thread(
            read_semantic_timeline_page,
            session_id,
            after=after,
            semantic_before=semantic_before if semantic_before is not None else before,
            limit=limit,
            tail=page_tail,
        )
        events = semantic_page["events"]
        latest_seq = int(semantic_page["latest_seq"])
        event_count = int(semantic_page["semantic_total"])
        omitted_before = int(semantic_page["semantic_omitted_before"])
        omitted_after = int(semantic_page["semantic_omitted_after"])
    elif visible or compact:
        if after > 0 and before is None and not page_tail:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_after_page,
                session_id,
                after=after,
                limit=limit,
                compact=compact,
            )
        else:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_page,
                session_id,
                after=after,
                before=before,
                limit=limit,
                tail=page_tail,
                compact=compact,
            )
    else:
        events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
            read_client_events_page,
            session_id,
            after=after,
            before=before,
            limit=limit,
            tail=page_tail,
        )
    await reconcile_idle_queue_session_from_snapshot(session_id)
    response = {
        "session": public_session(sess),
        "events": events,
        "queued_turns": await queued_turns_snapshot(session_id),
        "events_omitted_before": omitted_before,
        "events_omitted_after": omitted_after,
        "latest_seq": latest_seq,
        "event_count": event_count,
    }
    if semantic_page is not None:
        response.update({
            "semantic_item_count": semantic_page["semantic_item_count"],
            "semantic_total": semantic_page["semantic_total"],
            "semantic_omitted_before": semantic_page["semantic_omitted_before"],
            "semantic_omitted_after": semantic_page["semantic_omitted_after"],
            "next_semantic_before": semantic_page["next_semantic_before"],
        })
    return response


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    return await _update_session(session_id, req)


@router.get("/api/sessions/{session_id}/provider-commands")
async def get_session_provider_commands(
    session_id: str,
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Return one sanitized, session-scoped native command inventory."""

    session = STORE.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    snapshot, _inventory = await discover_session_provider_commands(
        session_id,
        dict(session),
        refresh=bool(refresh),
    )
    return snapshot


@router.get("/api/sessions/{session_id}/codex/runtime")
async def get_codex_runtime(session_id: str) -> dict[str, Any]:
    return await codex_runtime_snapshot(session_id)


@router.post(
    "/api/sessions/{session_id}/codex/interactions/{interaction_id}/resolve"
)
async def post_codex_interaction_response(
    session_id: str,
    interaction_id: str,
    req: CodexInteractionResponseRequest,
) -> dict[str, Any]:
    interaction = await resolve_codex_interaction(
        session_id,
        interaction_id,
        req.response,
    )
    return {"interaction": interaction}


@router.put("/api/sessions/{session_id}/claude/goal")
async def put_claude_goal(session_id: str, req: ClaudeGoalRequest) -> dict[str, Any]:
    return await set_claude_goal(session_id, req)


@router.delete("/api/sessions/{session_id}/claude/goal")
async def delete_claude_goal(session_id: str) -> dict[str, Any]:
    require_claude_goal_session(session_id)
    cleared_live = False
    # Serialize with ordinary turn admission while the native priority command
    # interrupts the current turn and returns its separate clear receipt.
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        async with RUNS.active_lock:
            active = dict(RUNS.active.get(session_id) or {})
        if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
            manager = await claude_sdk_manager()
            try:
                await manager.clear_goal(session_id, run_id=str(active.get("run_id") or ""))
            except ClaudeSDKSupervisorError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            cleared_live = True
    if not cleared_live:
        await start_claude_goal_command(session_id, "clear")
    await refresh_claude_goal(session_id)
    return await claude_runtime_snapshot(session_id)


@router.get("/api/sessions/{session_id}/claude/runtime")
async def get_claude_runtime(session_id: str) -> dict[str, Any]:
    return await claude_runtime_snapshot(session_id)


@router.post("/api/sessions/{session_id}/claude/context-usage/refresh")
async def post_claude_context_usage_refresh(
    session_id: str,
) -> dict[str, Any]:
    refreshed = await refresh_idle_claude_context_usage(session_id)
    if not refreshed:
        raise HTTPException(
            status_code=409,
            detail="Claude context changed while sampling; retry the refresh",
        )
    return {
        **await claude_runtime_snapshot(session_id),
        "context_usage_refreshed": True,
    }


@router.post(
    "/api/sessions/{session_id}/claude/interactions/{interaction_id}/resolve"
)
async def post_claude_interaction_response(
    session_id: str,
    interaction_id: str,
    req: ClaudeInteractionResponseRequest,
) -> dict[str, Any]:
    # Serialize approval resolution with Stop/Delete lifecycle changes. Once
    # deletion has reserved this chat, no late UI response may authorize a
    # provider action while cleanup is fencing the SDK callback.
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        interaction = await resolve_claude_interaction(
            session_id,
            interaction_id,
            req.response,
        )
        return {"interaction": interaction}


@router.get("/api/sessions/{session_id}/codex/goal")
async def get_codex_goal(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "enabled": CODEX_GOALS_ENABLED,
        "goal": session.get("codex_goal"),
        "time_budget_seconds": session.get("codex_goal_time_budget_seconds"),
        "time_budget_exhausted": codex_goal_time_budget_is_exhausted(session),
    }


@router.put("/api/sessions/{session_id}/codex/goal")
async def put_codex_goal(
    session_id: str,
    req: CodexGoalRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _put_codex_goal_locked(session_id, req)


@router.delete("/api/sessions/{session_id}/codex/goal")
async def delete_codex_goal(session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _delete_codex_goal_locked(session_id)


@router.post("/api/sessions/{session_id}/codex/compact")
async def post_codex_compact(session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _post_codex_compact_locked(session_id)


@router.post("/api/sessions/{session_id}/read")
async def mark_session_read(session_id: str, req: ReadSessionRequest) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = await STORE.mark_read(session_id, req.last_read_agent_event_seq)
    return {"session": public_session(sess)}


@router.post("/api/sessions/{session_id}/unread")
async def mark_session_unread(session_id: str) -> dict[str, Any]:
    ensure_session_not_initializing(session_id)
    sess = await STORE.mark_unread(session_id)
    return {"session": public_session(sess)}


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


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, discard_updated_at: str | None = None) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_initializing(session_id)
        if (STORE.sessions.get(session_id) or {}).get("gemini_cleanup_pending_run_id"):
            # Keep the persisted uncertainty fence even though its runtime was
            # removed. Deleting the row must not erase the only cleanup record.
            raise HTTPException(409, "Retired provider cleanup was not confirmed. Verify the old runtime on the server before deleting this chat.")
        if session_id in DELETED_SESSION_TOMBSTONES:
            # A previous delete can have committed before its request was
            # canceled. Retry any tracked bridge retirement on idempotent
            # deletion instead of returning while stale work remains alive.
            await SIDE_QUESTIONS.close_session(session_id)
            SIDE_QUESTIONS.delete_history(session_id)
            await cancel_claude_stop_fence_retry(
                session_id,
                clear_fence=True,
            )
            deleted_jobs = await JOBS.delete_for_session(session_id)
            return {
                "ok": True,
                "deleted": False,
                "deleted_jobs": deleted_jobs,
            }
        if (
            stop_cleanup_in_progress(session_id)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Explicit Stop cleanup is still finishing; the session "
                    "was not deleted. Retry shortly."
                ),
            )
        DELETING_SESSIONS.add(session_id)
        deleted = False
        try:
            if discard_updated_at is not None:
                await ensure_discardable_empty_session(session_id, discard_updated_at)
            cancel_generated_session_title(session_id)
            if (
                session_id in CLAUDE_STOP_FENCE_SESSIONS
                or session_id in CLAUDE_STOP_FENCE_RETRY_TASKS
                or any(
                    not task.done()
                    for task in CLAUDE_STOP_FENCE_ATTEMPT_TASKS.get(
                        session_id,
                        (),
                    )
                )
            ):
                fence_retry_stopped = await cancel_claude_stop_fence_retry(
                    session_id,
                    clear_fence=False,
                )
                if not fence_retry_stopped:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Claude Stop recovery is still finishing; the "
                            "session was not deleted. Retry shortly."
                        ),
                    )
            session = STORE.sessions.get(session_id) or {}
            provider_thread_ids = {
                thread_id
                for thread_id in (str(session_provider_id(session) or ""),)
                if thread_id
            }
            await SIDE_QUESTIONS.close_session(session_id)
            await cancel_codex_interactions(
                session_id,
                resolution="session_deleted",
            )
            await cancel_claude_interactions(
                session_id,
                resolution="session_deleted",
            )
            interactions_finished = await wait_for_session_tasks(
                CODEX_INTERACTION_HANDLER_TASKS,
                session_id,
            )
            claude_interactions_finished = await wait_for_session_tasks(
                CLAUDE_INTERACTION_HANDLER_TASKS,
                session_id,
            )
            if not interactions_finished or not claude_interactions_finished:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Provider approval cleanup is still finishing; "
                        "the session was not deleted. Retry shortly."
                    ),
                )
            async with RUNS.run_now_request_lock:
                run_now_entry = RUNS.run_now_requests.get(session_id)
                run_now_task = (
                    run_now_entry[1]
                    if run_now_entry is not None
                    else None
                )

            await cancel_codex_native_actions(session_id)

            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                busy = session_id in RUNS.busy

            if active and not active.get("codex_native_operation"):
                goal_pause_ok, _goal_paused, goal_pause_error = (
                    await pause_active_codex_goal_for_stop(session_id, active)
                )
                if not goal_pause_ok:
                    raise HTTPException(
                        status_code=409,
                        detail=goal_pause_error
                        or "The active Codex goal could not be paused. Retry deletion.",
                    )

            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                busy = session_id in RUNS.busy
                if active and not active.get("codex_native_operation"):
                    active["stop_requested"] = True
                    if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
                        active["claude_permissions_open"] = False
                    if active.get("run_id"):
                        RUNS.stopped_runs.add(str(active["run_id"]))
                elif busy and not active:
                    RUNS.stop_requests.add(session_id)

            if active and not active.get("codex_native_operation"):
                native_turn = active.get("codex_app_server_turn")
                proc = active.get("proc")
                if (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and native_turn is not None
                    and getattr(native_turn, "turn_id", "")
                ):
                    with suppress(Exception):
                        await native_turn.interrupt()
                elif (
                    active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
                ):
                    claude_sdk_run = active.get("claude_sdk_run")
                    if claude_sdk_run is not None:
                        try:
                            await interrupt_claude_sdk_run_bounded(
                                claude_sdk_run,
                            )
                        except Exception:
                            pass
                    # Delete is terminal, unlike Stop. Retire this one chat's
                    # SDK process immediately even when interrupt returned an
                    # acknowledgement: the provider stream is not required to
                    # emit a terminal Result after that acknowledgement.
                    if not await evict_claude_sdk_chat(
                        session_id,
                        force=True,
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "The Claude chat process is still shutting "
                                "down; the session was not deleted. Retry "
                                "shortly."
                            ),
                        )
                elif proc:
                    await terminate_process_tree(proc)

            turns_finished = await wait_for_session_tasks(
                RUNS.turn_tasks,
                session_id,
            )
            if not turns_finished:
                async with RUNS.active_lock:
                    unsettled_active = RUNS.active.get(session_id)
                if (
                    unsettled_active
                    and unsettled_active.get("transport")
                    == CLAUDE_TRANSPORT_AGENT_SDK
                ):
                    # An SDK interrupt acknowledgement does not guarantee the
                    # message stream will emit a terminal Result. Deletion is
                    # terminal, so retire this chat's process and cancel its
                    # local consumer before deciding cleanup is stuck.
                    claude_evicted = await evict_claude_sdk_chat(
                        session_id,
                        force=True,
                    )
                    if claude_evicted:
                        current_task = asyncio.current_task()
                        unsettled_tasks = {
                            task
                            for task in tuple(
                                RUNS.turn_tasks.get(session_id) or ()
                            )
                            if (
                                task is not current_task
                                and not task.done()
                            )
                        }
                        for task in unsettled_tasks:
                            task.cancel()
                        if unsettled_tasks:
                            await asyncio.wait(
                                unsettled_tasks,
                                timeout=max(
                                    0.01,
                                    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
                                ),
                            )
                        turns_finished = await wait_for_session_tasks(
                            RUNS.turn_tasks,
                            session_id,
                            timeout=0.01,
                        )
            if not turns_finished:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The active agent turn has not acknowledged interruption "
                        "yet; the session was not deleted. Retry shortly."
                    ),
                )
            if (
                isinstance(run_now_task, asyncio.Task)
                and run_now_task is not asyncio.current_task()
                and not run_now_task.done()
            ):
                _done, pending_run_now = await asyncio.wait(
                    {run_now_task},
                    timeout=max(0.01, CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS),
                )
                if pending_run_now:
                    run_now_task.cancel()
                    _done, still_pending_run_now = await asyncio.wait(
                        {run_now_task},
                        timeout=max(
                            0.01,
                            CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
                        ),
                    )
                    if still_pending_run_now:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Force Send cleanup is still finishing; "
                                "the session was not deleted. Retry shortly."
                            ),
                        )
            async with RUNS.run_now_request_lock:
                current_run_now = RUNS.run_now_requests.get(session_id)
                if (
                    current_run_now is not None
                    and current_run_now[1] is run_now_task
                ):
                    RUNS.run_now_requests.pop(session_id, None)
                for cache_key in tuple(RUN_NOW_COMPLETED_RESULTS):
                    if cache_key[0] == session_id:
                        RUN_NOW_COMPLETED_RESULTS.pop(cache_key, None)
            # A turn can bind its provider thread after deletion captured the
            # initial session record but before its task acknowledges stop.
            # The task registry is now drained, so this reread is stable.
            late_session = STORE.sessions.get(session_id) or {}
            late_provider_thread_id = str(
                session_provider_id(late_session) or ""
            )
            if late_provider_thread_id:
                provider_thread_ids.add(late_provider_thread_id)

            manager = existing_codex_app_server_manager(late_session or session)
            if manager is not None:
                for provider_thread_id in provider_thread_ids:
                    if manager.is_thread_loaded(provider_thread_id):
                        with suppress(CodexAppServerError):
                            await manager.unsubscribe_thread(
                                provider_thread_id
                            )
            claude_manager = PROVIDERS.claude_sdk_manager
            if claude_manager is not None:
                claude_evicted = await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    manager=claude_manager,
                )
                if not claude_evicted:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The Claude chat process is still shutting down; "
                            "the session was not deleted. Retry shortly."
                        ),
                    )
            if discard_updated_at is not None:
                await ensure_discardable_empty_session(session_id, discard_updated_at)
            # All fallible provider/task cleanup has completed. From here the
            # deletion is committed: revoke run authority first, then remove
            # queue/runtime ownership.
            async with RUNS.active_lock:
                deletion_run_ids = {
                    str(value or "")
                    for value in (
                        (RUNS.active.get(session_id) or {}).get("run_id"),
                        (RUNS.current_turns.get(session_id) or {}).get("run_id"),
                    )
                    if str(value or "")
                }
            for deletion_run_id in deletion_run_ids:
                await revoke_provider_authority(deletion_run_id)
            steering_waiter = RUNS.steering_waits.pop(session_id, None)
            if steering_waiter is not None and not steering_waiter[2].done():
                steering_waiter[2].cancel()
            async with RUNS.queue_lock:
                RUNS.queued.pop(session_id, None)
                RUNS.run_now.pop(session_id, None)
                RUNS.steering.discard(session_id)
            async with RUNS.active_lock:
                RUNS.active.pop(session_id, None)
                RUNS.busy.discard(session_id)
                RUNS.current_turns.pop(session_id, None)
                RUNS.stop_requests.discard(session_id)
            if provider_thread_ids:
                async with CODEX_APP_SERVER_THREAD_LRU_LOCK:
                    for provider_thread_id in provider_thread_ids:
                        CODEX_APP_SERVER_THREAD_LRU.pop(
                            provider_thread_id,
                            None,
                        )
                        CODEX_APP_SERVER_THREAD_PIN_COUNTS.pop(
                            provider_thread_id,
                            None,
                        )
                        CODEX_APP_SERVER_PINNED_THREADS.discard(
                            provider_thread_id
                        )
                        CODEX_APP_SERVER_INVALIDATED_THREADS.discard(
                            provider_thread_id
                        )
            for provider_thread_id in provider_thread_ids:
                CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS.pop(
                    provider_thread_id,
                    None,
                )
                CODEX_INTERACTIVE_CONTROL_THREADS.discard(provider_thread_id)
                if CODEX_THREAD_SESSION_INDEX.get(provider_thread_id) == session_id:
                    CODEX_THREAD_SESSION_INDEX.pop(provider_thread_id, None)
                for key in tuple(CODEX_APPROVAL_ITEM_CACHE):
                    if key[0] == provider_thread_id:
                        CODEX_APPROVAL_ITEM_CACHE.pop(key, None)
            CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)
            for provider_thread_id, owner_session_id in tuple(
                CODEX_QUARANTINED_GOAL_THREADS.items()
            ):
                if owner_session_id == session_id:
                    CODEX_QUARANTINED_GOAL_THREADS.pop(
                        provider_thread_id,
                        None,
                    )
            with suppress(Exception):
                await asyncio.to_thread(kill_terminal_session, session_id)
            async def finish_committed_delete() -> dict[str, Any]:
                async with event_delivery_lock(session_id):
                    if discard_updated_at is not None:
                        await ensure_discardable_empty_session(session_id, discard_updated_at)
                    committed_deleted = await STORE.delete(session_id)
                    DELETED_SESSION_TOMBSTONES.add(session_id)
                    DELETING_SESSIONS.discard(session_id)
                SIDE_QUESTIONS.delete_history(session_id)
                if session_id in CLAUDE_STOP_FENCE_SESSIONS:
                    # No retry/attempt task remains after the bounded
                    # pre-delete join above. Clear synchronously after the
                    # durable delete so cancellation cannot strand a local
                    # blocker.
                    CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                    RUNS.maintenance.discard(session_id)
                    CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.pop(session_id, None)
                if committed_deleted:
                    await broadcast_committed_emergency_removal(session_id)
                committed_deleted_jobs = await JOBS.delete_for_session(
                    session_id
                )
                return {
                    "ok": True,
                    "deleted": committed_deleted,
                    "deleted_jobs": committed_deleted_jobs,
                }

            delete_completion_task = asyncio.create_task(
                finish_committed_delete()
            )
            try:
                delete_result = await asyncio.shield(delete_completion_task)
            except asyncio.CancelledError as cancellation:
                # STORE.delete owns an un-cancellable filesystem replacement.
                # If this request disappears after that write starts, retain
                # ownership through the tombstone, file/sequence cleanup and
                # job removal so no runnable work survives a deleted chat.
                delete_result = await join_task_despite_caller_cancellation(
                    delete_completion_task
                )
                deleted = bool(delete_result.get("deleted"))
                raise cancellation
            deleted = bool(delete_result.get("deleted"))
            return delete_result
        except BaseException:
            if deleted:
                DELETED_SESSION_TOMBSTONES.add(session_id)
            elif session_id in CLAUDE_STOP_FENCE_SESSIONS:
                schedule_claude_stop_fence_retry(session_id)
            DELETING_SESSIONS.discard(session_id)
            raise


@router.post("/api/sessions/{session_id}/fork")
async def fork_session(session_id: str, req: ForkSessionRequest) -> dict[str, Any]:
    if retired := retired_session_runtime_message(STORE.sessions.get(session_id) or {}):
        raise HTTPException(410, retired)
    cleanup_state: dict[str, str | None] = {}
    async with session_lifecycle_lock(session_id):
        try:
            ensure_session_not_deleting(session_id)
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id) or {}
                running = session_id in RUNS.busy or bool(active)
                active_run_id = str(active.get("run_id") or "")
                latest_sequence = (STORE.sessions.get(session_id) or {}).get("latest_event_seq")
            snapshot = None
            if running:
                parent = STORE.sessions.get(session_id) or {}
                if parent.get("backend", DEFAULT_BACKEND) not in {BACKEND_CLAUDE, BACKEND_CODEX}:
                    raise HTTPException(status_code=409, detail="This backend cannot fork a running chat safely; wait for its turn to finish.")
                snapshot = completed_fork_events(
                    await asyncio.to_thread(lambda: list(iter_session_events(session_id))),
                    active_run_id=active_run_id,
                    through_sequence=latest_sequence if isinstance(latest_sequence, int) else None,
                )
            # Holding the lifecycle lock prevents a new turn or deletion from
            # crossing the provider snapshot and local-history snapshot.
            return await _fork_session_locked(
                session_id,
                req,
                cleanup_state=cleanup_state,
                **({"completed_snapshot": snapshot} if snapshot is not None else {}),
            )
        except BaseException:
            await cleanup_aborted_session_fork(cleanup_state)
            raise


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


@router.get("/api/sessions/{session_id}/runs/{run_id}/trace")
async def get_session_run_trace(
    session_id: str,
    run_id: str,
    anchor_seq: int | None = Query(default=None, ge=1),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=160, ge=1, le=1_000),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await asyncio.to_thread(
            read_indexed_run_trace,
            session_id,
            run_id,
            anchor_seq=anchor_seq,
            after_seq=after_seq,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc


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


@router.websocket("/api/sessions/{session_id}/terminal/ws")
async def session_terminal(
    session_id: str,
    ws: WebSocket,
    columns: int = 120,
    rows: int = 36,
    cwd: str | None = None,
) -> None:
    selected_subprotocol = websocket_endpoint_subprotocol(
        ws,
        TERMINAL_WEBSOCKET_PROTOCOL,
    )
    if not websocket_authorized(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4404)
        return
    if bool(STORE.sessions[session_id].get("archived")):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4409)
        return

    cols, lines = terminal_dimensions(columns, rows)
    reserved = await TERMINAL_ATTACHMENTS.reserve(session_id, ws)
    if not reserved:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=1012)
        return
    process: subprocess.Popen[bytes] | None = None
    master_fd: int | None = None
    try:
        await ws.accept(subprotocol=selected_subprotocol)
        process, master_fd, name = await TERMINAL_ATTACHMENTS.spawn(
            session_id,
            ws,
            cwd,
            cols,
            lines,
        )
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "name": name,
            "columns": cols,
            "rows": lines,
        })

        async def pump_output() -> None:
            assert master_fd is not None
            while True:
                try:
                    data = await read_terminal_output(master_fd)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        return
                    raise
                if not data:
                    return
                await ws.send_bytes(data)

        async def receive_input() -> None:
            assert master_fd is not None
            auto_scroll_mode = False
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data:
                        if auto_scroll_mode:
                            await asyncio.to_thread(exit_terminal_auto_scroll, session_id)
                            auto_scroll_mode = False
                        await write_terminal_input(master_fd, data)
                        continue
                    text = message.get("text")
                    if not text:
                        continue
                    try:
                        control = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(control, dict):
                        continue
                    if control.get("type") == "resize":
                        # Control frames are untrusted and may come from a
                        # mixed-version or partially restored renderer. Match
                        # scroll handling: discard malformed geometry without
                        # turning it into a fatal WebSocket error.
                        with suppress(TypeError, ValueError):
                            next_cols, next_rows = terminal_dimensions(control.get("columns"), control.get("rows"))
                            set_pty_dimensions(master_fd, next_cols, next_rows)
                            await asyncio.to_thread(resize_terminal_window, session_id, next_cols, next_rows)
                    elif control.get("type") == "scroll":
                        with suppress(TypeError, ValueError):
                            auto_scroll_mode = await asyncio.to_thread(
                                scroll_terminal_history,
                                session_id,
                                int(control.get("delta") or 0),
                                managed=auto_scroll_mode,
                            )
            finally:
                if auto_scroll_mode:
                    await asyncio.to_thread(exit_terminal_auto_scroll, session_id)

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(receive_input())
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                task.result()
    except (WebSocketDisconnect, TerminalAttachmentAdmissionClosed):
        pass
    except Exception as exc:
        logger.warning("terminal websocket failed session=%s error=%s", session_id, exc)
        with suppress(RuntimeError):
            await ws.send_json({"type": "error", "message": str(exc)[:1000]})
    finally:
        if process is not None and master_fd is not None:
            await asyncio.to_thread(stop_terminal_client, process, master_fd)
        await TERMINAL_ATTACHMENTS.release(ws)
        with suppress(RuntimeError):
            await ws.close()


@router.websocket("/api/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    ws: WebSocket,
    after: int = 0,
    visible: bool | None = None,
    reasoning_stream: bool = False,
    reasoning_text: bool = False,
    liveness: bool = False,
    history_projection: bool = False,
) -> None:
    selected_subprotocol = websocket_endpoint_subprotocol(
        ws,
        EVENTS_WEBSOCKET_PROTOCOL,
    )
    if not websocket_authorized(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4404)
        return
    await ws.accept(subprotocol=selected_subprotocol)
    schedule_provider_history_sync(STORE.sessions[session_id])
    try:
        if not await HUB.reserve(session_id, ws):
            await close_event_websocket_over_capacity(ws)
            return
        cursor = max(0, int(after or 0))
        # Omitted ``visible`` is the legacy protocol (including released iOS
        # clients): it receives every client-safe event rather than the newer
        # timeline-visible subset. Both protocols still require complete,
        # sequence-bound catch-up; the old single 500-row read silently skipped
        # the rest of a long offline gap.
        catchup_visible = visible is True
        # A durable import holds this lock until its source-proven projection
        # is ready. Reading its fsynced rows earlier can replay raw scheduled
        # inputs before this socket is registered for the repaired broadcast.
        async with event_delivery_lock(session_id):
            boundary = await asyncio.to_thread(
                last_event_seq_from_file,
                events_path(session_id),
            )
        while True:
            cursor = await send_event_catchup(
                session_id,
                ws,
                after=cursor,
                through=boundary,
                visible=catchup_visible,
            )
            if cursor < boundary:
                # Never register a socket with an acknowledged hole. A short
                # scan means the log could not substantiate its own boundary;
                # live delivery cannot repair that missing historical range.
                raise RuntimeError(
                    "event catch-up could not reach its stable boundary"
                )

            # No socket I/O occurs while holding the delivery lock. If an
            # append raced the page stream, capture its new fixed boundary,
            # release the lock, and drain it in the next pass. Registration is
            # performed only in a lock-held instant where no gap remains, so a
            # later append observes the subscriber before it broadcasts.
            activated = False
            reasoning_snapshot = None
            async with event_delivery_lock(session_id):
                gap_boundary = await asyncio.to_thread(
                    last_event_seq_from_file,
                    events_path(session_id),
                )
                if cursor >= gap_boundary:
                    activated = await HUB.register_accepted(session_id, ws, **(
                        {"reasoning_stream": True, **({"reasoning_text": True} if reasoning_text else {})} if reasoning_stream else {}))
                    if activated and reasoning_stream:
                        reasoning_snapshot = reasoning_summary_stream_snapshot(session_id)
                        if not reasoning_text:
                            reasoning_snapshot["items"] = [item for item in reasoning_snapshot["items"]
                                if item.get("phase") != "reasoning"]
            if cursor >= gap_boundary:
                if not activated:
                    # This should be unreachable after a successful
                    # reservation, but retain retryable overload semantics if
                    # a future lifecycle path releases it early. Socket I/O
                    # remains outside the event-delivery lock.
                    await close_event_websocket_over_capacity(ws)
                    return
                if reasoning_snapshot is not None:
                    await asyncio.wait_for(ws.send_json(reasoning_snapshot), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                if liveness:
                    await send_event_stream_status(ws, session_id, "ready")
                if history_projection:
                    # A proof may finish between the HTTP page and subscription.
                    # The revision closes that gap without replaying old rows.
                    await asyncio.wait_for(ws.send_json(history_projection_notice(session_id)), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)
                break
            boundary = gap_boundary
        while True:
            if not liveness:
                await ws.receive_text()
                continue
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=EVENT_STREAM_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # No history scan, provider call, or sequence acknowledgement.
                # This confirms that the application event path is still alive.
                await send_event_stream_status(ws, session_id, "heartbeat")
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ConnectionError):
        await close_event_websocket_after_delivery_failure(ws)
    finally:
        await HUB.unsubscribe(session_id, ws)


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


@router.get("/api/sessions/{session_id}/diffs/{run_id}")
async def get_turn_code_diff(session_id: str, run_id: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise HTTPException(status_code=404, detail="code diff not found")
    patch_path = code_diffs_dir(session_id) / f"{run_id}.patch"
    if not patch_path.is_file():
        raise HTTPException(status_code=404, detail="code diff not found")
    return FileResponse(
        patch_path,
        media_type="text/x-diff; charset=utf-8",
        filename=f"{run_id}.patch",
        content_disposition_type="inline",
    )


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


def uvicorn_graceful_shutdown_seconds() -> int:
    return max(1, int(configured_uvicorn_graceful_shutdown_seconds()))


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
EXTRACTED_MODULES: tuple[types.ModuleType, ...] = (sys.modules['hyadex.core.config'], sys.modules['hyadex.core.paths'], sys.modules['hyadex.core.identity'], sys.modules['hyadex.providers.codex.config'], sys.modules['hyadex.providers.instructions'], sys.modules['hyadex.core.util'], sys.modules['hyadex.core.server_logging'], sys.modules['hyadex.core.state'], sys.modules['hyadex.providers.claude.history'], sys.modules['hyadex.features.titles'], sys.modules['hyadex.features.usage'], sys.modules['hyadex.core.processes'], sys.modules['hyadex.core.sessions'], sys.modules['hyadex.providers.codex.history'], sys.modules['hyadex.providers.authority'], sys.modules['hyadex.core.event_log'], sys.modules['hyadex.features.terminal'], sys.modules['hyadex.features.history'], sys.modules['hyadex.features.files'], sys.modules['hyadex.providers.subagents'], sys.modules['hyadex.providers.claude.runtime'], sys.modules['hyadex.features.side_questions'], sys.modules['hyadex.providers.registry'], sys.modules['hyadex.core.queue'], sys.modules['hyadex.providers.codex.runtime'], sys.modules['hyadex.core.http'], sys.modules['hyadex.features.emergency'], sys.modules['hyadex.core.turns'], sys.modules['hyadex.core.stop'], sys.modules['hyadex.features.forks'], sys.modules['hyadex.features.group_runtime'], sys.modules['hyadex.features.jobs'], )


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
