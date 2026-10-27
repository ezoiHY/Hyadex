"""Chat terminals: tmux sessions, windows, attachments and terminal I/O."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import fcntl
import json
import logging.handlers
import os
from pathlib import Path
import pty
import pwd
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
from typing import Any
import uuid

from fastapi import HTTPException, WebSocket

from hyadex.core.config import (
    BACKEND_CODEX,
    DEFAULT_CWD,
    TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS,
    TMUX_INSTANCE_ARGS,
)
from hyadex.core.event_log import read_events
from hyadex.core.paths import existing_cwd, now_iso
from hyadex.core.processes import (
    best_process_label,
    cgroup_is_within,
    descendant_processes,
    hyadex_server_systemd_cgroup,
    process_cgroup_paths,
    ps_process_rows,
    run_tmux,
    tmux_bin,
    tmux_bootstrap_environment,
    tmux_server_pid,
    working_tmux_bin,
)
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ensure_backend_update_allowed,
    ensure_claude_permission_mode_update_allowed,
    ensure_session_not_deleting,
    ensure_session_not_initializing,
    execution_maintenance_blocks_work,
    managed_server_restart_blocks_work,
    meaningful_chat_cwd,
    preview_session_runtime_update,
    public_session,
    session_lifecycle_lock,
    STORE,
    UpdateSessionRequest,
)
from hyadex.core.state import DELETING_SESSIONS, SESSION_LIFECYCLE_UPDATE_FIELDS
from hyadex.core.util import runner_env, terminal_dimensions
from hyadex.core.wiring import WIRING
from hyadex.providers.authority import terminal_session_name
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.runtime import PROVIDER_SECRET_ENV_NAMES

logger = logging.getLogger("hyadex-server")

TMUX_CAPTURE_MAX_LINES = 2_000
TERMINAL_READ_BYTES = 64 * 1024
TERMINAL_INPUT_MAX_BYTES = 1024 * 1024
TERMINAL_INPUT_WRITE_TIMEOUT_SECONDS = 5.0


def tmux_capability(*, use_cache: bool = False) -> dict[str, Any]:
    available = working_tmux_bin(use_cache=use_cache) is not None
    if available:
        return {
            "available": True,
            "required": False,
            "message": "tmux is available.",
            "action": None,
        }
    if sys.platform == "darwin":
        action = "Install tmux with Homebrew (brew install tmux), then rerun the HyadexServer installer."
    elif sys.platform.startswith("linux"):
        action = (
            "Install tmux with your package manager (for example: sudo apt install tmux, "
            "sudo dnf install tmux, or sudo pacman -S tmux), then rerun the HyadexServer installer."
        )
    else:
        action = "Install tmux on the agent host, then rerun the HyadexServer installer."
    return {
        "available": False,
        "required": False,
        "message": (
            "tmux was not found or failed its version check, so the persistent chat terminal "
            "and tmux-pane inspection are unavailable. The rest of HyadexServer works without it."
        ),
        "action": action,
    }


def tmux_session_exists(name: str) -> bool:
    return run_tmux(["has-session", "-t", name], check=False).returncode == 0


def bootstrap_isolated_tmux_server() -> bool:
    """Best-effort bootstrap of a missing tmux server outside our service.

    A tmux daemon first spawned by HyadexServer inherits hyadex-server.service's
    cgroup. systemd then kills that daemon and its terminal sessions when
    the service restarts. A transient user scope gives the shared tmux daemon
    an independent lifetime. Existing servers are never moved or replaced.
    """

    service_cgroup = hyadex_server_systemd_cgroup()
    if service_cgroup is None or tmux_server_pid() is not None:
        return False
    systemd_run = shutil.which("systemd-run")
    tmux = working_tmux_bin(use_cache=True)
    if not systemd_run or not tmux:
        return False
    token = uuid.uuid4().hex[:12]
    sentinel = f"hyadex_server_tmux_bootstrap_{token}"
    unit = f"hyadex-server-tmux-bootstrap-{token}.scope"
    environment = tmux_bootstrap_environment()
    try:
        result = subprocess.run(
            [
                systemd_run,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                tmux,
                *TMUX_INSTANCE_ARGS,
                "new-session",
                "-d",
                "-s",
                sentinel,
                "sleep 60",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        run_tmux(["kill-session", "-t", sentinel], check=False)
        logger.warning("could not bootstrap isolated tmux server: %s", exc)
        return False
    if result.returncode != 0:
        run_tmux(["kill-session", "-t", sentinel], check=False)
        logger.warning(
            "could not bootstrap isolated tmux server: %s",
            (result.stderr or result.stdout or f"exit {result.returncode}").strip()[:500],
        )
        return False
    pid = tmux_server_pid()
    isolated = bool(
        pid is not None
        and not any(
            cgroup_is_within(path, service_cgroup)
            for path in process_cgroup_paths(pid)
        )
    )
    if isolated:
        # Keep the verified daemon alive after removing the temporary session.
        run_tmux(["set-option", "-g", "exit-empty", "off"], check=False)
    run_tmux(["kill-session", "-t", sentinel], check=False)
    if not isolated:
        logger.warning(
            "tmux bootstrap did not escape service cgroup %s",
            service_cgroup,
        )
    return isolated


def scrub_tmux_global_secret_environment() -> None:
    """Remove credentials from an existing daemon and every copied session env."""

    try:
        pid = tmux_server_pid()
    except Exception:
        return
    if pid is None:
        return
    global_commands: list[str] = []
    for name in PROVIDER_SECRET_ENV_NAMES:
        if global_commands:
            global_commands.append(";")
        global_commands.extend(["set-environment", "-gu", name])
    if global_commands:
        with suppress(Exception):
            run_tmux(global_commands, check=False)
    sessions: list[str] = []
    with suppress(Exception):
        result = run_tmux(
            ["list-sessions", "-F", "#{session_name}"],
            check=False,
        )
        if result.returncode == 0:
            sessions = [
                line.strip()
                for line in str(result.stdout or "").splitlines()
                if line.strip()
            ]
    session_commands: list[str] = []
    for session_name in sessions:
        for name in PROVIDER_SECRET_ENV_NAMES:
            if session_commands:
                session_commands.append(";")
            session_commands.extend(
                ["set-environment", "-t", session_name, "-u", name]
            )
    if session_commands:
        with suppress(Exception):
            run_tmux(session_commands, check=False)


TERMINAL_FALLBACK_SHELLS = ("/bin/bash", "/bin/zsh", "/bin/sh")
TERMINAL_DISABLED_SHELL_NAMES = {"false", "nologin"}
TERMINAL_SESSION_LOCK_STRIPES = tuple(threading.Lock() for _ in range(64))


def terminal_session_lock(session_id: str) -> threading.Lock:
    return TERMINAL_SESSION_LOCK_STRIPES[
        hash(str(session_id)) % len(TERMINAL_SESSION_LOCK_STRIPES)
    ]


def valid_terminal_login_shell(candidate: str | None) -> str | None:
    """Return an executable login shell path, excluding disabled account shells."""
    raw = str(candidate or "").strip()
    if not raw or "\x00" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.name.lower() in TERMINAL_DISABLED_SHELL_NAMES:
        return None
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        return str(path)
    except OSError:
        return None


def resolve_terminal_login_shell() -> str:
    """Resolve the server account's real shell without trusting inherited ``$SHELL``."""
    account_shell: str | None = None
    try:
        account_shell = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        pass
    account_shell = str(account_shell or "").strip()
    if account_shell:
        account_shell_name = Path(account_shell).name.lower()
        if account_shell_name in TERMINAL_DISABLED_SHELL_NAMES:
            raise HTTPException(
                status_code=503,
                detail="the agent server user has a disabled login shell",
            )
        if shell := valid_terminal_login_shell(account_shell):
            return shell
        raise HTTPException(
            status_code=503,
            detail="the agent server user's configured login shell is unavailable",
        )
    for candidate in TERMINAL_FALLBACK_SHELLS:
        if shell := valid_terminal_login_shell(candidate):
            return shell
    raise HTTPException(
        status_code=503,
        detail="no executable login shell is available for the agent server user",
    )


