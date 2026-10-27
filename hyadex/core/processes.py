"""Process inspection: process trees, cgroups, tmux server discovery and log hints."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any

from fastapi import HTTPException

from hyadex.core.config import (
    CONFIG_ENV_FILE,
    PROVIDER_CHILD_PROC_ROOT,
    SERVER_INSTANCE_NAME,
    STATE_DIR,
    STOP_GRACE_SECONDS,
    TMUX_INSTANCE_ARGS,
)
from hyadex.core.server_logging import command_log_hints, fd_log_hints, unique_log_hints
from hyadex.core.util import compact_memory_text
from hyadex.providers.runtime import PROVIDER_SECRET_ENV_NAMES
import server_instances

logger = logging.getLogger("hyadex-server")

TMUX_COMMAND_TIMEOUT_SECONDS = 4


async def terminate_process_tree(proc: asyncio.subprocess.Process, *, grace: float = STOP_GRACE_SECONDS) -> bool:
    if proc.returncode is not None:
        return False

    sent = False
    if os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
                sent = True
    if not sent:
        with suppress(ProcessLookupError):
            proc.terminate()
            sent = True

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return sent
    except asyncio.TimeoutError:
        pass

    killed = False
    if os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
                killed = True
    if not killed:
        with suppress(ProcessLookupError):
            proc.kill()
            killed = True
    try:
        await asyncio.wait_for(
            proc.wait(),
            timeout=max(float(grace), 0.05),
        )
    except asyncio.TimeoutError:
        # Some asyncio transports fail to resolve Process.wait() even after
        # the OS process has been killed. Process teardown is best-effort; the
        # caller must be allowed to continue into task/slot reconciliation.
        logger.warning(
            "process wait did not settle after SIGKILL pid=%s",
            getattr(proc, "pid", None),
        )
    return sent or killed


def process_group_for_pid(pid: int) -> int | None:
    with suppress(ProcessLookupError, PermissionError, OSError):
        return os.getpgid(pid)
    return None


def procfs_process_rows() -> list[dict[str, Any]]:
    if os.name == "nt" or not Path("/proc").is_dir():
        return []
    with suppress(Exception):
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    if "uptime" not in locals():
        uptime = 0.0
    clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
    page_size = os.sysconf("SC_PAGE_SIZE")
    mem_total_kb = 0
    with suppress(Exception):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
                break

    rows: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            right = stat.rfind(")")
            if right < 0:
                continue
            comm = stat[stat.find("(") + 1:right]
            fields = stat[right + 2:].split()
            if len(fields) < 20:
                continue
            state = fields[0]
            ppid = int(fields[1])
            pgid = int(fields[2])
            sid = int(fields[3])
            start_ticks = int(fields[19])
            elapsed = int(max(0, uptime - (start_ticks / clk_tck))) if uptime else 0
            rss_kb = 0
            with suppress(Exception):
                statm = (entry / "statm").read_text().split()
                if len(statm) > 1:
                    rss_kb = int(int(statm[1]) * page_size / 1024)
            args = ""
            with suppress(Exception):
                raw = (entry / "cmdline").read_bytes()
                args = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if not args:
                args = comm
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "pgid": pgid,
                "sid": sid,
                "stat": state,
                "elapsed_seconds": elapsed,
                "cpu_percent": 0.0,
                "mem_percent": (rss_kb / mem_total_kb * 100.0) if mem_total_kb else 0.0,
                "rss_kb": rss_kb,
                "command": comm,
                "args": args,
            })
        except (OSError, PermissionError, ValueError):
            continue
    return rows


def ps_process_rows() -> list[dict[str, Any]]:
    proc_rows = procfs_process_rows()
    if proc_rows:
        return proc_rows
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid=,sid=,stat=,etimes=,pcpu=,pmem=,rss=,comm=,args="],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("process snapshot ps scan timed out")
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 10:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            sid = int(parts[3])
            etimes = int(float(parts[5]))
            cpu = float(parts[6])
            mem = float(parts[7])
            rss = int(float(parts[8]))
        except ValueError:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "sid": sid,
            "stat": parts[4],
            "elapsed_seconds": etimes,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "rss_kb": rss,
            "command": parts[9],
            "args": parts[10] if len(parts) > 10 else parts[9],
        })
    return rows


def parse_ps_rows(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 10:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            sid = int(parts[3])
            etimes = int(float(parts[5]))
            cpu = float(parts[6])
            mem = float(parts[7])
            rss = int(float(parts[8]))
        except ValueError:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "sid": sid,
            "stat": parts[4],
            "elapsed_seconds": etimes,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "rss_kb": rss,
            "command": parts[9],
            "args": parts[10] if len(parts) > 10 else parts[9],
        })
    return rows


def parse_elapsed_seconds(value: str) -> int:
    """Parse BSD ``etime`` (``[[dd-]hh:]mm:ss``) into seconds."""

    text = str(value or "").strip()
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    fields = [int(float(part)) for part in text.split(":") if part != ""]
    if not fields:
        raise ValueError(f"invalid elapsed time: {value!r}")
    while len(fields) < 3:
        fields.insert(0, 0)
    hours, minutes, seconds = fields[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# No ``ucomm`` column: BSD ``ucomm`` is the 16-char process name and routinely
# contains spaces on macOS ("Activity Monitor", "Codex (Service)"), which would
# shift every later field. ``args`` is last and parsed as the remainder.
DARWIN_PS_COMMAND = [
    "ps",
    "-eo",
    "pid=,ppid=,pgid=,stat=,etime=,pcpu=,pmem=,rss=,args=",
    "-r",
]


def parse_darwin_ps_rows(stdout: str) -> list[dict[str, Any]]:
    """Parse BSD ``ps`` output (no ``sid``/``etimes``/``--sort`` on macOS).

    The GNU keyword set used on Linux makes macOS ``ps`` exit non-zero, so the
    host health monitor recorded an empty process list on every macOS sample
    (2026-09-04: 1,621 samples, none with processes).
    """

    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 8)
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            etimes = parse_elapsed_seconds(parts[4])
            cpu = float(parts[5])
            mem = float(parts[6])
            rss = int(float(parts[7]))
        except ValueError:
            continue
        args = parts[8]
        # The executable path is the first argv token; its basename is the
        # closest analogue of the Linux ``comm`` column.
        command = os.path.basename(args.split(None, 1)[0]) or args
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            # BSD ps has no session id column; the process group is the
            # closest stable grouping for the diagnostics that consume it.
            "sid": pgid,
            "stat": parts[3],
            "elapsed_seconds": etimes,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "rss_kb": rss,
            "command": command,
            "args": args,
        })
    return rows


def top_process_rows(limit: int = 20) -> list[dict[str, Any]]:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                DARWIN_PS_COMMAND,
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("top process ps scan timed out")
            return []
        rows = parse_darwin_ps_rows(result.stdout)
        return rows[: max(1, min(limit, 50))]
    command = ["ps", "-eo", "pid=,ppid=,pgid=,sid=,stat=,etimes=,pcpu=,pmem=,rss=,comm=,args=", "--sort=-pcpu"]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
        if result.returncode != 0:
            result = subprocess.run(command[:-1], text=True, capture_output=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("top process ps scan timed out")
        return []
    rows = parse_ps_rows(result.stdout)
    if not rows:
        rows = sorted(procfs_process_rows(), key=lambda row: int(row.get("rss_kb") or 0), reverse=True)
    return rows[: max(1, min(limit, 50))]


def proc_cwd(pid: int) -> str | None:
    if os.name == "nt":
        return None
    with suppress(OSError, PermissionError):
        return os.readlink(f"/proc/{pid}/cwd")
    return None


TMUX_PROBE_CACHE_SECONDS = 30.0
TMUX_PROBE_CACHE: dict[str, Any] = {
    "path": None,
    "available": False,
    "checked_at": 0.0,
}


def working_tmux_bin(*, use_cache: bool = False) -> str | None:
    found = shutil.which("tmux")
    now = time.monotonic()
    if (
        use_cache
        and TMUX_PROBE_CACHE.get("path") == found
        and now - float(TMUX_PROBE_CACHE.get("checked_at") or 0.0)
        < TMUX_PROBE_CACHE_SECONDS
    ):
        return found if TMUX_PROBE_CACHE.get("available") else None
    if not found:
        TMUX_PROBE_CACHE.update({
            "path": None,
            "available": False,
            "checked_at": now,
        })
        return None
    try:
        probe = subprocess.run(
            [found, "-V"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        TMUX_PROBE_CACHE.update({
            "path": found,
            "available": False,
            "checked_at": now,
        })
        return None
    available = probe.returncode == 0
    TMUX_PROBE_CACHE.update({
        "path": found,
        "available": available,
        "checked_at": now,
    })
    return found if available else None


def tmux_bin() -> str:
    found = working_tmux_bin(use_cache=True)
    if not found:
        raise HTTPException(status_code=503, detail="tmux is unavailable or not working on the agent server")
    return found


def run_tmux(args: list[str], *, check: bool = True, timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [tmux_bin(), *TMUX_INSTANCE_ARGS, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            # Any tmux command can be the one that implicitly starts the
            # shared daemon. Never let that first command seed server secrets
            # into tmux's long-lived global environment.
            env=tmux_bootstrap_environment(),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux command timed out")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "tmux command failed").strip()
        raise HTTPException(status_code=500, detail=detail[:1000])
    return result


HYADEX_SERVER_SYSTEMD_UNIT = server_instances.service_name(SERVER_INSTANCE_NAME) + ".service"


def process_cgroup_paths(pid: int) -> tuple[str, ...]:
    """Return Linux cgroup paths without assuming cgroup v1 or v2."""

    if not sys.platform.startswith("linux"):
        return ()
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
    except (OSError, ValueError):
        return ()
    paths: list[str] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        path = fields[2].strip()
        if path:
            paths.append(path)
    return tuple(dict.fromkeys(paths))


def hyadex_server_systemd_cgroup_from_paths(paths: tuple[str, ...]) -> str | None:
    """Derive the exact service cgroup from one captured cgroup snapshot."""

    for path in paths:
        parts = [part for part in path.split("/") if part]
        if HYADEX_SERVER_SYSTEMD_UNIT not in parts:
            continue
        index = parts.index(HYADEX_SERVER_SYSTEMD_UNIT)
        return "/" + "/".join(parts[: index + 1])
    return None


def hyadex_server_systemd_cgroup(pid: int | None = None) -> str | None:
    """Return this process's systemd-user service cgroup, when applicable."""

    paths = process_cgroup_paths(os.getpid() if pid is None else pid)
    return hyadex_server_systemd_cgroup_from_paths(paths)


