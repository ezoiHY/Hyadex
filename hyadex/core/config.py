"""Server settings: environment, paths under the state directory, provider binaries, limits and timeouts. Read once at import."""
from __future__ import annotations

import json
import logging.handlers
import os
import re
import secrets
import server_instances
import unicodedata
import uuid
from contextvars import ContextVar
from fastapi import HTTPException
from hyadex.providers.claude.sdk import CLAUDE_PROVIDER_MCP_SERVER_NAME
from hyadex.providers.runtime import PROVIDER_SECRET_ENV_NAMES
from hyadex.providers.runtime import is_scrubbed_provider_runtime_env_name
from hyadex.providers.runtime import scrub_provider_runtime_environment
from pathlib import Path

logger = logging.getLogger("hyadex-server")

BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
VALID_BACKENDS = {BACKEND_CLAUDE, BACKEND_CODEX}
CODEX_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
CODEX_EFFORT_ALIASES = {
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}


def env_setting(primary: str, default: str | None = None, *legacy: str) -> str | None:
    """Read a canonical setting while honoring pre-Hyadex deployments."""
    for name in (primary, *legacy):
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def hyadex_setting(suffix: str, default: str) -> str:
    value = env_setting(f"HYADEX_{suffix}", default, f"ZENITHBOT_{suffix}")
    return value if value is not None else default


SERVER_DISPLAY_NAME_MAX_BYTES = 160
SERVER_DISPLAY_NAME_ERROR = (
    "HYADEX_SERVER_NAME must be empty or at most 160 UTF-8 bytes "
    "and contain no control characters"
)


def canonical_server_display_name(
    configured: str | None,
    *,
    fallback: str | None = None,
) -> str:
    """Normalize the label exposed to native clients."""

    raw = "" if configured is None else configured
    if any(unicodedata.category(character) == "Cc" for character in raw):
        raise RuntimeError(SERVER_DISPLAY_NAME_ERROR)
    normalized = raw.strip()
    if not normalized:
        raw_fallback = (
            fallback
            if fallback is not None
            else (os.uname().nodename or "HyadexServer")
        )
        if any(
            unicodedata.category(character) == "Cc"
            for character in raw_fallback
        ):
            raise RuntimeError(SERVER_DISPLAY_NAME_ERROR)
        normalized = raw_fallback.strip() or "HyadexServer"
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RuntimeError(SERVER_DISPLAY_NAME_ERROR) from exc
    if len(encoded) > SERVER_DISPLAY_NAME_MAX_BYTES:
        raise RuntimeError(SERVER_DISPLAY_NAME_ERROR)
    return normalized