def terminal_session_path() -> str:
    """Build a useful, deterministic PATH even when tmux inherited a service PATH."""
    configured = runner_env().get("PATH") or os.defpath
    entries: list[str] = []
    for entry in configured.split(os.pathsep):
        clean = entry.strip()
        if clean and clean not in entries:
            entries.append(clean)
    for standard in ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if standard not in entries:
            entries.append(standard)
    return os.pathsep.join(entries)


def terminal_login_command(shell: str, path: str | None = None) -> str:
    """Return a tmux shell-command that starts a login shell with known env."""
    if path is None:
        return f"exec {shlex.quote(shell)} -l"
    return (
        f"SHELL={shlex.quote(shell)} PATH={shlex.quote(path)} "
        f"exec {shlex.quote(shell)} -l"
    )


def configure_terminal_session_shell(name: str, shell: str, path: str) -> None:
    """Scope shell defaults to one Hyadex session, never the shared tmux server."""
    login_command = terminal_login_command(shell)
    run_tmux(["set-environment", "-t", name, "SHELL", shell])
    run_tmux(["set-environment", "-t", name, "PATH", path])
    run_tmux(["set-option", "-t", name, "default-shell", shell])
    run_tmux(["set-option", "-t", name, "default-command", login_command])


def set_pty_dimensions(fd: int, columns: int, rows: int) -> None:
    cols, lines = terminal_dimensions(columns, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))


def resize_terminal_window(session_id: str, columns: int, rows: int) -> tuple[int, int]:
    cols, lines = terminal_dimensions(columns, rows)
    name = terminal_session_name(session_id)
    # PTY TIOCSWINSZ alone is not enough when a persistent tmux session has
    # previously been attached from a differently sized Mac or iPad.
    run_tmux(["resize-window", "-t", name, "-x", str(cols), "-y", str(lines)], check=False)
    return cols, lines


