"""Provider process state that is replaced while the server runs.

The shared Codex app-server and the Claude SDK supervisor are created lazily,
retired on binary or login changes, and closed at shutdown. Turn admission,
history sync, subagent reads and health checks all read the current manager,
so it lives on one object instead of module globals each module would copy.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager
    from hyadex.providers.codex.app_server_manager import CodexAppServerManager


@dataclass(eq=False)
class ProviderState:
    # Shared Codex app-server; replaced when the binary or login changes.
    codex_manager: CodexAppServerManager | None = None
    # Bumped for every new shared manager; cleanup records the epoch it closed.
    codex_manager_epoch: int = 0
    codex_manager_cleanup_epoch: int | None = None
    # Set while close_codex_app_server_manager tears the shared manager down.
    codex_manager_closing: bool = False
    # Background drain of retired managers.
    codex_manager_drain_task: asyncio.Task[Any] | None = None
    codex_manager_drain_requested: bool = False
    # Last observed Codex binary and when it was checked.
    codex_binary_identity: tuple[Any, ...] | None = None
    codex_binary_checked_at: float = 0.0
    # Last observed native Codex login (codex_auth.LoginRevision).
    codex_login_revision: Any = None
    # Model the Codex catalog currently migrates retired models to.
    codex_model_migration_target: str = "gpt-6-luna"
    # Shared Claude SDK supervisor.
    claude_sdk_manager: ClaudeSDKSupervisorManager | None = None


PROVIDERS = ProviderState()

LEGACY_NAMES: dict[str, str] = {
    "CODEX_APP_SERVER_MANAGER": "codex_manager",
    "CODEX_APP_SERVER_MANAGER_EPOCH": "codex_manager_epoch",
    "CODEX_APP_SERVER_MANAGER_CLEANUP_EPOCH": "codex_manager_cleanup_epoch",
    "CODEX_MANAGER_CLOSING": "codex_manager_closing",
    "CODEX_MANAGER_DRAIN_TASK": "codex_manager_drain_task",
    "CODEX_MANAGER_DRAIN_REQUESTED": "codex_manager_drain_requested",
    "CODEX_BINARY_IDENTITY": "codex_binary_identity",
    "CODEX_BINARY_CHECKED_AT": "codex_binary_checked_at",
    "CODEX_LOGIN_REVISION": "codex_login_revision",
    "CODEX_MODEL_MIGRATION_TARGET": "codex_model_migration_target",
    "CLAUDE_SDK_MANAGER": "claude_sdk_manager",
}


def NamespaceProviders(namespace: dict[str, Any]) -> Any:
    """Test support: a ProviderState read through a namespace's legacy names."""
    from hyadex.core.runs import NamespaceView
    return NamespaceView(namespace, LEGACY_NAMES, ProviderState)
