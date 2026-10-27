"""Provider authority: capabilities issued to provider helper tools and their lifetime."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import shlex
import time
from typing import Any

from fastapi import Request

from hyadex.core.config import (
    AGENT_TOKEN,
    BACKEND_CODEX,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CODEX_TRANSPORT_APP_SERVER,
    hyadex_setting,
    PROVIDER_AUTHORITY_ROOT,
    PROVIDER_JOBS_ACCESS_MODE_SET,
    SERVER_INSTANCE_NAME,
    SERVER_ROOT,
)
from hyadex.core.paths import codex_manifest_path, write_all
from hyadex.core.runs import RUNS
from hyadex.core.sessions import (
    add_provider_no_proxy_environment,
    codex_provider_mcp_connection_host,
    effective_provider_jobs_access,
    PROVIDER_AUTHORITIES,
    PROVIDER_AUTHORITY_LOCK,
    provider_helper_server_origin,
    STORE,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.core.util import decoded_exact_header_secret, runner_env, server_identity
from hyadex.providers.claude.history import provider_session_identifier
from hyadex.providers.codex.app_server import CodexAppServerError
from hyadex.providers.instructions import (
    PROVIDER_TOOL_HELPERS,
    PROVIDER_TOOL_MAX_ARGUMENT_BYTES,
    PROVIDER_TOOL_MAX_ARGUMENT_CHARS,
    PROVIDER_TOOL_MAX_ARGUMENTS,
    PROVIDER_TOOL_MAX_STDIN_BYTES,
    PROVIDER_TOOL_READ_ONLY_COMMANDS,
)
from hyadex.providers.runtime import ProviderRuntimeContextError, validate_provider_runtime_env

PROVIDER_AUTHORITY_TTL_SECONDS = max(
    60,
    # Exact CURRENT_TURNS binding and terminal revocation are the primary
    # fence. This generous stale fallback permits real overnight turns. The
    # former cross-chat setting name remains an accepted alias.
    int(hyadex_setting(
        "PROVIDER_AUTHORITY_TTL_SECONDS",
        hyadex_setting("CROSS_CHAT_CAPABILITY_TTL_SECONDS", "604800"),
    )),
)


def provider_authority_path(run_id: str, nonce: str | None = None) -> Path:
    if not re.fullmatch(r"run_[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid provider authority run id")
    if nonce is not None and not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("invalid provider authority nonce")
    suffix = f"-{nonce}" if nonce else ""
    return PROVIDER_AUTHORITY_ROOT / f"{run_id}{suffix}.json"


async def issued_provider_capability_snapshot(
    run_id: str,
    authority_path: Path,
) -> dict[str, Any]:
    """Copy the exact effective grant registered by capability issuance."""

    async with PROVIDER_AUTHORITY_LOCK:
        matches = [
            record
            for record in PROVIDER_AUTHORITIES.values()
            if str(record.get("source_run_id") or "") == str(run_id)
            and str(record.get("authority_path") or "") == str(authority_path)
        ]
        if len(matches) != 1:
            raise ProviderRuntimeContextError(
                "Hyadex provider authority registration is unavailable"
            )
        record = matches[0]
        actions = record.get("actions")
        jobs_access = record.get("provider_jobs_access")
        if not isinstance(actions, set) or jobs_access not in PROVIDER_JOBS_ACCESS_MODE_SET:
            raise ProviderRuntimeContextError(
                "Hyadex provider authority registration is malformed"
            )
        return {
            "actions": set(actions),
            "provider_jobs_access": str(jobs_access),
        }


async def provider_authority_runtime_env(
    run_id: str,
    authority_path: Path | None,
    source_session_id: str,
) -> dict[str, str]:
    """Project one live grant into opaque, bounded provider process state."""

    if authority_path is None:
        return {}
    capability = await issued_provider_capability_snapshot(run_id, authority_path)
    actions = set(capability["actions"])
    provider_jobs_access = str(capability["provider_jobs_access"])
    runtime_env: dict[str, str] = {
        "HYADEX_CHAT_ID": str(source_session_id),
        "HYADEX_PROVIDER_AUTHORITY_FILE": str(authority_path),
        "HYADEX_PROVIDER_AUTHORITY_ACTIONS": ",".join(sorted(actions)),
        "HYADEX_PROVIDER_SERVER_ORIGIN": provider_helper_server_origin(),
        "HYADEX_PROVIDER_JOBS_ACCESS": (
            provider_jobs_access if "jobs" in actions else "blocked"
        ),
    }
    validated = validate_provider_runtime_env(runtime_env)
    # Keep the projection beside the in-memory capability for the provider
    # tool's server-side execution. It is never serialized into the authority
    # file or provider history.
    async with PROVIDER_AUTHORITY_LOCK:
        matches = [
            record
            for record in PROVIDER_AUTHORITIES.values()
            if str(record.get("source_run_id") or "") == str(run_id)
            and str(record.get("authority_path") or "") == str(authority_path)
        ]
        if len(matches) != 1:
            raise ProviderRuntimeContextError(
                "Hyadex provider authority ended during runtime binding"
            )
        matches[0]["provider_runtime_env"] = dict(validated)
    return validated


PROVIDER_AUTHORITY_ACTIONS = frozenset({"jobs", "publish", "emergency", "notify"})


async def issue_provider_authority(
    source_session_id: str,
    run_id: str,
    *,
    actions: set[str] | None = None,
    native_transition_nonce: str | None = None,
) -> Path | None:
    """Issue one run's provider helper authority and its private file."""

    jobs_access = effective_provider_jobs_access(
        STORE.sessions.get(source_session_id)
    )
    effective_actions = set(
        PROVIDER_AUTHORITY_ACTIONS if actions is None else actions
    )
    effective_actions.intersection_update(PROVIDER_AUTHORITY_ACTIONS)
    if jobs_access == "blocked" or not AGENT_TOKEN:
        effective_actions.discard("jobs")
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = time.time() + PROVIDER_AUTHORITY_TTL_SECONDS
    authority_path = provider_authority_path(run_id, secrets.token_hex(16))
    PROVIDER_AUTHORITY_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is ignored when the directory already exists. Keep the
    # authority directory private even after an older or user-created install.
    os.chmod(PROVIDER_AUTHORITY_ROOT, 0o700)
    payload = json.dumps({
        "version": 1,
        "server_identity": server_identity(),
        "provider_server_origin": provider_helper_server_origin(),
        "source_session_id": source_session_id,
        "source_run_id": run_id,
        "provider_capability": token,
        # Compatibility for the first development CLI; never logged.
        "capability": token,
        "provider_jobs_access": jobs_access,
        "expires_at": expires_at,
    }, separators=(",", ":")).encode("utf-8")
    descriptor = -1
    capability_record: dict[str, Any] | None = None
    try:
        descriptor = os.open(
            authority_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        # The file name is part of the provider prompt. Persist the directory
        # entry before publishing the in-memory capability so a native steer
        # can never revoke its predecessor in favor of an authority path that
        # disappears after a crash. Windows flushes the file above; opening a
        # directory for fsync is not supported there.
        if os.name != "nt":
            directory_fd = os.open(
                PROVIDER_AUTHORITY_ROOT,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        capability_record = {
            "server_identity": server_identity(),
            "source_session_id": source_session_id,
            "source_run_id": run_id,
            "expires_at": expires_at,
            "actions": effective_actions,
            "provider_jobs_access": jobs_access,
            "native_transition_nonce": str(
                native_transition_nonce or ""
            ),
            "consumed": {},
            "authority_path": str(authority_path),
        }
        async with PROVIDER_AUTHORITY_LOCK:
            PROVIDER_AUTHORITIES[token_hash] = capability_record
    except BaseException:
        # An authority file without its matching in-memory grant is harmless,
        # but removing it here prevents a provider from being handed a dead
        # path when issuance is cancelled at the capability-lock boundary.
        if PROVIDER_AUTHORITIES.get(token_hash) is capability_record:
            PROVIDER_AUTHORITIES.pop(token_hash, None)
        with suppress(OSError):
            authority_path.unlink()
        raise
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    return authority_path


def provider_capability_is_attached_to_live_run(
    source_session_id: str,
    source_run_id: str,
    native_transition_nonce: str = "",
    *,
    allow_native_transition: bool = False,
) -> bool:
    """Return whether an authority belongs to the exact live logical run.

    Native steering has a narrow delivery interval in which the replacement
    provider prompt can execute before local lifecycle bookkeeping changes
    ``CURRENT_TURNS``. Only the explicitly reserved candidate for the same
    active chat is accepted in that interval. Stop closes the interval
    immediately; ordinary queued, stale, or neighboring run IDs never match.
    """

    active = RUNS.active.get(source_session_id) or {}
    transition_ready = active.get("logical_transition_ready")
    predecessor_run_id = str(
        active.get("logical_transition_predecessor_run_id") or ""
    )
    current = RUNS.current_turns.get(source_session_id) or {}
    current_run_id = str(current.get("run_id") or "")
    active_run_id = str(active.get("run_id") or "")
    candidate_run_id = str(
        active.get("logical_transition_candidate_run_id") or ""
    )
    transition_nonce = str(
        active.get("logical_transition_authority_nonce") or ""
    )
    # The provider transport is shared across the rollover. As soon as the
    # candidate authority is exposed to it, suspend the predecessor's still-
    # visible file so candidate execution cannot spend old Publish or Jobs
    # grants. Safe preaccept rollback restores the predecessor only
    # after release_transition removes this exact fenced state.
    predecessor_suspended = bool(
        active.get("logical_transition_authority_open") is True
        and isinstance(transition_ready, asyncio.Event)
        and not transition_ready.is_set()
        and predecessor_run_id == source_run_id
        and current_run_id == source_run_id
        and candidate_run_id
        and active_run_id in {source_run_id, candidate_run_id}
        and transition_nonce
    )
    # Promotion changes ACTIVE and CURRENT before the durable lifecycle batch is
    # appended.  Keep the candidate on its transition-only capability ceiling
    # throughout that in-memory commit window: exact Jobs may run (Claude can
    # invoke a tool before start_run returns), but Publish, Emergency and
    # Notify stay closed. Evaluate this before the ordinary CURRENT
    # fast path so promotion cannot turn a pending candidate into full authority.
    candidate_transition_pending = bool(
        isinstance(transition_ready, asyncio.Event)
        and not transition_ready.is_set()
        and candidate_run_id == source_run_id
        and predecessor_run_id
    )
    if candidate_transition_pending:
        return bool(
            source_session_id in RUNS.busy
            and allow_native_transition
            and source_run_id not in RUNS.stopped_runs
            and not active.get("stop_requested")
            and active.get("logical_transition_authority_open") is True
            and native_transition_nonce
            and transition_nonce
            and hmac.compare_digest(
                transition_nonce,
                native_transition_nonce,
            )
            and active_run_id in {predecessor_run_id, candidate_run_id}
            and current_run_id in {predecessor_run_id, candidate_run_id}
        )
    if current_run_id == source_run_id:
        if source_run_id in RUNS.stopped_runs:
            return False
        if active_run_id == source_run_id and active.get("stop_requested"):
            return False
        return not predecessor_suspended
    return bool(
        source_session_id in RUNS.busy
        and allow_native_transition
        and not active.get("stop_requested")
        and isinstance(transition_ready, asyncio.Event)
        and not transition_ready.is_set()
        and active.get("logical_transition_authority_open") is True
        and native_transition_nonce
        and hmac.compare_digest(
            transition_nonce,
            native_transition_nonce,
        )
        and candidate_run_id == source_run_id
        and predecessor_run_id
        and active_run_id == predecessor_run_id
        and current_run_id == predecessor_run_id
    )


class ProviderToolError(RuntimeError):
    """A safe, provider-visible failure from the run-bound helper tool."""


def validate_provider_tool_input(value: Any) -> tuple[str, list[str], str]:
    """Validate one bounded generic helper invocation without coercion."""

    if not isinstance(value, dict) or set(value) - {"helper", "arguments", "stdin"}:
        raise ProviderToolError("provider tool input is invalid")
    helper = value.get("helper")
    arguments = value.get("arguments")
    stdin = value.get("stdin", "")
    if helper not in PROVIDER_TOOL_HELPERS or not isinstance(arguments, list):
        raise ProviderToolError("provider tool helper or arguments are invalid")
    if len(arguments) > PROVIDER_TOOL_MAX_ARGUMENTS:
        raise ProviderToolError("provider tool has too many arguments")
    if not isinstance(stdin, str) or "\x00" in stdin:
        raise ProviderToolError("provider tool stdin is invalid")
    try:
        stdin_bytes = len(stdin.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProviderToolError("provider tool stdin is not valid UTF-8") from exc
    if stdin_bytes > PROVIDER_TOOL_MAX_STDIN_BYTES:
        raise ProviderToolError("provider tool stdin is too large")
    clean_arguments: list[str] = []
    argument_bytes = 0
    forbidden_flags = {
        "--authority-file",
        "--chat-id",
        "--session-id",
        "--source-session-id",
        "--run-id",
        "--provider-thread-id",
        "--provider-turn-id",
        "--token",
        "--capability",
        "--target",
        "--server-url",
        "--env",
    }
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or "\x00" in argument
            or len(argument) > PROVIDER_TOOL_MAX_ARGUMENT_CHARS
        ):
            raise ProviderToolError("provider tool argument is invalid")
        try:
            argument_bytes += len(argument.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ProviderToolError(
                "provider tool argument is not valid UTF-8"
            ) from exc
        if argument_bytes > PROVIDER_TOOL_MAX_ARGUMENT_BYTES:
            raise ProviderToolError("provider tool arguments are too large")
        flag = argument.partition("=")[0] if argument.startswith("--") else ""
        if (
            flag in forbidden_flags
            # argparse accepts unambiguous long-option abbreviations by
            # default. Reject every prefix of a protected option as well, so
            # model-supplied ``--auth``/``--cha`` cannot override the exact
            # authority or chat scope injected earlier in argv.
            or bool(flag) and any(
                protected.startswith(flag) for protected in forbidden_flags
            )
            or flag.startswith("--hyadex-")
            or flag.endswith("-token")
            or flag.endswith("-file")
        ):
            raise ProviderToolError("provider identity and authority arguments are forbidden")
        clean_arguments.append(argument)
    if not clean_arguments:
        raise ProviderToolError("provider tool requires a helper command")
    return str(helper), clean_arguments, stdin


def provider_tool_call_is_read_only(helper: str, arguments: list[str]) -> bool:
    """Return whether one already-validated helper call has no side effect."""

    if not arguments:
        return False
    command = arguments[0]
    if command in PROVIDER_TOOL_READ_ONLY_COMMANDS.get(helper, frozenset()):
        return True
    return False


def provider_tool_authority_path(
    capability: dict[str, Any],
    run_id: str,
) -> Path:
    """Return one canonical in-memory authority path without reading it."""

    path = Path(str(capability.get("authority_path") or ""))
    if (
        path.parent != PROVIDER_AUTHORITY_ROOT
        or re.fullmatch(
            re.escape(run_id) + r"-[0-9a-f]{32}\.json",
            path.name,
        )
        is None
    ):
        raise ProviderToolError("provider authority is unavailable")
    return path


def provider_tool_active_matches(
    session_id: str,
    run_id: str,
    *,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[bool, asyncio.Event | None]:
    """Check the no-await portion of the exact live provider ownership fence."""

    active = RUNS.active.get(session_id) or {}
    current = RUNS.current_turns.get(session_id) or {}
    ready = active.get("provider_tools_ready")
    expected_transport = (
        CODEX_TRANSPORT_APP_SERVER
        if backend == BACKEND_CODEX
        else CLAUDE_TRANSPORT_AGENT_SDK
    )
    matches = bool(
        session_id in RUNS.busy
        and session_id not in DELETING_SESSIONS
        and session_id not in DELETED_SESSION_TOMBSTONES
        and session_id in STORE.sessions
        and run_id
        and run_id not in RUNS.stopped_runs
        and str(active.get("run_id") or "") == run_id
        and str(current.get("run_id") or "") == run_id
        and active.get("backend") == backend
        and active.get("transport") == expected_transport
        and not active.get("stop_requested")
    )
    if backend == BACKEND_CODEX:
        matches = bool(
            matches
            and provider_thread_id
            and provider_turn_id
            and str(active.get("provider_thread_id") or "") == provider_thread_id
            and str(active.get("provider_turn_id") or "") == provider_turn_id
            and active.get("provider_turn_ready") is True
        )
    else:
        matches = bool(
            matches
            and claude_owner_token
            and hmac.compare_digest(
                str(active.get("claude_sdk_owner_token") or ""),
                claude_owner_token,
            )
            and str(active.get("claude_permission_run_id") or "") == run_id
            and active.get("claude_permissions_open") is True
            and isinstance(ready, asyncio.Event)
            and ready.is_set()
        )
    return matches, ready if isinstance(ready, asyncio.Event) else None


def resolve_provider_tool_arguments(
    helper: str,
    arguments: list[str],
    runtime_env: dict[str, str],
) -> list[str]:
    """Resolve non-secret static placeholders into exact in-memory grants."""

    resolved = list(arguments)
    for argument in resolved:
        flag = argument.partition("=")[0] if argument.startswith("--") else ""
        if flag and "--async-response".startswith(flag):
            raise ProviderToolError(
                "provider async mode is selected only by the live server grant"
            )
        if argument.startswith("--target-index"):
            raise ProviderToolError("--target-index is not available for this helper")
    return resolved


def redact_provider_tool_output(text: str, authority_path: Path) -> str:
    """Remove private capability material from helper diagnostics/results."""

    clean = str(text or "").replace(str(authority_path), "<provider-authority>")
    clean = re.sub(
        re.escape(str(PROVIDER_AUTHORITY_ROOT))
        + r"/run_[A-Za-z0-9_-]+-[0-9a-f]{32}\.json",
        "<provider-authority>",
        clean,
    )
    clean = re.sub(
        r'(?i)("?(?:provider_)?capability"?\s*[:=]\s*"?)[A-Za-z0-9._~-]{16,}',
        r"\1<redacted>",
        clean,
    )
    return clean


def terminal_session_name(session_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]", "_", session_id).strip("_") or "session"
    prefix = "hx_" if SERVER_INSTANCE_NAME == "default" else f"hxi_{SERVER_INSTANCE_NAME}_"
    return f"{prefix}{clean[:80]}"


def provider_capability_header(request: Request) -> str:
    return str(
        request.headers.get("x-hyadex-provider-capability") or ""
    ).strip()


LEGACY_PROVIDER_AUTHORITY_HEADER = "[Hyadex provider authority]"
LEGACY_PROVIDER_AUTHORITY_FOOTER = "[End Hyadex provider authority]"
LEGACY_PROVIDER_AUTHORITY_BINDING = "(bound to this server, chat, and live run)"
LEGACY_PROVIDER_AUTHORITY_VERBOSE_LEAD = (
    "This authority file is bound to this server, chat, and live run. Use it only through "
    "the Hyadex helper CLIs; never read, print, quote, copy, or expose it."
)
LEGACY_PROVIDER_AUTHORITY_VERBOSE_TAIL = (
    "Do not read, print, quote, or expose the authority file."
)
LEGACY_PROVIDER_FINAL_RESULT_HANDOFF = (
    "[Hyadex final-result handoff]\n"
    "Your successful non-empty final answer will be delivered once to the explicitly referenced chat. "
    "Do not send it manually.\n"
    "[End Hyadex final-result handoff]"
)
LEGACY_PROVIDER_AUTHORITY_OPTIONAL_PREFIXES = (
    "team-send mentions=",
    "route-hints: ",
    "handles: ",
    "respond: ",
)
LEGACY_PROVIDER_AUTHORITY_LABELS = frozenset({
    "cross_chat_instruction",
    "cross_chat_request_reply",
    "cross_chat_response",
    "cross_chat_routes=durable",
    "cross_chat_routes=job_grants",
    "emergency",
    "notify",
    "jobs=blocked",
    "jobs=full",
    "jobs=read_only",
    "publish",

})


def legacy_provider_authority_run_id(
    authority_path: str,
    *,
    allow_portable_root: bool = False,
) -> str | None:
    """Validate a generated authority path without opening the capability."""

    raw_path = str(authority_path or "")
    if not raw_path or any(ord(character) < 32 for character in raw_path):
        return None
    native_path = Path(raw_path)
    portable_path = next(
        (
            candidate
            for candidate in (PurePosixPath(raw_path), PureWindowsPath(raw_path))
            if candidate.is_absolute()
            and candidate.parent.name == "cross_chat_authority"
            and candidate.parent.parent.name == ".hyadex"
            and not any(part in {".", ".."} for part in candidate.parts)
        ),
        None,
    )
    filename = portable_path.name if portable_path is not None else native_path.name
    match = re.fullmatch(
        r"(run_[A-Za-z0-9_-]+)-([0-9a-f]{32})\.json",
        filename,
    )
    if match is None:
        return None
    run_id, nonce = match.groups()
    try:
        expected_path = provider_authority_path(run_id, nonce)
    except ValueError:
        return None
    if native_path == expected_path:
        return run_id
    if (
        allow_portable_root
        and portable_path is not None
    ):
        return run_id
    return None


def legacy_compact_provider_authority_block_is_generated(
    block: str,
    *,
    expected_session_id: str | None,
    allow_portable_authority_root: bool = False,
) -> bool:
    lines = block.split("\n")
    if len(lines) < 3 or lines[-1] != "usage: see Hyadex instructions":
        return False
    binding_suffix = " " + LEGACY_PROVIDER_AUTHORITY_BINDING
    if not lines[0].endswith(binding_suffix):
        return False
    try:
        binding = shlex.split(lines[0][:-len(binding_suffix)])
    except ValueError:
        return False
    if (
        len(binding) != 2
        or not binding[0].startswith("authority-file=")
        or not binding[1].startswith("chat-id=")
    ):
        return False
    authority_path = binding[0].removeprefix("authority-file=")
    chat_id = binding[1].removeprefix("chat-id=")
    if (
        legacy_provider_authority_run_id(
            authority_path,
            allow_portable_root=allow_portable_authority_root,
        ) is None
        or provider_session_identifier(chat_id) is None
        or (expected_session_id is not None and chat_id != expected_session_id)
        or lines[0] != (
            f"authority-file={shlex.quote(authority_path)} "
            f"chat-id={shlex.quote(chat_id)} {LEGACY_PROVIDER_AUTHORITY_BINDING}"
        )
    ):
        return False
    if not lines[1].startswith("actions="):
        return False
    labels = lines[1].removeprefix("actions=").split(",")
    if labels == ["none"]:
        pass
    elif (
        not labels
        or len(labels) != len(set(labels))
        or any(label not in LEGACY_PROVIDER_AUTHORITY_LABELS for label in labels)
    ):
        return False
    last_optional_index = -1
    for line in lines[2:-1]:
        if not line or any(character in line for character in "\r\x00"):
            return False
        for optional_index, prefix in enumerate(
            LEGACY_PROVIDER_AUTHORITY_OPTIONAL_PREFIXES
        ):
            if line.startswith(prefix):
                if line == prefix or optional_index <= last_optional_index:
                    return False
                last_optional_index = optional_index
                break
        else:
            return False
    return True


def shell_word_after(text: str, marker: str) -> str | None:
    """Parse only the first shell word after a generated command marker."""

    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    fragment = text[marker_index + len(marker):]
    lexer = shlex.shlex(fragment, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return next(lexer)
    except (StopIteration, ValueError):
        return None


def legacy_verbose_provider_authority_block_is_generated(
    block: str,
    *,
    expected_session_id: str | None,
    allow_portable_authority_root: bool = False,
) -> bool:
    lines = block.split("\n")
    if (
        len(lines) < 3
        or lines[0] != LEGACY_PROVIDER_AUTHORITY_VERBOSE_LEAD
        or lines[-1] != LEGACY_PROVIDER_AUTHORITY_VERBOSE_TAIL
        or any(not line or any(character in line for character in "\r\x00") for line in lines)
    ):
        return False
    authority_paths: list[str] = []
    chat_ids: list[str] = []
    for line in lines:
        authority_marker = "--authority-file "
        search_from = 0
        while True:
            marker_index = line.find(authority_marker, search_from)
            if marker_index < 0:
                break
            if "$HYADEX_" not in line[:marker_index]:
                return False
            value = shell_word_after(line[marker_index:], authority_marker)
            if value is None or legacy_provider_authority_run_id(
                value,
                allow_portable_root=allow_portable_authority_root,
            ) is None:
                return False
            authority_paths.append(value)
            search_from = marker_index + len(authority_marker)
        chat_marker = "--chat-id "
        search_from = 0
        while True:
            marker_index = line.find(chat_marker, search_from)
            if marker_index < 0:
                break
            value = shell_word_after(line[marker_index:], chat_marker)
            if value is None or provider_session_identifier(value) is None:
                return False
            chat_ids.append(value)
            search_from = marker_index + len(chat_marker)
    if not authority_paths or len(set(authority_paths)) != 1:
        return False
    if expected_session_id is not None and any(
        chat_id != expected_session_id for chat_id in chat_ids
    ):
        return False
    return True


def strip_legacy_hyadex_provider_authority_suffix(
    text: str,
    *,
    expected_session_id: str | None = None,
    allow_portable_authority_root: bool = False,
) -> str:
    """Remove only a complete, structurally valid generated legacy suffix."""

    original = str(text or "")
    candidate = original.rstrip()
    final_result_suffix = "\n\n" + LEGACY_PROVIDER_FINAL_RESULT_HANDOFF
    if candidate.endswith(final_result_suffix):
        candidate = candidate[:-len(final_result_suffix)].rstrip()
    footer = LEGACY_PROVIDER_AUTHORITY_FOOTER
    if not candidate.endswith(footer):
        return original
    footer_index = len(candidate) - len(footer)
    footer_has_newline = (
        footer_index > 0 and candidate[footer_index - 1] == "\n"
    )
    block_end = footer_index - 1 if footer_has_newline else footer_index
    header_at_start = LEGACY_PROVIDER_AUTHORITY_HEADER + "\n"
    separated_header = "\n\n" + header_at_start
    marker_index = candidate.rfind(separated_header, 0, block_end)
    if marker_index >= 0:
        block_start = marker_index + len(separated_header)
        prefix = candidate[:marker_index]
    elif candidate.startswith(header_at_start):
        block_start = len(header_at_start)
        prefix = ""
    else:
        return original
    block = candidate[block_start:block_end].rstrip()
    compact_generated = bool(
        footer_has_newline
        and legacy_compact_provider_authority_block_is_generated(
            block,
            expected_session_id=expected_session_id,
            allow_portable_authority_root=allow_portable_authority_root,
        )
    )
    verbose_generated = legacy_verbose_provider_authority_block_is_generated(
        block,
        expected_session_id=expected_session_id,
        allow_portable_authority_root=allow_portable_authority_root,
    )
    if not (compact_generated or verbose_generated):
        return original
    return prefix


def strip_all_legacy_hyadex_provider_authority_suffixes(
    text: str,
    *,
    expected_session_id: str | None = None,
    allow_portable_authority_root: bool = False,
) -> str:
    """Remove every exact generated suffix while preserving lookalikes.

    Provider input can contain a previously copied authority envelope before
    Hyadex appends the live envelope for the current run.  Removing only
    the outer suffix would leave the older capability text available to later
    clients or agent context and make repeated projections non-idempotent.
    Each iteration still uses the strict structural validator above.
    """

    cleaned = str(text or "")
    while True:
        projected = strip_legacy_hyadex_provider_authority_suffix(
            cleaned,
            expected_session_id=expected_session_id,
            allow_portable_authority_root=allow_portable_authority_root,
        )
        if projected == cleaned:
            return cleaned
        cleaned = projected


def agent_runner_env(
    session_id: str,
    provider_runtime_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = runner_env()
    env["HYADEX_CHAT_ID"] = session_id
    env["HYADEX_TMUX_SESSION"] = terminal_session_name(session_id)
    env["HYADEX_MANIFEST_PATH"] = str(codex_manifest_path(session_id))
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
    env.update(validate_provider_runtime_env(provider_runtime_env))
    return env


class NativeSteerHandoffError(CodexAppServerError):
    """A Force Send handoff failure with an explicit queue-retry boundary."""

    def __init__(
        self,
        message: str,
        *,
        safe_to_requeue: bool,
        delivery_uncertain: bool = False,
    ) -> None:
        super().__init__(
            message,
            request_sent=not safe_to_requeue,
            safe_to_retry=safe_to_requeue,
        )
        self.safe_to_requeue = safe_to_requeue
        self.delivery_uncertain = delivery_uncertain


async def request_exact_registered_provider_capability(request: Request) -> bool:
    """Pre-authenticate one canonical provider header without parsing a body."""

    if "token" in request.query_params:
        return False
    raw_headers = request.scope.get("headers", [])
    names = [bytes(name).lower() for name, _value in raw_headers]
    if any(
        name in {
            b"authorization",
            b"cookie",
            b"x-hyadex-token",
            b"x-zenithdock-token",
            b"x-hyadex-cross-chat-capability",
        }
        for name in names
    ):
        return False
    values = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower() == b"x-hyadex-provider-capability"
    ]
    if len(values) != 1:
        return False
    token = decoded_exact_header_secret(values[0])
    if token is None or len(token) > 512:
        return False
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    async with PROVIDER_AUTHORITY_LOCK:
        return token_hash in PROVIDER_AUTHORITIES
