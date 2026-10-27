"""Session create/update/fork compiled from production source with synthetic state."""
from __future__ import annotations
import ast
import asyncio
from contextlib import suppress
import copy
import json
import re
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, Literal
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
import local_session_ownership
from tests import test_codex_subagent_config_isolated as config_fixture
from hyadex.runs import NamespaceRuns
from tests import server_source

SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
FUNCTIONS = {"retired_session_runtime_message", "preview_session_runtime_update", "session_backend_locked", "public_session", "title_runtime_key",
    "session_subagent_limit_control", "validate_session_subagent_limit",
    "record_codex_subagent_limit_application",
    "create_session", "create_session_with_history", "update_session", "_update_session", "ensure_backend_update_allowed", "codex_runtime_settings", "_fork_session_locked"}
MODELS = {"CreateSessionRequest", "UpdateSessionRequest"}
tree = ast.parse(server_source.text())
nodes = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
        node.decorator_list = []
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name in MODELS:
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "SessionStore":
        node.body = [method for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and method.name in {"create", "update", "transaction"}]
        nodes.append(node)
assert {node.name for node in nodes} == FUNCTIONS | MODELS | {"SessionStore"}
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",
    names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])), str(SOURCE), "exec")


def make_namespace(root: Path):
    """Reusable native UI fixture: real routes/store methods, synthetic helpers."""
    locks = {}
    registry = local_session_ownership.Registry(root)
    ns = {"Any": Any, "Literal": Literal, "BaseModel": BaseModel, "Field": Field,
        "HISTORY_MAINTENANCE_STATE": {},
        "cancel_generated_session_title": Mock(),
        "local_session_ownership": local_session_ownership,
        "local_history_import_guard": lambda: local_session_ownership.history_import_lock(registry=registry),
        "other_local_instance_provider_keys": lambda: local_session_ownership.other_instance_provider_keys(root, registry=registry),
        "read_native_session_title": Mock(return_value=None),
        "Path": Path, "asyncio": asyncio, "suppress": suppress, "uuid": uuid, "json": json, "re": re,
        "RUNTIME_DIAGNOSTICS": {"claude": {"version": "2.1.277"}},
        "RUNTIME_DIAGNOSTICS_LOCK": threading.RLock(),
        "SERVER_INSTANCE_ID": "owned-server-instance",
        "existing_codex_app_server_manager": lambda session: None,
        "HTTPException": HTTPException,
        "REMOVED_PROVIDER_MESSAGE": "This provider was removed.",
        "REMOVED_ENDPOINT_MESSAGE": "Custom API endpoints were removed.",
        "MAX_SESSION_SYSTEM_PROMPT_CHARS": 10000, "DEFAULT_BACKEND": "codex",
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude",
        "VALID_BACKENDS": {"codex", "claude"}, "DEFAULT_CWD": str(root),
        "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
        "normalize_runtime_effort_for_model": lambda backend, model, effort, **kwargs: effort,
        "normalize_runtime_effort": lambda backend, effort, **kwargs: effort,
        "ensure_codex_thread_not_pending_fork_cleanup": lambda value: None,
        "ensure_dirs": lambda session: None, "now_iso": lambda: "2026-09-18T00:00:00Z",
        "clean_session_system_prompt": lambda value: value, "append_event": AsyncMock(),
        "CLAUDE_DEFAULT_PERMISSION_MODE": "default", "CLAUDE_PERMISSION_MODES": {"default"},
        "CODEX_DEFAULT_APPROVAL_POLICY": "never", "CODEX_APPROVAL_POLICIES": {"never"},
        "CODEX_DEFAULT_SANDBOX_MODE": "read-only", "CODEX_SANDBOX_MODES": {"read-only"},
        "CODEX_DEFAULT_PERMISSION_PROFILE": None, "CODEX_DEFAULT_APPROVALS_REVIEWER": "user",
        "CODEX_APPROVAL_REVIEWERS": {"user"}, "PROVIDER_JOBS_ACCESS_DEFAULT": "full",
        "PROVIDER_JOBS_ACCESS_MODE_SET": {"full", "read_only", "blocked"},
        "session_section_key": lambda session: ("folder", session.get("folder", "General")),
        "emergency_summary": lambda session: (None, 0), "effective_provider_jobs_access": lambda session: "full",
        "CLAUDE_STOP_FENCE_SESSIONS": set(), "codex_goal_time_budget_is_exhausted": lambda session: False,
        "HISTORY_SEARCH_DIRTY": set(), "logger": Mock(), "concise_error_message": lambda value: "safe fixture error",
        "session_provider_id": lambda session: session.get("session_id"), "import_session_history": AsyncMock(),
        "ensure_session_not_initializing": lambda session: None,
        "SESSION_LIFECYCLE_UPDATE_FIELDS": {"backend", "model", "effort", "archived"},
        "session_lifecycle_lock": lambda session: locks.setdefault(session, asyncio.Lock()),
        "ensure_claude_permission_mode_update_allowed": AsyncMock(),
        "ACTIVE_LOCK": asyncio.Lock(), "ACTIVE": {}, "BUSY_SESSIONS": set(), "SESSION_TURN_TASKS": {},
        "session_codex_thread_id": lambda session: session.get("codex_thread_id") or session.get("session_id") or "",
        "codex_user_config_defaults": lambda: ("normal-model", "high", "fast"),
        "CODEX_DEFAULT_MODEL": "normal-model", "CODEX_DEFAULT_EFFORT": "high",
        "clamp_codex_runtime_effort": lambda model, effort: effort,
        "codex_available_model": lambda model: model,
        "codex_default_service_tier": lambda model: "fast",
        "CODEX_SETTINGS_FILE": root / "codex-settings.json", "CODEX_NONINTERACTIVE_APPROVAL_POLICY": "never",
        "CODEX_PROVIDER_MCP_NAME": "fixture", "codex_provider_mcp_config": lambda: {},
        "codex_app_server_service_tier": lambda value: value,
        "codex_thread_instructions": lambda *args: "synthetic instructions", "codex_goals_cli_args": lambda: [],
        "CODEX_BIN": "unused-codex"}
    ns["RUNS"] = NamespaceRuns(ns)
    exec(config_fixture.code, ns)
    # Hyadex's enabled-backend gate is covered in test_hyadex_server.
    ns.setdefault("require_enabled_backend", lambda backend: backend)
    ns["RUNS"] = NamespaceRuns(ns)
    exec(CODE, ns)
    for name in MODELS:
        ns[name].model_rebuild(_types_namespace=ns)
    for name, model in (("create_session", "CreateSessionRequest"), ("update_session", "UpdateSessionRequest")):
        ns[name].__annotations__["req"] = ns[model]
    store = ns["SessionStore"]()
    store.sessions = {}
    store._lock = asyncio.Lock()
    store.top_order_for_section = lambda *args, **kwargs: 1000
    async def save():
        (root / "sessions.json").write_text(json.dumps(store.sessions))
    store.save = AsyncMock(side_effect=save)
    async def persist_restored_state(**kwargs):
        await save()
    store.persist_restored_state = AsyncMock(side_effect=persist_restored_state)
    ns["STORE"] = store
    return ns