def _ensure_terminal_session_locked(
    session_id: str,
    cwd: str | None = None,
    *,
    columns: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_deleting(session_id)
    if bool(sess.get("archived")):
        raise HTTPException(status_code=409, detail="unarchive this chat before opening its terminal")
    name = terminal_session_name(session_id)
    shell = resolve_terminal_login_shell()
    path = terminal_session_path()
    created = False
    if not tmux_session_exists(name):
        # Keep the shared daemon outside hyadex-server.service when this is the
        # first tmux use on a Linux systemd-user installation.
        bootstrap_isolated_tmux_server()
        scrub_tmux_global_secret_environment()
        workdir = existing_cwd(cwd or sess.get("cwd") or DEFAULT_CWD)
        cols, lines = terminal_dimensions(columns, rows)
        try:
            run_tmux([
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                str(cols),
                "-y",
                str(lines),
                "-c",
                workdir,
                terminal_login_command(shell, path),
            ])
            created = True
        except HTTPException:
            # Another server process or a direct tmux client can still win
            # outside this process-local lock. A now-existing exact session is
            # the successful outcome; every other failure remains visible.
            if not tmux_session_exists(name):
                raise
    else:
        scrub_tmux_global_secret_environment()
    # The tmux daemon may predate HyadexServer and carry a stale SHELL/PATH
    # from an SSH tunnel or service. Keep all repairs scoped to this Dock-owned
    # session. This also corrects future windows/panes in pre-existing sessions
    # without interrupting a user's currently running pane.
    configure_terminal_session_shell(name, shell, path)
    # These are session-scoped defaults. Existing user-created windows and
    # panes remain untouched while newly created panes keep useful history.
    run_tmux(["set-option", "-t", name, "history-limit", "100000"], check=False)
    mouse_initialized = run_tmux(
        ["show-options", "-qv", "-t", name, "@hyadex_mouse_initialized"],
        check=False,
    ).stdout.strip()
    if created or mouse_initialized != "1":
        # Local selection is the useful default in an embedded terminal. Users
        # can opt into tmux mouse capture from the terminal actions menu.
        run_tmux(["set-option", "-t", name, "mouse", "off"], check=False)
        run_tmux(["set-option", "-t", name, "@hyadex_mouse_initialized", "1"], check=False)
    run_tmux(["set-option", "-t", name, "window-size", "latest"], check=False)
    if columns is not None or rows is not None:
        resize_terminal_window(session_id, columns or 120, rows or 36)
    # Hyadex renders its own window tabs and terminal controls. The tmux
    # status line would duplicate those controls and consume a row in the PTY.
    run_tmux(["set-option", "-t", name, "status", "off"], check=False)
    return terminal_snapshot(session_id, created=created)


def ensure_terminal_session(
    session_id: str,
    cwd: str | None = None,
    *,
    columns: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        return _ensure_terminal_session_locked(
            session_id,
            cwd,
            columns=columns,
            rows=rows,
        )


def spawn_terminal_client(
    session_id: str,
    cwd: str | None,
    columns: int,
    rows: int,
) -> tuple[subprocess.Popen[bytes], int, str]:
    snapshot = ensure_terminal_session(session_id, cwd, columns=columns, rows=rows)
    name = str(snapshot["name"])
    workdir = existing_cwd(snapshot.get("cwd") or cwd or STORE.sessions[session_id].get("cwd") or DEFAULT_CWD)
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    for secret_name in PROVIDER_SECRET_ENV_NAMES:
        env.pop(secret_name, None)
    # A server launched from tmux must still be able to attach a child client.
    # Keeping these variables makes tmux reject the nested attachment.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.setdefault("LANG", "C.UTF-8")
    try:
        set_pty_dimensions(slave_fd, columns, rows)
        process = subprocess.Popen(
            [tmux_bin(), *TMUX_INSTANCE_ARGS, "attach-session", "-t", name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workdir,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    # PTY input is fed by an asyncio WebSocket task. A blocking master can
    # freeze the entire server when the foreground process stops draining its
    # tty input, so all later writes are readiness-driven.
    try:
        os.set_blocking(master_fd, False)
    except BaseException:
        stop_terminal_client(process, master_fd)
        raise
    return process, master_fd, name


async def write_terminal_input(fd: int, data: bytes) -> None:
    if len(data) > TERMINAL_INPUT_MAX_BYTES:
        raise ValueError(
            f"terminal input frame exceeds {TERMINAL_INPUT_MAX_BYTES} bytes"
        )
    loop = asyncio.get_running_loop()
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            writable: asyncio.Future[None] = loop.create_future()

            def on_writable() -> None:
                if not writable.done():
                    writable.set_result(None)

            loop.add_writer(fd, on_writable)
            try:
                await asyncio.wait_for(
                    writable,
                    timeout=TERMINAL_INPUT_WRITE_TIMEOUT_SECONDS,
                )
            finally:
                loop.remove_writer(fd)
            continue
        if written <= 0:
            raise OSError(errno.EIO, "terminal input closed")
        view = view[written:]


async def read_terminal_output(fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[bytes] = loop.create_future()

    def on_readable() -> None:
        if ready.done():
            return
        try:
            ready.set_result(os.read(fd, TERMINAL_READ_BYTES))
        except BlockingIOError:
            return
        except OSError as exc:
            ready.set_exception(exc)

    loop.add_reader(fd, on_readable)
    try:
        return await ready
    finally:
        loop.remove_reader(fd)


def stop_terminal_client(process: subprocess.Popen[bytes], master_fd: int) -> None:
    with suppress(OSError):
        os.close(master_fd)
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)


class TerminalAttachmentAdmissionClosed(RuntimeError):
    """Raised when a terminal reconnect races a managed server drain."""


async def await_terminal_task_after_cancellation(
    task: asyncio.Task[Any],
) -> Any:
    """Finish one owned task even if its caller is cancelled repeatedly.

    This helper is used only after the caller has already committed to
    propagating ``CancelledError``. Temporarily consuming cancellation counts
    lets it capture and reap a to-thread spawn result before re-raising that
    original cancellation to the endpoint owner.
    """

    current = asyncio.current_task()

    def consume_cancellations() -> None:
        if current is None:
            return
        uncancel = getattr(current, "uncancel", None)
        cancelling = getattr(current, "cancelling", None)
        if not callable(uncancel) or not callable(cancelling):
            return
        for _index in range(max(0, int(cancelling()))):
            uncancel()

    consume_cancellations()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            consume_cancellations()
            continue
        except BaseException:
            break
    return task.result()


class TerminalAttachmentRegistry:
    """Own every tmux attachment transport spawned by this server process.

    The shared tmux daemon and its sessions intentionally live outside the
    service cgroup.  Only the short-lived ``tmux attach-session`` transports
    belong here.  Update admission and terminal reservation share one lock so
    a reconnect is either registered for retirement or rejected before it can
    spawn an untracked service-cgroup child.
    """

    def __init__(self) -> None:
        self._attachments: dict[WebSocket, dict[str, Any]] = {}
        self._admission_open = True
        self._permanently_closed = False
        self._lock = asyncio.Lock()

    async def reserve(self, session_id: str, ws: WebSocket) -> bool:
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("terminal attachment has no owning task")
        async with self._lock:
            maintenance_blocked = execution_maintenance_blocks_work()
            restart_blocked = managed_server_restart_blocks_work()
            if (
                not self._permanently_closed
                and not self._admission_open
                and not maintenance_blocked
                and not restart_blocked
            ):
                # A released maintenance lease reopens terminal admission.
                self._admission_open = True
            if (
                self._permanently_closed
                or not self._admission_open
                or maintenance_blocked
                or restart_blocked
            ):
                return False
            self._attachments[ws] = {
                "session_id": session_id,
                "owner_task": owner_task,
                "spawn_task": None,
                "cleanup_task": None,
                "process": None,
                "master_fd": None,
            }
            return True

    async def spawn(
        self,
        session_id: str,
        ws: WebSocket,
        cwd: str | None,
        columns: int,
        rows: int,
    ) -> tuple[subprocess.Popen[bytes], int, str]:
        """Spawn and bind one reserved attachment without a cancellation leak."""

        async with self._lock:
            entry = self._attachments.get(ws)
            if entry is None or entry.get("session_id") != session_id:
                raise TerminalAttachmentAdmissionClosed(
                    "terminal attachment admission is closed"
                )
            spawn_task = asyncio.create_task(
                asyncio.to_thread(
                    spawn_terminal_client,
                    session_id,
                    cwd,
                    columns,
                    rows,
                )
            )
            entry["spawn_task"] = spawn_task

        spawned: tuple[subprocess.Popen[bytes], int, str] | None = None
        try:
            # Cancelling an asyncio to_thread waiter does not stop its worker.
            # Shield it and explicitly reap a late result before propagating
            # cancellation, otherwise an attach client can appear after the
            # server has already finished terminal cleanup.
            spawned = await asyncio.shield(spawn_task)
            process, master_fd, _name = spawned
            async with self._lock:
                current = self._attachments.get(ws)
                if current is entry:
                    current["process"] = process
                    current["master_fd"] = master_fd
                admitted = current is entry and self._admission_open
            if not admitted:
                await asyncio.to_thread(
                    stop_terminal_client,
                    process,
                    master_fd,
                )
                raise TerminalAttachmentAdmissionClosed(
                    "terminal attachment admission is closed"
                )
            return spawned
        except asyncio.CancelledError:
            # Hand the non-cancellable worker result to an independent task
            # before awaiting anything else. A second cancellation of the
            # WebSocket owner can then interrupt only the shielded wait below,
            # never ownership of the eventual process/fd pair.
            cleanup_task = entry.get("cleanup_task")
            if not isinstance(cleanup_task, asyncio.Task):
                cleanup_task = asyncio.create_task(
                    self._reap_cancelled_spawn(
                        ws,
                        entry,
                        spawn_task,
                        spawned,
                    ),
                    name="terminal-attachment-late-spawn-cleanup",
                )
                entry["cleanup_task"] = cleanup_task
            with suppress(BaseException):
                await await_terminal_task_after_cancellation(cleanup_task)
            raise

    async def _reap_cancelled_spawn(
        self,
        ws: WebSocket,
        entry: dict[str, Any],
        spawn_task: asyncio.Task[Any],
        spawned: tuple[subprocess.Popen[bytes], int, str] | None,
    ) -> None:
        if spawned is None:
            try:
                spawned = await asyncio.shield(spawn_task)
            except BaseException:
                return
        process, master_fd, _name = spawned
        async with self._lock:
            if self._attachments.get(ws) is entry:
                entry["process"] = process
                entry["master_fd"] = master_fd
        try:
            await asyncio.to_thread(
                stop_terminal_client,
                process,
                master_fd,
            )
        finally:
            async with self._lock:
                if self._attachments.get(ws) is entry:
                    entry["process"] = None
                    entry["master_fd"] = None

    async def release(self, ws: WebSocket) -> None:
        async with self._lock:
            entry = self._attachments.get(ws)
            if entry is None:
                return
            owned_tasks = (
                entry.get("spawn_task"),
                entry.get("cleanup_task"),
            )
            if any(
                isinstance(task, asyncio.Task) and not task.done()
                for task in owned_tasks
            ):
                return
            self._attachments.pop(ws, None)

    async def close_admission_and_all(self, *, permanent: bool = False) -> int:
        """Fence reconnects, close every attachment, and join its owner."""

        async with self._lock:
            self._admission_open = False
            if permanent:
                self._permanently_closed = True
            entries = list(self._attachments.items())

        await asyncio.gather(
            *(
                asyncio.wait_for(
                    ws.close(code=1012),
                    timeout=TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS,
                )
                for ws, _entry in entries
            ),
            return_exceptions=True,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TERMINAL_ATTACHMENT_CLOSE_TIMEOUT_SECONDS
        current_task = asyncio.current_task()
        owner_tasks = {
            entry.get("owner_task")
            for _ws, entry in entries
            if (
                isinstance(entry.get("owner_task"), asyncio.Task)
                and entry.get("owner_task") is not current_task
                and not entry["owner_task"].done()
            )
        }
        for owner_task in owner_tasks:
            owner_task.cancel()
        if owner_tasks:
            await asyncio.wait(
                owner_tasks,
                timeout=max(0.0, deadline - loop.time()),
            )

        # ``asyncio.to_thread`` keeps running after its waiter is cancelled.
        # The endpoint owns a shielded spawn task plus an independent cleanup
        # task, both retained in the registry until their worker has finished.
        # Join those exact tasks before lifecycle cleanup can consider the
        # service cgroup empty. A late spawn can therefore never materialize
        # after that proof.
        async with self._lock:
            tracked_tasks = {
                task
                for entry in self._attachments.values()
                for task in (
                    entry.get("spawn_task"),
                    entry.get("cleanup_task"),
                )
                if isinstance(task, asyncio.Task) and not task.done()
            }
        if tracked_tasks:
            await asyncio.wait(
                tracked_tasks,
                timeout=max(0.0, deadline - loop.time()),
            )

        async with self._lock:
            pending = {
                task
                for entry in self._attachments.values()
                for task in (
                    entry.get("owner_task"),
                    entry.get("spawn_task"),
                    entry.get("cleanup_task"),
                )
                if (
                    isinstance(task, asyncio.Task)
                    and task is not current_task
                    and not task.done()
                )
            }

        if pending:
            raise RuntimeError(
                "terminal attachment cleanup did not finish before its deadline"
            )

        # A second cancellation or a faulty endpoint must not leave a bound
        # attachment alive.  Directly reap any exact process still registered.
        async with self._lock:
            remaining = list(self._attachments.items())
        bound = [
            (entry.get("process"), entry.get("master_fd"))
            for _ws, entry in remaining
            if entry.get("process") is not None
            and isinstance(entry.get("master_fd"), int)
        ]
        if bound:
            await asyncio.gather(
                *(
                    asyncio.to_thread(stop_terminal_client, process, master_fd)
                    for process, master_fd in bound
                ),
                return_exceptions=True,
            )

        async with self._lock:
            for ws, entry in list(self._attachments.items()):
                owner_task = entry.get("owner_task")
                tracked = (
                    entry.get("spawn_task"),
                    entry.get("cleanup_task"),
                )
                if (
                    isinstance(owner_task, asyncio.Task)
                    and owner_task.done()
                    and all(
                        not isinstance(task, asyncio.Task) or task.done()
                        for task in tracked
                    )
                ):
                    self._attachments.pop(ws, None)

        return len(entries)

    async def reopen_if_lifecycle_inactive(self) -> bool:
        async with self._lock:
            if self._permanently_closed:
                return False
            if (
                execution_maintenance_blocks_work()
                or managed_server_restart_blocks_work()
            ):
                return False
            self._admission_open = True
            return True

    async def reopen_admission(self) -> None:
        await self.reopen_if_lifecycle_inactive()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "admission_open": self._admission_open,
                "permanently_closed": self._permanently_closed,
                "active_connections": len(self._attachments),
                "active_sessions": len({
                    str(entry.get("session_id") or "")
                    for entry in self._attachments.values()
                }),
            }


TERMINAL_ATTACHMENTS = TerminalAttachmentRegistry()


def terminal_snapshot(session_id: str, *, lines: int = 240, created: bool = False) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    exists = tmux_session_exists(name)
    line_count = max(20, min(int(lines or 240), TMUX_CAPTURE_MAX_LINES))
    capture = ""
    cwd = None
    command = None
    pane_pid = None
    attached = None
    columns = None
    rows = None
    if exists:
        capture = run_tmux(["capture-pane", "-t", name, "-p", "-J", "-S", f"-{line_count}"]).stdout
        meta = run_tmux(
            [
                "display-message",
                "-p",
                "-t",
                name,
                "#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}\t#{session_attached}\t#{pane_width}\t#{pane_height}",
            ],
            check=False,
        )
        if meta.returncode == 0:
            parts = meta.stdout.rstrip("\n").split("\t")
            cwd = parts[0] if len(parts) > 0 and parts[0] else None
            command = parts[1] if len(parts) > 1 and parts[1] else None
            pane_pid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            attached = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            columns = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
            rows = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None
    return {
        "session_id": session_id,
        "name": name,
        "exists": exists,
        "created": created,
        "cwd": cwd,
        "command": command,
        "pane_pid": pane_pid,
        "attached": attached,
        "columns": columns,
        "rows": rows,
        "lines": line_count,
        "text": capture,
        "updated_at": now_iso(),
    }


def require_existing_terminal_session(session_id: str) -> str:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_deleting(session_id)
    if bool(session.get("archived")):
        raise HTTPException(status_code=409, detail="archived chats do not have an active terminal")
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        raise HTTPException(status_code=409, detail="terminal session ended; reopen it to continue")
    return name


def send_terminal_input(session_id: str, text: str | None = None, *, enter: bool = True, key: str | None = None) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        name = require_existing_terminal_session(session_id)
        if key:
            run_tmux(["send-keys", "-t", name, key])
        if text:
            run_tmux(["send-keys", "-t", name, "-l", text])
        if enter:
            run_tmux(["send-keys", "-t", name, "Enter"])
        return terminal_snapshot(session_id)


def scroll_terminal_history(session_id: str, delta: int, *, managed: bool = False) -> bool:
    """Scroll the active tmux pane's persistent history without enabling mouse capture."""
    if session_id not in STORE.sessions:
        return False
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        return False
    amount = max(1, min(abs(int(delta or 0)), 80))
    if not delta:
        return False
    pane_in_mode = run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_in_mode}"],
        check=False,
    ).stdout.strip() == "1"
    entered_copy_mode = False
    if delta < 0:
        if not pane_in_mode:
            # Keep persistent tmux history scrolling without painting tmux's
            # copy-mode position indicator over the terminal's top row. Older
            # tmux versions predate -H, so preserve scrolling with -e there.
            entered = run_tmux(["copy-mode", "-eH", "-t", name], check=False)
            if entered.returncode != 0:
                entered = run_tmux(["copy-mode", "-e", "-t", name], check=False)
            entered_copy_mode = entered.returncode == 0
        run_tmux(["send-keys", "-X", "-N", str(amount), "-t", name, "scroll-up"], check=False)
    elif pane_in_mode:
        run_tmux(["send-keys", "-X", "-N", str(amount), "-t", name, "scroll-down"], check=False)
    still_in_mode = run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_in_mode}"],
        check=False,
    ).stdout.strip() == "1"
    return still_in_mode and (managed or entered_copy_mode)


