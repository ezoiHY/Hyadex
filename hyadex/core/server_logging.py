"""Server log files, rotation, access-log filtering and log hints for processes."""
from __future__ import annotations

from contextlib import suppress
import json
import logging.handlers
import os
from pathlib import Path
import re
import shlex
import threading
import time
from typing import Any

from hyadex.core.config import hyadex_setting
from hyadex.core.util import compact_memory_text

logger = logging.getLogger("hyadex-server")

LOG_PATH_SUFFIXES = {
    ".log", ".out", ".err", ".stderr", ".stdout", ".txt", ".jsonl", ".trace"
}


def unique_log_hints(hints: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for hint in hints:
        path = hint.get("path", "")
        source = hint.get("source", "")
        key = (path, source)
        if not path or key in seen:
            continue
        seen.add(key)
        out.append(hint)
    return out[:8]


def fd_log_hints(pid: int) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    if os.name == "nt":
        return hints
    fd_dir = Path("/proc") / str(pid) / "fd"
    for fd, label in (("1", "stdout"), ("2", "stderr")):
        try:
            target = os.readlink(fd_dir / fd)
        except OSError:
            continue
        if target.startswith(("pipe:", "socket:", "anon_inode:")):
            continue
        clean = target.removesuffix(" (deleted)")
        path = Path(clean)
        if path.is_file():
            hints.append({"source": label, "path": str(path)})
    return hints


def command_log_hints(args: str, cwd: str | None) -> list[dict[str, str]]:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    hints: list[dict[str, str]] = []
    base = Path(cwd) if cwd else None
    for token in tokens:
        clean = token.strip("'\" ,;")
        if not clean:
            continue
        suffix = Path(clean).suffix.lower()
        if suffix not in LOG_PATH_SUFFIXES:
            continue
        candidates = [Path(clean)]
        if base and not Path(clean).is_absolute():
            candidates.append(base / clean)
        for candidate in candidates:
            if candidate.is_file():
                hints.append({"source": "command", "path": str(candidate.resolve())})
                break
    return hints


def concise_error_message(value: Any) -> str:
    if value is None:
        return "Unknown error"
    if isinstance(value, str):
        text = value.strip()
        with suppress(Exception):
            parsed = json.loads(text)
            return concise_error_message(parsed)
        return text or "Unknown error"
    if isinstance(value, dict):
        if "error" in value:
            message = concise_error_message(value.get("error"))
            status = value.get("status")
            if status and f"status {status}" not in message.lower():
                return f"{message} (status {status})"
            return message
        message = str(value.get("message") or value.get("detail") or "").strip()
        code = str(value.get("code") or value.get("type") or "").strip()
        if message and code:
            return f"{message} ({code})"
        if message:
            return message
        if code:
            return code
        with suppress(Exception):
            return compact_memory_text(json.dumps(value, separators=(",", ":")), 4000)
    return str(value)


SERVER_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
SERVER_LOG_DIR_NAME = "logs"
SERVER_LOG_FILE_NAME = "hyadex-server.log"
SERVER_LOG_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
SERVER_LOG_DEFAULT_BACKUPS = 5
REPEATED_LOG_SUPPRESSION_SECONDS = 60.0
# Client polls that are pure noise at status 200. Health is hit every 5 s by
# each desktop/mobile client; the rest are list/runtime refreshes.
POLLING_ACCESS_LOG_EXACT_PATHS = frozenset({"/api/health", "/api/jobs"})
POLLING_ACCESS_LOG_PATH_RE = re.compile(
    r"^/api/sessions/[^/]+/(?:codex|claude)/runtime$"
    r"|^/api/team-hub-secure/[^/]+/v1/teams/[^/]+/network/mailbox$"
)
ACCESS_LOG_MESSAGE_RE = re.compile(
    r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[0-9.]+" (?P<status>[0-9]{3})'
)
ACCESS_LOG_TOKEN_QUERY_RE = re.compile(
    r"(?P<prefix>[?&](?:t|%74)(?:o|%6f)(?:k|%6b)"
    r"(?:e|%65)(?:n|%6e)=)[^&\s\"]*",
    re.IGNORECASE,
)


def server_log_rotation_settings() -> tuple[int, int]:
    """Return ``(max_bytes, backup_count)`` honoring the operator overrides."""

    def bounded(name: str, default: int, minimum: int) -> int:
        try:
            value = int(hyadex_setting(name, str(default)))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    return (
        bounded("LOG_MAX_BYTES", SERVER_LOG_DEFAULT_MAX_BYTES, 64 * 1024),
        bounded("LOG_BACKUPS", SERVER_LOG_DEFAULT_BACKUPS, 0),
    )


def is_polling_access_log_request(method: Any, path: Any, status: Any) -> bool:
    """True for a successful GET that a client issues on a timer."""

    if str(method or "").upper() != "GET":
        return False
    try:
        if int(status) != 200:
            return False
    except (TypeError, ValueError):
        return False
    route, _, query = str(path or "").partition("?")
    if route in POLLING_ACCESS_LOG_EXACT_PATHS:
        return True
    if route == "/api/sessions":
        return "summary=true" in query.lower().split("&")
    return POLLING_ACCESS_LOG_PATH_RE.match(route) is not None


# Old snapshot URLs can still arrive after retirement. Never log their tokens.
RETIRED_SHARE_PATH_RE = re.compile(r'''(/share/)[^/?\s"']+''')


def redact_access_log_token(value: Any) -> str:
    """Remove query and public-share bearer values before access-log handlers."""

    return RETIRED_SHARE_PATH_RE.sub(r"\1<redacted>", ACCESS_LOG_TOKEN_QUERY_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        str(value or ""),
    ))


