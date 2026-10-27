"""Process-wide server state that changes while the server runs.

Startup, shutdown and the launcher set these; admission, providers and health
read them. Keeping them on one object means every reader sees the current
value, wherever its code lives.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class ServerState:
    # Address and port the server listens on (the launcher may override them).
    bind_address: str = "0.0.0.0"
    port: int = 7870
    # Set once shutdown begins; new work is refused from then on.
    shutting_down: bool = False
    # Startup rebuilds durable queue rows from the chat timelines. While this
    # task is present, queue and start admission wait for it (or fail
    # retryably) so a new prompt cannot overtake an older row the scan has not
    # found yet. A failed task stays installed and keeps admission closed.
    queue_recovery_task: asyncio.Task[tuple[int, int]] | None = None
    # Worker maintenance hold supplied by the execution service, if any.
    execution_maintenance: Any = None
    # Exclusive lock on the state directory for this process.
    state_process_lock: Any = None


LEGACY_NAMES: dict[str, str] = {
    "SERVER_BIND_ADDRESS": "bind_address",
    "SERVER_PORT": "port",
    "SERVER_SHUTTING_DOWN": "shutting_down",
    "QUEUE_RECOVERY_TASK": "queue_recovery_task",
    "EXECUTION_MAINTENANCE": "execution_maintenance",
    "SERVER_STATE_PROCESS_LOCK": "state_process_lock",
}


def NamespaceServer(namespace: dict[str, Any]) -> Any:
    """Test support: a ServerState read through a namespace's legacy names."""
    from hyadex.core.runs import NamespaceView
    return NamespaceView(namespace, LEGACY_NAMES, ServerState)