def _kill_terminal_session_locked(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    existed = False
    if shutil.which("tmux") is not None:
        existed = tmux_session_exists(name)
        if existed:
            run_tmux(["kill-session", "-t", name], check=False)
    return {
        "session_id": session_id,
        "name": name,
        "exists": False,
        "killed": existed,
        "text": "",
        "updated_at": now_iso(),
    }


def kill_terminal_session(session_id: str) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        return _kill_terminal_session_locked(session_id)


def terminal_windows_snapshot(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        return {"session_id": session_id, "name": name, "exists": False, "mouse_enabled": False, "windows": []}
    result = run_tmux([
        "list-windows",
        "-t",
        name,
        "-F",
        "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}",
    ])
    windows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        windows.append({
            "id": parts[0],
            "index": int(parts[1]) if parts[1].isdigit() else 0,
            "name": parts[2] or "shell",
            "active": parts[3] == "1",
            "panes": int(parts[4]) if parts[4].isdigit() else 1,
        })
    mouse = run_tmux(["show-options", "-qv", "-t", name, "mouse"], check=False).stdout.strip()
    return {
        "session_id": session_id,
        "name": name,
        "exists": True,
        "mouse_enabled": mouse == "on",
        "windows": windows,
    }


def _terminal_action_locked(session_id: str, action: str, target: str | None = None) -> dict[str, Any]:
    name = require_existing_terminal_session(session_id)
    clean_action = str(action or "").strip().lower()
    active_cwd = run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_current_path}"],
        check=False,
    ).stdout.strip()
    workdir = existing_cwd(active_cwd or STORE.sessions[session_id].get("cwd") or DEFAULT_CWD)
    if clean_action == "new-window":
        run_tmux(["new-window", "-t", name, "-c", workdir])
    elif clean_action == "split-right":
        run_tmux(["split-window", "-h", "-t", name, "-c", workdir])
    elif clean_action == "split-down":
        run_tmux(["split-window", "-v", "-t", name, "-c", workdir])
    elif clean_action == "next-window":
        run_tmux(["next-window", "-t", name])
    elif clean_action == "previous-window":
        run_tmux(["previous-window", "-t", name])
    elif clean_action == "select-window":
        clean_target = str(target or "").strip()
        if not re.fullmatch(r"\d+", clean_target):
            raise HTTPException(status_code=400, detail="invalid tmux window")
        run_tmux(["select-window", "-t", f"{name}:{clean_target}"])
    elif clean_action == "kill-window":
        clean_target = str(target or "").strip()
        if not re.fullmatch(r"\d+", clean_target):
            raise HTTPException(status_code=400, detail="invalid tmux window")
        windows = terminal_windows_snapshot(session_id)["windows"]
        if len(windows) <= 1:
            raise HTTPException(
                status_code=409,
                detail="This is the final window. Kill the terminal session instead.",
            )
        if not any(str(window["index"]) == clean_target for window in windows):
            raise HTTPException(status_code=404, detail="tmux window not found")
        run_tmux(["kill-window", "-t", f"{name}:{clean_target}"])
    elif clean_action == "kill-pane":
        pane_count = run_tmux(["list-panes", "-t", name, "-F", "#{pane_id}"]).stdout.splitlines()
        if len(pane_count) <= 1:
            raise HTTPException(status_code=409, detail="This is the last pane. Kill the terminal session instead.")
        run_tmux(["kill-pane", "-t", name])
    elif clean_action == "toggle-mouse":
        current = run_tmux(["show-options", "-qv", "-t", name, "mouse"], check=False).stdout.strip()
        run_tmux(["set-option", "-t", name, "mouse", "off" if current == "on" else "on"])
        run_tmux(["set-option", "-t", name, "@hyadex_mouse_initialized", "1"], check=False)
    else:
        raise HTTPException(status_code=400, detail="unsupported terminal action")
    return terminal_windows_snapshot(session_id)


