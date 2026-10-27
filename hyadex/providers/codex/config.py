"""Per-thread Codex configuration: defaults, validation, and the overrides a chat or the server applies."""
from __future__ import annotations

import json
import logging.handlers
from hyadex.core.config import CODEX_SETTINGS_FILE
from pathlib import Path
from typing import Any

logger = logging.getLogger("hyadex-server")

# Per-thread Codex config overrides (codex-cli 0.149.1: thread/start,
# thread/resume and thread/fork accept a "config" object). Leave concurrency
# unset unless the operator or chat explicitly configures it: Codex owns its
# default, and Hyadex must not silently replace it with a four-child cap.
# Only allow-listed keys are forwarded; unknown settings are warned once.
CODEX_THREAD_CONFIG_DEFAULTS: dict[str, Any] = {}
# Legacy alias for max_concurrent_threads_per_session.
CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY = "max_threads"


def _codex_config_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _codex_config_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


CODEX_THREAD_CONFIG_AGENT_VALIDATORS: dict[str, Any] = {
    "enabled": lambda value: isinstance(value, bool),
    "max_concurrent_threads_per_session": _codex_config_positive_int,
    CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY: _codex_config_positive_int,
    "default_subagent_model": _codex_config_non_empty_str,
    "default_subagent_reasoning_effort": _codex_config_non_empty_str,
    "job_max_runtime_seconds": _codex_config_positive_int,
}
CODEX_THREAD_CONFIG_TOP_LEVEL_VALIDATORS: dict[str, Any] = {
    "model_auto_compact_token_limit": _codex_config_positive_int,
    "model_auto_compact_token_limit_scope": _codex_config_non_empty_str,
}
_CODEX_THREAD_CONFIG_WARNED_KEYS: set[tuple[str, str]] = set()


def _warn_codex_thread_config_once(source: str, key: str, reason: str) -> None:
    marker = (source, key)
    if marker in _CODEX_THREAD_CONFIG_WARNED_KEYS:
        return
    _CODEX_THREAD_CONFIG_WARNED_KEYS.add(marker)
    logger.warning(
        "ignoring Codex thread config key %r from %s: %s",
        key,
        source,
        reason,
    )


def sanitize_codex_thread_config(value: Any, *, source: str) -> dict[str, Any]:
    """Return only allow-listed, well-typed Codex per-thread config overrides.

    Unknown keys and values of the wrong type are dropped, warning once per
    (source, key). The legacy ``agents.max_threads`` alias is canonicalized to
    ``max_concurrent_threads_per_session`` unless the canonical key is present.
    """

    if not isinstance(value, dict):
        if value is not None:
            _warn_codex_thread_config_once(source, "thread_config", "not an object")
        return {}
    clean: dict[str, Any] = {}
    for key, raw in value.items():
        key = str(key)
        if key == "agents":
            if not isinstance(raw, dict):
                _warn_codex_thread_config_once(source, key, "not an object")
                continue
            agents: dict[str, Any] = {}
            for agent_key, agent_value in raw.items():
                agent_key = str(agent_key)
                validator = CODEX_THREAD_CONFIG_AGENT_VALIDATORS.get(agent_key)
                if validator is None:
                    _warn_codex_thread_config_once(
                        source, f"agents.{agent_key}", "not an allowed key"
                    )
                    continue
                if not validator(agent_value):
                    _warn_codex_thread_config_once(
                        source, f"agents.{agent_key}", "invalid value type"
                    )
                    continue
                agents[agent_key] = agent_value
            legacy = agents.pop(CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY, None)
            if legacy is not None and "max_concurrent_threads_per_session" not in agents:
                agents["max_concurrent_threads_per_session"] = legacy
            if agents:
                clean["agents"] = agents
            continue
        validator = CODEX_THREAD_CONFIG_TOP_LEVEL_VALIDATORS.get(key)
        if validator is None:
            _warn_codex_thread_config_once(source, key, "not an allowed key")
            continue
        if not validator(raw):
            _warn_codex_thread_config_once(source, key, "invalid value type")
            continue
        clean[key] = raw
    return clean


def merge_codex_thread_config(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Overlay one sanitized config onto another; the [agents] table merges."""

    merged: dict[str, Any] = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in base.items()
    }
    for key, value in overlay.items():
        if key == "agents" and isinstance(value, dict):
            agents = dict(merged.get("agents") or {})
            agents.update(value)
            merged["agents"] = agents
        else:
            merged[key] = value
    return merged


def read_codex_thread_config_overrides(path: Path | None = None) -> dict[str, Any]:
    """Read server-wide Codex per-thread config overrides from the settings file.

    The ``thread_config`` object in ``codex-settings.json`` is merged over
    :data:`CODEX_THREAD_CONFIG_DEFAULTS`. A missing file or key keeps the
    defaults without injecting a subagent limit. Explicit operator and chat
    overrides still take precedence over the provider's own defaults.
    """

    settings_path = path or CODEX_SETTINGS_FILE
    defaults = merge_codex_thread_config(CODEX_THREAD_CONFIG_DEFAULTS, {})
    try:
        value = json.loads(settings_path.read_text())
    except FileNotFoundError:
        return defaults
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "could not read Codex thread config from %s: %s",
            settings_path,
            exc,
        )
        return defaults
    if not isinstance(value, dict) or "thread_config" not in value:
        return defaults
    return merge_codex_thread_config(
        defaults,
        sanitize_codex_thread_config(
            value.get("thread_config"),
            source=str(settings_path),
        ),
    )


def codex_session_config_overrides(sess: dict[str, Any]) -> dict[str, Any]:
    """Sanitized per-chat Codex config overrides (``codex_config_overrides``)."""

    raw = sess.get("codex_config_overrides") if isinstance(sess, dict) else None
    clean = sanitize_codex_thread_config(
        raw,
        source=f"session {str(sess.get('id') or '') or '<unknown>'}",
    ) if raw is not None else {}
    if _codex_config_positive_int(sess.get("subagent_limit")):
        agents = dict(clean.get("agents") or {})
        agents["max_concurrent_threads_per_session"] = sess["subagent_limit"]
        clean["agents"] = agents
    return clean


def codex_effective_thread_config(sess: dict[str, Any]) -> dict[str, Any]:
    """Server defaults, then the settings file, then the chat's own overrides."""

    return merge_codex_thread_config(
        read_codex_thread_config_overrides(),
        codex_session_config_overrides(sess),
    )
