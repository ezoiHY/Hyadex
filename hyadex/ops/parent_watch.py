"""End a server whose owning app is gone.

The Mac app starts its local backend with ``scripts/dev_server.py
--exit-with-parent``, which records the app's process id in
``HYADEX_EXIT_WITH_PARENT_PID``. The app stops that backend when it quits
normally. When it crashes or is force-quit instead, the backend is re-parented;
this watch notices and asks the server to shut down as if it were stopped, so
the next launch of the app can start its instance again. Installed services
never set the variable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger("hyadex-server")

PARENT_WATCH_INTERVAL_SECONDS = 1.0


def owning_parent_pid() -> int | None:
    """The process this server must not outlive, if it was given one."""
    value = os.environ.get("HYADEX_EXIT_WITH_PARENT_PID", "").strip()
    return int(value) if value.isdecimal() and int(value) > 1 else None


async def exit_with_parent(parent_pid: int, *, interval: float = PARENT_WATCH_INTERVAL_SECONDS) -> None:
    """Request a normal shutdown (SIGTERM to this process) once ``parent_pid`` is no longer our parent."""
    logger.info("server stops when its owner exits pid=%s", parent_pid)
    while os.getppid() == parent_pid:
        await asyncio.sleep(interval)
    logger.warning("owner process %s exited; shutting down", parent_pid)
    os.kill(os.getpid(), signal.SIGTERM)