def terminal_action(session_id: str, action: str, target: str | None = None) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        return _terminal_action_locked(session_id, action, target)


TMUX_SUBMITTER_KEYWORDS = (
    "submit", "submitter", "sbatch", "slurm", "osmo", "train", "training",
    "render", "rollout", "eval", "launch", "wandb", "ray", "torchrun",
)
TMUX_CHAT_MATCH_LABELS = {"chat tmux", "chat target"}
TMUX_TARGET_RE = re.compile(
    r"\btmux\b[^\n;&|]*"
    r"(?:\s-[A-Za-z]*[st]\s+|\s--(?:session-name|target|target-pane|target-session)\s+)"
    r"['\"]?([A-Za-z0-9_.:@+-]{4,})"
)
TMUX_NOISE_TOKENS = {
    "bash", "chat", "codex", "claude", "default", "false", "general",
    "home", "launch", "local", "login", "none", "null", "osmo", "python",
    "python3", "script", "scripts", "server", "sleep", "submit", "submitter",
    "tail", "this", "true", "wandb", "hyadex", "hyadexserver",
}


def path_is_within(path: str | None, root: str | None) -> bool:
    if not path or not root:
        return False
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        base = Path(root).expanduser().resolve(strict=False)
        return candidate == base or base in candidate.parents
    except Exception:
        clean_path = path.rstrip("/")
        clean_root = root.rstrip("/")
        return clean_path == clean_root or clean_path.startswith(clean_root + "/")


