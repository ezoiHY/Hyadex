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


class ForkSessionRequest(BaseModel):
    title: str | None = None


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

def event_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in public_job(job).items() if key != "prompt"}


def new_job_revision() -> str:
    """Return a private persisted CAS revision for one scheduled job."""

    return "job_rev_" + uuid.uuid4().hex


def clear_job_runtime_unavailable_deferral(job: dict[str, Any]) -> None:
    job.pop("_runtime_unavailable_occurrence", None)
    job.pop("_runtime_unavailable_defer_count", None)


def clear_job_update_park(job: dict[str, Any]) -> None:
    """Remove private ownership metadata for an idle-update job park."""

    job.pop("_update_park_schedule_id", None)
    job.pop("_update_park_occurrence", None)
    job.pop("_update_park_original_next_run_at", None)
    job.pop("_update_park_revision", None)


def durable_scheduled_job_admissions(
    jobs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Find admitted occurrences that still exactly own persisted schedules.

    ``turn_started`` is the provider-admission boundary. A process can die
    after that fsynced event but before ``mark_ran`` advances the job file.
    Scan only for the exact current private revision and canonical occurrence;
    an edited/replaced schedule must remain untouched.
    """

    expected_by_session: dict[
        str,
        dict[str, tuple[str, float | None, bool]],
    ] = defaultdict(dict)
    for job_id, job in jobs.items():
        session_id = str(job.get("session_id") or "")
        revision = str(job.get("_revision") or "")
        manual_pending = bool(job.get("manual_run_pending"))
        scheduled_at = job.get("scheduled_run_at")
        if scheduled_at is None:
            scheduled_at = job.get("next_run_at")
        occurrence: float | None = None
        try:
            occurrence = float(scheduled_at)
        except (TypeError, ValueError):
            pass
        if occurrence is not None and not math.isfinite(occurrence):
            occurrence = None
        if (
            not session_id
            or not revision.startswith("job_rev_")
            or (occurrence is None and not manual_pending)
        ):
            continue
        expected_by_session[session_id][str(job_id)] = (
            revision,
            occurrence,
            manual_pending,
        )

    admitted: dict[str, dict[str, Any]] = {}
    for session_id in sorted(expected_by_session):
        pending = dict(expected_by_session[session_id])
        for event in reversed_jsonl_events(events_path(session_id)):
            if not pending:
                break
            if (
                event.get("type") != "turn_started"
                or event.get("purpose") != "scheduled_job"
            ):
                continue
            job_id = str(event.get("job_id") or "")
            expected = pending.get(job_id)
            if expected is None:
                continue
            revision, occurrence, manual_pending = expected
            if str(event.get("job_revision") or "") != revision:
                continue
            try:
                event_occurrence = float(event.get("job_scheduled_run_at"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(event_occurrence):
                continue
            event_is_manual = event.get("manual_run") is True
            if event_is_manual:
                if not manual_pending:
                    continue
            elif occurrence is None or event_occurrence != occurrence:
                continue
            admitted[job_id] = event
            pending.pop(job_id, None)
    return admitted


class ScheduledJobRevisionChanged(HTTPException):
    """A scheduled dispatch lost its race with an edit or revocation."""

    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail="scheduled job changed before dispatch; retry the current revision",
        )


def validate_job_text_bounds(title: Any, prompt: Any) -> None:
    """Enforce storage bounds even for internal callers bypassing Pydantic."""

    if len(str(title)) > MAX_JOB_TITLE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"job title must be at most {MAX_JOB_TITLE_CHARS} characters",
        )
    if len(str(prompt)) > MAX_JOB_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"job prompt must be at most {MAX_JOB_PROMPT_CHARS} characters",
        )


def write_jobs_registry(jobs: dict[str, dict[str, Any]]) -> None:
    """Atomically serialize the job registry outside the asyncio event loop."""

    ensure_dirs()
    atomic_json(JOBS_FILE, jobs)


class JobStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._save_sequence = 0
        self._last_saved_sequence = 0
        self.jobs: dict[str, dict[str, Any]] = {}
        self._scheduler_task: asyncio.Task | None = None
        # Durable intent lives on the job itself.  This set only claims the
        # short admission window so an HTTP retry and the scheduler cannot
        # launch the same pending manual run twice in one process.
        self._manual_runs_in_flight: set[str] = set()

    def _retired_provider(self, job: dict[str, Any]) -> bool:
        parent = STORE.sessions.get(str(job.get("session_id") or "")) or {}
        parent_backend = str(parent.get("backend") or DEFAULT_BACKEND).strip().lower()
        backend = str(job.get("backend") or parent_backend).strip().lower()
        return parent_backend not in VALID_BACKENDS or backend not in VALID_BACKENDS

    async def load(self) -> None:
        ensure_dirs()
        changed = False
        if JOBS_FILE.exists():
            try:
                loaded_jobs = json.loads(JOBS_FILE.read_text())
                if not isinstance(loaded_jobs, dict):
                    raise ValueError("jobs registry must be a JSON object")
                self.jobs = loaded_jobs
            except Exception as e:
                # Jobs have no secondary registry. Never turn a transient or
                # torn read into a successful empty load that the next save
                # permanently commits.
                raise RuntimeError(
                    f"refusing to replace unreadable jobs registry: {e}"
                ) from e
        for job in self.jobs.values():
            if not isinstance(job.get("team_references"), list):
                changed = True
            if "manual_run_pending" not in job:
                job["manual_run_pending"] = False
                changed = True
            if not str(job.get("_revision") or "").startswith("job_rev_"):
                job["_revision"] = new_job_revision()
                changed = True
            if job.get("schedule_kind") not in VALID_JOB_SCHEDULE_KINDS:
                job["schedule_kind"] = job_schedule_kind(job)
                changed = True
            if not str(job.get("timezone") or "").strip():
                job["timezone"] = "UTC"
                changed = True
            if "schedule_start_at" not in job:
                # Keep an upgraded interval job's exact next_run_at. This is a
                # recurrence anchor only; load must not reschedule legacy jobs.
                job["schedule_start_at"] = job.get("scheduled_run_at", job.get("next_run_at"))
                changed = True
            if "scheduled_run_at" not in job:
                job["scheduled_run_at"] = job.get("next_run_at") if job.get("enabled") else None
                changed = True
            context_mode_was_explicit = "context_mode" in job
            normalized_context_mode = job_context_mode(job)
            if not context_mode_was_explicit:
                parent_session = STORE.sessions.get(
                    str(job.get("session_id") or "")
                )
                if parent_session:
                    try:
                        normalized_context_mode, _backend = (
                            resolve_job_context_contract(
                                parent_session,
                                normalized_context_mode,
                                job.get("backend"),
                                context_mode_explicit=False,
                                enabled_backends=VALID_BACKENDS,
                                default_backend=DEFAULT_BACKEND,
                            )
                        )
                    except HTTPException:
                        # Do not make server startup fail on a corrupt legacy
                        # backend. Existing runtime validation will surface it
                        # if that job is run or edited.
                        normalized_context_mode = "chat"
            if job.get("context_mode") != normalized_context_mode:
                # Preserve the legacy contract: an omitted/same-provider job
                # continued its parent chat, while an explicitly different
                # provider ran independently.
                job["context_mode"] = normalized_context_mode
                changed = True
            parent_session = STORE.sessions.get(str(job.get("session_id") or ""))
            retired_provider = self._retired_provider(job)
            if retired_provider and job.get("disabled_reason") != "provider_removed":
                job["disabled_reason"] = "provider_removed"
                changed = True
            if (
                (retired_provider or (parent_session and parent_session.get("archived")))
                and (job.get("enabled") or job.get("manual_run_pending"))
            ):
                # Retired providers and archived chats cannot run. Persist the
                # pause before starting the scheduler; do not append recurring
                # failures to the saved chat on every occurrence.
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
                job["manual_run_pending"] = False
                job.pop("manual_run_requested_at", None)
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                clear_job_update_park(job)
                job["updated_at"] = now_iso()
                job["_revision"] = new_job_revision()
                changed = True
        if changed:
            await self.save()

    async def save(self) -> None:
        # Every mutator calls save while holding _lock. Detach every nested
        # value before the first await so a worker never traverses live state
        # after cancellation lets a later mutation begin.
        snapshot = json.loads(json.dumps(self.jobs, ensure_ascii=False))
        self._save_sequence += 1
        save_sequence = self._save_sequence

        async def persist_snapshot() -> None:
            async with self._save_lock:
                # Concurrent direct save calls can be scheduled out of order.
                # Once a newer snapshot exists, an older one must never become
                # the final durable registry.
                if save_sequence < self._save_sequence:
                    return
                await asyncio.to_thread(write_jobs_registry, snapshot)
                self._last_saved_sequence = save_sequence

        save_task = asyncio.create_task(persist_snapshot())
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            # to_thread cannot stop its filesystem write. Join through any
            # repeated cancellation before the caller releases the mutation
            # lock, then restore cancellation to the caller.
            await join_task_despite_caller_cancellation(save_task)
            raise

    async def snapshot(
        self,
        jid: str,
        *,
        expected_session_id: str | None = None,
        ensure_revision: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or (
                expected_session_id is not None
                and job.get("session_id") != expected_session_id
            ):
                raise HTTPException(status_code=404, detail="job not found")
            if ensure_revision and not str(job.get("_revision") or "").startswith(
                "job_rev_"
            ):
                job["_revision"] = new_job_revision()
            return dict(job)

    async def reconcile_admitted_runs_after_restart(self) -> int:
        """Advance crash-admitted occurrences before the scheduler starts."""

        async with self._lock:
            snapshot = {
                job_id: dict(job)
                for job_id, job in self.jobs.items()
            }
        admissions = await asyncio.to_thread(
            durable_scheduled_job_admissions,
            snapshot,
        )
        recovered = 0
        for job_id in sorted(admissions):
            event = admissions[job_id]
            manual = event.get("manual_run") is True
            marked = await self.mark_ran(
                job_id,
                manual=manual,
                expected_revision=str(event.get("job_revision") or ""),
                scheduled_run_at=float(event["job_scheduled_run_at"]),
                require_exact_match=True,
            )
            if marked is not None:
                recovered += 1
        return recovered

    async def assert_dispatch_revision(
        self,
        jid: str,
        session_id: str,
        expected_revision: str,
    ) -> None:
        async with self._lock:
            current = self.jobs.get(jid)
            if (
                not current
                or current.get("session_id") != session_id
                or current.get("_revision") != expected_revision
            ):
                raise ScheduledJobRevisionChanged()

    async def create(
        self,
        req: CreateJobRequest,
        *,
        emit_event: bool = True,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        validate_job_text_bounds(req.title, req.prompt)
        parent_session = STORE.sessions.get(req.session_id)
        if not parent_session:
            raise HTTPException(status_code=404, detail="session not found")
        if parent_session.get("archived"):
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before scheduling jobs",
            )
        context_mode, backend = resolve_job_context_contract(
            parent_session,
            req.context_mode,
            req.backend,
            context_mode_explicit="context_mode" in request_fields_set(req),
            enabled_backends=VALID_BACKENDS,
            default_backend=DEFAULT_BACKEND,
        )
        jid = f"job_{uuid.uuid4().hex[:16]}"
        now = now_iso()
        now_timestamp = time.time()
        schedule_kind = requested_job_schedule_kind(
            req.schedule_kind,
            req.interval_seconds,
            req.cron_expression,
            req.rrule,
        )
        timezone_name = normalize_job_timezone(req.timezone)
        interval_seconds = normalize_interval_seconds(req.interval_seconds) if schedule_kind == "interval" else None
        first_run_at = parse_job_timestamp(req.first_run_at, timezone_name)
        schedule_start_at = first_run_at if first_run_at is not None else now_timestamp
        if schedule_kind == "rrule" and first_run_at is None:
            # Keep DTSTART strictly after the request instant so COUNT includes
            # the first scheduled execution instead of consuming a past anchor.
            schedule_start_at = float(math.ceil(now_timestamp))
        cron_expression = None
        rrule_expression = None
        if schedule_kind == "cron":
            cron_expression = normalize_cron_expression(req.cron_expression, jid)
        elif schedule_kind == "rrule":
            rrule_expression = normalize_rrule_expression(req.rrule, timezone_name, schedule_start_at)

        recurring_calendar = schedule_kind in {"cron", "rrule"}
        loop = True if recurring_calendar else bool(req.loop)
        if recurring_calendar:
            max_runs = max(1, int(req.max_runs)) if req.max_runs is not None else None
        else:
            max_runs = None if loop else max(1, int(req.max_runs or 1))
        if schedule_kind == "interval" and first_run_at is None and interval_seconds:
            schedule_start_at = now_timestamp + interval_seconds
        job = {
            "id": jid,
            "session_id": req.session_id,
            "title": req.title,
            "prompt": req.prompt,
            "schedule_kind": schedule_kind,
            "interval_seconds": interval_seconds,
            "cron_expression": cron_expression,
            "rrule": rrule_expression,
            "timezone": timezone_name,
            "schedule_start_at": schedule_start_at,
            "scheduled_run_at": None,
            "loop": loop,
            "max_runs": max_runs,
            "enabled": req.enabled,
            "backend": backend,
            "context_mode": context_mode,
            "created_at": now,
            "updated_at": now,
            "last_run_at": None,
            "next_run_at": None,
            "run_count": 0,
            "manual_run_pending": False,
            "_revision": new_job_revision(),
        }
        if group_id:
            # Set only by the group jobs route; the job posts into that group.
            job["group_id"] = group_id
        if req.enabled:
            if first_run_at is not None:
                # An explicit first run is exact, even when it is off-rule.
                next_run_at = first_run_at
            elif schedule_kind == "interval":
                next_run_at = schedule_start_at if interval_seconds else None
            else:
                next_run_at = next_job_occurrence(job, now_timestamp, inclusive=False)
            job["next_run_at"] = next_run_at
            job["scheduled_run_at"] = next_run_at
            if next_run_at is None and schedule_kind in {"cron", "rrule"}:
                job["enabled"] = False
        async with self._lock:
            # Recheck inside the job lock. If archive won the race, its durable
            # session state is already visible; if create won, archive waits
            # for this lock and pauses the newly inserted job immediately.
            current_parent = STORE.sessions.get(req.session_id)
            if not current_parent:
                raise HTTPException(status_code=404, detail="session not found")
            if current_parent.get("archived"):
                raise HTTPException(
                    status_code=409,
                    detail="unarchive this chat before scheduling jobs",
                )
            self.jobs[jid] = job
            try:
                await self.save()
            except Exception:
                # A failed create must not leave a runnable in-memory job.
                # Cancellation is different:
                # save() joins its non-cancellable worker before re-raising,
                # so that snapshot is already durable and memory must retain
                # the same job instead of creating a restart split-brain.
                if self.jobs.get(jid) is job:
                    self.jobs.pop(jid, None)
                raise
        if emit_event:
            await append_event(req.session_id, "job_created", {
                "job": event_job(job),
                "job_id": jid,
                "message": f"Scheduled job created: {req.title}",
            })
        return job

    async def update(
        self,
        jid: str,
        patch: dict[str, Any],
        *,
        expected_session_id: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        updated_job: dict[str, Any]
        validate_job_text_bounds(
            patch.get("title", ""),
            patch.get("prompt", ""),
        )
        async with self._lock:
            stored_job = self.jobs.get(jid)
            if not stored_job or (expected_session_id is not None and stored_job.get("session_id") != expected_session_id):
                raise HTTPException(status_code=404, detail="job not found")
            job = dict(stored_job)
            parent_session = STORE.sessions.get(str(job.get("session_id") or ""))
            prospective_job = {**job, **({"backend": patch["backend"]} if patch.get("backend") else {})}
            if self._retired_provider(prospective_job):
                if patch.get("enabled") is True:
                    raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
                patch = {**patch, "enabled": False}
                job["disabled_reason"] = "provider_removed"
            elif job.get("disabled_reason") == "provider_removed":
                job.pop("disabled_reason")
            if parent_session and parent_session.get("archived"):
                if patch.get("enabled") is True:
                    raise HTTPException(
                        status_code=409,
                        detail="unarchive this chat before enabling scheduled jobs",
                    )
                # A metadata edit must not preserve a legacy enabled state for
                # an archived chat.
                patch = dict(patch)
                patch["enabled"] = False
            if "chat_references" in patch or patch.get("prompt") is not None:
                # Saved @Chat targets were removed with cross-chat messaging.
                # A legacy job keeps them, and stays paused at dispatch, until
                # the user clears them or replaces the prompt they annotate.
                job.pop("chat_references", None)
            was_enabled = bool(job.get("enabled"))
            if "backend" in patch and patch["backend"] is not None and patch["backend"] not in VALID_BACKENDS:
                raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
            if (
                "context_mode" in patch
                and patch["context_mode"] is not None
                and patch["context_mode"] not in VALID_JOB_CONTEXT_MODES
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "context_mode must be one of "
                        f"{sorted(VALID_JOB_CONTEXT_MODES)}"
                    ),
                )
            runtime_contract_changed = (
                ("backend" in patch and patch["backend"] is not None)
                or (
                    "context_mode" in patch
                    and patch["context_mode"] is not None
                )
            )
            if runtime_contract_changed:
                parent_session = STORE.sessions.get(
                    str(job.get("session_id") or "")
                )
                if not parent_session:
                    raise HTTPException(status_code=404, detail="session not found")
                context_mode_explicit = (
                    "context_mode" in patch
                    and patch["context_mode"] is not None
                )
                next_context_mode = (
                    patch["context_mode"]
                    if patch.get("context_mode") is not None
                    else job_context_mode(job)
                )
                next_backend = (
                    patch["backend"]
                    if patch.get("backend") is not None
                    else job.get("backend")
                )
                resolved_context_mode, normalized_backend = (
                    resolve_job_context_contract(
                        parent_session,
                        next_context_mode,
                        next_backend,
                        context_mode_explicit=context_mode_explicit,
                        enabled_backends=VALID_BACKENDS,
                        default_backend=DEFAULT_BACKEND,
                    )
                )
                patch = dict(patch)
                patch["context_mode"] = resolved_context_mode
                if "backend" in patch and patch["backend"] is not None:
                    patch["backend"] = normalized_backend
            current_kind = job_schedule_kind(job)
            requested_kind = str(patch.get("schedule_kind") or "").strip().lower()
            legacy_interval_patch = (
                current_kind in {"cron", "rrule"}
                and not requested_kind
                and patch.get("interval_seconds") is not None
                and not str(patch.get("cron_expression") or "").strip()
                and not str(patch.get("rrule") or "").strip()
            )
            if legacy_interval_patch:
                # Contract-v7 clients only understand intervals and include
                # interval/mode fields whenever they save a job. Preserve a
                # v8 calendar schedule unless the caller explicitly selects a
                # new schedule kind.
                patch = dict(patch)
                for key in ("interval_seconds", "loop", "max_runs"):
                    patch.pop(key, None)
            supplied_kinds: list[str] = []
            if patch.get("interval_seconds") is not None:
                supplied_kinds.append("interval")
            if str(patch.get("cron_expression") or "").strip():
                supplied_kinds.append("cron")
            if str(patch.get("rrule") or "").strip():
                supplied_kinds.append("rrule")
            if len(supplied_kinds) > 1:
                raise HTTPException(status_code=400, detail="choose only one schedule expression")
            if requested_kind and requested_kind not in VALID_JOB_SCHEDULE_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"schedule_kind must be one of {sorted(VALID_JOB_SCHEDULE_KINDS)}",
                )
            if requested_kind and supplied_kinds and requested_kind != supplied_kinds[0]:
                raise HTTPException(status_code=400, detail="schedule fields conflict with schedule_kind")
            next_kind = requested_kind or (supplied_kinds[0] if supplied_kinds else current_kind)

            has_next_run_field = "next_run_at" in patch
            has_exact_next_run_patch = has_next_run_field and patch["next_run_at"] is not None
            reset_next_run_to_schedule = has_next_run_field and patch["next_run_at"] is None
            timezone_name = normalize_job_timezone(
                patch.get("timezone") if patch.get("timezone") is not None else job.get("timezone")
            )
            schedule_changed = next_kind != current_kind or timezone_name != normalize_job_timezone(job.get("timezone"))
            if next_kind == "interval" and patch.get("interval_seconds") is not None:
                schedule_changed = schedule_changed or normalize_interval_seconds(patch["interval_seconds"]) != job.get("interval_seconds")
            elif next_kind == "cron" and str(patch.get("cron_expression") or "").strip():
                schedule_changed = schedule_changed or normalize_cron_expression(patch["cron_expression"], jid) != job.get("cron_expression")
            elif next_kind == "rrule" and str(patch.get("rrule") or "").strip():
                comparison_anchor = float(job.get("schedule_start_at") or time.time())
                schedule_changed = schedule_changed or normalize_rrule_expression(
                    patch["rrule"], timezone_name, comparison_anchor
                ) != job.get("rrule")
            next_run_at = (
                parse_job_timestamp(patch.get("next_run_at"), timezone_name)
                if has_exact_next_run_patch
                else None
            )
            for key in ("title", "prompt", "enabled", "backend", "context_mode"):
                if key in patch and patch[key] is not None:
                    job[key] = patch[key]
            job["schedule_kind"] = next_kind
            job["timezone"] = timezone_name

            if next_kind == "interval":
                interval_value = patch.get("interval_seconds") if "interval_seconds" in patch else job.get("interval_seconds")
                job["interval_seconds"] = normalize_interval_seconds(interval_value)
                job["cron_expression"] = None
                job["rrule"] = None
                if "loop" in patch and patch["loop"] is not None:
                    job["loop"] = bool(patch["loop"])
                elif current_kind != "interval":
                    job["loop"] = False
                if "max_runs" in patch:
                    if job.get("loop"):
                        job["max_runs"] = None
                    elif patch["max_runs"] is not None:
                        job["max_runs"] = max(1, int(patch["max_runs"]))
                elif "loop" in patch and job.get("loop"):
                    job["max_runs"] = None
                elif (current_kind != "interval" or "loop" in patch) and not job.get("loop"):
                    job["max_runs"] = max(
                        int(job.get("run_count") or 0) + 1,
                        int(job.get("max_runs") or 1),
                    )
            elif next_kind == "cron":
                expression = patch.get("cron_expression") if "cron_expression" in patch else job.get("cron_expression")
                job["cron_expression"] = normalize_cron_expression(expression, jid)
                job["interval_seconds"] = None
                job["rrule"] = None
                job["loop"] = True
                if "max_runs" in patch:
                    job["max_runs"] = max(1, int(patch["max_runs"])) if patch["max_runs"] is not None else None
            else:
                expression = patch.get("rrule") if "rrule" in patch else job.get("rrule")
                if current_kind != "rrule" or job.get("schedule_start_at") is None:
                    job["schedule_start_at"] = time.time()
                job["rrule"] = normalize_rrule_expression(
                    expression,
                    timezone_name,
                    float(job.get("schedule_start_at") or time.time()),
                )
                job["interval_seconds"] = None
                job["cron_expression"] = None
                job["loop"] = True
                if "max_runs" in patch:
                    job["max_runs"] = max(1, int(patch["max_runs"])) if patch["max_runs"] is not None else None

            if schedule_changed:
                schedule_reference = time.time()
                if next_kind == "interval" and job.get("interval_seconds"):
                    job["schedule_start_at"] = schedule_reference + int(job["interval_seconds"])
                elif next_kind == "rrule":
                    job["schedule_start_at"] = float(math.ceil(schedule_reference))
                else:
                    job["schedule_start_at"] = schedule_reference
            elif job.get("schedule_start_at") is None:
                job["schedule_start_at"] = job.get("scheduled_run_at") or job.get("next_run_at") or time.time()

            if next_kind == "interval" and has_exact_next_run_patch:
                # For interval schedules, the selected next run is the new
                # cadence anchor, just like an explicit first run at creation.
                # Treating it as a one-off makes every later run snap back to
                # the old wall-clock time.
                job["schedule_start_at"] = next_run_at

            max_runs = job.get("max_runs")
            exhausted = max_runs is not None and int(job.get("run_count") or 0) >= int(max_runs)
            if exhausted:
                job["enabled"] = False
            if job.get("enabled") and has_exact_next_run_patch:
                job["next_run_at"] = next_run_at
                job["scheduled_run_at"] = next_run_at
            elif job.get("enabled") and (
                reset_next_run_to_schedule
                or schedule_changed
                or patch.get("enabled") is True
                or not job.get("next_run_at")
            ):
                reference = time.time()
                if (
                    next_kind == "interval"
                    and job.get("interval_seconds")
                    and patch.get("enabled") is True
                    and not was_enabled
                ):
                    job["schedule_start_at"] = reference + int(job["interval_seconds"])
                next_occurrence = next_job_occurrence(job, reference, inclusive=False)
                job["next_run_at"] = next_occurrence
                job["scheduled_run_at"] = next_occurrence
                if next_occurrence is None:
                    job["enabled"] = False
            if not job.get("enabled"):
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
            # Any explicit edit owns a fresh revision and resets the bounded
            # unavailable-runtime budget. A stale scheduler snapshot is then
            # unable to consume or advance the edited occurrence.
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            self.jobs[jid] = job
            try:
                await self.save()
            except Exception:
                self.jobs[jid] = stored_job
                raise
            updated_job = dict(job)
        if emit_event:
            await append_event(str(updated_job.get("session_id") or ""), "job_updated", {
                "job": event_job(updated_job),
                "job_id": jid,
                "message": f"Scheduled job updated: {updated_job.get('title') or jid}",
            })
        return updated_job

    async def delete(self, jid: str, *, expected_session_id: str | None = None) -> bool:
        deleted_job: dict[str, Any] | None = None
        async with self._lock:
            current = self.jobs.get(jid)
            if current and (expected_session_id is None or current.get("session_id") == expected_session_id):
                deleted_job = self.jobs.pop(jid)
            elif expected_session_id is not None:
                raise HTTPException(status_code=404, detail="job not found")
            try:
                await self.save()
            except Exception:
                # An ordinary persistence failure did not durably commit the
                # removal, so keep the exact previously-owned object in
                # memory. Cancellation is intentionally different: save()
                # joins its shielded writer before propagating cancellation,
                # which means the deletion has committed and must stay gone.
                if deleted_job is not None:
                    self.jobs[jid] = deleted_job
                raise
        if deleted_job:
            await append_event(str(deleted_job.get("session_id") or ""), "job_deleted", {
                "job": event_job(deleted_job),
                "job_id": jid,
                "message": f"Scheduled job deleted: {deleted_job.get('title') or jid}",
            })
        return deleted_job is not None

    async def delete_for_session(self, session_id: str) -> int:
        async with self._lock:
            doomed = {
                jid: job
                for jid, job in self.jobs.items()
                if job.get("session_id") == session_id
            }
            for jid in doomed:
                self.jobs.pop(jid, None)
            try:
                await self.save()
            except Exception:
                # Keep failed cleanup retryable. The session tombstone prevents
                # these jobs from running while a later delete/scheduler pass
                # retries the durable removal.
                self.jobs.update(doomed)
                raise
            return len(doomed)

    async def pause_for_session(
        self,
        session_id: str,
        *,
        persist_unchanged: bool = False,
    ) -> int:
        paused_jobs: list[dict[str, Any]] = []
        async with self._lock:
            updated_at = now_iso()
            for job in self.jobs.values():
                if job.get("session_id") != session_id:
                    continue
                enabled = bool(job.get("enabled"))
                manual_pending = bool(job.get("manual_run_pending"))
                if not enabled and not manual_pending:
                    continue
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
                clear_job_runtime_unavailable_deferral(job)
                clear_job_update_park(job)
                if manual_pending:
                    job["manual_run_pending"] = False
                    job.pop("manual_run_requested_at", None)
                    job.pop("manual_run_defer_reason", None)
                    job.pop("_last_manual_defer_event_at", None)
                job["updated_at"] = updated_at
                job["_revision"] = new_job_revision()
                paused_jobs.append(dict(job))
            if not paused_jobs and not persist_unchanged:
                return 0
            # Deliberately keep the in-memory jobs paused if persistence fails.
            # The live scheduler must fail safe, and load() will retry the
            # durable reconciliation from the archived session on restart.
            await self.save()

        for job in paused_jobs:
            try:
                await append_event(session_id, "job_updated", {
                    "job": event_job(job),
                    "job_id": job.get("id"),
                    "message": (
                        "Scheduled job paused because chat was archived: "
                        f"{job.get('title') or job.get('id')}"
                    ),
                })
            except Exception as exc:
                # The pause is already durable. A timeline/audit projection
                # failure must never reactivate a schedule.
                logger.warning(
                    "could not append archived job pause event session=%s job=%s: %s",
                    session_id,
                    job.get("id"),
                    concise_error_message(exc),
                )
        return len(paused_jobs)

    async def pause_for_chat_reference_repair(
        self,
        jid: str,
        error: ScheduledJobChatReferenceRepairRequired,
        *,
        expected_revision: str,
    ) -> bool:
        paused_job: dict[str, Any] | None = None
        detail = str(error.detail or "saved chat target is unavailable").strip()
        async with self._lock:
            job = self.jobs.get(jid)
            if (
                job is None
                or job.get("_revision") != expected_revision
            ):
                return False
            job["enabled"] = False
            job["next_run_at"] = None
            job["scheduled_run_at"] = None
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["manual_run_pending"] = False
            job.pop("manual_run_requested_at", None)
            job.pop("manual_run_defer_reason", None)
            job.pop("_last_manual_defer_event_at", None)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            paused_job = dict(job)
            # Keep the in-memory schedule paused if persistence fails. It is
            # safer to require repair in this process than to keep dispatching
            # a known-invalid durable authority.
            await self.save()

        message = (
            f"Scheduled job paused: {paused_job.get('title') or jid} — "
            f"its saved @ chat target is invalid ({detail}). "
            "Edit or remove the target, then re-enable the job."
        )
        try:
            await append_event(
                str(paused_job.get("session_id") or ""),
                "job_error",
                {
                    "job": {
                        key: value
                        for key, value in provider_public_job(paused_job).items()
                        if key != "prompt"
                    },
                    "job_id": jid,
                    "error_code": "scheduled_chat_reference_invalid",
                    "repair_required": True,
                    "message": message,
                },
            )
        except Exception as exc:
            # The durable pause is authoritative even if its timeline
            # projection fails. Never reactivate or retry the invalid grant.
            logger.warning(
                "could not append scheduled chat-reference pause event job=%s: %s",
                jid,
                concise_error_message(exc),
            )
        return True

    async def mark_ran(
        self,
        jid: str,
        *,
        manual: bool = False,
        expected_revision: str | None = None,
        scheduled_run_at: float | None = None,
        require_exact_match: bool = False,
    ) -> dict[str, Any] | None:
        """Record an admitted run without advancing a replacement schedule.

        A user edit can win after ``turn_started`` is durable but before this
        bookkeeping acquires the job lock. Count that legitimate run, while
        advancing recurrence only when the stored job still points at the exact
        occurrence that was admitted. A manual run remains additive when a
        future scheduled occurrence is already pending.
        """
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                return None
            completed_at = time.time()
            admitted_scheduled_at = float(
                scheduled_run_at
                if scheduled_run_at is not None
                else job.get("scheduled_run_at")
                or job.get("next_run_at")
                or completed_at
            )
            current_scheduled_at = job.get("scheduled_run_at")
            if current_scheduled_at is None:
                current_scheduled_at = job.get("next_run_at")
            same_revision = (
                expected_revision is None
                or job.get("_revision") == expected_revision
            )
            same_occurrence = False
            if current_scheduled_at is not None:
                with suppress(TypeError, ValueError):
                    same_occurrence = (
                        float(current_scheduled_at) == admitted_scheduled_at
                    )
            if require_exact_match and (
                not same_revision
                or (manual and not job.get("manual_run_pending"))
                or (not manual and not same_occurrence)
            ):
                return None
            advance_admitted_occurrence = same_revision or same_occurrence
            job["last_run_at"] = now_iso()
            if manual:
                job["last_manual_run_at"] = job["last_run_at"]
                job["manual_run_pending"] = False
                job.pop("manual_run_requested_at", None)
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                job.pop("last_manual_run_error", None)
                job.pop("last_manual_run_error_at", None)
            else:
                job["last_scheduled_run_at"] = admitted_scheduled_at
            job["run_count"] = int(job.get("run_count") or 0) + 1
            run_count = int(job.get("run_count") or 0)
            max_runs = job.get("max_runs")
            limit_reached = max_runs is not None and run_count >= int(max_runs)
            recurring = (
                job_schedule_kind(job) in {"cron", "rrule"}
                or bool(job.get("loop"))
                or (job.get("interval_seconds") is not None and max_runs is not None and run_count < int(max_runs))
            )
            # A manual run requested before the next occurrence is additive:
            # it must not consume or move that future scheduled occurrence.
            preserve_future_schedule = False
            if (
                manual
                and bool(job.get("enabled"))
                and current_scheduled_at is not None
                and recurring
                and not limit_reached
            ):
                with suppress(TypeError, ValueError):
                    preserve_future_schedule = (
                        float(current_scheduled_at) > completed_at
                    )
            if preserve_future_schedule:
                pass
            elif (
                advance_admitted_occurrence
                and job.get("enabled")
                and recurring
                and not limit_reached
            ):
                next_occurrence = next_job_occurrence(
                    job,
                    max(admitted_scheduled_at, completed_at),
                    inclusive=False,
                )
                job["next_run_at"] = next_occurrence
                job["scheduled_run_at"] = next_occurrence
                if next_occurrence is None:
                    job["enabled"] = False
            elif limit_reached or (
                advance_admitted_occurrence
                and (not job.get("enabled") or not recurring)
            ):
                job["enabled"] = False
                job["next_run_at"] = None
                job["scheduled_run_at"] = None
            clear_job_runtime_unavailable_deferral(job)
            clear_job_update_park(job)
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            await self.save()
            return dict(job)

    async def defer(
        self,
        jid: str,
        reason: str,
        delay_seconds: int | None = None,
        *,
        expected_revision: str | None = None,
        expected_next_run_at: float | None = None,
    ) -> bool:
        delay = int(delay_seconds or JOB_BUSY_RETRY_SECONDS)
        emit_event = False
        event_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or not job.get("enabled"):
                return False
            if (
                expected_revision is not None
                and job.get("_revision") != expected_revision
            ):
                return False
            if expected_next_run_at is not None:
                try:
                    same_occurrence = (
                        float(job.get("next_run_at"))
                        == float(expected_next_run_at)
                    )
                except (TypeError, ValueError):
                    same_occurrence = False
                if not same_occurrence:
                    return False
            now = time.time()
            clear_job_update_park(job)
            job["next_run_at"] = now + max(delay, 5)
            job["last_deferred_at"] = now_iso()
            job["last_defer_reason"] = reason
            last_emit = float(job.get("_last_defer_event_at") or 0)
            if JOB_DEFER_EVENT_MIN_SECONDS <= 0 or now - last_emit >= JOB_DEFER_EVENT_MIN_SECONDS:
                job["_last_defer_event_at"] = now
                emit_event = True
                event_job = public_job(job)
            job["updated_at"] = now_iso()
            revision = new_job_revision()
            job["_revision"] = revision
            await self.save()
        if emit_event and event_job and event_job.get("session_id"):
            await append_event(str(event_job["session_id"]), "job_deferred", {
                "job": event_job,
                "job_id": jid,
                "message": f"Scheduled job deferred: {event_job.get('title') or jid} — {reason}",
            })
        return True


    async def resume_legacy_update_parked(self) -> int:
        """Rearm unchanged occurrences parked by an ended idle reservation."""

        resumed = 0
        changed = False
        async with self._lock:
            now = time.time()
            updated_at = now_iso()
            for job in self.jobs.values():
                parked_schedule_id = str(
                    job.get("_update_park_schedule_id") or ""
                ).strip()
                if not parked_schedule_id:
                    continue
                changed = True
                parked_revision = str(
                    job.get("_update_park_revision") or ""
                )
                try:
                    parked_occurrence = float(
                        job.get("_update_park_occurrence")
                    )
                    current_occurrence = float(job.get("scheduled_run_at"))
                    original_next_run_at = float(
                        job.get("_update_park_original_next_run_at")
                    )
                except (TypeError, ValueError):
                    exact_park = False
                else:
                    exact_park = (
                        math.isfinite(parked_occurrence)
                        and math.isfinite(current_occurrence)
                        and math.isfinite(original_next_run_at)
                        and job.get("_revision") == parked_revision
                        and bool(job.get("enabled"))
                        and current_occurrence == parked_occurrence
                    )
                clear_job_update_park(job)
                if exact_park:
                    # Preserve the canonical occurrence, but make its retry
                    # immediately due. A future edit has already failed the
                    # revision/occurrence CAS above and remains authoritative.
                    job["next_run_at"] = min(original_next_run_at, now)
                    resumed += 1
                job["updated_at"] = updated_at
                job["_revision"] = new_job_revision()
            if changed:
                await self.save()
        return resumed

    async def defer_runtime_unavailable(
        self,
        jid: str,
        reason: str,
        *,
        expected_revision: str,
        expected_next_run_at: float,
    ) -> Literal["deferred", "exhausted", "stale"]:
        """Durably retry one unavailable-runtime occurrence under a hard cap."""

        event_snapshot: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if (
                job is None
                or not job.get("enabled")
                or job.get("_revision") != expected_revision
            ):
                return "stale"
            try:
                same_due_time = (
                    float(job.get("next_run_at"))
                    == float(expected_next_run_at)
                )
            except (TypeError, ValueError):
                same_due_time = False
            if not same_due_time:
                return "stale"
            try:
                occurrence = float(
                    job.get("scheduled_run_at")
                    if job.get("scheduled_run_at") is not None
                    else expected_next_run_at
                )
            except (TypeError, ValueError):
                return "stale"
            try:
                stored_occurrence = float(
                    job.get("_runtime_unavailable_occurrence")
                )
            except (TypeError, ValueError):
                stored_occurrence = math.nan
            try:
                defer_count = int(job.get("_runtime_unavailable_defer_count") or 0)
            except (TypeError, ValueError):
                defer_count = 0
            if stored_occurrence != occurrence:
                defer_count = 0
            defer_count = max(0, min(defer_count, JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS))
            if defer_count >= JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS:
                return "exhausted"

            now = time.time()
            clear_job_update_park(job)
            job["_runtime_unavailable_occurrence"] = occurrence
            job["_runtime_unavailable_defer_count"] = defer_count + 1
            job["next_run_at"] = now + max(JOB_BUSY_RETRY_SECONDS, 5)
            job["last_deferred_at"] = now_iso()
            job["last_defer_reason"] = reason
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            event_snapshot = event_job(job)
            await self.save()

        if event_snapshot and event_snapshot.get("session_id"):
            await append_event(
                str(event_snapshot["session_id"]),
                "job_deferred",
                {
                    "job": event_snapshot,
                    "job_id": jid,
                    "message": (
                        "Scheduled job deferred: "
                        f"{event_snapshot.get('title') or jid} — {reason}"
                    ),
                },
            )
        return "deferred"

    def _manual_run_deferred_result(
        self,
        jid: str,
        reason: str,
    ) -> dict[str, Any]:
        job = self.jobs.get(jid) or {}
        title = str(job.get("title") or jid)
        clean_reason = str(reason or "the chat is temporarily unavailable")
        return {
            "ok": True,
            "job_id": jid,
            "job": public_job(job),
            "run_id": None,
            "queued": True,
            "deferred": True,
            "manual_run_pending": True,
            "message": (
                f"Run now is queued for {title}: {clean_reason}. "
                "It will start automatically when the chat is available."
            ),
        }

    async def _record_manual_run_deferred(
        self,
        jid: str,
        reason: str,
    ) -> dict[str, Any]:
        deferred_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            if job.get("manual_run_pending"):
                clean_reason = str(reason or "the chat is temporarily unavailable")
                if job.get("manual_run_defer_reason") != clean_reason:
                    job["manual_run_defer_reason"] = clean_reason
                    job["updated_at"] = now_iso()
                    now = time.time()
                    last_emit = float(job.get("_last_manual_defer_event_at") or 0)
                    if (
                        JOB_DEFER_EVENT_MIN_SECONDS <= 0
                        or now - last_emit >= JOB_DEFER_EVENT_MIN_SECONDS
                    ):
                        job["_last_manual_defer_event_at"] = now
                        deferred_job = event_job(job)
                    job["_revision"] = new_job_revision()
                    await self.save()
        if deferred_job and deferred_job.get("session_id"):
            try:
                await append_event(str(deferred_job["session_id"]), "job_deferred", {
                    "job": deferred_job,
                    "job_id": jid,
                    "message": (
                        f"Manual run deferred: {deferred_job.get('title') or jid} "
                        f"— {reason}"
                    ),
                })
            except Exception as event_error:
                logger.warning(
                    "could not append manual job deferred event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )
        return self._manual_run_deferred_result(jid, reason)

    async def _clear_failed_manual_run(
        self,
        jid: str,
        error: Exception,
    ) -> None:
        failed_job: dict[str, Any] | None = None
        error_message = concise_error_message(error)
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or not job.get("manual_run_pending"):
                return
            job["manual_run_pending"] = False
            job.pop("manual_run_requested_at", None)
            job.pop("manual_run_defer_reason", None)
            job.pop("_last_manual_defer_event_at", None)
            job["last_manual_run_error"] = error_message
            job["last_manual_run_error_at"] = now_iso()
            job["updated_at"] = now_iso()
            job["_revision"] = new_job_revision()
            failed_job = event_job(job)
            await self.save()
        if failed_job and failed_job.get("session_id"):
            try:
                await append_event(str(failed_job["session_id"]), "job_error", {
                    "job": failed_job,
                    "job_id": jid,
                    "message": (
                        f"Manual run failed: {failed_job.get('title') or jid} "
                        f"— {error_message}"
                    ),
                })
            except Exception as event_error:
                logger.warning(
                    "could not append manual job error event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )

    async def _start_job_run(
        self,
        jid: str,
        *,
        manual: bool,
    ) -> dict[str, Any]:
        # Updates replace the stored dict under the job lock.  Hold one
        # immutable snapshot so prompt text and its structured grants cannot
        # come from different job revisions.
        job = await self.snapshot(jid, ensure_revision=True)
        if self._retired_provider(job):
            raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
        job_revision = str(job.get("_revision") or "")
        if not job_revision:
            raise ScheduledJobRevisionChanged()
        session_id = str(job.get("session_id") or "")
        parent_session = STORE.sessions.get(session_id)
        if parent_session and parent_session.get("archived"):
            await self.pause_for_session(session_id)
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before running scheduled jobs",
            )
        scheduled_run_at = (
            time.time()
            if manual
            else float(
                job.get("scheduled_run_at")
                or job.get("next_run_at")
                or time.time()
            )
        )
        if job.get("team_references") or job.get("chat_references"):
            # Saved Team and @Chat targets were retired. Running the prompt
            # without them would silently drop part of the job's routing.
            error = ScheduledJobChatReferenceRepairRequired(
                status_code=409,
                detail=(
                    "saved Team targets are no longer supported"
                    if job.get("team_references")
                    else "saved @Chat targets are no longer supported"
                ),
            )
            await self.pause_for_chat_reference_repair(jid, error, expected_revision=job_revision)
            raise error
        group_id = str(job.get("group_id") or "")
        if group_id:
            result = await start_group_job_run(group_id, job, jid)
            marked_job = await self.mark_ran(
                jid,
                manual=manual,
                expected_revision=job_revision,
                scheduled_run_at=scheduled_run_at,
            )
            ran_job = public_job(marked_job if isinstance(marked_job, dict) else job)
            await append_event(session_id, "job_ran", {
                "job": ran_job,
                "job_id": jid,
                "run_id": None,
                "group_id": group_id,
                "group_message_seq": result["group_message_seq"],
                "job_scheduled_run_at": scheduled_run_at,
                "manual_run": manual,
                "context_mode": "group",
                "message": (
                    f"Scheduled group job ran{' manually' if manual else ''}: "
                    f"{ran_job.get('title') or jid}"
                ),
            })
            return result
        req = TurnRequest(
            prompt=job["prompt"],
            file_ids=[],
            backend=job.get("backend"),
            purpose="scheduled_job",
            job_id=jid,
            job_title=str(job.get("title") or jid),
            job_scheduled_run_at=scheduled_run_at,
        )
        context_mode = job_context_mode(job)
        result = await start_turn(
            session_id,
            req,
            queue_if_busy=False,
            provider_context_mode=context_mode,
            scheduled_job_revision=job_revision,
            scheduled_job_manual_run=manual,
        )
        marked_job = await self.mark_ran(
            jid,
            manual=manual,
            expected_revision=job_revision,
            scheduled_run_at=scheduled_run_at,
        )
        # Deleting a job after its turn was admitted prevents recurrence but
        # cannot undo that run.  Keep its timeline marker without indexing the
        # now-absent mutable store entry.
        ran_job = public_job(marked_job if isinstance(marked_job, dict) else job)
        await append_event(session_id, "job_ran", {
            "job": ran_job,
            "job_id": jid,
            "run_id": result["run_id"],
            "job_scheduled_run_at": scheduled_run_at,
            "manual_run": manual,
            "context_mode": context_mode,
            "message": (
                f"Scheduled job ran{' manually' if manual else ''}: "
                f"{ran_job.get('title') or jid}"
            ),
        })
        return result

    async def run_job(self, jid: str) -> dict[str, Any]:
        return await self._start_job_run(jid, manual=False)

    async def _dispatch_pending_manual_run(
        self,
        jid: str,
        *,
        blocker_checked: bool = False,
    ) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job.get("manual_run_pending"):
            return {
                "ok": True,
                "job_id": jid,
                "job": public_job(job),
                "run_id": None,
                "queued": False,
                "deferred": False,
                "manual_run_pending": False,
                "message": "The pending manual run has already been admitted.",
            }
        if jid in self._manual_runs_in_flight:
            return self._manual_run_deferred_result(
                jid,
                "manual run admission is already in progress",
            )

        # No await occurs between the membership check and add, so this is an
        # atomic claim within the event loop. Persistence is the durable layer;
        # this claim only coalesces concurrent dispatch attempts.
        self._manual_runs_in_flight.add(jid)
        try:
            job = self.jobs.get(jid)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            session_id = str(job.get("session_id") or "")
            parent_session = STORE.sessions.get(session_id)
            if parent_session and parent_session.get("archived"):
                await self.pause_for_session(session_id)
                raise HTTPException(
                    status_code=409,
                    detail="unarchive this chat before running scheduled jobs",
                )

            try:
                if not blocker_checked:
                    blocker = await scheduled_job_blocker(
                        session_id,
                        manual=True,
                        **({"group": True} if job.get("group_id") else {}),
                    )
                    if blocker:
                        return await self._record_manual_run_deferred(jid, blocker)
                result = await self._start_job_run(jid, manual=True)
            except Exception as exc:
                if isinstance(exc, ScheduledJobChatReferenceRepairRequired):
                    raise
                parent_session = STORE.sessions.get(session_id)
                archived_rejection = (
                    isinstance(exc, HTTPException)
                    and exc.status_code == 409
                    and "archiv" in str(exc.detail).lower()
                )
                if (
                    (parent_session and parent_session.get("archived"))
                    or archived_rejection
                ):
                    await self.pause_for_session(session_id)
                    raise
                if (
                    isinstance(exc, HTTPException)
                    and exc.status_code in {409, 423, 429, 503}
                ):
                    return await self._record_manual_run_deferred(
                        jid,
                        str(exc.detail or "the chat is temporarily unavailable"),
                    )
                await self._clear_failed_manual_run(jid, exc)
                raise

            return {
                **result,
                "ok": True,
                "job_id": jid,
                "job": public_job(self.jobs[jid]),
                "queued": False,
                "deferred": False,
                "manual_run_pending": False,
                "message": f"Run now started for {job.get('title') or jid}.",
            }
        finally:
            self._manual_runs_in_flight.discard(jid)

    async def request_manual_run(self, jid: str, *, expected_session_id: str | None = None) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job or (expected_session_id is not None and job.get("session_id") != expected_session_id):
            raise HTTPException(status_code=404, detail="job not found")
        if self._retired_provider(job):
            raise HTTPException(410, "This job's provider was removed; create a job in a supported chat.")
        session_id = str(job.get("session_id") or "")
        parent_session = STORE.sessions.get(session_id)
        if parent_session and parent_session.get("archived"):
            await self.pause_for_session(session_id)
            raise HTTPException(
                status_code=409,
                detail="unarchive this chat before running scheduled jobs",
            )

        pending_event_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job or (expected_session_id is not None and job.get("session_id") != expected_session_id):
                raise HTTPException(status_code=404, detail="job not found")
            if not job.get("manual_run_pending"):
                job["manual_run_pending"] = True
                job["manual_run_requested_at"] = now_iso()
                job.pop("manual_run_defer_reason", None)
                job.pop("_last_manual_defer_event_at", None)
                job.pop("last_manual_run_error", None)
                job.pop("last_manual_run_error_at", None)
                job["updated_at"] = now_iso()
                job["_revision"] = new_job_revision()
                pending_event_job = event_job(job)
                await self.save()

        if pending_event_job and pending_event_job.get("session_id"):
            try:
                await append_event(
                    str(pending_event_job["session_id"]),
                    "job_updated",
                    {
                        "job": pending_event_job,
                        "job_id": jid,
                        "message": (
                            "Manual run requested: "
                            f"{pending_event_job.get('title') or jid}"
                        ),
                    },
                )
            except Exception as event_error:
                logger.warning(
                    "could not append manual job pending event job=%s: %s",
                    jid,
                    concise_error_message(event_error),
                )

        return await self._dispatch_pending_manual_run(jid)

    def start_scheduler(self) -> None:
        if (
            not SERVER.shutting_down
            and (self._scheduler_task is None or self._scheduler_task.done())
        ):
            self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    async def scheduler_loop(self) -> None:
        """Keep scheduling after an individual iteration escapes unexpectedly."""

        while True:
            try:
                await self._scheduler_loop_until_failure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "job scheduler iteration failed; continuing error=%s",
                    concise_error_message(exc),
                )

    async def _scheduler_loop_until_failure(self) -> None:
        logger.info("job scheduler started")
        while True:
            await asyncio.sleep(max(JOB_SCHEDULER_INTERVAL_SECONDS, 1.0))
            now = time.time()
            due = [
                (
                    job["id"],
                    job.get("_revision"),
                    (
                        float(job["next_run_at"])
                        if (
                            not job.get("manual_run_pending")
                            and job.get("enabled")
                            and job.get("next_run_at")
                        )
                        else None
                    ),
                )
                for job in list(self.jobs.values())
                if job.get("manual_run_pending")
                or (
                    job.get("enabled")
                    and job.get("next_run_at")
                    and float(job["next_run_at"]) <= now
                )
            ]
            for jid, due_revision, due_next_run_at in due:
                job = self.jobs.get(jid)
                if not job:
                    continue
                manual_run_pending = bool(job.get("manual_run_pending"))
                job_session_id = str(job.get("session_id") or "")
                if (
                    not job_session_id
                    or job_session_id not in STORE.sessions
                    or job_session_id in DELETING_SESSIONS
                    or job_session_id in DELETED_SESSION_TOMBSTONES
                ):
                    try:
                        await self.delete_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "scheduled job cleanup failed session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                if STORE.sessions[job_session_id].get("archived"):
                    try:
                        await self.pause_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "could not persist archived job pause session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                blocker = await scheduled_job_blocker(
                    job_session_id,
                    manual=manual_run_pending,
                    **({"group": True} if job.get("group_id") else {}),
                )
                if blocker:
                    if manual_run_pending:
                        await self._record_manual_run_deferred(jid, blocker)
                    else:
                        await self.defer(
                            jid,
                            blocker,
                            JOB_BUSY_RETRY_SECONDS,
                            expected_revision=due_revision,
                            expected_next_run_at=due_next_run_at,
                        )
                    continue
                # The blocker check yields. Archive/pause or a user edit may
                # have changed this due item while it was in flight; never
                # dispatch from the stale due-list snapshot.
                job = self.jobs.get(jid)
                if not job:
                    continue
                if job.get("_revision") != due_revision:
                    # Even when an edit leaves its replacement occurrence due,
                    # dispatch it only from a fresh scheduler snapshot. Error
                    # handling below is guarded by the dispatched revision;
                    # launching a newer revision from this stale iteration
                    # would make a permanent failure look stale and retry it.
                    continue
                manual_run_pending = bool(job.get("manual_run_pending"))
                scheduled_run_due = bool(
                    job.get("enabled")
                    and job.get("next_run_at")
                    and float(job["next_run_at"]) <= time.time()
                )
                if (
                    not manual_run_pending
                    and due_next_run_at is not None
                    and float(job.get("next_run_at") or 0) != due_next_run_at
                ):
                    continue
                if not manual_run_pending and not scheduled_run_due:
                    continue
                # ``scheduled_job_blocker`` awaits several locks. Session
                # deletion can commit during that yield before its jobs are
                # removed; revalidate the exact parent before dereferencing it.
                parent_session = STORE.sessions.get(job_session_id)
                if (
                    parent_session is None
                    or job_session_id in DELETING_SESSIONS
                    or job_session_id in DELETED_SESSION_TOMBSTONES
                ):
                    try:
                        await self.delete_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "scheduled job cleanup failed session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                if parent_session.get("archived"):
                    try:
                        await self.pause_for_session(job_session_id)
                    except Exception as exc:
                        logger.warning(
                            "could not persist archived job pause session=%s: %s",
                            job_session_id,
                            concise_error_message(exc),
                        )
                    continue
                try:
                    if manual_run_pending:
                        await self._dispatch_pending_manual_run(
                            jid,
                            blocker_checked=True,
                        )
                    else:
                        await self.run_job(jid)
                except Exception as e:
                    if isinstance(e, ScheduledJobRevisionChanged):
                        # An edit/revoke won before admission. The current job
                        # revision already owns its next-run state; never emit
                        # an error or reschedule from the stale due snapshot.
                        continue
                    if isinstance(
                        e,
                        ScheduledJobChatReferenceRepairRequired,
                    ):
                        logger.warning(
                            "scheduled job %s paused for chat-reference repair: %s",
                            jid,
                            str(e.detail or "invalid saved target"),
                        )
                        # run_job already committed the pause and its one clear
                        # repair event. Do not append a duplicate generic error
                        # or compute another recurrence from the stale due item.
                        continue
                    parent_session = STORE.sessions.get(job_session_id)
                    archived_rejection = (
                        isinstance(e, HTTPException)
                        and e.status_code == 409
                        and "archiv" in str(e.detail).lower()
                    )
                    if (
                        (parent_session and parent_session.get("archived"))
                        or archived_rejection
                    ):
                        try:
                            await self.pause_for_session(job_session_id)
                        except Exception as exc:
                            logger.warning(
                                "could not persist archived job pause session=%s: %s",
                                job_session_id,
                                concise_error_message(exc),
                            )
                        continue
                    runtime_unavailable_reason = (
                        scheduled_job_runtime_unavailable_reason(e)
                        if not manual_run_pending
                        else None
                    )
                    if (
                        runtime_unavailable_reason
                        and due_next_run_at is not None
                    ):
                        runtime_defer_result = await self.defer_runtime_unavailable(
                            jid,
                            runtime_unavailable_reason,
                            expected_revision=str(due_revision or ""),
                            expected_next_run_at=due_next_run_at,
                        )
                        if runtime_defer_result in {"deferred", "stale"}:
                            continue
                        # Exhaustion deliberately falls through to the normal
                        # terminal error/recurrence path for this occurrence.
                    lifecycle_defer_reason = (
                        scheduled_job_lifecycle_admission_defer_reason(e)
                    )
                    if lifecycle_defer_reason:
                        if manual_run_pending:
                            await self._record_manual_run_deferred(
                                jid,
                                lifecycle_defer_reason,
                            )
                        else:
                            # Preserve the canonical occurrence and use the due
                            # snapshot as a CAS guard. If a user edited/replaced
                            # the schedule while dispatch was in flight, that
                            # revision owns timing and defer cannot overwrite it.
                            await self.defer(
                                jid,
                                lifecycle_defer_reason,
                                JOB_BUSY_RETRY_SECONDS,
                                expected_revision=due_revision,
                                expected_next_run_at=due_next_run_at,
                            )
                        continue
                    if manual_run_pending:
                        logger.warning(
                            "pending manual job %s failed permanently: %s",
                            jid,
                            e,
                        )
                        await self._clear_failed_manual_run(jid, e)
                        continue
                    logger.warning("scheduled job %s failed: %s", jid, e)
                    async with self._lock:
                        current = self.jobs.get(jid)
                        if (
                            current is not job
                            or current.get("_revision") != due_revision
                        ):
                            # A user edit/revoke won while provider dispatch
                            # was in flight. Its replacement revision owns all
                            # timing; never serialize or emit an error from the
                            # stale due snapshot.
                            continue
                        failed_at = time.time()
                        scheduled_at = float(
                            job.get("scheduled_run_at")
                            or job.get("next_run_at")
                            or failed_at
                        )
                        run_count = int(job.get("run_count") or 0)
                        max_runs = job.get("max_runs")
                        limit_reached = (
                            max_runs is not None
                            and run_count >= int(max_runs)
                        )
                        recurring = (
                            job_schedule_kind(job) in {"cron", "rrule"}
                            or bool(job.get("loop"))
                            or (
                                job.get("interval_seconds") is not None
                                and max_runs is not None
                                and run_count < int(max_runs)
                            )
                        )
                        if (
                            job.get("enabled")
                            and recurring
                            and not limit_reached
                        ):
                            next_occurrence = next_job_occurrence(
                                job,
                                max(scheduled_at, failed_at),
                                inclusive=False,
                            )
                            job["next_run_at"] = next_occurrence
                            job["scheduled_run_at"] = next_occurrence
                            if next_occurrence is None:
                                job["enabled"] = False
                        else:
                            job["enabled"] = False
                            job["next_run_at"] = None
                            job["scheduled_run_at"] = None
                        clear_job_runtime_unavailable_deferral(job)
                        clear_job_update_park(job)
                        await self.save()
                        failed_job = dict(job)
                    if failed_job.get("session_id"):
                        await append_event(
                            failed_job["session_id"],
                            "job_error",
                            {
                                "job": public_job(failed_job),
                                "job_id": jid,
                                "message": (
                                    "Scheduled job failed: "
                                    f"{failed_job.get('title') or jid} — {e}"
                                ),
                            },
                        )

    async def stop_scheduler(self) -> None:
        """Cancel and join the scheduler before provider shutdown begins."""

        task = self._scheduler_task
        self._scheduler_task = None
        if task is None or task is asyncio.current_task() or task.done():
            if task is not None and not task.cancelled():
                with suppress(BaseException):
                    task.result()
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


JOBS = JobStore()
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


async def run_queued_turn_now(session_id: str, queued_id: str) -> dict[str, Any]:
    """Coalesce duplicate Force Send requests without replaying a steer."""
    await wait_for_queue_recovery_admission(session_id)
    ensure_session_not_deleting(session_id)
    await reconcile_idle_queue_session(
        session_id,
        schedule=False,
        reason="force_send_admission",
    )
    cached: dict[str, Any] | None = None
    async with RUNS.run_now_request_lock:
        # Delete reserves the tombstone before acquiring this same lock. A
        # request that was waiting behind deletion must not register a new
        # control task after deletion captured the previous owner.
        ensure_session_not_deleting(session_id)
        now = time.monotonic()
        for key, outcome in list(RUN_NOW_COMPLETED_RESULTS.items()):
            if float(outcome.get("expires_at") or 0) <= now:
                RUN_NOW_COMPLETED_RESULTS.pop(key, None)
        cache_key = (session_id, queued_id)
        cached = RUN_NOW_COMPLETED_RESULTS.get(cache_key)
        if cached is not None:
            RUN_NOW_COMPLETED_RESULTS.move_to_end(cache_key)
        existing = RUNS.run_now_requests.get(session_id)
        if cached is not None:
            task = None
        elif existing is not None:
            existing_queued_id, existing_task = existing
            if existing_queued_id != queued_id:
                raise HTTPException(
                    status_code=409,
                    detail=force_send_conflict_detail(
                        session_id,
                        queued_id,
                        guard="run_now_request",
                        message=(
                            "Another Force Send is already being applied. "
                            "Wait for it to finish before steering a different message."
                        ),
                        action="Refresh the queue before trying a different message.",
                        retryable=True,
                        owner_queued_id=existing_queued_id,
                        owner_task=existing_task,
                    ),
                )
            task = existing_task
        else:
            operation_id = f"force_send_{uuid.uuid4().hex[:16]}"
            task = asyncio.create_task(
                _run_queued_turn_now_and_release(session_id, queued_id)
            )
            setattr(task, "_hyadex_force_send_operation_id", operation_id)
            setattr(task, "_hyadex_force_send_queued_id", queued_id)
            setattr(
                task,
                "_hyadex_force_send_started_at",
                time.monotonic(),
            )
            setattr(task, "_hyadex_force_send_phase", "admission")
            RUNS.run_now_requests[session_id] = (queued_id, task)

    if cached is not None:
        error = cached.get("error")
        if isinstance(error, dict):
            raise NativeSteerHandoffError(
                str(error.get("message") or "Force Send delivery is uncertain"),
                safe_to_requeue=False,
                delivery_uncertain=bool(
                    error.get("delivery_uncertain", True)
                ),
            )
        result = cached.get("result")
        if isinstance(result, dict):
            return dict(result)
        raise HTTPException(status_code=409, detail="Force Send result is unavailable")

    # A dropped HTTP connection must not cancel a steer that may already have
    # reached the provider. Later duplicate requests join this task.
    assert task is not None
    return await asyncio.shield(task)


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


async def _run_queued_turn_now_and_release(
    session_id: str,
    queued_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    uncertain_error: dict[str, Any] | None = None
    try:
        try:
            # Native steering already has its own provider handoff fence and
            # must remain able to complete without waiting behind a Stop that
            # targets the same live provider turn.  A non-native Force Send,
            # however, is a Stop-then-start lifecycle transition and must be
            # serialized with the public Stop endpoint.
            result = await _run_queued_turn_now_once(
                session_id,
                queued_id,
                require_native=True,
            )
        except NonNativeForceSendRequiresLifecycleLock:
            async with session_lifecycle_lock(session_id):
                result = await _run_queued_turn_now_once(session_id, queued_id)
        return result
    except NativeSteerHandoffError as exc:
        if exc.safe_to_requeue:
            result = await deferred_force_send_outcome(session_id, queued_id, exc)
            return result
        if exc.delivery_uncertain or not exc.safe_to_requeue:
            uncertain_error = {
                "message": str(exc),
                "delivery_uncertain": bool(exc.delivery_uncertain),
            }
        raise
    finally:
        owner_task = asyncio.current_task()

        async def settle_force_send_request() -> None:
            async with RUNS.run_now_request_lock:
                paused_after_stop = False
                if (
                    result is not None
                    and result.get("ok") is True
                    and not result.get("deferred")
                ):
                    # Stop may durably pause a non-native promotion after its
                    # handoff returned but before this completion cache is written.
                    # Validate under the same lock order used by Stop so a later
                    # Run Now can never receive stale success for a held message.
                    async with RUNS.queue_lock:
                        candidates = list(RUNS.queued.get(session_id) or ())
                        promoted = RUNS.run_now.get(session_id)
                        if promoted is not None:
                            candidates.append(promoted)
                        paused_after_stop = any(
                            str(item.get("queued_id") or "") == queued_id
                            and item.get("_paused_after_stop") is True
                            for item in candidates
                        )
                cacheable_result = (
                    result
                    if result is not None
                    and result.get("ok") is True
                    and not result.get("deferred")
                    and not paused_after_stop
                    else None
                )
                if cacheable_result is not None or uncertain_error is not None:
                    cache_key = (session_id, queued_id)
                    RUN_NOW_COMPLETED_RESULTS[cache_key] = {
                        "expires_at": (
                            time.monotonic() + RUN_NOW_COMPLETED_TTL_SECONDS
                        ),
                        **(
                            {"result": dict(cacheable_result)}
                            if cacheable_result is not None
                            else {"error": uncertain_error}
                        ),
                    }
                    RUN_NOW_COMPLETED_RESULTS.move_to_end(cache_key)
                    while len(RUN_NOW_COMPLETED_RESULTS) > RUN_NOW_COMPLETED_MAX:
                        RUN_NOW_COMPLETED_RESULTS.popitem(last=False)
                current = RUNS.run_now_requests.get(session_id)
                if current is not None and current[1] is owner_task:
                    RUNS.run_now_requests.pop(session_id, None)

        settlement = asyncio.create_task(settle_force_send_request())
        try:
            await asyncio.shield(settlement)
        except BaseException:
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(settlement)


async def _run_queued_turn_now_once(
    session_id: str,
    queued_id: str,
    *,
    require_native: bool = False,
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    if stop_cleanup_in_progress(session_id):
        raise HTTPException(
            status_code=409,
            detail="Stop is still finishing for this chat; retry Force Send shortly",
        )

    async with RUNS.active_lock:
        active_turn = dict(RUNS.active.get(session_id) or {})
        interrupted_turn = dict(RUNS.current_turns.get(session_id) or {})

    selected: dict[str, Any] | None = None
    selected_index: int | None = None
    selected_predecessor_id: str | None = None
    selected_successor_id: str | None = None
    selected_was_paused = False
    native_steer = False
    goal_followup = codex_goal_followup_requires_native(
        STORE.sessions[session_id], active_turn, interrupted_turn,
    )
    # A goal may be created during an ordinary turn, including a turn with
    # run-bound provider authority. Its goal-only lane preserves that owner;
    # it must never reopen the generic logical-run/authority replacement lane.
    native_steer_queue_key = (
        "codex_goal_steer_queue"
        if goal_followup and active_turn.get("codex_native_operation_kind") != "goal_resume"
        else "native_steer_queue"
    )
    native_steer_queue = active_turn.get(native_steer_queue_key)
    selected_backend = str(
        STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND
    )
    remaining: int
    async with RUNS.queue_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        promoted_owner = RUNS.run_now.get(session_id)
        if session_id in RUNS.steering or promoted_owner is not None:
            waiter_entry = RUNS.steering_waits.get(session_id)
            guard = (
                "run_now_promotion"
                if promoted_owner is not None
                else "steering_reservation"
            )
            raise HTTPException(
                status_code=409,
                detail=force_send_conflict_detail(
                    session_id,
                    queued_id,
                    guard=guard,
                    message=(
                        "Force Send is already being applied. "
                        "Wait for it to finish before steering another message."
                    ),
                    action="Refresh the queue and retry after the current promotion settles.",
                    retryable=True,
                    owner_queued_id=(
                        str(promoted_owner.get("queued_id") or "")
                        if promoted_owner is not None
                        else None
                    ),
                    owner_task=(
                        waiter_entry[2]
                        if waiter_entry is not None
                        else None
                    ),
                ),
            )
        queue = RUNS.queued.get(session_id)
        if queue:
            items = list(queue)
            selected_index = next(
                (idx for idx, item in enumerate(items) if item.get("queued_id") == queued_id),
                None,
            )
            if selected_index is not None:
                selected = items[selected_index]
                if selected.get("_native_delivery_fenced") is True:
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id,
                            queued_id,
                            guard="delivery_uncertain",
                            message=(
                                "This message may already have reached the "
                                "provider and cannot be Force Sent again."
                            ),
                            action=(
                                "Inspect the provider history, then remove "
                                "this held queue item when it is safe."
                            ),
                            retryable=False,
                            owner_queued_id=queued_id,
                        ),
                    )
                if selected.get("purpose") in RETIRED_TURN_PURPOSES | {"scheduled_job"}:
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id,
                            queued_id,
                            guard="selected_internal_turn",
                            message=(
                                "A scheduled run cannot Force Send or "
                                "steer another turn."
                            ),
                            action=(
                                "Let the scheduled run start in queue order "
                                "or remove it from the queue."
                            ),
                            retryable=False,
                            owner_queued_id=queued_id,
                        ),
                    )
                # Explicit Send now promotes only the selected pending row;
                # every earlier row retains its relative order and content.
                selected_was_paused = (
                    selected.get("_paused_after_stop") is True
                )
                if selected_index > 0:
                    selected_predecessor_id = str(
                        items[selected_index - 1].get("queued_id") or ""
                    ) or None
                if selected_index + 1 < len(items):
                    selected_successor_id = str(
                        items[selected_index + 1].get("queued_id") or ""
                    ) or None
                selected_backend = str(
                    selected.get("backend")
                    or STORE.sessions[session_id].get("backend")
                    or DEFAULT_BACKEND
                )
                # QUEUE_LOCK admission and async message lookup can yield while
                # the live provider creates a goal or replaces its turn. Read
                # the latest owner here without an inverse ACTIVE/QUEUE lock
                # acquisition. There is no await before the decision below.
                # Once observed, goal protection must not turn into Stop
                # fallback merely because the goal cache changes meanwhile.
                active_turn = dict(RUNS.active.get(session_id) or {})
                interrupted_turn = dict(RUNS.current_turns.get(session_id) or {})
                goal_followup = goal_followup or codex_goal_followup_requires_native(
                    STORE.sessions[session_id], active_turn, interrupted_turn,
                )
                native_steer_queue_key = (
                    "codex_goal_steer_queue"
                    if goal_followup and active_turn.get("codex_native_operation_kind") != "goal_resume"
                    else "native_steer_queue"
                )
                native_steer_queue = active_turn.get(native_steer_queue_key)
                native_steer = bool(
                    (
                        active_turn.get("provider_turn_ready")
                        or (
                            goal_followup
                            and active_turn.get("codex_native_operation_kind") == "goal_resume"
                        )
                    )
                    and native_steer_queue is not None
                    and (
                        not goal_followup or (
                            isinstance(STORE.sessions[session_id].get("codex_goal"), dict)
                            and STORE.sessions[session_id]["codex_goal"].get("status") == "active"
                            and not STORE.sessions[session_id].get("codex_goal_time_budget_exhausted")
                            and codex_goal_steer_selection_is_plain(selected)
                        )
                    )
                    and not queued_turn_uses_retired_routing(selected)
                    and selected.get("skill_selection") is None
                    # Only ordinary logical-run replacement issues fresh
                    # authority. A goal steer keeps its exact owner and does
                    # not replace the original turn's references or command.
                    and (
                        goal_followup or (
                            interrupted_turn.get("skill_selection") is None
                            and not queued_turn_uses_retired_routing(interrupted_turn)
                        )
                    )
                    and (
                        (
                            active_turn.get("transport")
                            == CODEX_TRANSPORT_APP_SERVER
                            and selected_backend == BACKEND_CODEX
                            # Native goal steering sends input to the running
                            # turn. Picker changes apply to future turns; they
                            # do not need to match its already pinned runtime.
                            and (
                                goal_followup or queued_codex_runtime_matches_active(
                                    session_id, selected, active_turn,
                                )
                            )
                        )
                        or (
                            active_turn.get("transport")
                            == CLAUDE_TRANSPORT_AGENT_SDK
                            and selected_backend == BACKEND_CLAUDE
                            and queued_claude_runtime_matches_active(
                                session_id,
                                selected,
                                active_turn,
                            )
                        )
                    )
                )
                if goal_followup and not native_steer:
                    unsupported_input = not codex_goal_steer_selection_is_plain(selected)
                    raise HTTPException(
                        status_code=409,
                        detail=force_send_conflict_detail(
                            session_id, queued_id,
                            guard="active_goal_requires_native_steer",
                            message=(
                                "This queued action adds chat access or invokes a provider command, which cannot be applied inside the running Codex turn."
                                if unsupported_input else
                                "Codex does not have a running goal turn ready to receive this message."
                            ),
                            action=(
                                "It remains queued for a new turn. Text and file follow-ups can be sent now."
                                if unsupported_input else
                                "Your message remains queued. Retry Send now when Codex is running."
                            ),
                            retryable=not unsupported_input,
                            owner_queued_id=queued_id,
                        ),
                    )
                if require_native and not native_steer:
                    # Probe before removing or marking the queue item.  The
                    # caller will retry under the per-chat lifecycle lock so
                    # explicit Stop cannot interleave between interruption and
                    # promotion of this queued message.
                    raise NonNativeForceSendRequiresLifecycleLock
                if native_steer:
                    # Provider-side fencing happens after this item leaves the
                    # live deque. Preserve its exact durable position so crash
                    # recovery and a safe rejection can restore the same order.
                    selected["_native_delivery_queue_position"] = (
                        selected_index + 1
                    )
                kept = deque(
                    item
                    for idx, item in enumerate(items)
                    if idx != selected_index
                )
                if kept:
                    RUNS.queued[session_id] = kept
                    remaining = len(kept)
                else:
                    RUNS.queued.pop(session_id, None)
                    remaining = 0
            else:
                remaining = len(items)
        else:
            remaining = 0
        if selected is not None:
            RUNS.steering.add(session_id)
            if not native_steer:
                # The original queue event is durable, but its promotion is
                # not restart-safe until turn_queue_run_now is persisted.
                selected["_update_transitioning"] = True
                RUNS.run_now[session_id] = selected

    if selected is None:
        raise HTTPException(status_code=404, detail="queued turn not found")
    # Selecting Run Now is an explicit request to release a queue hold created
    # by Stop. Native steering does not pass through start_next_queued_turn(),
    # so clear it here for both transport paths.
    selected["_paused_after_stop"] = False

    if native_steer:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        owner_task = asyncio.current_task()
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "provider_handoff_queued",
            )
        try:
            request = {
                "selected": selected,
                "remaining": remaining,
                "future": future,
                "phase": "queued",
                "accepted_event": asyncio.Event(),
                "owner_task": owner_task,
                "expected_provider_turn_id": (
                    str(active_turn.get("provider_turn_id") or "")
                    if active_turn.get("provider_turn_ready") else ""
                ),
                "goal_identity": (
                    str((STORE.sessions[session_id].get("codex_goal") or {}).get("id") or ""),
                    str((STORE.sessions[session_id].get("codex_goal") or {}).get("objective") or ""),
                ) if goal_followup else None,
            }
            # Commit delivery against the live ACTIVE record. The provider can
            # become terminal after the initial eligibility snapshot; putting
            # into that now-detached queue would strand this Future forever.
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if (
                    not current
                    or not (
                        current.get("provider_turn_ready")
                        or (
                            goal_followup
                            and current.get("codex_native_operation_kind") == "goal_resume"
                        )
                    )
                    or current.get(native_steer_queue_key) is not native_steer_queue
                    or str(current.get("run_id") or "")
                    != str(active_turn.get("run_id") or "")
                ):
                    raise NativeSteerHandoffError(
                        "The active provider turn finished before steering delivery",
                        safe_to_requeue=True,
                    )
                if not current.get("provider_turn_ready"):
                    # The native goal owner will bind the pending message to
                    # its next turn/started event, never a stale completed ID.
                    request["expected_provider_turn_id"] = ""
                try:
                    native_steer_queue.put_nowait(request)
                except asyncio.QueueFull as exc:
                    raise NativeSteerHandoffError(
                        "The active provider is already applying another steering message",
                        safe_to_requeue=True,
                    ) from exc
            result = await await_native_steer_result(
                session_id,
                selected,
                backend=selected_backend,
                native_steer_queue=native_steer_queue,
                request=request,
                future=future,
            )
            result["remaining"] = remaining
            return result
        except BaseException as exc:
            safe_to_requeue = bool(getattr(exc, "safe_to_requeue", False))
            if isinstance(exc, asyncio.CancelledError):
                safe_to_requeue = withdraw_unaccepted_native_steer(
                    native_steer_queue,
                    request,
                )
                if not safe_to_requeue:
                    fence_task = asyncio.create_task(
                        fence_native_steer_delivery(
                            session_id,
                            selected,
                            backend=selected_backend,
                        )
                    )
                    try:
                        await asyncio.shield(fence_task)
                    except BaseException:
                        with suppress(BaseException):
                            await join_task_despite_caller_cancellation(
                                fence_task
                            )
            if selected_index is not None and safe_to_requeue:
                requeue_task = asyncio.create_task(
                    requeue_native_steer_after_safe_rejection(
                        session_id,
                        selected,
                        selected_index=selected_index,
                        selected_predecessor_id=selected_predecessor_id,
                        selected_successor_id=selected_successor_id,
                        preserve_pause=goal_followup,
                    )
                )
                try:
                    await asyncio.shield(requeue_task)
                except BaseException:
                    await join_task_despite_caller_cancellation(requeue_task)
            raise
        finally:
            async def release_native_steering_fence() -> None:
                async with RUNS.queue_lock:
                    RUNS.steering.discard(session_id)
                async with RUNS.active_lock:
                    still_busy = session_id in RUNS.busy
                if not still_busy:
                    schedule_next_queued_turn(session_id)

            cleanup = asyncio.create_task(release_native_steering_fence())
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                with suppress(BaseException):
                    await join_task_despite_caller_cancellation(cleanup)

    promotion_committed = False
    try:
        owner_task = asyncio.current_task()
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "stopping_active_turn",
            )
        claude_sdk_stop_run_id = (
            str(active_turn.get("run_id") or interrupted_turn.get("run_id") or "").strip()
            if active_turn.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
            else ""
        )
        stop_result = await stop_turn(
            session_id,
            expected_run_id=claude_sdk_stop_run_id or None,
            emit_event=False,
            schedule_queue=False,
            require_provider_turn_ready=True,
            cascade_codex_subagents=False,
            cascade_claude_subagents=False,
            hard_terminalize_on_timeout=bool(claude_sdk_stop_run_id),
            pause_queued_turns_on_stop=False,
            preserve_active_goal=True,
        )
        # A pending interrupt has not released the provider slot. Promoting it
        # would publish "Starting" and leave an unbounded BUSY waiter behind.
        deferred = bool(
            stop_result.get("deferred")
            or stop_result.get("pending")
            or stop_result.get("superseded")
        )
        interrupted = bool(stop_result.get("stopped")) and not deferred
        if deferred:
            already_notified = bool(selected.get("_turn_deferred_notified"))
            selected["_turn_deferred_notified"] = True
            selected.pop("_update_transitioning", None)
            async with RUNS.queue_lock:
                RUNS.run_now.pop(session_id, None)
                RUNS.steering.discard(session_id)
                queue_items = list(RUNS.queued.get(session_id) or [])
                insert_at = max(0, min(int(selected_index or 0), len(queue_items)))
                queue_items.insert(insert_at, selected)
                RUNS.queued[session_id] = deque(queue_items)
                remaining = len(queue_items)
            if stop_result.get("pending"):
                message = (
                    "Stop is still finishing. This message remains queued; "
                    "try Force Send again shortly."
                )
            elif stop_result.get("superseded"):
                message = (
                    "The active turn changed before it could be stopped. "
                    "This message remains queued; try Force Send again shortly."
                )
            else:
                message = (
                    "The provider is still starting, so the current turn was not interrupted. "
                    "This message remains queued; try Force Send again shortly."
                )
            if not already_notified:
                await append_event(session_id, "turn_deferred", {
                    "queued_id": queued_id,
                    "message": message,
                    "remaining": remaining,
                })
            async with RUNS.active_lock:
                still_busy = session_id in RUNS.busy
            if not still_busy:
                schedule_next_queued_turn(session_id)
            return {
                "ok": False,
                "queued_id": queued_id,
                "interrupted": False,
                "deferred": True,
                "replays_interrupted_message": False,
                "message": message,
                "remaining": remaining,
                "superseded_queued_ids": [],
            }
        prepared = prepare_steered_turn(selected, interrupted_turn if interrupted else None)
        async with RUNS.queue_lock:
            current = RUNS.run_now.get(session_id)
            if current and current.get("queued_id") == queued_id:
                RUNS.run_now[session_id] = prepared

        display_prompt = str(prepared.get("display_prompt") or prepared.get("steering_prompt") or selected.get("prompt") or "")
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "committing_promotion",
            )
        await append_durable_event(session_id, "turn_queue_run_now", {
            "queued_id": queued_id,
            "backend": prepared.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
            "prompt": display_prompt,
            "request_prompt": prepared.get("prompt") or "",
            "display_prompt": display_prompt,
            "file_ids": list(prepared.get("file_ids") or []),
            "display_file_ids": list(
                prepared.get("display_file_ids")
                if prepared.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            ),
            "interrupted_run_id": prepared.get("steer_interrupted_run_id"),
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "steering_lineage": normalize_steering_lineage(prepared.get("steering_lineage")),
            "purpose": prepared.get("purpose"),
            "skill_selection": prepared.get("skill_selection"),
            "message": (
                "Steering message promoted; it will continue the confirmed native provider thread."
                if interrupted
                else "Queued message promoted to run next."
            ),
            "remaining": remaining,
            "superseded_queued_ids": [],
        })
        promotion_committed = True
        prepared.pop("_update_transitioning", None)
        schedule_steered_turn_slot_waiter(session_id, queued_id)
        if owner_task is not None:
            setattr(
                owner_task,
                "_hyadex_force_send_phase",
                "promotion_committed",
            )
        return {
            "ok": True,
            "queued_id": queued_id,
            "interrupted": interrupted,
            "deferred": deferred,
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "superseded_queued_ids": [],
        }
    except BaseException:
        async def restore_uncommitted_promotion() -> None:
            async with RUNS.queue_lock:
                if not promotion_committed:
                    current = RUNS.run_now.get(session_id)
                    if current is not None and current.get("queued_id") == queued_id:
                        RUNS.run_now.pop(session_id, None)
                    selected.pop("_update_transitioning", None)
                    selected["_paused_after_stop"] = selected_was_paused
                    items = list(RUNS.queued.get(session_id) or ())
                    if not any(
                        item.get("queued_id") == queued_id
                        for item in items
                    ):
                        successor_index = (
                            next(
                                (
                                    idx
                                    for idx, item in enumerate(items)
                                    if item.get("queued_id") == selected_successor_id
                                ),
                                None,
                            )
                            if selected_successor_id is not None
                            else None
                        )
                        predecessor_index = (
                            next(
                                (
                                    idx
                                    for idx, item in enumerate(items)
                                    if item.get("queued_id") == selected_predecessor_id
                                ),
                                None,
                            )
                            if selected_predecessor_id is not None
                            else None
                        )
                        if successor_index is not None:
                            insert_at = successor_index
                        elif predecessor_index is not None:
                            insert_at = predecessor_index + 1
                        else:
                            insert_at = min(int(selected_index or 0), len(items))
                        items.insert(insert_at, selected)
                        RUNS.queued[session_id] = deque(items)
                RUNS.steering.discard(session_id)
            schedule_next_queued_turn(session_id)

        # Force Send temporarily turns a durable queue row into a restart-
        # blocking transition. A second cancellation can arrive while rollback
        # waits for QUEUE_LOCK; settle the exact rollback child before exposing
        # cancellation so `_update_transitioning` cannot become a stale managed-
        # update blocker and its steering fence cannot become ownerless.
        restoration = asyncio.create_task(restore_uncommitted_promotion())
        try:
            await asyncio.shield(restoration)
        except BaseException:
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(restoration)
        raise


RESTART_ORPHAN_ACTIVITY_TYPES = {
    "turn_started",
    "process_started",
    "provider_session",
    "assistant_text",
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "artifact_created",
    "idle_warning",
    # Provider errors are emitted before the normal turn_finished marker. A
    # crash in that narrow window still leaves an open turn to retire.
    "error",
}
RESTART_ORPHAN_TERMINAL_TYPES = {
    "turn_finished",
    "turn_stopped",
}


def abandoned_turn_after_restart(
    session_id: str,
    session: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the newest run left open when the server process disappeared.

    Active turns live in memory, so a SIGKILL cannot persist their normal
    terminal event.  Restrict the recovery scan to chats whose last durable
    event is turn activity; this keeps startup proportional to the number of
    interrupted chats instead of rescanning every large history.
    """

    persisted_active = (
        dict(session.get("active_run"))
        if isinstance(session.get("active_run"), dict)
        else {}
    )
    latest_type = str(session.get("latest_event_type") or "")
    if (
        not str(persisted_active.get("run_id") or "").strip()
        and latest_type not in RESTART_ORPHAN_ACTIVITY_TYPES
        and not latest_type.startswith("codex_")
    ):
        return None
    records = tail_jsonl_file(
        events_path(session_id),
        limit=200,
        max_bytes=8 * 1024 * 1024,
    )
    active_run_id = str(persisted_active.get("run_id") or "").strip()
    open_runs: dict[str, dict[str, Any]] = (
        {active_run_id: persisted_active}
        if active_run_id
        else {}
    )
    closed_runs: set[str] = set()
    for event in records:
        event_type = str(event.get("type") or "")
        run_id = str(event.get("run_id") or "").strip()
        if not run_id:
            continue
        if event_type in RESTART_ORPHAN_TERMINAL_TYPES:
            open_runs.pop(run_id, None)
            closed_runs.add(run_id)
            continue
        if event_type == "turn_started":
            closed_runs.discard(run_id)
            open_runs[run_id] = dict(event)
            open_runs[run_id]["last_activity_seq"] = int(event.get("seq") or 0)
            continue
        if event_type in RESTART_ORPHAN_ACTIVITY_TYPES and run_id not in closed_runs:
            candidate = open_runs.setdefault(run_id, {"run_id": run_id})
            candidate["last_activity_seq"] = int(event.get("seq") or 0)
            for key in (
                "backend",
                "purpose",
                "provider_context_mode",
                "job_context_mode",
                "job_id",
                "job_title",
                "digest_job_id",
                "source_session_id",
                "target_session_id",
                "cross_chat_envelope_id",
            ):
                if candidate.get(key) is None and event.get(key) is not None:
                    candidate[key] = event.get(key)
    if not open_runs:
        return None
    return max(
        open_runs.values(),
        key=lambda event: int(event.get("last_activity_seq") or event.get("seq") or 0),
    )


async def recover_abandoned_turns_after_start(
    *,
    forced_restart_request_id: str | None = None,
) -> int:
    """Close runs orphaned by a crash without replaying accepted user input."""

    session_items = [
        (session_id, dict(session))
        for session_id, session in STORE.sessions.items()
    ]

    def scan_candidates() -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for session_id, session in session_items:
            try:
                abandoned = abandoned_turn_after_restart(session_id, session)
            except Exception as exc:
                logger.warning(
                    "could not inspect abandoned turn session=%s error=%s",
                    session_id,
                    concise_error_message(exc),
                )
                continue
            if abandoned:
                found.append((session_id, abandoned))
        return found

    candidates = await asyncio.to_thread(scan_candidates)
    recovered = 0
    for session_id, abandoned in candidates:
        run_id = str(abandoned.get("run_id") or "").strip()
        if not run_id:
            continue
        payload: dict[str, Any] = {
            "run_id": run_id,
            "backend": abandoned.get("backend")
            or (STORE.sessions.get(session_id) or {}).get("backend"),
            "stopped": True,
            "recovered_after_restart": True,
            "message": (
                "This agent turn was interrupted when HyadexServer restarted. "
                "Its accepted user message was not replayed."
            ),
        }
        if forced_restart_request_id:
            payload.update({
                "forced_restart": True,
                "restart_request_id": forced_restart_request_id,
                "message": (
                    "This agent turn was forcibly interrupted when HyadexServer "
                    "restarted. Its accepted user message was not replayed."
                ),
            })
        for key in (
            "purpose",
            "provider_context_mode",
            "job_context_mode",
            "job_id",
            "job_title",
            "digest_job_id",
            "source_session_id",
            "target_session_id",
            "cross_chat_envelope_id",
            "secure_peer_envelope_id",
        ):
            if abandoned.get(key) is not None:
                payload[key] = abandoned[key]
        try:
            await append_event(session_id, "turn_stopped", payload)
        except Exception as exc:
            logger.warning(
                "could not close abandoned turn session=%s run=%s error=%s",
                session_id,
                run_id,
                concise_error_message(exc),
            )
        else:
            recovered += 1
    if recovered:
        logger.warning(
            "closed abandoned agent turns after restart count=%d",
            recovered,
        )
    return recovered


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


def tail_jsonl_file(path: Path, limit: int = 40, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    line_count = max(1, min(int(limit or 40), 200))
    size = path.stat().st_size
    byte_count = max(4096, min(int(max_bytes or 2 * 1024 * 1024), 8 * 1024 * 1024))
    with path.open("rb") as f:
        if size > byte_count:
            f.seek(size - byte_count)
        data = f.read(byte_count)
    records: list[dict[str, Any]] = []
    for line in data.decode("utf-8", "replace").splitlines()[-line_count:]:
        with suppress(Exception):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


SEMANTIC_TIMELINE_JOB_RUN_LIMIT = 7
SEMANTIC_TIMELINE_JOB_EXTRA_LIMIT = 64
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


async def authorize_provider_jobs_operation(
    request: Request,
    *,
    session_id: str,
    operation: Literal["read", "write"],
) -> dict[str, Any]:
    """Intersect the turn's issued Jobs ceiling with the chat's live policy."""

    capability = await authorize_provider_action(
        request,
        action="jobs",
        session_id=session_id,
    )
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    issued_access = effective_provider_jobs_access({
        "provider_jobs_access": capability.get("provider_jobs_access"),
    })
    live_access = effective_provider_jobs_access(session)
    if issued_access == "blocked" or live_access == "blocked":
        raise HTTPException(
            status_code=403,
            detail="agent scheduled-jobs access is blocked for this chat",
        )
    if operation == "write" and (
        issued_access != "full" or live_access != "full"
    ):
        raise HTTPException(
            status_code=403,
            detail="agent scheduled-jobs access is read-only for this chat",
        )
    return capability


async def append_provider_job_mutation_event(
    job: dict[str, Any],
    event_type: Literal["job_created", "job_updated"],
) -> None:
    """Best-effort timeline projection for an already committed job mutation."""

    job_id = str(job.get("id") or "")
    session_id = str(job.get("session_id") or "")
    verb = "created" if event_type == "job_created" else "updated"
    try:
        await append_event(session_id, event_type, {
            "job": event_job(job),
            "job_id": job_id,
            "message": (
                f"Scheduled job {verb}: {job.get('title') or job_id}"
            ),
        })
    except Exception as exc:
        # Persistence has already succeeded. An audit/timeline projection
        # failure must not make the provider retry or refund route authority.
        logger.warning(
            "could not append provider job mutation event job=%s type=%s: %s",
            job_id,
            event_type,
            concise_error_message(exc),
        )


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if not str(k).startswith("_")}
    out["manual_run_pending"] = bool(job.get("manual_run_pending"))
    out["schedule_kind"] = job_schedule_kind(out)
    out["context_mode"] = job_context_mode(out)
    out["timezone"] = str(out.get("timezone") or "UTC")
    for key in ("next_run_at", "scheduled_run_at", "schedule_start_at", "last_scheduled_run_at"):
        if out.get(key) is not None:
            out[f"{key}_iso"] = datetime.fromtimestamp(
                float(out[key]),
                tz=timezone.utc,
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return out


def provider_public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Project a job without disclosing saved target identities.

    Jobs saved before @Chat and Team targets were retired may still carry
    them. Agents see how many saved chat targets keep a job paused, never the
    targets or their chat ids.
    """

    out = public_job(job)
    raw_references = out.pop("chat_references", None)
    references = raw_references if isinstance(raw_references, list) else []
    retired_references = out.pop("team_references", None)
    target_ids: set[str] = set()
    for reference in [
        *references,
        *(retired_references if isinstance(retired_references, list) else []),
    ]:
        if isinstance(reference, dict):
            target_ids.update(
                str(reference[key]).strip()
                for key in ("session_id", "team_id", "target_id")
                if str(reference.get(key) or "").strip()
            )
    out["chat_target_count"] = len(references)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [redact(nested) for nested in value]
        if isinstance(value, str):
            result = value
            for target_id in sorted(target_ids, key=len, reverse=True):
                result = result.replace(target_id, "saved chat target")
            return result
        return value

    return redact(out)


def provider_public_job_run(run: dict[str, Any]) -> dict[str, Any]:
    """Project scheduled-run history without exposing saved target identities.

    Runs recorded by older servers can carry cross-chat routing identities;
    they stay private to the user-facing timeline.
    """

    private_fields = {
        "chat_references",
        "team_references",
        "target_session_id",
        "source_session_id",
        "source_run_id",
        "authorization_source_run_id",
        "authorization_route_id",
        "cross_chat_envelope_id",
        "cross_chat_exchange_id",
        "cross_chat_exchange_leg_id",
        "exchange_id",
        "exchange_leg_id",
        "inbound_leg_id",
        "envelope_id",
        "leg_id",
        "parent_leg_id",
        "cross_chat_obligation_ids",
        "cross_chat_exchange_ids",
        "cross_chat_direct_message_ids",
        "secure_peer_envelope_id",
        "requester_session_id",
        "responder_session_id",
        "target_chat_id",
    }
    private_ids: set[str] = set()

    def collect_private_values(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect_private_values(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_private_values(nested)
        elif isinstance(value, str) and value:
            private_ids.add(value)

    def collect_private_ids(value: Any) -> None:
        if isinstance(value, dict):
            raw_references = value.get("chat_references")
            if isinstance(raw_references, list):
                for raw_reference in raw_references:
                    if not isinstance(raw_reference, dict):
                        continue
                    clean_target_id = str(raw_reference.get("session_id") or "").strip()
                    if clean_target_id:
                        private_ids.add(clean_target_id)
            for key, nested in value.items():
                if key in private_fields and key != "chat_references":
                    collect_private_values(nested)
                if (
                    isinstance(nested, str)
                    and re.fullmatch(r"(?:handoff|exchange|leg)_[A-Za-z0-9_-]+", nested)
                ):
                    private_ids.add(nested)
                collect_private_ids(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_private_ids(nested)

    collect_private_ids(run)
    out = dict(run)
    raw_references = out.pop("chat_references", None)
    if isinstance(raw_references, list):
        out["chat_target_count"] = len(raw_references)
    out.pop("team_references", None)
    if isinstance(out.get("job"), dict):
        out["job"] = provider_public_job(out["job"])

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    nested
                    if key == "session_id"
                    else redact(nested)
                )
                for key, nested in value.items()
                if key not in private_fields
                and not (
                    key == "id"
                    and isinstance(nested, str)
                    and re.fullmatch(
                        r"(?:handoff|exchange|leg)_[A-Za-z0-9_-]+",
                        nested,
                    )
                )
            }
        if isinstance(value, list):
            return [redact(nested) for nested in value]
        if isinstance(value, str):
            result = value
            for private_id in sorted(private_ids, key=len, reverse=True):
                result = result.replace(private_id, "private cross-chat reference")
            return result
        return value

    return redact(out)


def compact_job_interval(value: Any) -> str:
    try:
        seconds = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "unspecified"
    if seconds <= 0:
        return "unspecified"
    parts: list[str] = []
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, unit)
        if amount:
            parts.append(f"{amount}{suffix}")
        if len(parts) == 2:
            break
    return " ".join(parts)


def scheduled_job_resource_diagnostics(pressure: dict[str, Any], active_count: int) -> dict[str, Any]:
    """Explain resource admission from one snapshot, without taking run locks.

    Per-chat ownership, provider readiness and maintenance remain separate
    admission checks. Health must not wait for their locks to explain memory.
    """

    available = pressure.get("available_mem_mb")
    blocker = None
    for cap, minimum, action, message in (
        (MAX_ACTIVE_AGENT_RUNS, MIN_START_AVAILABLE_MEM_MB, "start an agent turn",
         f"server already has {active_count} active agent run(s)"),
        (JOB_MAX_ACTIVE_RUNS, JOB_MIN_AVAILABLE_MEM_MB, "start a scheduled job",
         f"{active_count} active agent run(s)"),
    ):
        if cap > 0 and active_count >= cap:
            blocker = message
            break
        if isinstance(available, int) and minimum > 0 and available < minimum:
            blocker = low_available_memory_message(available, minimum, action=action)
            break
    return {
        "effective_min_available_mem_mb": max(0, MIN_START_AVAILABLE_MEM_MB, JOB_MIN_AVAILABLE_MEM_MB),
        "resource_blocked": blocker is not None,
        "resource_blocking_reason": blocker,
    }


async def scheduled_job_blocker(
    session_id: str,
    *,
    manual: bool = False,
    group: bool = False,
) -> str | None:
    async with RUNS.active_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            return lifecycle_blocker
        # A group job only posts a message (busy members queue it), so only
        # the server-wide limits below apply, not its anchor chat's state.
        if not group:
            session = STORE.sessions.get(session_id) or {}
            backend = str(
                session.get("backend") or DEFAULT_BACKEND
            ).strip().lower()
            if CODEX_GOALS_RECONFIGURING and backend == BACKEND_CODEX:
                return "wait for Codex goals configuration to finish"
            if session_id in RUNS.maintenance:
                return (
                    "wait for Claude Stop recovery to finish"
                    if session_id in CLAUDE_STOP_FENCE_SESSIONS
                    else "wait for provider session maintenance to finish"
                )
            if stop_cleanup_in_progress(session_id):
                return "chat is finishing an explicit Stop"
            if session_id in RUNS.busy:
                return "chat already has a running turn"
        active_count = len(RUNS.busy)

    global_blocker = await turn_start_blocker()
    if global_blocker:
        return global_blocker

    if JOB_MAX_ACTIVE_RUNS > 0 and active_count >= JOB_MAX_ACTIVE_RUNS:
        return f"{active_count} active agent run(s)"

    pressure = host_pressure_snapshot()
    available_mem_mb = pressure.get("available_mem_mb")
    if (
        isinstance(available_mem_mb, int)
        and JOB_MIN_AVAILABLE_MEM_MB > 0
        and available_mem_mb < JOB_MIN_AVAILABLE_MEM_MB
    ):
        return low_available_memory_message(
            available_mem_mb, JOB_MIN_AVAILABLE_MEM_MB, action="start a scheduled job",
        )

    return None


async def save_staged_fork_provider_reference(
    session_id: str,
    thread_id: str,
) -> None:
    """Persist the provider ID before the cleanup ledger can be released."""

    clean_thread_id = str(thread_id or "").strip()
    if not clean_thread_id:
        raise ValueError("missing staged provider fork thread")
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session or not session.get("_fork_initializing"):
            raise RuntimeError("staged fork session is unavailable")
        previous_thread_id = session.get("codex_thread_id")
        session["codex_thread_id"] = clean_thread_id
        try:
            await STORE.save()
        except BaseException:
            session["codex_thread_id"] = previous_thread_id
            raise


async def clear_staged_fork_provider_reference(
    session_id: str,
    thread_id: str,
) -> None:
    """Release staged ownership only after cleanup is independently durable."""

    clean_thread_id = str(thread_id or "").strip()
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if not session or not session.get("_fork_initializing"):
            raise RuntimeError("staged fork session is unavailable")
        current_thread_id = str(session.get("codex_thread_id") or "").strip()
        if current_thread_id and current_thread_id != clean_thread_id:
            raise RuntimeError("staged fork provider identity changed unexpectedly")
        previous_session_id = session.get("session_id")
        previous_thread_id = session.get("codex_thread_id")
        if str(session.get("session_id") or "").strip() == clean_thread_id:
            session["session_id"] = None
        session["codex_thread_id"] = None
        try:
            await STORE.save()
        except BaseException:
            session["session_id"] = previous_session_id
            session["codex_thread_id"] = previous_thread_id
            raise


async def cleanup_abandoned_fork_provider_threads(
    startup_thread_ids: set[str] | None = None,
) -> None:
    """Retire durable provider orphans without delaying server startup."""

    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        pending = set(ABANDONED_FORK_PROVIDER_THREADS)
        if startup_thread_ids is not None:
            # The startup worker runs after the server begins accepting
            # requests. Never let it consume a journal entry created by a new
            # in-flight fork after STORE.load() completed.
            pending.intersection_update(startup_thread_ids)
    if not pending:
        return

    resolved: set[str] = set()
    for thread_id in sorted(pending):
        # The ledger is recovery state, but never trust it over current session
        # ownership. A manually resumed/imported thread must not be deleted.
        if any(
            session_references_codex_thread(session, thread_id)
            for session in STORE.sessions.values()
            if isinstance(session, dict)
            and not session.get("_fork_initializing")
        ):
            logger.warning(
                "discarding stale abandoned-fork cleanup entry for owned thread=%s",
                thread_id,
            )
            resolved.add(thread_id)
            continue
        try:
            if await retire_failed_codex_fork(thread_id):
                resolved.add(thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "abandoned provider fork cleanup failed thread=%s: %s",
                thread_id,
                concise_error_message(exc),
            )

    if not resolved:
        return
    async with ABANDONED_FORK_PROVIDER_THREADS_LOCK:
        remaining = ABANDONED_FORK_PROVIDER_THREADS - resolved
        try:
            await asyncio.to_thread(
                write_abandoned_fork_thread_ids,
                remaining,
            )
        except OSError as exc:
            # Keep the in-memory entries too; a later retry or restart must
            # still know that durable cleanup was not committed.
            logger.warning(
                "could not update abandoned fork cleanup ledger: %s",
                concise_error_message(exc),
            )
            return
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(remaining)


async def cleanup_aborted_session_fork(
    cleanup_state: dict[str, str | None],
) -> None:
    """Best-effort cleanup that survives cancellation of the HTTP request."""

    provider_thread_id = str(
        cleanup_state.get("provider_thread_id") or ""
    ).strip()
    child_session_id = str(cleanup_state.get("child_session_id") or "").strip()

    # Fence synchronously, before the first await. A provider callback may
    # already be queued when the HTTP task is cancelled; it must neither route
    # back to this staged child nor recreate its directory via append_event.
    if child_session_id:
        DELETING_SESSIONS.add(child_session_id)
        DELETED_SESSION_TOMBSTONES.add(child_session_id)
        for thread_id, owner_session_id in tuple(
            CODEX_THREAD_SESSION_INDEX.items()
        ):
            if owner_session_id == child_session_id:
                CODEX_THREAD_SESSION_INDEX.pop(thread_id, None)
        with CODEX_SUBAGENT_INDEX_LOCK:
            owned_child_threads = [
                thread_id
                for thread_id, owner_session_id
                in CODEX_SUBAGENT_SESSION_INDEX.items()
                if owner_session_id == child_session_id
            ]
            for thread_id in owned_child_threads:
                CODEX_SUBAGENT_SESSION_INDEX.pop(thread_id, None)
                CODEX_SUBAGENT_STATE.pop(thread_id, None)
                CODEX_SUBAGENT_LIVE_GENERATIONS.pop(thread_id, None)
                CODEX_SUBAGENT_LIVE_MANAGERS.pop(thread_id, None)
        for thread_id, owner_session_id in tuple(
            CODEX_QUARANTINED_GOAL_THREADS.items()
        ):
            if owner_session_id == child_session_id:
                CODEX_QUARANTINED_GOAL_THREADS.pop(thread_id, None)
        CODEX_GOAL_SYNC_GENERATIONS.pop(child_session_id, None)

    async def delete_local_child() -> None:
        # An append that passed its second deletion check already owns this
        # lock. Join it before removing the directory; all later appends see
        # the tombstone and return without touching the filesystem.
        async with event_delivery_lock(child_session_id):
            await STORE.delete(child_session_id)

    async def cleanup_fork_resources() -> None:
        provider_reference_is_durable = not provider_thread_id
        if provider_thread_id:
            try:
                provider_reference_is_durable = await retire_or_record_failed_codex_fork(
                    provider_thread_id
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                logger.warning(
                    "aborted provider fork retirement failed thread=%s: %s",
                    provider_thread_id,
                    concise_error_message(exc),
                )
        if child_session_id and provider_reference_is_durable:
            await delete_local_child()
        elif child_session_id:
            logger.warning(
                "retaining hidden staged fork after cleanup persistence failure "
                "session=%s thread=%s",
                child_session_id,
                provider_thread_id,
            )

    cleanup_task = asyncio.create_task(cleanup_fork_resources())
    try:
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(cleanup_task)
    finally:
        if child_session_id:
            # The durable runtime fence is the tombstone. "Deleting" only
            # describes the period while local/provider cleanup is joining.
            DELETING_SESSIONS.discard(child_session_id)


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
def scheduled_job_lifecycle_admission_defer_reason(
    error: Exception,
) -> str | None:
    """Return a retry reason for a transient turn-admission fence.

    The scheduler checks its blockers before dispatch, but worker maintenance
    or another transient admission path can establish its fence after that
    check and before ``start_turn`` reserves the chat. That rejection did not
    execute the due occurrence, so it must be parked rather than recorded as a
    failed run. Other HTTP errors can be durable provider/configuration
    failures and keep the ordinary failure/recurrence behavior.
    """

    if isinstance(error, TransientAdmissionWait):
        return str(error.detail or "").strip() or "turn admission deferred"
    if not isinstance(error, HTTPException) or error.status_code != 503:
        return None
    reason = str(error.detail or "").strip()
    if reason in {
        EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
        MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    }:
        return reason
    return None


def scheduled_job_runtime_unavailable_reason(error: Exception) -> str | None:
    """Recognize the provider runtime probe's exact structured rejection."""

    if not isinstance(error, HTTPException) or error.status_code != 503:
        return None
    detail = error.detail
    if not isinstance(detail, dict) or detail.get("code") != "runtime_unavailable":
        return None
    message = str(detail.get("message") or "").strip()
    return message or "The selected provider runtime is unavailable."


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


async def join_cancelled_tasks(*tasks: Any) -> None:
    """Await already-cancelled tasks without letting any of them raise."""

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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


class HyadexGroupRuntime:
    """Connect group discussions to ordinary member chats.

    Every group participant is a normal session marked with ``group_member``:
    its turns use the same admission, queue, provider runtime and recovery as
    any chat, and it loads the agent's own MCP servers, skills and settings.
    """

    def resolve_cwd(self, cwd: str | None) -> str:
        requested = (cwd or "").strip()
        if requested:
            path = Path(requested).expanduser()
            if not path.is_dir():
                raise HTTPException(status_code=400, detail="The group working directory does not exist.")
            return str(path.resolve())
        return str(Path(existing_cwd(DEFAULT_CWD)).resolve())

    def validate_backend(self, backend: str) -> None:
        require_enabled_backend(backend)
        interactive_client_capabilities({"backend": backend})

    async def create_member_session(self, *, group_id: str, title: str, handle: str,
                                    spec: dict[str, Any], cwd: str) -> str:
        request = CreateSessionRequest(
            title=title[:200], auto_title_enabled=False, cwd=cwd, backend=spec["backend"],
            model=spec.get("model"), effort=spec.get("effort"),
            claude_permission_mode=spec.get("claude_permission_mode"),
            codex_approval_policy=spec.get("codex_approval_policy"),
            codex_sandbox_mode=spec.get("codex_sandbox_mode"),
        )
        session = await STORE.create(request)
        async with STORE.transaction():
            session["group_member"] = {"group_id": group_id, "handle": handle}
        await STORE.save()
        return str(session["id"])

    async def start_member_turn(self, session_id: str, *, prompt: str, request_id: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="member chat not found")
        # The dispatch id is recorded on turn_queued/turn_started, so restart
        # recovery can find this exact turn.
        request = TurnRequest(prompt=prompt, client_request_id=request_id,
                              client_capabilities=interactive_client_capabilities(session))
        result = await start_turn_durably(session_id, request)
        return {"run_id": result.get("run_id"), "queued_id": result.get("queued_id"),
                "queued": bool(result.get("queued"))}

    async def stop_member(self, session_id: str) -> None:
        if session_id in STORE.sessions:
            await request_explicit_stop(session_id)

    async def cancel_queued(self, session_id: str, queued_id: str) -> bool:
        if session_id not in STORE.sessions:
            return False
        try:
            await unqueue_turn(session_id, queued_id)
        except HTTPException:
            # Already started, already gone, or the queue is temporarily fenced.
            return False
        return True

    async def retire_member_session(self, session_id: str) -> None:
        session = STORE.sessions.get(session_id)
        if session and not session.get("archived"):
            await _update_session(session_id, UpdateSessionRequest(archived=True))

    def member_status(self, session_id: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            return {"exists": False}
        return {
            "exists": True,
            "running": session_id in RUNS.busy,
            "queued": len(RUNS.queued.get(session_id) or ()),
            "needs_action": bool(session.get("claude_needs_user_action") or session.get("codex_needs_user_action")),
        }

    def reply_text(self, event: dict[str, Any]) -> str:
        return clean_assistant_text(str(event.get("result_text") or ""))

    async def find_member_progress(self, session_id: str, *, run_id: str | None, queued_id: str | None,
                                   request_id: str) -> dict[str, Any]:
        if session_id not in STORE.sessions:
            return {"exists": False}
        events = await asyncio.to_thread(
            read_events, session_id, 0, None, 1_000_000,
            include_internal=True, cap_to_response_limit=False,
        )
        resolved_run, resolved_queue = run_id, queued_id
        for event in events:
            if event.get("client_request_id") != request_id:
                continue
            if event.get("type") == "turn_started" and event.get("run_id"):
                resolved_run = resolved_run or str(event["run_id"])
            elif event.get("type") == "turn_queued" and event.get("queued_id"):
                resolved_queue = resolved_queue or str(event["queued_id"])
        if not resolved_run and resolved_queue:
            resolved_run = next((str(event["run_id"]) for event in events
                                 if event.get("type") == "turn_started" and event.get("queued_id") == resolved_queue
                                 and event.get("run_id")), None)
        terminal = None
        if resolved_run:
            terminal = next((event for event in reversed(events)
                             if event.get("type") in group_chat.TERMINAL_EVENT_TYPES
                             and str(event.get("run_id") or "") == resolved_run), None)
        unqueued = bool(resolved_queue and not resolved_run and any(
            event.get("type") == "turn_unqueued" and event.get("queued_id") == resolved_queue for event in events))
        return {"exists": True, "run_id": resolved_run, "queued_id": resolved_queue,
                "terminal": terminal, "unqueued": unqueued}

    async def set_member_goal(self, session_id: str, objective: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="member chat not found")
        backend = str(session.get("backend") or DEFAULT_BACKEND)
        if backend == BACKEND_CLAUDE:
            return await set_claude_goal(session_id, ClaudeGoalRequest(condition=objective))
        if backend == BACKEND_CODEX:
            async with session_lifecycle_lock(session_id):
                ensure_session_not_deleting(session_id)
                return await _put_codex_goal_locked(
                    session_id, CodexGoalRequest(objective=objective, status="active"))
        raise HTTPException(status_code=400, detail="Goals require a Claude or Codex member")

    async def broadcast(self, key: str, event: dict[str, Any]) -> None:
        await HUB.broadcast(key, event)

    async def participant_removed(self, group_id: str, removed_session_id: str, anchor_session_id: str) -> None:
        async with WIRING.jobs._lock:
            moved = False
            for job in WIRING.jobs.jobs.values():
                if job.get("group_id") == group_id and job.get("session_id") == removed_session_id:
                    job["session_id"] = anchor_session_id
                    job["updated_at"] = now_iso()
                    job["_revision"] = new_job_revision()
                    moved = True
            if moved:
                await WIRING.jobs.save()

    async def group_deleted(self, group_id: str) -> None:
        for job_id, job in list(WIRING.jobs.jobs.items()):
            if job.get("group_id") == group_id:
                with suppress(HTTPException):
                    await WIRING.jobs.delete(job_id, expected_session_id=job.get("session_id"))


GROUPS = group_chat.GroupEngine(STATE_DIR / "groups", HyadexGroupRuntime())
# Group discussions follow their member chats' turns; observing only schedules
# the engine's own work and never delays the chat's event path.
EVENTS.on_observe(lambda session_id, event: GROUPS.observe_member_event(session_id, event),
                  types=group_chat.OBSERVED_EVENT_TYPES)
AUTOPILOT = autopilot.AutopilotEngine(STATE_DIR / "autopilot", GROUPS)
router.include_router(create_autopilot_router(AUTOPILOT))


async def start_group_job_run(group_id: str, job: dict[str, Any], job_id: str) -> dict[str, Any]:
    posted = await GROUPS.post_message(
        group_id,
        group_chat.PostGroupMessageRequest(text=str(job.get("prompt") or "")),
        origin="job",
        meta={"job_id": job_id, "job_title": str(job.get("title") or job_id)},
    )
    return {"run_id": None, "group_id": group_id, "group_message_seq": posted["message"]["seq"],
            "targets": posted["targets"]}


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


async def ensure_discardable_empty_session(session_id: str, expected_updated_at: str) -> None:
    """Fail closed while the caller holds the lifecycle lock/deletion fence.

    Automatic placeholder cleanup must never behave like an explicit Delete:
    it may not interrupt a turn, cancel queued work or remove uploaded content.
    Local drafts are additionally checked by the originating desktop client.
    """
    def untouched() -> bool:
        session = STORE.sessions.get(session_id)
        return bool(
            session and expected_updated_at
            and session.get("updated_at") == expected_updated_at
            and session.get("title") == "New chat"
            and not session_backend_locked(session)
            and not any(session.get(key) for key in (
                "parent_id", "pinned", "archived", "manual_unread",
                "codex_goal", "claude_goal", "emergency_alert",
            ))
            and session_id not in RUNS.active and session_id not in RUNS.busy
            and session_id not in RUNS.current_turns
            and not RUNS.queued.get(session_id)
            and session_id not in RUNS.run_now
            and session_id not in RUNS.run_now_requests
            and not any(not task.done() for task in RUNS.turn_tasks.get(session_id, ()))
            and not any(job.get("session_id") == session_id for job in JOBS.jobs.values())
        )

    def only_creation_event() -> bool:
        # Unlike timeline projection, do not skip malformed/hidden records.
        # Any extra byte or uncertain history preserves the chat.
        with events_path(session_id).open("rb") as stream:
            line = stream.readline(65537)
            if not line or len(line) > 65536 or stream.read(1):
                return False
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return False
        return isinstance(event, dict) and event.get("type") == "session_created" and event.get("seq") == 1

    if not untouched():
        raise HTTPException(status_code=409, detail="Chat is no longer an unused placeholder; it was preserved.")
    history_empty, files, terminal = await asyncio.gather(
        asyncio.to_thread(only_creation_event),
        asyncio.to_thread(list_session_file_records, session_id),
        asyncio.to_thread(terminal_windows_snapshot, session_id),
    )
    if not untouched() or not history_empty or files or terminal.get("exists"):
        raise HTTPException(status_code=409, detail="Chat has content or resources; it was preserved.")


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


def completed_fork_events(
    events: list[dict[str, Any]],
    *,
    active_run_id: str,
    through_sequence: int | None,
) -> list[dict[str, Any]]:
    """Freeze the visible completed prefix, never a partially observed turn."""
    prefix: list[dict[str, Any]] = []
    completed_length = 0
    for event in events:
        if through_sequence is not None and int(event.get("seq") or 0) > through_sequence:
            break
        if active_run_id and str(event.get("run_id") or "") == active_run_id:
            break
        prefix.append(event)
        if (
            event.get("type") == "turn_finished"
            and (event.get("exit_code") == 0 or event.get("imported") is True)
            and not event.get("stopped")
            and not event.get("is_error")
            # Replay bookkeeping is not a newly completed provider turn. In
            # particular, metadata-only repair batches carry no native turn
            # identity and must not replace an already resumable boundary.
            and not (event.get("imported") is True and event.get("metadata_only") is True)
            and event.get("purpose") not in FORK_INTERNAL_PURPOSES
        ):
            completed_length = len(prefix)
    return prefix[:completed_length]


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


async def _fork_session_locked(
    session_id: str,
    req: ForkSessionRequest,
    *,
    cleanup_state: dict[str, str | None] | None = None,
    completed_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if cleanup_state is None:
        cleanup_state = {}
    parent_record = STORE.sessions.get(session_id)
    parent = dict(parent_record) if isinstance(parent_record, dict) else None
    if not parent:
        raise HTTPException(status_code=404, detail="session not found")
    fork_cwd = validated_fork_cwd(parent)
    parent["cwd"] = fork_cwd
    parent_backend = (parent.get("backend") or DEFAULT_BACKEND).lower()
    live_snapshot = completed_snapshot is not None
    empty_snapshot = live_snapshot and not completed_snapshot
    parent_codex_thread_id = parent.get("codex_thread_id") or (
        parent.get("session_id") if parent_backend == BACKEND_CODEX else None
    )
    parent_claude_session_id = (
        validated_claude_fork_provider_id(parent, session_id, fork_cwd)
        if parent_backend == BACKEND_CLAUDE and not empty_snapshot
        else None
    )
    # A not-yet-started Claude child still resumes its ancestor at this exact
    # cutoff. Forking that child again must not lose the inherited boundary.
    claude_cutoff = parent.get("fork_resume_session_at") if parent.get("fork_from") and not empty_snapshot else None
    codex_cutoff = None
    if live_snapshot and not empty_snapshot:
        terminal = completed_snapshot[-1]
        if parent_backend == BACKEND_CLAUDE and not parent_claude_session_id:
            raise HTTPException(status_code=409, detail="The completed Claude turn has no resumable provider snapshot. The running chat was left unchanged.")
        if parent_backend == BACKEND_CLAUDE and parent_claude_session_id:
            try:
                claude_cutoff = await asyncio.to_thread(
                    claude_completed_fork_boundary, parent, parent_claude_session_id, completed_snapshot,
                )
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail="The completed Claude transcript snapshot is unavailable. The running chat was left unchanged.") from exc
        elif parent_backend == BACKEND_CODEX:
            codex_cutoff = str(terminal.get("provider_turn_id") or "")
            if not parent_codex_thread_id or not codex_cutoff or str(terminal.get("provider_thread_id") or "") != str(parent_codex_thread_id):
                raise HTTPException(status_code=409, detail="The last completed Codex turn has no verifiable native snapshot. The running chat was left unchanged; retry after it finishes.")
    forked_codex_thread_id: str | None = None
    codex_fork_error: str | None = None

    if parent_backend == BACKEND_CODEX and parent_codex_thread_id and not empty_snapshot:
        try:
            forked_codex_thread_id = await fork_codex_thread(
                str(parent_codex_thread_id), parent,
                **({"last_turn_id": codex_cutoff} if codex_cutoff else {}),
            )
            cleanup_state["provider_thread_id"] = forked_codex_thread_id
            logger.info(
                "forked codex thread parent_session=%s source_thread=%s forked_thread=%s",
                session_id,
                parent_codex_thread_id,
                forked_codex_thread_id,
            )
        except CodexForkCleanupError as e:
            # The low-level fork could neither delete nor journal its provider
            # object. Preserve the ID for the endpoint's cancellation-safe
            # cleanup and fail closed instead of publishing a memory fallback.
            cleanup_state["provider_thread_id"] = e.thread_id
            raise
        except Exception as e:
            reason = {
                "thread/fork ancestry could not be verified": "Codex returned a fork with unverifiable ancestry.",
                "thread/fork working directory could not be verified": "Codex returned a fork in a different working directory.",
                "thread/fork completed-turn boundary could not be verified": "Codex returned a fork that did not end at the last completed turn.",
                "thread/fork cleanup journal could not be persisted": "The fork recovery record could not be saved.",
                "thread/fork returned the source thread id": "Codex did not create a separate conversation.",
                "thread/fork returned a thread with a different source id": "Codex returned a fork from a different conversation.",
            }.get(str(e), "Codex could not create or verify the completed-turn fork.")
            if isinstance(e, CodexAppServerTimeout):
                reason = "Codex timed out while creating or verifying the fork."
            elif isinstance(e, CodexAppServerDisconnected):
                reason = "The Codex connection closed while creating or verifying the fork."
            elif isinstance(e, CodexAppServerRequestError):
                reason = "Codex rejected the request to create or verify the fork."
            logger.warning(
                "codex fork failed parent_session=%s source_thread=%s cutoff=%s error_type=%s: %s",
                session_id,
                parent_codex_thread_id,
                codex_cutoff,
                type(e).__name__,
                reason,
            )
            if live_snapshot:
                raise HTTPException(status_code=409, detail=f"{reason} The running chat was left unchanged.") from e
            codex_fork_error = str(e)
    elif parent_backend == BACKEND_CODEX and not empty_snapshot:
        # A goal-reconciliation rollover can deliberately detach an unsafe
        # provider thread. A UI fork must still inherit bounded conversational
        # memory; silently creating an empty Codex thread makes the fork look
        # correct while the model has none of the displayed context.
        codex_fork_error = (
            "the parent Codex provider thread is unavailable; "
            "using a bounded memory fork"
        )

    child = await STORE.create(
        CreateSessionRequest(
            title=req.title or f"Fork of {parent.get('title') or session_id}",
            folder=parent.get("folder"),
            cwd=parent.get("cwd"),
            backend=parent_backend,
            model=parent.get("model"),
            effort=parent.get("effort"),
            subagent_limit=parent.get("subagent_limit"),
            system_prompt=parent.get("system_prompt"),
            claude_permission_mode=effective_claude_permission_mode(parent),
            codex_approval_policy=parent.get("codex_approval_policy"),
            codex_sandbox_mode=parent.get("codex_sandbox_mode"),
            codex_permission_profile=parent.get("codex_permission_profile"),
            codex_approvals_reviewer=parent.get("codex_approvals_reviewer"),
            provider_jobs_access=effective_provider_jobs_access(parent),
            pinned=bool(parent.get("pinned")),
            archived=bool(parent.get("archived")),
            # Bind a native Codex fork only after the child session ID
            # exists, because manifest/jobs/terminal policy is chat-scoped.
            provider_session_id=None,
            codex_thread_id=None,
        ),
        parent_id=session_id,
        initializing_fork=True,
    )
    cleanup_state["child_session_id"] = child["id"]

    if forked_codex_thread_id:
        # Transfer crash-recovery ownership from the cleanup ledger to the
        # hidden staged child before binding. Startup can reconstruct the
        # ledger from this child if the process dies at any later point.
        await save_staged_fork_provider_reference(
            child["id"],
            forked_codex_thread_id,
        )
        if not await forget_abandoned_fork_provider_thread(
            forked_codex_thread_id
        ):
            raise RuntimeError(
                "could not commit native fork cleanup ownership transfer"
            )
        child = STORE.sessions[child["id"]]

    if str(child.get("cwd") or "") != fork_cwd:
        raise HTTPException(
            status_code=409,
            detail="the forked chat did not preserve the parent working directory",
        )

    parent_goal = parent.get("codex_goal")
    parent_goal_active = bool(
        isinstance(parent_goal, dict)
        and str(parent_goal.get("status") or "") == "active"
    )
    if parent_backend == BACKEND_CODEX and isinstance(parent_goal, dict):
        async with STORE.transaction():
            current = STORE.sessions.get(child["id"])
            if current:
                current["codex_goal"] = {
                    **parent_goal,
                    "status": "paused" if parent_goal_active else parent_goal.get("status"),
                }
                if parent.get("codex_goal_time_budget_seconds") is not None:
                    current["codex_goal_time_budget_seconds"] = parent.get(
                        "codex_goal_time_budget_seconds"
                    )
                await STORE.save()
                child = current
    if forked_codex_thread_id:
        try:
            forked_codex_thread_id, _instruction_hash = (
                await bind_forked_codex_thread(
                    child["id"],
                    forked_codex_thread_id,
                    child,
                    require_goal_support=isinstance(parent_goal, dict),
                    expected_goal=(
                        dict(parent_goal)
                        if isinstance(parent_goal, dict)
                        else None
                    ),
                )
            )
            cleanup_state["provider_thread_id"] = forked_codex_thread_id
            child = STORE.sessions[child["id"]]
        except Exception as exc:
            logger.warning(
                "codex fork policy bind failed parent_session=%s child_session=%s "
                "forked_thread=%s: %s",
                session_id,
                child["id"],
                forked_codex_thread_id,
                exc,
            )
            cleanup_is_durable = await retire_or_record_failed_codex_fork(
                forked_codex_thread_id
            )
            if not cleanup_is_durable:
                # bind_forked_codex_thread deliberately restores the provider
                # ID on the hidden staged child. Leave both cleanup references
                # intact and fail closed; the outer cancellation-safe cleanup
                # will retry and retain the child if persistence remains down.
                raise RuntimeError(
                    "failed Codex fork could not be retired or journaled"
                ) from exc
            await clear_staged_fork_provider_reference(
                child["id"],
                forked_codex_thread_id,
            )
            cleanup_state["provider_thread_id"] = None
            if live_snapshot:
                raise HTTPException(status_code=409, detail="The completed-turn fork could not be bound safely. The running chat was left unchanged.") from exc
            codex_fork_error = str(exc)
            forked_codex_thread_id = None
    ordered_sessions = await STORE.reorder(child["id"], target_id=session_id, placement="after")
    child = STORE.sessions[child["id"]]
    if parent_backend == BACKEND_CODEX and codex_fork_error:
        child["memory_seed"] = await asyncio.to_thread(
            build_fork_memory,
            parent,
            session_id,
            reason=codex_fork_error,
        )
        child["memory_seed_used"] = False
        child["memory_forked"] = True
        child["memory_fork_reason"] = codex_fork_error[:2000]
        async with STORE.transaction():
            STORE.sessions[child["id"]] = child
            await STORE.save()
    if parent_claude_session_id:
        child["fork_from"] = parent_claude_session_id
        if claude_cutoff:
            child["fork_resume_session_at"] = claude_cutoff
        async with STORE.transaction():
            STORE.sessions[child["id"]] = child
            await STORE.save()
    # Individual missing/corrupt attachments degrade to visible artifact_error
    # rows inside copy_fork_history. Any exception escaping that function is a
    # transactional clone failure, so fail closed and let the outer wrapper
    # remove both the local child and its provider fork.
    copied = await copy_fork_history(
        session_id, child["id"],
        **({"source_events": completed_snapshot} if live_snapshot else {}),
    )
    history_copy_error: str | None = None
    if forked_codex_thread_id:
        await append_event(
            child["id"],
            "provider_session",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": forked_codex_thread_id,
                "forked_from_provider_id": parent_codex_thread_id,
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    elif codex_fork_error:
        await append_event(
            child["id"],
            "history_imported",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": parent_codex_thread_id,
                "message": "The native Codex provider thread could not be forked, so this chat uses bounded rough history. The first turn will seed a fresh Codex thread with that memory.",
                "error": codex_fork_error[:4000],
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    else:
        # Claude and any future backend still need one current event so
        # copied source timestamps do not become the child's latest activity.
        await append_event(
            child["id"],
            "history_imported",
            {
                "backend": parent_backend,
                "message": (
                    "Forked "
                    f"{copied} conversation events from the parent chat. "
                ).strip(),
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    # Copied events retain source timestamps for truthful chronology. Keep the
    # newly created chat itself at the current edge of the sidebar ordering and
    # persist any memory-fork metadata after the clone completes.
    child = await STORE.update(child["id"], {})
    async with STORE.transaction():
        current = STORE.sessions.get(child["id"])
        if current is None:
            raise HTTPException(status_code=409, detail="forked session disappeared before publish")
        initializing_marker = current.pop("_fork_initializing", None)
        try:
            await STORE.save()
        except BaseException:
            # Visibility is committed by the same durable save that removes
            # the marker. Restore the in-memory fence if persistence fails so
            # list/get/turn routes cannot observe a half-published child.
            if initializing_marker is not None:
                current["_fork_initializing"] = initializing_marker
            raise
        child = current
    # The fork is now complete and durable. Cancellation after this commit
    # point must leave the usable child in place rather than rolling it back.
    cleanup_state["provider_thread_id"] = None
    cleanup_state["child_session_id"] = None
    try:
        await append_event(
            session_id,
            "session_forked",
            {
                "child_id": child["id"],
                "copied_events": copied,
                "history_copy_error": history_copy_error,
            },
        )
    except asyncio.CancelledError:
        # The child is already committed and must survive request
        # cancellation. Preserve cancellation semantics for the caller.
        raise
    except Exception:
        # A parent-side audit marker is best effort and must never roll back a
        # fully committed, independently usable child.
        logger.exception(
            "fork parent audit event failed parent_session=%s child_session=%s",
            session_id,
            child["id"],
        )
    ordered_sessions = sorted_sessions([
        session
        for session in STORE.sessions.values()
        if not session.get("_fork_initializing")
    ])
    return {"session": public_session(child), "sessions": [public_session(sess) for sess in ordered_sessions]}


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


async def run_explicit_stop_operation(
    session_id: str,
    admission_ready: asyncio.Event,
) -> dict[str, Any]:
    """Finish one explicit Stop after its short serialized admission edge.

    The operation is bounded by ``EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS``.
    A Stop whose provider interrupt or subagent teardown never settles used to
    leave its HTTP operation pending forever. On expiry that outer operation
    returns a visible timeout, while the exact inner teardown stays keyed as an
    admission/delete/restart fence until its session-wide effects are finished.
    """

    stop_task = asyncio.create_task(
        stop_turn(
            session_id,
            schedule_queue=False,
            _admission_ready=admission_ready,
        )
    )
    RUNS.detached_stop_tasks.add(stop_task)
    stop_task.add_done_callback(RUNS.detached_stop_tasks.discard)
    register_session_task(RUNS.detached_stop_tasks_by_session, session_id, stop_task)
    outer_fence_released = False
    queue_wake_scheduled = False

    def wake_queue_once() -> None:
        nonlocal queue_wake_scheduled

        if queue_wake_scheduled:
            return
        queue_wake_scheduled = True
        schedule_next_queued_turn(session_id)

    def detached_stop_finished(completed: asyncio.Task[Any]) -> None:
        if not completed.cancelled():
            with suppress(BaseException):
                completed.exception()
        # Normal completion is woken exactly once by the outer finalizer. A
        # child that outlives that outer fence owns the one wake after its
        # keyed session-wide effects have finished.
        if outer_fence_released:
            wake_queue_once()

    stop_task.add_done_callback(detached_stop_finished)
    try:
        done, _pending = await asyncio.wait(
            {stop_task},
            timeout=max(1.0, float(EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS)),
        )
        if done:
            return stop_task.result()
        logger.error(
            "explicit Stop did not finish within %.0fs session=%s; returning "
            "a timeout while teardown retains the admission fence",
            EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS,
            session_id,
        )
        with suppress(Exception):
            await append_event(session_id, "error", {
                "stop_timeout": True,
                "message": (
                    "Stop did not finish within "
                    f"{int(EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS)} seconds. "
                    "New messages remain queued and server restart waits "
                    "until the agent process finishes shutting down. Use "
                    "Force Restart if cleanup does not finish."
                ),
            })
        return {
            "ok": False,
            "stopped": False,
            "pending": True,
            "timed_out": True,
            "message": (
                "Stop is taking longer than expected; new turns remain "
                "queued while the agent process finishes shutting down. Use "
                "Force Restart if cleanup does not finish."
            ),
        }
    finally:
        # An admission exception/cancellation must also wake the endpoint;
        # otherwise it would retain the lifecycle lock while this finalizer
        # waits to acquire that same lock.
        admission_ready.set()
        # Clearing the narrow fence is itself serialized with turn admission.
        # A queued successor can therefore either observe this operation or
        # own the next turn slot, never slip between cleanup and fence release.
        # The lock wait is bounded: a wedged lifecycle lock must not keep the
        # fence alive after the operation itself has ended.
        async with bounded_lock(
            session_lifecycle_lock(session_id),
            EXPLICIT_STOP_FENCE_RELEASE_TIMEOUT_SECONDS,
        ) as lock_held:
            if not lock_held:
                logger.error(
                    "explicit Stop fence release could not take the lifecycle "
                    "lock session=%s; releasing the fence anyway",
                    session_id,
                )
            current_task = asyncio.current_task()
            if RUNS.explicit_stops.get(session_id) is current_task:
                RUNS.explicit_stops.pop(session_id, None)
        outer_fence_released = True
        # A prompt child completion is now safe to wake after the outer fence
        # is gone. A still-live child owns that one wake through its callback.
        if stop_task.done():
            wake_queue_once()

@router.post("/api/sessions/{session_id}/stop")
async def stop_turn_endpoint(session_id: str) -> dict[str, Any]:
    return await request_explicit_stop(session_id)


async def request_explicit_stop(session_id: str) -> dict[str, Any]:
    # Serialize only Stop admission with turn admission. Provider interrupts,
    # subagent teardown, and bounded terminal confirmation continue under the
    # narrower EXPLICIT_STOP_OPERATIONS fence so unrelated route reads and
    # queue writes do not wait behind slow cleanup.
    async with session_lifecycle_lock(session_id):
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        operation = RUNS.explicit_stops.get(session_id)
        if operation is None or operation.done():
            if detached_stop_in_progress(session_id):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Explicit Stop cleanup is still finishing; retry "
                        "shortly."
                    ),
                )
            admission_ready = asyncio.Event()
            operation = asyncio.create_task(
                run_explicit_stop_operation(session_id, admission_ready)
            )
            RUNS.explicit_stops[session_id] = operation
            # stop_turn sets this immediately after it has fenced the observed
            # owner under ACTIVE_LOCK. The explicit-operation entry itself
            # prevents queued promotion while the durable queue pause and all
            # provider cleanup continue outside this broad lock. It is also
            # set on admission failure so this lock can never be stranded.
            await admission_ready.wait()

    # Every waiter is observational. A renderer timeout or HTTP disconnect
    # must not cancel teardown after Stop has fenced the provider owner.
    return await asyncio.shield(operation)


async def stop_turn(
    session_id: str,
    *,
    expected_run_id: str | None = None,
    emit_event: bool = True,
    schedule_queue: bool = True,
    require_provider_turn_ready: bool = False,
    cascade_codex_subagents: bool = True,
    cascade_claude_subagents: bool = True,
    hard_terminalize_on_timeout: bool = True,
    pause_queued_turns_on_stop: bool = True,
    preserve_active_goal: bool = False,
    _admission_ready: asyncio.Event | None = None,
) -> dict[str, Any]:
    deferred = False
    native_interrupt_reserved = False
    native_control_interrupt_reserved = False
    native_claude_interrupt_reserved = False
    transition_ready: asyncio.Event | None = None
    owned_tasks: list[asyncio.Task[Any]] = []
    stopping_run_id: str | None = None
    stopping_purpose = ""
    stopping_control_reservation_id = ""
    stopping_control_thread_id = ""
    stopping_admission_id = ""
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        busy = session_id in RUNS.busy
        current_turn = RUNS.current_turns.get(session_id) or {}
        expected_run_id = str(expected_run_id or "").strip() or None
        if expected_run_id is not None:
            active_run_id = str((active or {}).get("run_id") or "")
            current_run_id = str(current_turn.get("run_id") or "")
            if (
                not busy
                or expected_run_id not in {active_run_id, current_run_id}
                or any(
                    owner_id not in {"", expected_run_id}
                    for owner_id in (active_run_id, current_run_id)
                )
            ):
                if _admission_ready is not None:
                    _admission_ready.set()
                return {
                    "ok": True,
                    "stopped": False,
                    "pending": False,
                    "superseded": True,
                    "message": "The requested run no longer owns this chat.",
                }
        if (STORE.sessions.get(session_id) or {}).get("gemini_cleanup_pending_run_id"):
            if _admission_ready is not None:
                _admission_ready.set()
            return {"ok": False, "stopped": False, "pending": True,
                    "message": "Retired provider cleanup was not confirmed; verify the old runtime on the server."}
        if preserve_active_goal and codex_goal_followup_requires_native(
            STORE.sessions.get(session_id) or {}, active or {}, current_turn,
        ):
            # Force Send may have passed its non-goal probe before the
            # provider created a goal. Reject at the final, synchronous Stop
            # admission boundary, before closing authority or marking a run
            # stopped. Its caller restores the selected row in place.
            if _admission_ready is not None:
                _admission_ready.set()
            raise HTTPException(
                status_code=409,
                detail=force_send_conflict_detail(
                    session_id, "",
                    guard="active_goal_requires_native_steer",
                    message="A Codex goal became active before Force Send could stop the turn. The message remains queued; the goal was not paused.",
                    action="Retry the follow-up through the active goal's native steering lane.",
                    retryable=True,
                ),
            )
        if active:
            if require_provider_turn_ready and not (
                active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                and active.get("codex_child_continuation_waiting") is True
                and isinstance(active.get("codex_child_continuation_stop"), asyncio.Event)
            ) and (
                not active.get("provider_turn_ready")
                or (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and not active.get("provider_session_id")
                )
            ):
                deferred = True
            else:
                active["stop_requested"] = True
                child_stop = active.get("codex_child_continuation_stop")
                if isinstance(child_stop, asyncio.Event):
                    child_stop.set()
                if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
                    active["claude_permissions_open"] = False
                if active.get("run_id"):
                    stopping_run_id = str(active["run_id"])
                    RUNS.stopped_runs.add(stopping_run_id)
                transition_value = active.get("logical_transition_ready")
                if isinstance(transition_value, asyncio.Event):
                    transition_ready = transition_value
                native_turn = active.get("codex_app_server_turn")
                if (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and native_turn is not None
                    and getattr(native_turn, "turn_id", "")
                    and getattr(native_turn, "_completed", False) is not True
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_interrupt_reserved = True
                elif (
                    active.get("transport") == CODEX_TRANSPORT_APP_SERVER
                    and active.get("codex_native_operation")
                    and active.get("provider_thread_id")
                    and active.get("provider_turn_id")
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_control_interrupt_reserved = True
                elif (
                    active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
                    and active.get("claude_sdk_run") is not None
                    and not active.get("native_interrupt_sent")
                ):
                    active["native_interrupt_sent"] = True
                    native_claude_interrupt_reserved = True
        elif busy:
            if require_provider_turn_ready:
                deferred = True
            else:
                stopping_run_id = str(
                    current_turn.get("run_id") or ""
                ) or None
                if expected_run_id is not None and stopping_run_id:
                    # A targeted cancellation is run-scoped. A session-wide
                    # startup marker could otherwise stop a successor if the
                    # expected launch loses its slot before binding ACTIVE.
                    RUNS.stopped_runs.add(stopping_run_id)
                else:
                    RUNS.stop_requests.add(session_id)
        # Identity of the reservation this Stop observed. A Stop that outlives
        # its deadline runs detached; it must never act on a successor turn
        # admitted after the fence was released.
        stopping_admission_id = str(
            current_turn.get("_server_restart_admission_id") or ""
        )
        stopping_control_reservation_id = str(
            (active or {}).get("codex_control_reservation_id")
            or current_turn.get("codex_control_reservation_id")
            or ""
        ).strip()
        stopping_control_thread_id = str(
            (active or {}).get("provider_thread_id")
            or (active or {}).get("provider_session_id")
            or ""
        ).strip()
        stopping_purpose = str(
            current_turn.get("purpose")
            or (RUNS.run_metadata.get(stopping_run_id or "") or {}).get("purpose")
            or ""
        )
        current_task = asyncio.current_task()
        owned_tasks = [
            task
            for task in (
                *tuple(RUNS.turn_tasks.get(session_id) or ()),
                *tuple(
                    task
                    for (task_session_id, _operation_id), task in
                    CODEX_NATIVE_ACTION_TASKS.items()
                    if task_session_id == session_id
                ),
                active.get("owner_task") if active else None,
            )
            if (
                isinstance(task, asyncio.Task)
                and task is not current_task
                and not task.done()
            )
        ]
    # The observed owner is now fenced synchronously under ACTIVE_LOCK.  The
    # explicit-operation entry blocks replacement admission, so release the
    # broad lifecycle lock before any durable queue IO or provider cleanup.
    if _admission_ready is not None:
        _admission_ready.set()
    subagent_stop = empty_subagent_stop_result()
    session = STORE.sessions.get(session_id) or {}
    codex_manager = existing_codex_app_server_manager(session)
    pause_queued_successors = bool(
        pause_queued_turns_on_stop
        and stopping_purpose != "scheduled_job"
    )

    def schedule_unpaused_queue_after_release(released: bool = True) -> None:
        if released and schedule_queue and not pause_queued_successors:
            schedule_next_queued_turn(session_id)

    async def release_stopping_slot(
        expected_run_id: str | None,
        *,
        allow_prebind: bool = False,
    ) -> bool:
        """Release only the owner Stop observed at admission time."""

        if stopping_control_reservation_id:
            return await release_codex_control_slot(
                session_id,
                expected_thread_id=stopping_control_thread_id,
                expected_reservation_id=stopping_control_reservation_id,
                allow_prebind=allow_prebind,
            )
        return await release_turn_slot(
            session_id,
            expected_run_id=expected_run_id,
        )

    async def settle_stopped_claude_subagents() -> None:
        """Fence surviving Claude children only after this parent is idle."""

        nonlocal subagent_stop
        if (
            not cascade_claude_subagents
            or str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE
        ):
            return
        owns_maintenance = False
        async with RUNS.active_lock:
            session_idle = (
                session_id not in RUNS.active
                and session_id not in RUNS.busy
            )
            if session_idle:
                already_reserved = session_id in RUNS.maintenance
                owns_maintenance = (
                    not already_reserved
                    or session_id in CLAUDE_STOP_FENCE_SESSIONS
                )
                if owns_maintenance:
                    RUNS.maintenance.add(session_id)
        if not session_idle or not owns_maintenance:
            return
        # Cancellation anywhere after this point must leave update admission
        # excluded unless the durable child-stop marker confirms completion.
        subagent_stop["fence_committed"] = False
        retry_required = False
        try:
            subagent_stop = await stop_idle_claude_background_subagents_bounded(
                session_id,
                emit_event=emit_event,
            )
        finally:
            fence_committed = bool(subagent_stop.get("fence_committed", False))
            async with RUNS.active_lock:
                if fence_committed:
                    CLAUDE_STOP_FENCE_SESSIONS.discard(session_id)
                    RUNS.maintenance.discard(session_id)
                    discard_unused_claude_stop_fence_attempt_lock(session_id)
                else:
                    CLAUDE_STOP_FENCE_SESSIONS.add(session_id)
                    RUNS.maintenance.add(session_id)
                    retry_required = True
            if retry_required:
                schedule_claude_stop_fence_retry(session_id)

    if deferred:
        return {
            "ok": True,
            "stopped": False,
            "deferred": True,
            "message": "The provider had not accepted the current turn yet, so it was left running.",
        }
    if pause_queued_successors:
        await pause_queued_turns_after_explicit_stop(session_id)
    if not deferred and stopping_run_id:
        # ACTIVE.stop_requested / STOPPED_RUNS already closes provider helper
        # authority synchronously. Remove the capability record outside the
        # broad lifecycle-lock admission edge.
        await revoke_provider_authority(stopping_run_id)
    root_thread_id = str(
        (active or {}).get("provider_thread_id")
        or (active or {}).get("provider_session_id")
        or session_provider_id(session)
        or ""
    ).strip()
    if (
        cascade_codex_subagents
        and not active
        and str(session.get("backend") or "") == BACKEND_CODEX
        and root_thread_id
        and codex_manager is not None
    ):
        subagent_stop = await stop_codex_descendant_subagents(
            session_id,
            root_thread_id,
            manager=codex_manager,
        )
    if not active and not busy:
        await settle_stopped_claude_subagents()
    # ACTIVE is now fenced with stop_requested under the same lock consulted
    # by provider permission callbacks. Cancel existing requests only after
    # that fence, so no callback can register in the old cancel-before-stop
    # gap and survive this Stop operation.
    await cancel_codex_interactions(session_id, resolution="turn_stopped")
    await cancel_claude_interactions(session_id, resolution="turn_stopped")
    if not active:
        if busy:
            deadline = (
                asyncio.get_running_loop().time()
                + STOP_CONFIRM_TIMEOUT_SECONDS
            )
            current_turn: dict[str, Any] = {}
            pending = set(owned_tasks)
            while True:
                current_task = asyncio.current_task()
                async with RUNS.active_lock:
                    bound_active = RUNS.active.get(session_id)
                    still_busy = session_id in RUNS.busy
                    current_turn = dict(RUNS.current_turns.get(session_id) or {})
                    reservation_replaced = bool(
                        still_busy
                        and stopping_admission_id
                        and str(current_turn.get("_server_restart_admission_id") or "")
                        != stopping_admission_id
                    )
                    pending = {
                        task
                        for task in RUNS.turn_tasks.get(session_id) or ()
                        if task is not current_task and not task.done()
                    } | {
                        task
                        for (task_session_id, _operation_id), task in
                        CODEX_NATIVE_ACTION_TASKS.items()
                        if (
                            task_session_id == session_id
                            and task is not current_task
                            and not task.done()
                        )
                    }
                if reservation_replaced:
                    # A successor turn owns the chat: the reservation this Stop
                    # observed is gone. Leave the new turn untouched.
                    logger.info(
                        "explicit Stop superseded by a new turn session=%s",
                        session_id,
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "superseded": True,
                        "subagents": subagent_stop,
                    }
                if bound_active:
                    # The runner bound while Stop was waiting. Re-enter through
                    # the normal provider-interrupt path instead of recording a
                    # false terminal event.
                    return await stop_turn(
                        session_id,
                        expected_run_id=expected_run_id,
                        emit_event=emit_event,
                        schedule_queue=schedule_queue,
                        require_provider_turn_ready=require_provider_turn_ready,
                        cascade_codex_subagents=cascade_codex_subagents,
                        cascade_claude_subagents=cascade_claude_subagents,
                        hard_terminalize_on_timeout=hard_terminalize_on_timeout,
                        pause_queued_turns_on_stop=False,
                    )
                if not still_busy:
                    await settle_stopped_claude_subagents()
                    goal_result = await settle_idle_codex_goal_for_stop(
                        session_id
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "subagents": subagent_stop,
                        **goal_result,
                    }
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                if pending:
                    await asyncio.wait(
                        pending,
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(min(0.01, remaining))

            if pending:
                if (
                    hard_terminalize_on_timeout
                    and str(session.get("backend") or "") in {BACKEND_CODEX, BACKEND_CLAUDE}
                ):
                    backend = str(session.get("backend") or "")
                    run_id = str(current_turn.get("run_id") or "") or None
                    if run_id:
                        RUNS.stopped_runs.add(run_id)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(
                            pending,
                            timeout=min(1.0, STOP_CONFIRM_TIMEOUT_SECONDS),
                        )
                    if backend == BACKEND_CLAUDE:
                        await evict_claude_sdk_chat(
                            session_id,
                            force=True,
                        )
                    released = await release_stopping_slot(
                        run_id,
                        allow_prebind=True,
                    )
                    if not released and stopping_control_reservation_id:
                        # The cancelled acquisition may have released its own
                        # exact reservation while Stop was joining the task.
                        # Count that as this Stop's release only if the chat is
                        # still idle; a promoted replacement must remain owned.
                        async with RUNS.active_lock:
                            released = not RUNS.holds_turn(session_id)
                    await settle_stopped_claude_subagents()
                    if emit_event and released:
                        await append_event(session_id, "turn_stopped", {
                            "run_id": run_id,
                            "backend": backend,
                            "message": (
                                "Stopped while the agent process was "
                                "still starting."
                            ),
                            "hard_stop": True,
                        })
                    RUNS.run_metadata.pop(run_id, None) if run_id else None
                    if run_id and all(task.done() for task in pending):
                        RUNS.stopped_runs.discard(run_id)
                    schedule_unpaused_queue_after_release(released)
                    goal_result = (
                        await settle_idle_codex_goal_for_stop(session_id)
                        if backend == BACKEND_CODEX
                        else {}
                    )
                    return {
                        "ok": True,
                        "stopped": True,
                        "pending": False,
                        "hard_stop": True,
                        "subagents": subagent_stop,
                        **goal_result,
                    }
                # Keep the deferred STOP_REQUESTS marker. A legitimate slow
                # launch owns this reservation and will reconcile the stop as
                # soon as it binds; do not cancel it mid-pin or mid-handshake.
                return {
                    "ok": True,
                    "stopped": False,
                    "pending": True,
                    "message": (
                        "Stop is waiting for the agent process to finish "
                        "starting."
                    ),
                }
            # No request or runner owns this BUSY reservation after the grace
            # period, so it is an orphan and can be safely released.
            run_id = str(current_turn.get("run_id") or "") or None
            released = await release_stopping_slot(
                run_id,
                allow_prebind=True,
            )
            await settle_stopped_claude_subagents()
            if run_id:
                RUNS.run_metadata.pop(run_id, None)
            if emit_event and released:
                await append_event(session_id, "turn_stopped", {
                    "run_id": run_id,
                    "message": "Stopped before the agent process was ready.",
                })
            schedule_unpaused_queue_after_release(released)
            goal_result = await settle_idle_codex_goal_for_stop(session_id)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "subagents": subagent_stop,
                **goal_result,
            }
        schedule_unpaused_queue_after_release()
        # The requested terminal state is already satisfied. Returning
        # stopped=True also lets clients clear a stale local running marker.
        goal_result = await settle_idle_codex_goal_for_stop(session_id)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "subagents": subagent_stop,
            **goal_result,
        }
    proc = active.get("proc") if active else None
    native_turn = active.get("codex_app_server_turn") if active else None
    active_run_id = str(active.get("run_id") or "") or None
    goal_pause_ok, goal_paused, goal_pause_error = (
        await pause_active_codex_goal_for_stop(session_id, active)
    )

    async def terminal_goal_pause_failure_result(
        native_interrupt: bool,
    ) -> dict[str, Any]:
        thread_id = str(
            active.get("provider_thread_id")
            or active.get("provider_session_id")
            or session_provider_id(STORE.sessions.get(session_id) or {})
            or ""
        ).strip()
        fenced = await quarantine_codex_goal_thread(
            session_id,
            thread_id,
            run_id=str(active.get("run_id") or "") or None,
            reason=goal_pause_error
            or "Codex goal reconciliation failed while stopping the turn.",
        )
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "native_interrupt": native_interrupt,
            "goal_paused": False,
            "goal_fenced": fenced,
            "subagents": subagent_stop,
            "message": (
                "Turn stopped. The stale Codex goal thread was fenced and "
                "will not be reused."
            ),
        }

    async def hard_terminalize_active_stop(
        *,
        native_interrupt: bool,
        reason: str,
        tasks: set[asyncio.Task[Any]],
    ) -> dict[str, Any]:
        """Bound explicit Stop even if a managed provider consumer is stale."""

        run_id = str(active.get("run_id") or "") or None
        if run_id:
            RUNS.stopped_runs.add(run_id)
        current_task = asyncio.current_task()
        owned = {
            task
            for task in tasks
            if task is not current_task and not task.done()
        }
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.wait(
                owned,
                timeout=min(1.0, STOP_CONFIRM_TIMEOUT_SECONDS),
            )
        is_claude_sdk = (
            active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK
        )
        is_codex_app_server = bool(
            active.get("backend") == BACKEND_CODEX
            and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
        )
        # A natural terminal notification may have won the race with local
        # cancellation. In that case its runner already emitted the terminal
        # row and released the reservation, so do not manufacture a duplicate.
        async with RUNS.active_lock:
            still_owned = (
                RUNS.active.get(session_id) is active
                and session_id in RUNS.busy
            )
        if not still_owned:
            if run_id and all(task.done() for task in owned):
                RUNS.stopped_runs.discard(run_id)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }

        if is_claude_sdk:
            # Scope force teardown to the exact supervisor generation. A
            # delayed Stop must not evict a replacement that bound while its
            # old runner was unwinding.
            ownership_token = str(
                active.get("claude_sdk_owner_token") or ""
            )
            if ownership_token:
                await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    ownership_token=ownership_token,
                )

        fenced = False
        if is_codex_app_server:
            thread_id = str(
                active.get("provider_thread_id")
                or active.get("provider_session_id")
                or session_provider_id(STORE.sessions.get(session_id) or {})
                or ""
            ).strip()
            fenced = await quarantine_codex_goal_thread(
                session_id,
                thread_id,
                run_id=run_id,
                reason=reason,
            )
        released = await release_stopping_slot(
            run_id,
        )
        await settle_stopped_claude_subagents()
        active_run = (STORE.sessions.get(session_id) or {}).get("active_run")
        should_emit_terminal = bool(
            emit_event
            and released
            and (
                not run_id
                or (
                    isinstance(active_run, dict)
                    and str(active_run.get("run_id") or "") == run_id
                )
            )
        )
        if should_emit_terminal:
            await append_event(session_id, "turn_stopped", {
                "run_id": run_id,
                "backend": active.get("backend"),
                "native_interrupt": native_interrupt,
                "goal_fenced": fenced,
                "hard_stop": True,
                "message": (
                    "Stopped after the provider did not acknowledge interruption in time."
                ),
            })
        if run_id:
            RUNS.run_metadata.pop(run_id, None)
            if all(task.done() for task in owned):
                RUNS.stopped_runs.discard(run_id)
        schedule_unpaused_queue_after_release(released)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "hard_stop": True,
            "native_interrupt": native_interrupt,
            "goal_paused": goal_paused,
            "goal_fenced": fenced,
            "subagents": subagent_stop,
            "message": (
                "Turn stopped. The unresponsive Claude runner was retired."
                if active.get("backend") == BACKEND_CLAUDE
                else (
                    "Turn stopped. The stale Codex thread was fenced and will "
                    "not be reused."
                    if is_codex_app_server
                    else "Turn stopped. The unresponsive provider runner was retired."
                )
            ),
        }

    native_interrupt = False
    if native_interrupt_reserved and native_turn is not None:
        try:
            await native_turn.interrupt()
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Codex app-server interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    elif native_control_interrupt_reserved and codex_manager is not None:
        try:
            await codex_manager.request(
                "turn/interrupt",
                {
                    "threadId": str(active.get("provider_thread_id") or ""),
                    "turnId": str(active.get("provider_turn_id") or ""),
                },
            )
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Codex native control interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    elif native_claude_interrupt_reserved:
        claude_sdk_run = active.get("claude_sdk_run")
        try:
            await interrupt_claude_sdk_run_bounded(
                claude_sdk_run,
            )
            native_interrupt = True
        except Exception as exc:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            logger.warning(
                "Claude Agent SDK interrupt failed session=%s error=%s",
                session_id,
                exc,
            )
    if (
        cascade_codex_subagents
        and str(session.get("backend") or "") == BACKEND_CODEX
        and active.get("transport") == CODEX_TRANSPORT_APP_SERVER
        and root_thread_id
        and codex_manager is not None
    ):
        subagent_stop = await stop_codex_descendant_subagents(
            session_id,
            root_thread_id,
            manager=codex_manager,
        )
    # The app-server process may serve multiple Codex chats and must never be
    # terminated as a per-chat Stop action.
    if proc and active.get("transport") != CODEX_TRANSPORT_APP_SERVER:
        await terminate_process_tree(proc)
    if transition_ready is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(transition_ready.wait()),
                timeout=STOP_CONFIRM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            async with RUNS.active_lock:
                current = RUNS.active.get(session_id)
                if current is active:
                    current["native_interrupt_sent"] = False
            if (
                hard_terminalize_on_timeout
                and active.get("transport")
                in HARD_TERMINALIZABLE_TURN_TRANSPORTS
            ):
                return await hard_terminalize_active_stop(
                    native_interrupt=native_interrupt,
                    reason=(
                        goal_pause_error
                        or "Provider steering transition did not settle after Stop."
                    ),
                    tasks=set(owned_tasks),
                )
            return {
                "ok": True,
                "stopped": False,
                "pending": True,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
                "message": (
                    goal_pause_error
                    or "Stop was sent; waiting for the steering transition to settle."
                ),
            }
    deadline = (
        asyncio.get_running_loop().time()
        + STOP_CONFIRM_TIMEOUT_SECONDS
    )
    dynamic_tasks = set(owned_tasks)
    while True:
        current_task = asyncio.current_task()
        async with RUNS.active_lock:
            current = RUNS.active.get(session_id)
            still_busy = session_id in RUNS.busy
            dynamic_tasks = {
                task
                for task in RUNS.turn_tasks.get(session_id) or ()
                if task is not current_task and not task.done()
            } | {
                task
                for (task_session_id, _operation_id), task in
                CODEX_NATIVE_ACTION_TASKS.items()
                if (
                    task_session_id == session_id
                    and task is not current_task
                    and not task.done()
                )
            }
            owner_task = active.get("owner_task")
            if (
                isinstance(owner_task, asyncio.Task)
                and owner_task is not current_task
                and not owner_task.done()
            ):
                dynamic_tasks.add(owner_task)
        if current is not active:
            if not goal_pause_ok:
                return await terminal_goal_pause_failure_result(native_interrupt)
            await settle_stopped_claude_subagents()
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }
        if not still_busy:
            if not goal_pause_ok:
                released = await release_stopping_slot(
                    active_run_id,
                )
                schedule_unpaused_queue_after_release(released)
                return await terminal_goal_pause_failure_result(native_interrupt)
            released = await release_stopping_slot(
                active_run_id,
            )
            await settle_stopped_claude_subagents()
            schedule_unpaused_queue_after_release(released)
            return {
                "ok": True,
                "stopped": True,
                "pending": False,
                "native_interrupt": native_interrupt,
                "goal_paused": goal_paused,
                "subagents": subagent_stop,
            }
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        if dynamic_tasks:
            await asyncio.wait(
                dynamic_tasks,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            await asyncio.sleep(min(0.01, remaining))

    if not dynamic_tasks:
        if not goal_pause_ok:
            released = await release_stopping_slot(
                active_run_id,
            )
            if emit_event and released:
                await append_event(session_id, "turn_stopped", {
                    "run_id": active.get("run_id"),
                    "backend": active.get("backend"),
                    "native_interrupt": native_interrupt,
                    "goal_fenced": True,
                })
            schedule_unpaused_queue_after_release(released)
            return await terminal_goal_pause_failure_result(native_interrupt)
        # No request or runner still owns this ACTIVE record. It is stale and
        # safe to clear after the bounded confirmation window.
        released = await release_stopping_slot(
            active_run_id,
        )
        await settle_stopped_claude_subagents()
        if emit_event and released:
            await append_event(session_id, "turn_stopped", {
                "run_id": active.get("run_id"),
                "backend": active.get("backend"),
                "native_interrupt": native_interrupt,
            })
        schedule_unpaused_queue_after_release(released)
        return {
            "ok": True,
            "stopped": True,
            "pending": False,
            "native_interrupt": native_interrupt,
            "goal_paused": goal_paused,
            "subagents": subagent_stop,
        }

    # A provider interrupt is an acknowledgement, not terminal state. Leave
    # the chat active and permit a later Stop click to retry the interrupt
    # instead of rendering a false "Turn Stopped" row.
    async with RUNS.active_lock:
        current = RUNS.active.get(session_id)
        if current is active:
            current["native_interrupt_sent"] = False
    if (
        hard_terminalize_on_timeout
        and active.get("transport") in HARD_TERMINALIZABLE_TURN_TRANSPORTS
    ):
        return await hard_terminalize_active_stop(
            native_interrupt=native_interrupt,
            reason=(
                goal_pause_error
                or "The provider did not confirm the requested Stop before timeout."
            ),
            tasks=dynamic_tasks,
        )
    return {
        "ok": True,
        "stopped": False,
        "pending": True,
        "native_interrupt": native_interrupt,
        "goal_paused": goal_paused,
        "message": (
            goal_pause_error
            or "Stop was sent; waiting for the provider to finish stopping."
        ),
    }


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


async def list_session_jobs(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    jobs = sorted(
        (public_job(job) for job in JOBS.jobs.values() if job.get("session_id") == session_id),
        key=lambda job: job.get("updated_at") or "",
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


async def get_session_job_runs(
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
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await asyncio.to_thread(
            read_scheduled_job_runs,
            session_id,
            job_id,
            timeline_group_id=timeline_group_id,
            before_seq=before_seq,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc


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
EXTRACTED_MODULES: tuple[types.ModuleType, ...] = (sys.modules['hyadex.core.config'], sys.modules['hyadex.core.paths'], sys.modules['hyadex.core.identity'], sys.modules['hyadex.providers.codex.config'], sys.modules['hyadex.providers.instructions'], sys.modules['hyadex.core.util'], sys.modules['hyadex.core.server_logging'], sys.modules['hyadex.core.state'], sys.modules['hyadex.providers.claude.history'], sys.modules['hyadex.features.titles'], sys.modules['hyadex.features.usage'], sys.modules['hyadex.core.processes'], sys.modules['hyadex.core.sessions'], sys.modules['hyadex.providers.codex.history'], sys.modules['hyadex.providers.authority'], sys.modules['hyadex.core.event_log'], sys.modules['hyadex.features.terminal'], sys.modules['hyadex.features.history'], sys.modules['hyadex.features.files'], sys.modules['hyadex.providers.subagents'], sys.modules['hyadex.providers.claude.runtime'], sys.modules['hyadex.features.side_questions'], sys.modules['hyadex.providers.registry'], sys.modules['hyadex.core.queue'], sys.modules['hyadex.providers.codex.runtime'], sys.modules['hyadex.core.http'], sys.modules['hyadex.features.emergency'], sys.modules['hyadex.core.turns'], )


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
