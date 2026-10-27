"""Codex details kept on chat records: threads, compaction terminals, app-server environment."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import ipaddress
import json
import os
import threading
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    CODEX_BIN,
    CODEX_FALLBACK_SERVICE_TIERS,
    CODEX_SESSION_INDEX_PATH,
    DEFAULT_BACKEND,
    MAX_CODEX_SESSION_INDEX_BYTES,
    MAX_CODEX_SESSION_INDEX_LINES,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    SERVER_ROOT,
)
from hyadex.core.server import SERVER
from hyadex.core.util import normalized_network_address, runner_env
from hyadex.providers.claude.history import (
    native_session_title,
    provider_session_identifier,
    session_provider_id,
)
from hyadex.providers.codex.app_server import CodexAppServerManager
from hyadex.providers.runtime import scrub_provider_runtime_environment
from hyadex.providers.state import PROVIDERS


def codex_provider_mcp_connection_host(bind_address: str | None = None) -> str:
    """Resolve a reachable same-host MCP address for every supported bind."""

    configured = str(
        SERVER.bind_address if bind_address is None else bind_address
    ).strip()
    if configured.lower() == "localhost":
        return "127.0.0.1"
    address = normalized_network_address(configured)
    if address.is_unspecified:
        return "::1" if isinstance(address, ipaddress.IPv6Address) else "127.0.0.1"
    return str(address)


# Set only by the independently managed execution entrypoint, before startup.
# Public bind/port remain unchanged for native client discovery.
PROVIDER_CALLBACK_ORIGIN: str | None = None


def provider_helper_server_origin() -> str:
    """Return the exact same-host origin used by provider helper transports."""

    if PROVIDER_CALLBACK_ORIGIN is not None:
        return PROVIDER_CALLBACK_ORIGIN
    host = codex_provider_mcp_connection_host()
    url_host = (
        f"[{host}]"
        if isinstance(normalized_network_address(host), ipaddress.IPv6Address)
        else host
    )
    return f"http://{url_host}:{SERVER.port}"


def add_provider_no_proxy_environment(
    environment: dict[str, str],
    host: str,
) -> None:
    """Keep the process-secret same-host transport out of ambient proxies."""

    required = [host]
    if ":" in host:
        required.append(f"[{host}]")
    for name in ("NO_PROXY", "no_proxy"):
        existing = [
            item.strip()
            for item in str(environment.get(name) or "").split(",")
            if item.strip()
        ]
        for item in required:
            if item not in existing:
                existing.append(item)
        environment[name] = ",".join(existing)


CODEX_RETIRED_APP_SERVER_MANAGERS: list[CodexAppServerManager] = []
CODEX_SESSION_APP_SERVER_MANAGERS: dict[str, CodexAppServerManager] = {}


CODEX_THREAD_SESSION_INDEX: dict[str, str] = {}


CODEX_SUBAGENT_SESSION_INDEX: dict[str, str] = {}
CODEX_SUBAGENT_STATE: dict[str, dict[str, Any]] = {}
# Runtime-only proof that an active child state came from the current provider
# generation. Persisted "running" cards are historical hints, not evidence
# that work survived an HyadexServer/app-server restart.
CODEX_SUBAGENT_LIVE_GENERATIONS: dict[str, int] = {}
CODEX_SUBAGENT_LIVE_MANAGERS: dict[str, Any] = {}
# Timeline indexes are built in worker threads while provider notifications
# update these maps on the event loop. Protect compound mutations and snapshots
# so a semantic-page rebuild never iterates a dictionary that is changing.
CODEX_SUBAGENT_INDEX_LOCK = threading.RLock()


CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT = 128


ABANDONED_FORK_PROVIDER_THREADS: set[str] = set()


def ensure_codex_thread_not_pending_fork_cleanup(thread_id: Any) -> None:
    """Fence provider identities while post-crash fork cleanup owns them."""

    clean_thread_id = str(thread_id or "").strip()
    if clean_thread_id and clean_thread_id in ABANDONED_FORK_PROVIDER_THREADS:
        raise HTTPException(
            status_code=409,
            detail=(
                "this Codex thread is being retired after an interrupted fork; "
                "retry after cleanup completes"
            ),
        )


def session_codex_thread_id(session: dict[str, Any]) -> str:
    """Return the chat's Codex root whether Codex is active or parked."""

    parked = str(session.get("codex_thread_id") or "").strip()
    if parked:
        return parked
    if (
        str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
        == BACKEND_CODEX
    ):
        return str(session.get("session_id") or "").strip()
    return ""


TIMELINE_INDEX_CODEX_GOAL_TYPES = {
    "codex_goal_budget_limited",
}
TIMELINE_INDEX_CODEX_COMPACTION_TYPES = {
    "codex_compaction_started",
    "codex_compaction_completed",
}


def timeline_index_codex_lifecycle_key(
    event: dict[str, Any],
) -> str | None:
    """Return the stable semantic key for durable Codex control markers."""
    event_type = str(event.get("type") or "")
    if event_type in TIMELINE_INDEX_CODEX_GOAL_TYPES:
        return "codex:goal-budget"
    if event_type not in TIMELINE_INDEX_CODEX_COMPACTION_TYPES:
        return None
    compaction_id = str(event.get("compaction_id") or "").strip()
    if compaction_id:
        return f"codex:compaction:{compaction_id}"
    operation_id = str(event.get("operation_id") or "").strip()
    if operation_id:
        return f"codex:compaction:{operation_id}"
    native_id = (
        str(event.get("turn_id") or "").strip()
        or str(event.get("item_id") or "").strip()
        or str(event.get("id") or "").strip()
        or str(event.get("seq") or "")
    )
    return f"codex:compaction:{native_id}"


