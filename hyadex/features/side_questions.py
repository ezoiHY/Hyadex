"""Side questions asked against a chat without changing it."""
from __future__ import annotations

from typing import Any

from hyadex.core.server import SERVER
from hyadex.core.sessions import server_lifecycle_admission_blocker
import side_questions

CODEX_GOALS_RECONFIGURING = False


# The Codex app-server's thread/start and turn-override JSON-RPC params only
# accept a narrower serviceTier enum ("fast"/"flex") than the canonical
# service_tier values used for `codex exec` CLI config overrides (see
# docs/DEV_LOG.md "GPT-5.6 Priority Tier Root Cause": "fast" is the documented
# user-facing alias for the canonical "priority" tier). Translate at this
# boundary only; the CLI-facing `-c service_tier=...` invocations keep using
# the canonical value, which is already validated to work there.
CODEX_APP_SERVER_SERVICE_TIER_ALIASES = {"priority": "fast"}


def codex_app_server_service_tier(service_tier: str) -> str:
    clean = str(service_tier or "").strip()
    return CODEX_APP_SERVER_SERVICE_TIER_ALIASES.get(clean, clean)


def flatten_codex_config_overrides(
    config: dict[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten a nested override map into dotted Codex config keys."""

    flat: dict[str, Any] = {}
    for key, value in config.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_codex_config_overrides(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def require_side_question_admission():
    # Side questions share worker-maintenance and restart admission with
    # ordinary provider work.
    blocker = server_lifecycle_admission_blocker()
    if SERVER.shutting_down or blocker:
        raise side_questions.SideQuestionError(503, blocker or "Server is shutting down")