class PollingAccessLogFilter(logging.Filter):
    """Redact URL credentials and drop successful client polling noise.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with
    ``(client, method, path, http_version, status)`` as ``record.args``; the
    formatted message is parsed as a fallback so a custom formatter or a
    future uvicorn cannot silently disable the filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            redacted_args = tuple(
                redact_access_log_token(value)
                if isinstance(value, str)
                else value
                for value in args
            )
            if redacted_args != args:
                record.args = redacted_args
            if len(redacted_args) >= 5 and "HTTP/" in str(record.msg):
                return not is_polling_access_log_request(
                    redacted_args[1],
                    redacted_args[2],
                    redacted_args[4],
                )
        if isinstance(record.msg, str):
            record.msg = redact_access_log_token(record.msg)
        try:
            match = ACCESS_LOG_MESSAGE_RE.search(record.getMessage())
        except Exception:
            return True
        if match is None:
            return True
        return not is_polling_access_log_request(
            match.group("method"),
            match.group("path"),
            match.group("status"),
        )


class RepeatedMessageFilter(logging.Filter):
    """Collapse byte-identical WARNING+ messages repeated inside a window.

    The first occurrence passes. Identical repeats within
    ``window_seconds`` of that emission are dropped; when the message changes
    (or the window lapses and the same message recurs) one summary record
    ``... (suppressed N repeats)`` is emitted ahead of the new record.
    """

    def __init__(
        self,
        window_seconds: float = REPEATED_LOG_SUPPRESSION_SECONDS,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        super().__init__()
        self.window_seconds = max(0.0, float(window_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._last_key: tuple[int, str, str] | None = None
        self._last_emitted_at = 0.0
        self._suppressed = 0
        self._emitting_summary = False

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or self._emitting_summary:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        key = (record.levelno, record.name, message)
        now = float(self._clock())
        with self._lock:
            if (
                key == self._last_key
                and now - self._last_emitted_at < self.window_seconds
            ):
                self._suppressed += 1
                return False
            suppressed = self._suppressed
            previous_key = self._last_key
            self._suppressed = 0
            self._last_key = key
            self._last_emitted_at = now
        if suppressed and previous_key is not None:
            self._emit_summary(record, previous_key, suppressed)
        return True

    def _emit_summary(
        self,
        record: logging.LogRecord,
        previous_key: tuple[int, str, str],
        suppressed: int,
    ) -> None:
        levelno, name, message = previous_key
        summary = logging.LogRecord(
            name,
            levelno,
            record.pathname,
            record.lineno,
            "%s (suppressed %d repeats)",
            (message, suppressed),
            None,
        )
        self._emitting_summary = True
        try:
            logging.getLogger(name).handle(summary)
        except Exception:
            pass
        finally:
            self._emitting_summary = False


class ServerLoggingHandlers:
    """What ``configure_server_logging`` installed, for tests and teardown."""

    __slots__ = ("access_filter", "file_handler", "repeat_filter", "stream_handler")

    def __init__(
        self,
        *,
        stream_handler: logging.Handler,
        file_handler: logging.handlers.RotatingFileHandler | None,
        access_filter: PollingAccessLogFilter,
        repeat_filter: RepeatedMessageFilter,
    ) -> None:
        self.stream_handler = stream_handler
        self.file_handler = file_handler
        self.access_filter = access_filter
        self.repeat_filter = repeat_filter


def configure_server_logging(
    state_dir: Path,
    *,
    level: int = logging.INFO,
    root_logger: logging.Logger | None = None,
    access_logger: logging.Logger | None = None,
    error_logger: logging.Logger | None = None,
    server_logger: logging.Logger | None = None,
) -> ServerLoggingHandlers:
    """Install the stderr handler, a rotating file, and the noise filters.

    launchd/systemd keep capturing stderr as before. The rotating file under
    ``state_dir/logs`` is the bounded on-disk record; the service managers'
    own redirect files are not rotated, so the poll and repeat filters keep
    them from growing without bound too.
    """

    root = root_logger if root_logger is not None else logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(SERVER_LOG_FORMAT)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    file_handler: logging.handlers.RotatingFileHandler | None = None
    try:
        log_dir = Path(state_dir) / SERVER_LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        max_bytes, backup_count = server_log_rotation_settings()
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / SERVER_LOG_FILE_NAME,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        logger.warning(
            "server log file unavailable; logging to stderr only: %s",
            concise_error_message(exc),
        )
    access_filter = PollingAccessLogFilter()
    access_target = access_logger or logging.getLogger("uvicorn.access")
    error_target = error_logger or logging.getLogger("uvicorn.error")
    access_target.addFilter(access_filter)
    if error_target is not access_target:
        # Uvicorn emits WebSocket accepted/rejected/closed request lines on
        # uvicorn.error with the raw query path in tuple arg 1, not through its
        # HTTP access logger. The same filter redacts those tuple shapes while
        # limiting polling suppression to HTTP-formatted records.
        error_target.addFilter(access_filter)
    repeat_filter = RepeatedMessageFilter()
    (server_logger or logger).addFilter(repeat_filter)
    return ServerLoggingHandlers(
        stream_handler=stream_handler,
        file_handler=file_handler,
        access_filter=access_filter,
        repeat_filter=repeat_filter,
    )