def cgroup_is_within(path: str, ancestor: str) -> bool:
    clean_path = "/" + path.strip("/")
    clean_ancestor = "/" + ancestor.strip("/")
    return clean_path == clean_ancestor or clean_path.startswith(clean_ancestor + "/")


def tmux_server_pid() -> int | None:
    """Return the default tmux server PID without starting a missing server."""

    result = run_tmux(["list-sessions", "-F", "#{pid}"], check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def server_restart_tmux_cgroup_state() -> dict[str, Any]:
    """Inspect whether systemd would terminate the shared tmux daemon.

    This probe is deliberately read-only.  Private PID/cgroup values are used
    only to bind a force confirmation to the exact inspected process state;
    callers must never return them through the API.
    """

    state: dict[str, Any] = {
        "tmux_server_in_service_cgroup": False,
        "tmux_server_cgroup_unknown": False,
        "_tmux_server_pid": None,
        "_tmux_server_cgroup_paths": (),
        "_server_service_cgroup": None,
        "_tmux_server_inspection": "not-systemd-managed",
    }
    if not sys.platform.startswith("linux"):
        return state
    try:
        probe = run_tmux(
            ["list-sessions", "-F", "#{pid}"],
            check=False,
        )
    except Exception as exc:
        state["tmux_server_cgroup_unknown"] = True
        state["_server_service_cgroup"] = hyadex_server_systemd_cgroup()
        state["_tmux_server_inspection"] = (
            f"lookup-failed:{type(exc).__name__}"[:160]
        )
        return state
    probe_fingerprint = hashlib.sha256(json.dumps({
        "returncode": int(probe.returncode),
        "stdout": str(probe.stdout or "")[:4_096],
        "stderr": str(probe.stderr or "")[:4_096],
    }, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )).hexdigest()
    state["_tmux_server_probe_revision"] = probe_fingerprint
    if probe.returncode != 0:
        socket_path = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / (TMUX_INSTANCE_ARGS[-1] if TMUX_INSTANCE_ARGS else "default")
        try:
            socket_present = stat.S_ISSOCK(socket_path.lstat().st_mode)
        except OSError:
            socket_present = False
        if socket_present:
            state["tmux_server_cgroup_unknown"] = True
            state["_server_service_cgroup"] = hyadex_server_systemd_cgroup()
            state["_tmux_server_inspection"] = "lookup-failed-with-socket"
        else:
            state["_tmux_server_inspection"] = "not-running"
        return state

    pids = {
        int(line.strip())
        for line in str(probe.stdout or "").splitlines()
        if line.strip().isdigit() and int(line.strip()) > 0
    }
    if len(pids) != 1:
        state["tmux_server_cgroup_unknown"] = True
        state["_server_service_cgroup"] = hyadex_server_systemd_cgroup()
        state["_tmux_server_inspection"] = "pid-unavailable"
        return state
    pid = next(iter(pids))

    service_cgroup = hyadex_server_systemd_cgroup()
    state["_server_service_cgroup"] = service_cgroup
    state["_tmux_server_pid"] = pid
    if service_cgroup is None:
        state["tmux_server_cgroup_unknown"] = True
        state["_tmux_server_inspection"] = "server-cgroup-unavailable"
        return state
    paths = tuple(sorted(process_cgroup_paths(pid)))
    state["_tmux_server_cgroup_paths"] = paths
    if not paths:
        state["tmux_server_cgroup_unknown"] = True
        state["_tmux_server_inspection"] = "cgroup-unavailable"
        return state
    state["tmux_server_in_service_cgroup"] = any(
        cgroup_is_within(path, service_cgroup) for path in paths
    )
    state["_tmux_server_inspection"] = "verified"
    return state


def tmux_bootstrap_environment() -> dict[str, str]:
    """Return the non-secret environment allowed to seed a shared tmux daemon.

    tmux keeps its global environment for the daemon lifetime. Starting it
    from the authenticated server process with ``os.environ`` would therefore
    expose the server bearer to every pane in every shared tmux session.
    """

    exact_names = {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "TERM",
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in exact_names or key.startswith("LC_")
    }
    environment.update(server_instance_environment())
    for name in PROVIDER_SECRET_ENV_NAMES:
        environment.pop(name, None)
    return environment


def descendant_processes(root_pid: int | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not root_pid:
        return []
    rows_by_pid = {int(row["pid"]): row for row in rows}
    selected: set[int] = set()
    if root_pid in rows_by_pid:
        selected.add(root_pid)
    changed = True
    while changed:
        changed = False
        for row in rows:
            row_pid = int(row["pid"])
            if row_pid in selected:
                continue
            if int(row.get("ppid") or -1) in selected:
                selected.add(row_pid)
                changed = True
    ordered = ordered_process_tree(root_pid, selected, rows_by_pid)
    out: list[dict[str, Any]] = []
    for row in ordered[:12]:
        pid = int(row["pid"])
        cwd = proc_cwd(pid)
        args = str(row.get("args") or "")
        out.append({
            **row,
            "cwd": cwd,
            "depth": process_depth(pid, rows_by_pid, selected),
            "log_hints": unique_log_hints(fd_log_hints(pid) + command_log_hints(args, cwd)),
        })
    return out


def best_process_label(processes: list[dict[str, Any]], fallback: str | None) -> str:
    shell_names = {"bash", "sh", "zsh", "fish", "tmux", "login"}
    for proc in reversed(processes):
        command = str(proc.get("command") or "").split("/")[-1]
        args = str(proc.get("args") or "").strip()
        if command and command not in shell_names and args:
            return compact_memory_text(args, 240)
    for proc in processes:
        args = str(proc.get("args") or "").strip()
        if args:
            return compact_memory_text(args, 240)
    return fallback or "tmux pane"


def process_depth(pid: int, rows_by_pid: dict[int, dict[str, Any]], selected: set[int]) -> int:
    depth = 0
    seen: set[int] = set()
    current = rows_by_pid.get(pid)
    while current:
        ppid = int(current.get("ppid") or 0)
        if ppid not in selected or ppid in seen:
            break
        seen.add(ppid)
        depth += 1
        current = rows_by_pid.get(ppid)
    return min(depth, 12)


def ordered_process_tree(root_pid: int, selected: set[int], rows_by_pid: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for pid in selected:
        row = rows_by_pid.get(pid)
        if not row:
            continue
        ppid = int(row.get("ppid") or 0)
        if ppid in selected and ppid != pid:
            children.setdefault(ppid, []).append(pid)
        else:
            roots.append(pid)
    for values in children.values():
        values.sort()

    ordered: list[dict[str, Any]] = []
    visited: set[int] = set()

    def walk(pid: int) -> None:
        if pid in visited:
            return
        row = rows_by_pid.get(pid)
        if not row:
            return
        visited.add(pid)
        ordered.append(row)
        for child in children.get(pid, []):
            walk(child)

    if root_pid in selected:
        walk(root_pid)
    for root in sorted(roots):
        walk(root)
    for pid in sorted(selected - visited):
        walk(pid)
    return ordered


async def bounded_restart_tmux_cgroup_state(
    timeout_seconds: float,
) -> tuple[dict[str, Any], bool]:
    """Probe the tmux cgroup state without letting the probe hang the caller."""

    probe = asyncio.ensure_future(
        asyncio.to_thread(server_restart_tmux_cgroup_state)
    )
    try:
        state = await asyncio.wait_for(
            asyncio.shield(probe),
            timeout=max(0.0, float(timeout_seconds)),
        )
        return (state if isinstance(state, dict) else {}), False
    except (TimeoutError, asyncio.TimeoutError):
        probe.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return {"tmux_server_cgroup_unknown": True}, True
    except Exception:
        return {"tmux_server_cgroup_unknown": True}, True


PROVIDER_CHILD_PROBE_TIMEOUT_SECONDS = 1.0


def _provider_child_ps(pid: int, column: str) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-o", f"{column}=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PROVIDER_CHILD_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return ""
    return str(completed.stdout or "").strip()


def provider_child_parent_pid(pid: int) -> int | None:
    proc_stat = PROVIDER_CHILD_PROC_ROOT / str(pid) / "stat"
    if proc_stat.exists():
        with suppress(OSError, ValueError, IndexError):
            text = proc_stat.read_text()
            # ``pid (comm) state ppid ...``; comm may contain spaces/parens.
            fields = text[text.rindex(")") + 2 :].split()
            return int(fields[1])
    text = _provider_child_ps(pid, "ppid")
    with suppress(ValueError):
        return int(text)
    return None


def server_instance_environment() -> dict[str, str]:
    """Preserve Linux's user-service bus when the detached tmux server is stale."""
    result = {
        "HYADEX_SERVER_INSTANCE": SERVER_INSTANCE_NAME,
        "HYADEX_SERVER_CONFIG_DIR": str(CONFIG_ENV_FILE.parent),
        "HYADEX_STATE_DIR": str(STATE_DIR),
        "HYADEX_SERVER_INSTALL_DIR": str(os.environ.get("HYADEX_SERVER_INSTALL_DIR") or ""),
    }
    if not sys.platform.startswith("linux"):
        return result
    runtime_dir = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if not runtime_dir:
        candidate = Path("/run/user") / str(os.getuid())
        if candidate.is_dir():
            runtime_dir = str(candidate)
    if not runtime_dir:
        return result
    result["XDG_RUNTIME_DIR"] = runtime_dir
    bus_address = str(os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    if not bus_address and (Path(runtime_dir) / "bus").exists():
        bus_address = f"unix:path={Path(runtime_dir) / 'bus'}"
    if bus_address:
        result["DBUS_SESSION_BUS_ADDRESS"] = bus_address
    return result