def tmux_target_token(token: str) -> str | None:
    clean = token.strip(" \t\r\n\"'`.,;:()[]{}<>").lower()
    if len(clean) < 4 or clean in TMUX_NOISE_TOKENS:
        return None
    if clean.isdigit():
        return None
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1]
    if len(clean) < 8 and not re.search(r"[_@:+.-]|\d", clean):
        return None
    return clean


def tmux_explicit_chat_targets(session_id: str, sess: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        if clean := tmux_target_token(str(value)):
            tokens.add(clean)

    add(terminal_session_name(session_id))
    for key in ("session_id", "claude_session_id", "codex_thread_id"):
        add(sess.get(key))

    for event in read_events(session_id, limit=240, tail=True):
        text = json.dumps(event, ensure_ascii=False)
        for match in TMUX_TARGET_RE.finditer(text):
            add(match.group(1))
        if len(tokens) > 80:
            break
    return tokens


def tmux_panes_snapshot(session_id: str, *, include_all: bool = False) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    result = run_tmux([
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_active}\t#{session_attached}\t#{pane_title}",
    ], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip().lower()
        if "no server running" in stderr:
            return {
                "session_id": session_id,
                "panes": [],
                "total_panes": 0,
                "filtered": not include_all,
                "generated_at": now_iso(),
            }
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "tmux list-panes failed").strip()[:1000])

    rows = ps_process_rows()
    chat_cwd = meaningful_chat_cwd(sess.get("cwd") or DEFAULT_CWD)
    chat_tokens = tmux_explicit_chat_targets(session_id, sess)
    owned_name = terminal_session_name(session_id)
    panes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 11:
            parts.append("")
        pane_pid = int(parts[5]) if parts[5].isdigit() else None
        processes = descendant_processes(pane_pid, rows)
        search_text = "\n".join([
            parts[0],
            parts[2],
            parts[6],
            parts[7],
            parts[10],
            *[str(proc.get("args") or "") for proc in processes],
        ]).lower()
        matches: list[str] = []
        if parts[0] == owned_name:
            matches.append("chat tmux")
        if chat_cwd and path_is_within(parts[7], chat_cwd):
            matches.append("chat cwd")
        if chat_tokens and any(token in search_text for token in chat_tokens):
            matches.append("chat target")
        if any(keyword in search_text for keyword in TMUX_SUBMITTER_KEYWORDS):
            matches.append("submitter")
        if any(float(proc.get("cpu_percent") or 0.0) > 2.0 for proc in processes):
            matches.append("active")
        chat_linked = any(match in TMUX_CHAT_MATCH_LABELS for match in matches)
        if not include_all and not chat_linked:
            continue
        panes.append({
            "session_name": parts[0],
            "window_index": int(parts[1]) if parts[1].isdigit() else None,
            "window_name": parts[2] or None,
            "pane_index": int(parts[3]) if parts[3].isdigit() else None,
            "pane_id": parts[4],
            "pane_pid": pane_pid,
            "command": parts[6] or None,
            "cwd": parts[7] or None,
            "active": parts[8] == "1",
            "attached": int(parts[9]) if parts[9].isdigit() else None,
            "title": parts[10] or None,
            "matches": matches,
            "display": best_process_label(processes, parts[6] or parts[0]),
            "processes": processes,
        })
    panes.sort(key=lambda pane: (
        0 if "chat tmux" in pane.get("matches", []) else 1,
        0 if "chat target" in pane.get("matches", []) else 1,
        0 if "chat cwd" in pane.get("matches", []) else 1,
        0 if "submitter" in pane.get("matches", []) else 1,
        0 if "active" in pane.get("matches", []) else 1,
        str(pane.get("session_name") or ""),
        int(pane.get("window_index") or 0),
        int(pane.get("pane_index") or 0),
    ))
    return {
        "session_id": session_id,
        "panes": panes,
        "total_panes": len(result.stdout.splitlines()),
        "filtered": not include_all,
        "generated_at": now_iso(),
    }