CONFIG_ENV_FILE = (
    Path(
        os.environ.get("HYADEX_SERVER_CONFIG_DIR")
        or (Path.home() / ".config" / "hyadex-server")
    )
    / "env"
)
CONFIG_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_config_env_file(text: str) -> dict[str, str]:
    """Parse the installer's `KEY=VALUE` config file, systemd-style.

    Deliberately forgiving: a malformed line is skipped rather than raised,
    because this runs during import and must never stop the server from
    starting over one stray hand-edited line.
    """

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not CONFIG_ENV_NAME_RE.fullmatch(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            if value[0] == '"':
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    decoded = value[1:-1]
                value = decoded if isinstance(decoded, str) else value[1:-1]
            else:
                value = value[1:-1]
        values[name] = value
    return values


CONFIG_ENV_RUNTIME_OVERRIDE_NAMES = {
    "HYADEX_SERVER_NAME",
    "HYADEX_TEAM_HUB_MODE",
}


def load_config_env_file(path: Path | None = None) -> list[str]:
    """Apply installer config-file variables the process does not already have.

    install.sh writes this file and deliberately preserves any extra lines an
    operator adds to it, and the Linux unit loads it via `EnvironmentFile`.
    launchd has no equivalent, so on macOS those extra lines - a company
    provider's `COMPANY_API_KEY`, for example - silently never reached the
    provider CLI, with nothing reporting that the file was ignored. Reading
    it here makes both platforms behave the same.

    Existing process variables always win except for the two live-mutable
    canonical settings (including the server name). This lets a launchd
    process honor a role switch on its next launch without allowing the file
    to override its token, port, paths, or other service-manager authority.
    Returns the names applied, for logging; values are never logged.
    """

    target = CONFIG_ENV_FILE if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    applied: list[str] = []
    for name, value in parse_config_env_file(text).items():
        if name in os.environ and name not in CONFIG_ENV_RUNTIME_OVERRIDE_NAMES:
            continue
        os.environ[name] = value
        applied.append(name)
    return sorted(applied)


CONFIG_ENV_APPLIED = load_config_env_file()


def resolve_state_dir() -> Path:
    configured = env_setting(
        "HYADEX_STATE_DIR",
        None,
        "HYADEX_SERVER_STATE_DIR",
        "ZENITHBOT_AGENT_DIR",
    )
    if configured:
        return Path(configured).expanduser()

    canonical = Path.home() / ".hyadex"
    legacy = Path.home() / ".zenithbot-agent"
    if canonical.exists() or not legacy.exists() or legacy.is_symlink():
        return canonical
    try:
        legacy.rename(canonical)
        legacy.symlink_to(canonical, target_is_directory=True)
        logger.info("migrated legacy state directory to %s", canonical)
        return canonical
    except OSError as exc:
        logger.warning("could not migrate legacy state directory %s: %s", legacy, exc)
        return legacy


STATE_DIR = resolve_state_dir()
SERVER_INSTANCE_NAME = server_instances.instance_name(os.environ.get("HYADEX_SERVER_INSTANCE", "default"))
TMUX_INSTANCE_ARGS = () if SERVER_INSTANCE_NAME == "default" else ("-L", f"hyadex-server-{SERVER_INSTANCE_NAME}")


SERVER_ROOT = Path(__file__).resolve().parent
SERVER_VERSION_FILE = SERVER_ROOT / "VERSION"


SESSIONS_FILE = STATE_DIR / "sessions.json"
JOBS_FILE = STATE_DIR / "jobs.json"
# The directory keeps its original name: legacy provider history embeds paths
# inside it, and startup purges any authority file a crashed server left there.
PROVIDER_AUTHORITY_ROOT = STATE_DIR / "cross_chat_authority"
FILES_ROOT = STATE_DIR / "files"
CODE_DIFFS_ROOT = STATE_DIR / "code_diffs"
HOST_HEALTH_FILE = STATE_DIR / "host_health.jsonl"
SERVER_ADMIN_ROOT = STATE_DIR / "admin"


SERVER_RESTART_STATUS_FILE = SERVER_ADMIN_ROOT / "server-restart.json"
CODEX_SETTINGS_FILE = SERVER_ADMIN_ROOT / "codex-settings.json"
ABANDONED_FORK_THREADS_FILE = SERVER_ADMIN_ROOT / "abandoned-fork-threads.json"
# Process-group ids of provider children this server spawned in their own
# session (``start_new_session=True``). A SIGKILL of the server cannot reach
# them, so the hard-kill watchdog and the next startup reap them from here.
PROVIDER_CHILDREN_FILE = SERVER_ADMIN_ROOT / "provider-children.json"
PROVIDER_CHILD_PROC_ROOT = Path("/proc")


CLAUDE_PROJECTS_ROOT = Path(
    os.environ.get("CLAUDE_PROJECTS_ROOT")
    or Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")).expanduser() / "projects"
).expanduser()
CODEX_SESSIONS_ROOT = Path(
    os.environ.get("CODEX_SESSIONS_ROOT")
    or (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        / "sessions"
    )
)
CODEX_SESSION_INDEX_PATH = (
    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "session_index.jsonl"
)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")

CODEX_DEFAULT_MODEL = hyadex_setting("CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
_configured_codex_effort = hyadex_setting("CODEX_EFFORT", "xhigh").strip().lower() or "xhigh"
CODEX_DEFAULT_EFFORT = CODEX_EFFORT_ALIASES.get(_configured_codex_effort, _configured_codex_effort)


DEFAULT_CWD = hyadex_setting("AGENT_CWD", str(Path.home()))
DEFAULT_BACKEND = hyadex_setting("BACKEND", BACKEND_CLAUDE).lower()


def enabled_backends() -> set[str]:
    """Backends new chats may use. Existing chats on other backends still load.

    HYADEX_ENABLED_BACKENDS can restrict the supported Claude/Codex backends
    with a comma-separated list; retired runtime names never enable execution.
    """
    configured = os.environ.get("HYADEX_ENABLED_BACKENDS", "")
    names = {item.strip().lower() for item in configured.split(",") if item.strip()}
    return (names & VALID_BACKENDS) or {BACKEND_CLAUDE, BACKEND_CODEX}


def require_enabled_backend(backend: str | None) -> str:
    resolved = str(backend or DEFAULT_BACKEND).strip().lower()
    if resolved not in enabled_backends():
        raise HTTPException(
            status_code=400,
            detail=f"The {resolved} backend is not enabled on this Hyadex server.",
        )
    return resolved


CODEX_GOALS_DEFAULT_ENABLED = True


def read_codex_goals_enabled(path: Path | None = None) -> bool:
    """Goals are available without a server-wide opt-in.

    Ignore the retired environment/config switch, including persisted False
    from older clients. Do not rewrite shared settings or resume paused goals.
    Keep the optional path argument for compatibility with existing callers.
    """
    return True


CODEX_GOALS_ENABLED = read_codex_goals_enabled()

CODEX_FALLBACK_MODELS = (
    ("gpt-6-luna", "GPT-6-Luna"),
    ("gpt-5.6-sol", "GPT-5.6-Sol"),
    ("gpt-5.6-terra", "GPT-5.6-Terra"),
    ("gpt-5.6-luna", "GPT-5.6-Luna"),
    ("gpt-5.5", "GPT-5.5"),
    ("gpt-5.4", "GPT-5.4"),
    ("gpt-5.4-mini", "GPT-5.4-Mini"),
)
CODEX_FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_EFFORT_ORDER = ("none", "minimal", *CODEX_FALLBACK_EFFORTS)
CODEX_FALLBACK_MODEL_EFFORTS = {
    "gpt-6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
    "gpt-5.4": ("low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("low", "medium", "high", "xhigh"),
}
CODEX_FALLBACK_SERVICE_TIERS = {
    "gpt-5.6-sol": "priority",
    "gpt-5.6-terra": "priority",
    "gpt-5.6-luna": "priority",
}

REQUEST_TIMEOUT_SECONDS = int(hyadex_setting("REQUEST_TIMEOUT_SECONDS", "86400"))
CODEX_APP_SERVER_TIMEOUT_SECONDS = int(hyadex_setting("CODEX_APP_SERVER_TIMEOUT_SECONDS", "30"))
CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS = max(
    CODEX_APP_SERVER_TIMEOUT_SECONDS,
    int(
        hyadex_setting(
            "CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS",
            "300",
        )
    ),
)
CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS = max(
    5,
    int(hyadex_setting("CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS", "20")),
)
CODEX_TRANSPORT_AUTO = "auto"
CODEX_TRANSPORT_APP_SERVER = "app-server"
CODEX_TRANSPORT_EXEC = "exec"
CODEX_INTERACTIVE_CLIENT_CAPABILITY = "codex_interactive_v1"
CLAUDE_TRANSPORT_AUTO = "auto"
CLAUDE_TRANSPORT_AGENT_SDK = "agent-sdk"
CLAUDE_TRANSPORT_PRINT = "print"
HARD_TERMINALIZABLE_TURN_TRANSPORTS = {
    CODEX_TRANSPORT_APP_SERVER,
    CODEX_TRANSPORT_EXEC,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
}
CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY = "claude_sdk_interactive_v1"
CLAUDE_PERMISSION_MODE_OPTIONS = (
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "dontAsk",
    "bypassPermissions",
)
CLAUDE_PERMISSION_MODES = set(CLAUDE_PERMISSION_MODE_OPTIONS)
CLAUDE_DEFAULT_PERMISSION_MODE = "default"
PROVIDER_JOBS_ACCESS_MODES = ("full", "read_only", "blocked")
PROVIDER_JOBS_ACCESS_MODE_SET = set(PROVIDER_JOBS_ACCESS_MODES)
PROVIDER_JOBS_ACCESS_DEFAULT = "full"
EMERGENCY_MESSAGE_MAX_CHARS = 500
EMERGENCY_REQUESTS_PER_RUN = 3
NOTIFICATION_REQUESTS_PER_RUN = 5
EMERGENCY_ACTIVE_ALERT_LIMIT = 32
EMERGENCY_WEBSOCKET_PROTOCOL = "hyadex-emergency-v1"
EVENTS_WEBSOCKET_PROTOCOL = "hyadex-events-v1"
TERMINAL_WEBSOCKET_PROTOCOL = "hyadex-terminal-v1"
EMERGENCY_AUTHORITY_DENIED_PURPOSES = {
    "chat_mailbox_wake",
    "cross_chat_handoff_delivery",
    "secure_peer_handoff_delivery",
    "handoff_digest",
    "handoff_digest_delivery",
}
CODEX_APPROVAL_POLICIES = {"never", "on-request", "untrusted"}
CODEX_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
CODEX_APPROVAL_REVIEWERS = {"user", "auto_review", "guardian_subagent"}
CODEX_DEFAULT_APPROVAL_POLICY = "never"
CODEX_DEFAULT_SANDBOX_MODE = "danger-full-access"
CODEX_DEFAULT_PERMISSION_PROFILE: str | None = None
CODEX_DEFAULT_APPROVALS_REVIEWER = "user"
# Turns from clients without the interactive app-server capability cannot
# service approval requests. Keep their deliberate fail-closed policy separate
# from the user-facing session defaults above.
CODEX_NONINTERACTIVE_APPROVAL_POLICY = "never"
CODEX_SANDBOX_POLICY_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}
CODEX_GOAL_STATUSES = {
    "active",
    "paused",
    "blocked",
    "usageLimited",
    "budgetLimited",
    "complete",
}
CODEX_INTERACTION_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "applyPatchApproval",
    "execCommandApproval",
}
CODEX_TRANSPORT = hyadex_setting("CODEX_TRANSPORT", CODEX_TRANSPORT_AUTO).strip().lower()


CLAUDE_TRANSPORT = hyadex_setting(
    "CLAUDE_TRANSPORT",
    CLAUDE_TRANSPORT_AUTO,
).strip().lower()


CLAUDE_SDK_MAX_LOADED_CHATS = max(
    1,
    int(hyadex_setting("CLAUDE_SDK_MAX_LOADED_CHATS", "4")),
)
CLAUDE_SDK_CONNECT_TIMEOUT_SECONDS = max(
    5.0,
    float(hyadex_setting("CLAUDE_SDK_CONNECT_TIMEOUT_SECONDS", "30")),
)
CLAUDE_SDK_IDLE_TTL_SECONDS = max(
    30,
    int(hyadex_setting("CLAUDE_SDK_IDLE_TTL_SECONDS", "300")),
)
CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS = max(
    1.0,
    float(
        hyadex_setting(
            "CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS",
            "180",
        )
    ),
)
CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS = max(
    1.0,
    float(hyadex_setting("CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS", "15")),
)
CODEX_APP_SERVER_MAX_LOADED_THREADS = max(
    1,
    int(hyadex_setting("CODEX_APP_SERVER_MAX_LOADED_THREADS", "12")),
)
CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS = int(hyadex_setting("CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS", "120"))
RUNTIME_CATALOG_TIMEOUT_SECONDS = float(hyadex_setting("RUNTIME_CATALOG_TIMEOUT_SECONDS", "6"))
# Leave room for HTTP/JSON overhead before desktop and mobile's 30s deadline.
RUNTIME_CATALOG_BUDGET_SECONDS = 25.0
RUNTIME_CATALOG_DEADLINE: ContextVar[float | None] = ContextVar(
    "runtime_catalog_deadline", default=None,
)
RUNTIME_DIAGNOSTIC_TTL_SECONDS = float(hyadex_setting("RUNTIME_DIAGNOSTIC_TTL_SECONDS", "60"))
# Claude Code only promises the family aliases in ``--help``; account-scoped
# pinned models come from Anthropic's Models API when an API key is available.
# These entries are therefore a last-resort catalog when native SDK metadata
# and the API are unavailable, not the primary source of truth. Keep aliases
# so new models remain selectable even before this fallback is updated.
CLAUDE_CLI_MODEL_ALIASES = (
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
    ("opus[1m]", "Opus 1M"),
)
CLAUDE_CURRENT_MODEL_FALLBACKS = (
    ("claude-fable-5-1", "Fable 5.1", False),
    ("claude-mythos-5-1", "Mythos 5.1 (limited access)", True),
    ("claude-opus-5", "Opus 5", False),
    ("claude-sonnet-5", "Sonnet 5", False),
    ("claude-haiku-4-5-20251001", "Haiku 4.5", False),
)
CLAUDE_LEGACY_MODEL_FALLBACKS = (
    ("claude-fable-5", "Fable 5", False),
    ("claude-mythos-5", "Mythos 5 (limited access)", True),
    ("claude-opus-4-8", "Opus 4.8", False),
    ("claude-opus-4-8[1m]", "Opus 4.8 1M", False),
)
CLAUDE_MODEL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,255}$")
CLAUDE_MODELS_API_MAX_BYTES = 2 * 1024 * 1024
CLAUDE_PROVIDER_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
JOB_SCHEDULER_INTERVAL_SECONDS = float(hyadex_setting("JOB_SCHEDULER_INTERVAL_SECONDS", "5"))
JOB_BUSY_RETRY_SECONDS = int(hyadex_setting("JOB_BUSY_RETRY_SECONDS", "60"))
# Zero means scheduled jobs have no scheduler-specific concurrency ceiling.
# The optional global cap and low-memory check still apply.
JOB_MAX_ACTIVE_RUNS = int(hyadex_setting("JOB_MAX_ACTIVE_RUNS", "0"))
JOB_MIN_AVAILABLE_MEM_MB = int(hyadex_setting("JOB_MIN_AVAILABLE_MEM_MB", "1536"))
JOB_DEFER_EVENT_MIN_SECONDS = int(hyadex_setting("JOB_DEFER_EVENT_MIN_SECONDS", "300"))
# A missing or logged-out provider runtime may recover without operator action,
# but an occurrence must not be parked forever.  Keep the small retry budget on
# the durable job row so a restart cannot reset it into an infinite loop.
JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS = 3
# No server-wide chat-run ceiling by default. Operators can explicitly set a
# positive cap; launch memory and managed-update guards remain independent.
MAX_ACTIVE_AGENT_RUNS = int(hyadex_setting("MAX_ACTIVE_AGENT_RUNS", "0"))
MIN_START_AVAILABLE_MEM_MB = int(hyadex_setting("MIN_START_AVAILABLE_MEM_MB", "2048"))
HOST_MONITOR_INTERVAL_SECONDS = float(hyadex_setting("HOST_MONITOR_INTERVAL_SECONDS", "15"))
HOST_HEALTH_MAX_BYTES = int(hyadex_setting("HOST_HEALTH_MAX_BYTES", str(20 * 1024 * 1024)))
IDLE_WARN_SECONDS = int(hyadex_setting("IDLE_WARN_SECONDS", "1800"))
IDLE_KILL_SECONDS = int(hyadex_setting("IDLE_KILL_SECONDS", "21600"))
CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS = max(
    30.0,
    float(hyadex_setting("CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS", "600")),
)
# An active Claude turn has no useful wall-clock lifetime limit. Long-running
# tool work and user approvals can legitimately span hours; cancel remains the
# explicit terminal control. Operators that need a hard policy ceiling may set
# a positive value, while zero (the default) disables only this absolute bound.
CLAUDE_SDK_TURN_TIMEOUT_SECONDS = max(
    0.0,
    float(hyadex_setting("CLAUDE_SDK_TURN_TIMEOUT_SECONDS", "0")),
)
CLAUDE_SDK_IDLE_TIMEOUT_SECONDS = max(
    30.0,
    float(
        hyadex_setting(
            "CLAUDE_SDK_IDLE_TIMEOUT_SECONDS",
            str(IDLE_KILL_SECONDS),
        )
    ),
)
CLAUDE_SDK_IDLE_WARN_SECONDS = max(
    1.0,
    min(float(IDLE_WARN_SECONDS), CLAUDE_SDK_IDLE_TIMEOUT_SECONDS / 2),
)
# A Codex app-server notification can be silently dropped if it arrives for a
# subscription that already closed (see _route_notification in
# hyadex/providers/codex/app_server.py) - the turn then waits forever with zero visible
# activity, but IDLE_WARN/IDLE_KILL_SECONDS above are sized for legitimate
# gaps *between* activity on an already-productive turn, not "nothing has
# happened since the turn started." This bounds only that first-activity
# wait, far tighter, so a stalled turn fails fast and (if resuming an
# existing thread) is picked up by the existing rollover-retry path instead
# of hanging with no error for up to IDLE_KILL_SECONDS.
CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS = int(
    hyadex_setting("CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS", "180")
)
STOP_GRACE_SECONDS = float(hyadex_setting("STOP_GRACE_SECONDS", "2.0"))
PROCESS_STREAM_LIMIT = int(hyadex_setting("PROCESS_STREAM_LIMIT", str(16 * 1024 * 1024)))
CODEX_APP_SERVER_JSONL_LIMIT_BYTES = max(
    PROCESS_STREAM_LIMIT,
    int(
        hyadex_setting(
            "CODEX_APP_SERVER_JSONL_LIMIT_BYTES",
            str(256 * 1024 * 1024),
        )
    ),
)
CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS = max(
    12_000,
    int(
        hyadex_setting(
            "CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS",
            "12000",
        )
    ),
)
CODEX_EXEC_RAW_EVENT_MAX_CHARS = max(
    4096,
    int(
        hyadex_setting(
            "CODEX_EXEC_RAW_EVENT_MAX_CHARS",
            str(32 * 1024),
        )
    ),
)
CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT = max(
    128,
    int(
        hyadex_setting(
            "CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT",
            "8192",
        )
    ),
)
WEBSOCKET_SEND_TIMEOUT_SECONDS = max(
    0.1,
    float(hyadex_setting("WEBSOCKET_SEND_TIMEOUT_SECONDS", "2")),
)
EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL = max(
    1,
    int(hyadex_setting("EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL", "256")),
)
EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION = max(
    1,
    min(
        EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL,
        int(hyadex_setting("EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION", "32")),
    ),
)
EVENT_WEBSOCKET_OVER_CAP_CLOSE_CODE = 1013
EVENT_WEBSOCKET_DELIVERY_FAILURE_CLOSE_CODE = 1011
TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS = max(
    1.0,
    float(hyadex_setting("TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS", "6")),
)
MAX_UPLOAD_BYTES = int(hyadex_setting("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024 * 1024)))
MAX_ARTIFACT_PUBLISH_FILES = int(hyadex_setting("ARTIFACT_PUBLISH_MAX_FILES", "64"))
MAX_ARTIFACT_TITLE_CHARS = int(hyadex_setting("ARTIFACT_TITLE_MAX_CHARS", "1000"))
MAX_ARTIFACT_TEXT_CHARS = int(hyadex_setting("ARTIFACT_TEXT_MAX_CHARS", "12000"))
MAX_IMPORT_MESSAGES = int(hyadex_setting("HISTORY_IMPORT_LIMIT", "400"))
# How far back to look for messages already on the timeline when deciding
# what a provider transcript added elsewhere. Bounded so opening a very long
# chat cannot turn into an unbounded scan.
HISTORY_SYNC_EVENT_SCAN_LIMIT = int(
    hyadex_setting("HISTORY_SYNC_EVENT_SCAN_LIMIT", "20000")
)
MAX_IMPORTED_TEXT_CHARS = int(hyadex_setting("HISTORY_IMPORT_TEXT_CHARS", "12000"))
MAX_LOCAL_SESSION_SCAN_FILES = 10_000
# Retained text/metadata budget. Opaque native media/tool bytes are streamed.
# Private timeline/index readers still use this as their raw record bound.
MAX_LOCAL_TRANSCRIPT_LINE_BYTES = 32 * 1024 * 1024
HISTORY_SYNC_BATCH_BYTES = 16 * 1024 * 1024
MAX_LOCAL_TRANSCRIPT_SCAN_LINES = 250_000
MAX_CODEX_SESSION_INDEX_BYTES = 16 * 1024 * 1024
MAX_CODEX_SESSION_INDEX_LINES = 100_000
MAX_FORK_MEMORY_CHARS = int(hyadex_setting("FORK_MEMORY_CHARS", "24000"))
MAX_FORK_MEMORY_ITEM_CHARS = int(hyadex_setting("FORK_MEMORY_ITEM_CHARS", "1800"))


MAX_JOB_TITLE_CHARS = 200


MAX_CODEX_GOAL_CHARS = 4_000
MAX_CODEX_INTERACTION_TEXT_CHARS = 24_000
MAX_CODEX_PENDING_INTERACTIONS = 128
MAX_CODEX_APPROVAL_ITEM_CACHE = 256
CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS = 15.0
STOP_CONFIRM_TIMEOUT_SECONDS = 5.0
# Bound the user-visible Stop operation so callers do not hang forever. Its
# cancellation-hostile inner teardown remains a fail-closed per-chat fence on
# expiry: admitting a successor would let late Stop effects cancel or mutate
# that new turn. The always-available forced server restart is the recovery
# path when the exact teardown itself never settles.
EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS = float(
    hyadex_setting("EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS", "90")
)
EXPLICIT_STOP_FENCE_RELEASE_TIMEOUT_SECONDS = 5.0
CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS = max(
    1.0,
    float(hyadex_setting("CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS", "15")),
)
# GET /api/health is polled by every client every few seconds. Queue
# reconciliation is a repair pass, not a per-request duty; run it at most this
# often from health polls so pollers cannot multiply promotion attempts.
HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS = float(
    hyadex_setting("HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS", "15")
)
QUEUE_FENCE_LOG_INTERVAL_SECONDS = 60.0
# A warm sample answers in milliseconds, but the first one after the Claude
# process starts takes one to two seconds, more on a busy machine. Timing out
# also resets the SDK client, so the next turn starts cold again; a limit near
# the cold time made a missed sample repeat on every following turn.
CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS = max(
    0.1,
    float(hyadex_setting("CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS", "10")),
)
CODEX_CONTEXT_WINDOW_RESERVE_TOKENS = 12_000
CODEX_GOAL_CONTROL_TIMEOUT_SECONDS = max(
    0.1,
    float(hyadex_setting("CODEX_GOAL_CONTROL_TIMEOUT_SECONDS", "5")),
)
DEFAULT_SESSION_EVENT_LIMIT = int(hyadex_setting("SESSION_EVENT_LIMIT", "100"))
MAX_EVENT_RESPONSE_LIMIT = int(hyadex_setting("MAX_EVENT_RESPONSE_LIMIT", "1000"))
MAX_WORKSPACE_PATH_CHARS = int(hyadex_setting("WORKSPACE_PATH_MAX_CHARS", "4096"))
MAX_WORKING_DIRECTORY_COMPLETIONS = 50
MAX_WORKING_DIRECTORY_SCAN_ENTRIES = 5_000
AGENT_TOKEN = env_setting(
    "HYADEX_AGENT_TOKEN",
    "",
    "ZENITHDOCK_AGENT_TOKEN",
    "ZENITHBOT_AGENT_TOKEN",
) or ""


def scrub_server_secret_environment() -> tuple[str, ...]:
    """Remove server-only bearer material before any child can inherit it."""

    removed: list[str] = []
    for name in tuple(os.environ):
        if name in PROVIDER_SECRET_ENV_NAMES or is_scrubbed_provider_runtime_env_name(name):
            removed.append(name)
    scrub_provider_runtime_environment(os.environ)
    return tuple(removed)


# Authentication uses the captured immutable value above. Keeping its source
# variables in the process environment would expose the admin bearer to any
# subprocess that forgets an explicit env (including the Claude SDK's `-v`
# probe and repository-configured Git filters).
SCRUBBED_SERVER_SECRET_ENV_NAMES = scrub_server_secret_environment()


SERVER_INSTANCE_ID = uuid.uuid4().hex
CODEX_PROVIDER_MCP_NAME = CLAUDE_PROVIDER_MCP_SERVER_NAME
CODEX_PROVIDER_MCP_PATH = "/api/agent/provider-tools/mcp"
CODEX_PROVIDER_MCP_HEADER_NAME = "X-Hyadex-Provider-MCP"
# Both values are process-only and rotate on every HyadexServer restart. They
# are never placed in a provider environment, prompt, transcript, or store.
CODEX_PROVIDER_MCP_HEADER_SECRET = secrets.token_urlsafe(48)
CODEX_PROVIDER_MCP_PROOF_KEY = secrets.token_bytes(32)


API_CONTRACT_VERSION = 30
SESSION_ORDER_STEP = 1000.0
# Cross-chat messaging, mailbox wakes and handoff digests were removed. Their
# server-internal runs remain in older event logs and stay hidden from forks,
# timelines and history; no current code starts a turn with these purposes.
FORK_INTERNAL_PURPOSES = {
    "chat_mailbox_wake",
    "cross_chat_handoff_delivery",
    "handoff_digest",
    "handoff_digest_delivery",
}
# Durable queue rows written by older servers may still carry these purposes
# or routing fields. Such a row can never run with only part of its routing,
# so queue recovery and promotion discard it visibly instead of retrying.
RETIRED_TURN_PURPOSES = {
    "chat_mailbox_wake",
    "cross_chat_handoff_delivery",
    "handoff_digest",
    "handoff_digest_delivery",
    "secure_peer_handoff_delivery",
}
# Identity of an old internal delivery row; it keeps the row's queue events
# hidden from clients exactly like its original turn_queued event.
LEGACY_DELIVERY_IDENTITY_FIELDS = (
    "cross_chat_envelope_id",
    "cross_chat_exchange_id",
    "cross_chat_exchange_leg_id",
    "exchange_id",
    "exchange_leg_id",
)
RETIRED_QUEUED_TURN_FIELDS = (
    "cross_chat_envelope_id",
    "cross_chat_exchange_id",
    "cross_chat_exchange_leg_id",
    "exchange_id",
    "exchange_leg_id",
    "cross_chat_exchange_status",
    "cross_chat_obligation_ids",
    "cross_chat_exchange_ids",
    "chat_references",
    "digest_job_id",
    "team_references",
    "secure_peer_envelope_id",
)
CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS = max(
    0.1,
    float(hyadex_setting("CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS", "15")),
)
CODE_DIFF_SNAPSHOT_MAX_PATHS = max(
    1,
    int(hyadex_setting("CODE_DIFF_SNAPSHOT_MAX_PATHS", "20000")),
)
CODE_DIFF_SNAPSHOT_MAX_BYTES = max(
    1,
    int(
        hyadex_setting(
            "CODE_DIFF_SNAPSHOT_MAX_BYTES",
            str(200 * 1024 * 1024),
        )
    ),
)
CODE_DIFF_SNAPSHOT_MAX_FILE_BYTES = max(
    1,
    int(
        hyadex_setting(
            "CODE_DIFF_SNAPSHOT_MAX_FILE_BYTES",
            str(64 * 1024 * 1024),
        )
    ),
)
CODE_DIFF_DISCOVERY_MAX_BYTES = max(
    1024,
    int(
        hyadex_setting(
            "CODE_DIFF_DISCOVERY_MAX_BYTES",
            str(8 * 1024 * 1024),
        )
    ),
)
CODE_DIFF_INDEX_MAX_BYTES = max(
    1024,
    int(
        hyadex_setting(
            "CODE_DIFF_INDEX_MAX_BYTES",
            str(32 * 1024 * 1024),
        )
    ),
)
CODE_DIFF_PATCH_MAX_BYTES = max(
    1024,
    int(
        hyadex_setting(
            "CODE_DIFF_PATCH_MAX_BYTES",
            str(8 * 1024 * 1024),
        )
    ),
)
CODE_DIFF_NUMSTAT_MAX_BYTES = max(
    1024,
    int(
        hyadex_setting(
            "CODE_DIFF_NUMSTAT_MAX_BYTES",
            str(8 * 1024 * 1024),
        )
    ),
)
SUBAGENT_SNAPSHOT_STATE_LIMIT = 256
# Most recently updated persisted descendants reconciled per chat open.
CODEX_SUBAGENT_RECONCILE_LIMIT = int(
    hyadex_setting("CODEX_SUBAGENT_RECONCILE_LIMIT", "200")
)
SUBAGENT_SNAPSHOT_LOG_LIMIT = 80
SUBAGENT_SNAPSHOT_TEXT_LIMIT = 600


# Unknown values fall back to safe defaults rather than failing at startup.
if CODEX_DEFAULT_EFFORT not in CODEX_EFFORTS:
    CODEX_DEFAULT_EFFORT = "xhigh"
if DEFAULT_BACKEND not in VALID_BACKENDS:
    DEFAULT_BACKEND = BACKEND_CLAUDE
if CODEX_TRANSPORT not in {CODEX_TRANSPORT_AUTO, CODEX_TRANSPORT_APP_SERVER, CODEX_TRANSPORT_EXEC}:
    CODEX_TRANSPORT = CODEX_TRANSPORT_AUTO
if CLAUDE_TRANSPORT not in {CLAUDE_TRANSPORT_AUTO, CLAUDE_TRANSPORT_AGENT_SDK, CLAUDE_TRANSPORT_PRINT}:
    CLAUDE_TRANSPORT = CLAUDE_TRANSPORT_AUTO

try:
    SERVER_VERSION = SERVER_VERSION_FILE.read_text().strip() or "development"
except OSError:
    SERVER_VERSION = "development"