def native_codex_compaction_id(
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> str:
    """Return one durable identity shared by native start/completion events."""

    return ":".join((
        "native",
        str(thread_id or "-").strip() or "-",
        str(turn_id or "-").strip() or "-",
        str(item_id or "-").strip() or "-",
    ))


def native_codex_compaction_terminal_aliases(
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> tuple[str, ...]:
    """Return stable, unambiguous identities for a provider compaction.

    App-server can replay a completion with either the turn or item field
    omitted after the owning ACTIVE record has been released. Thread+turn and
    thread+item remain safe aliases; thread alone does not, because it could
    suppress a later legitimate compaction on the same conversation.

    The thread+turn alias is only produced while the item id is unknown. One
    long turn can compact several times, each with its own item id, so a
    lookup that carries an item id must never be satisfied by the turn alias
    of an earlier compaction in the same turn.
    """

    thread_id = str(thread_id or "").strip()
    turn_id = str(turn_id or "").strip()
    item_id = str(item_id or "").strip()
    if not thread_id or (not turn_id and not item_id):
        return ()

    aliases = [
        f"codex:compaction:{native_codex_compaction_id(thread_id, turn_id, item_id)}"
    ]
    alias_parts = (("item", item_id),) if item_id else (("turn", turn_id),)
    for kind, value in alias_parts:
        if not value:
            continue
        encoded = json.dumps(
            [thread_id, value],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
        aliases.append(
            f"codex:compaction-native:{kind}:{hashlib.sha256(encoded).hexdigest()}"
        )
    return tuple(aliases)


def codex_compaction_count(sess: dict[str, Any]) -> int:
    """Return the persisted per-thread compaction tally, tolerating bad data."""

    value = sess.get("codex_compaction_count")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def codex_session_index_thread_names() -> dict[str, str]:
    """Best-effort id -> human title map. Not authoritative: many real Codex

    sessions (including every ``codex exec`` run) never get an entry here, so
    this is only used to prefer a nicer label, never to decide what counts as
    a session — that comes from scanning the transcripts themselves.
    """
    names: dict[str, str] = {}
    if not CODEX_SESSION_INDEX_PATH.exists():
        return names
    with suppress(OSError):
        if CODEX_SESSION_INDEX_PATH.stat().st_size > MAX_CODEX_SESSION_INDEX_BYTES:
            return names
        with CODEX_SESSION_INDEX_PATH.open("rb") as stream:
            for _index in range(MAX_CODEX_SESSION_INDEX_LINES):
                raw_line = stream.readline(MAX_LOCAL_TRANSCRIPT_LINE_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_LOCAL_TRANSCRIPT_LINE_BYTES:
                    continue
                if not raw_line.strip():
                    continue
                try:
                    entry = json.loads(raw_line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                provider_id = provider_session_identifier(entry.get("id"))
                thread_name = native_session_title(entry.get("thread_name"))
                if provider_id and thread_name:
                    names[provider_id] = thread_name
    return names


def codex_app_server_env() -> dict[str, str]:
    """Process-wide environment; chat scope is supplied explicitly per thread."""
    env = runner_env()
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    server_origin = provider_helper_server_origin()
    env["HYADEX_SERVER_URL"] = server_origin
    add_provider_no_proxy_environment(
        env,
        codex_provider_mcp_connection_host(),
    )
    env["HYADEX_JOBS_CLI"] = str(SERVER_ROOT / "hyadex_jobs.py")
    env["HYADEX_PUBLISH_CLI"] = str(SERVER_ROOT / "hyadex_publish.py")
    env["HYADEX_EMERGENCY_CLI"] = str(SERVER_ROOT / "hyadex_emergency.py")
    env["HYADEX_NOTIFY_CLI"] = str(SERVER_ROOT / "hyadex_notify.py")
    scrub_provider_runtime_environment(env)
    return env


def codex_app_server_managers() -> tuple[CodexAppServerManager, ...]:
    managers: list[CodexAppServerManager] = []
    for manager in (PROVIDERS.codex_manager, *CODEX_RETIRED_APP_SERVER_MANAGERS):
        if manager is not None and not any(manager is item for item in managers):
            managers.append(manager)
    return tuple(managers)


def existing_codex_app_server_manager(sess: dict[str, Any] | None = None) -> CodexAppServerManager | None:
    session_id = str((sess or {}).get("id") or "")
    owner = CODEX_SESSION_APP_SERVER_MANAGERS.get(session_id)
    if not (owner is not None and any(owner is item for item in codex_app_server_managers())):
        thread_id = str(session_provider_id(sess) or "") if sess else ""
        owner = next((item for item in codex_app_server_managers()
                      if thread_id and item.is_thread_loaded(thread_id)), None)
    return owner or PROVIDERS.codex_manager


def codex_default_service_tier(model: str) -> str:
    return CODEX_FALLBACK_SERVICE_TIERS.get(str(model or "").strip(), "")


def codex_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    with suppress(Exception):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def bounded_codex_output_text(value: Any) -> str:
    text = codex_output_text(value)
    if len(text) <= CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS:
        return text
    marker = "[Earlier tool output truncated by HyadexServer]\n"
    tail_chars = max(0, CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS - len(marker))
    return marker + (text[-tail_chars:] if tail_chars else "")