def capture_tmux_pane(session_id: str, pane_id: str, *, lines: int = 500) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    clean = str(pane_id or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="pane_id is required")
    line_count = max(20, min(int(lines or 500), TMUX_CAPTURE_MAX_LINES))
    # Validate that the target is one of tmux's pane IDs before using it.
    known = tmux_panes_snapshot(session_id, include_all=True)
    if clean not in {pane.get("pane_id") for pane in known.get("panes", [])}:
        raise HTTPException(status_code=404, detail="tmux pane not found")
    text = run_tmux(["capture-pane", "-t", clean, "-p", "-J", "-S", f"-{line_count}"]).stdout
    return {
        "session_id": session_id,
        "pane_id": clean,
        "lines": line_count,
        "text": text,
        "generated_at": now_iso(),
    }


async def _update_session(
    session_id: str, req: UpdateSessionRequest, *,
    native_archive_owner: tuple[str, str] | None = None,
) -> dict[str, Any]:
    # Internal provenance is not an HTTP parameter. Recheck after acquiring
    # the same lock as Stop, replacement and new-turn admission.
    if native_archive_owner is not None and req.archived is not True:
        raise ValueError("native archive ownership only applies to archive")
    ensure_session_not_initializing(session_id)
    patch = req.model_dump(exclude_unset=True)
    if SESSION_LIFECYCLE_UPDATE_FIELDS.intersection(patch):
        async with session_lifecycle_lock(session_id):
            current = STORE.sessions.get(session_id)
            if not current:
                raise HTTPException(status_code=404, detail="session not found")
            if native_archive_owner is not None:
                thread_id, run_id = native_archive_owner
                if (session_id in DELETING_SESSIONS or current.get("backend") != BACKEND_CODEX
                        or str(session_provider_id(current) or "") != thread_id
                        or (RUNS.active.get(session_id) or {}).get("run_id") != run_id):
                    return {"session": public_session(current)}
            if req.archived is False and current.get("archived"):
                try:
                    # An earlier archive may have failed to save jobs.json
                    # after disabling jobs in memory. Force a durable write
                    # before clearing the archived session bit so restart can
                    # never resurrect them.
                    await WIRING.jobs.pause_for_session(
                        session_id,
                        persist_unchanged=True,
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "scheduled jobs could not be durably paused; "
                            "retry unarchiving this chat"
                        ),
                    ) from exc
            # Validate the complete model/effort pair atomically. App-server
            # applies the saved selection through the next turn/start; it does
            # not currently expose a client RPC for mutating a loaded thread.
            preview_session_runtime_update(current, patch)
            await ensure_backend_update_allowed(
                session_id,
                current,
                patch,
            )
            await ensure_claude_permission_mode_update_allowed(
                session_id,
                current,
                patch,
            )
            sess = await STORE.update(session_id, patch)
            if req.archived is True:
                try:
                    # Keep archive and its scheduling boundary serialized with
                    # unarchive and turn admission. Job create/enable also
                    # rechecks the durable archived bit under the job lock.
                    await WIRING.jobs.pause_for_session(session_id)
                except Exception as exc:
                    # The archived session is already durable and the
                    # in-memory job store fails safe. A later unarchive forces
                    # this state to disk before clearing the archive bit.
                    logger.warning(
                        "could not persist scheduled-job pause for archived session %s: %s",
                        session_id,
                        concise_error_message(exc),
                    )
    else:
        # Title, folder, and pin edits remain lightweight. Archive changes are
        # serialized above because they form a turn/job admission boundary.
        sess = await STORE.update(session_id, patch)
    if req.archived is True:
        try:
            await asyncio.to_thread(kill_terminal_session, session_id)
        except Exception as exc:
            # Archiving is already durable at this point. Terminal cleanup is
            # best-effort and must not turn a successful archive into a 500.
            logger.warning("could not clean up terminal for archived session %s: %s", session_id, exc)
    return {"session": public_session(sess)}
