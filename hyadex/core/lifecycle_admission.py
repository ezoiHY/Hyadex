"""Whether new work may start while the server restarts, shuts down or is held for maintenance."""
from __future__ import annotations

from hyadex.core.restart_status import (
    MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    managed_server_restart_blocks_work,
)
from hyadex.core.server import SERVER

EXECUTION_MAINTENANCE_ACTIVE_DETAIL = "HyadexServer execution worker is in maintenance"


def execution_maintenance_blocks_work() -> bool:
    """Return whether the owned execution worker is reserved for retirement."""
    return SERVER.execution_maintenance is not None and SERVER.execution_maintenance.is_held()


def server_lifecycle_admission_blocker() -> str | None:
    if execution_maintenance_blocks_work():
        return EXECUTION_MAINTENANCE_ACTIVE_DETAIL
    if managed_server_restart_blocks_work():
        return MANAGED_SERVER_RESTART_ACTIVE_DETAIL
    return None
