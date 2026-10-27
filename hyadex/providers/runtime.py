"""Validated provider input with distinct user text and runtime control data.

Environment allowlisting and legacy-secret scrubbing share one contract.
No configuration, provider process or application state is created on import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PROVIDER_SECRET_ENV_NAMES = (
    "HYADEX_AGENT_TOKEN",
    "ZENITHDOCK_AGENT_TOKEN",
    "ZENITHBOT_AGENT_TOKEN",
    "HYADEX_PUBLISH_TOKEN",
    "HYADEX_PROVIDER_AUTHORITY_FILE",
)


PROVIDER_RUNTIME_ENV_EXACT_NAMES = frozenset({
    "HYADEX_CHAT_ID",
    "HYADEX_PROVIDER_AUTHORITY_FILE",
    "HYADEX_PROVIDER_AUTHORITY_ACTIONS",
    "HYADEX_PROVIDER_JOBS_ACCESS",
    "HYADEX_PROVIDER_SERVER_ORIGIN",
})


# Run-scoped names issued by the removed cross-chat and Team features. They
# are never issued now, but a value inherited from an older parent process is
# still scrubbed before any provider child can see it.
LEGACY_PROVIDER_RUNTIME_ENV_EXACT_NAMES = frozenset({
    "HYADEX_PROVIDER_FINAL_RESULT_HANDOFF",
    "HYADEX_PROVIDER_TEAM_MAIL_PREBOUND",
})


LEGACY_PROVIDER_RUNTIME_ENV_PREFIXES = (
    "HYADEX_CROSS_CHAT_",
)


def is_provider_runtime_env_name(name: Any) -> bool:
    """Whether a run-scoped variable may be issued to a provider process."""

    return str(name or "") in PROVIDER_RUNTIME_ENV_EXACT_NAMES


def is_scrubbed_provider_runtime_env_name(name: Any) -> bool:
    """Whether an inherited variable must be stripped from provider children."""

    clean = str(name or "")
    return (
        is_provider_runtime_env_name(clean)
        or clean in LEGACY_PROVIDER_RUNTIME_ENV_EXACT_NAMES
        or clean.startswith(LEGACY_PROVIDER_RUNTIME_ENV_PREFIXES)
    )


def scrub_provider_runtime_environment(
    environment: dict[str, str],
    *,
    shadow: bool = False,
) -> None:
    """Remove or explicitly shadow every run-scoped provider variable."""

    names = {
        name
        for name in environment
        if name in PROVIDER_SECRET_ENV_NAMES or is_scrubbed_provider_runtime_env_name(name)
    }
    for name in names:
        if shadow:
            environment[name] = ""
        else:
            environment.pop(name, None)


MAX_PROVIDER_RUNTIME_CONTEXT_CHARS = 32 * 1024


MAX_PROVIDER_RUNTIME_CONTEXT_BYTES = 32 * 1024


MAX_PROVIDER_RUNTIME_ENV_VARS = 64


MAX_PROVIDER_RUNTIME_ENV_KEY_CHARS = 128


MAX_PROVIDER_RUNTIME_ENV_VALUE_CHARS = 4096


MAX_PROVIDER_RUNTIME_ENV_BYTES = 32 * 1024


class ProviderRuntimeContextError(RuntimeError):
    """Generated per-turn control context is unsafe to deliver."""


def validate_provider_runtime_context(value: Any) -> str:
    """Return bounded runtime control text without ever truncating grants."""

    if not isinstance(value, str):
        raise ProviderRuntimeContextError(
            "Hyadex provider runtime context must be text"
        )
    context = value
    try:
        context_bytes = len(context.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProviderRuntimeContextError(
            "Hyadex provider runtime context is not valid UTF-8"
        ) from exc
    if (
        len(context) > MAX_PROVIDER_RUNTIME_CONTEXT_CHARS
        or context_bytes > MAX_PROVIDER_RUNTIME_CONTEXT_BYTES
    ):
        raise ProviderRuntimeContextError(
            "Hyadex provider runtime context exceeds the safe per-turn limit"
        )
    return context


def validate_provider_runtime_env(value: Any) -> dict[str, str]:
    """Return one bounded, allow-listed per-turn environment projection."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderRuntimeContextError(
            "Hyadex provider runtime environment must be an object"
        )
    if len(value) > MAX_PROVIDER_RUNTIME_ENV_VARS:
        raise ProviderRuntimeContextError(
            "Hyadex provider runtime environment has too many variables"
        )
    clean: dict[str, str] = {}
    total_bytes = 0
    for raw_name, raw_value in value.items():
        name = str(raw_name or "")
        if (
            not name
            or len(name) > MAX_PROVIDER_RUNTIME_ENV_KEY_CHARS
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None
            or not is_provider_runtime_env_name(name)
        ):
            raise ProviderRuntimeContextError(
                "Hyadex provider runtime environment has an invalid variable"
            )
        if not isinstance(raw_value, str) or "\x00" in raw_value:
            raise ProviderRuntimeContextError(
                "Hyadex provider runtime environment has an invalid value"
            )
        if len(raw_value) > MAX_PROVIDER_RUNTIME_ENV_VALUE_CHARS:
            raise ProviderRuntimeContextError(
                "Hyadex provider runtime environment value exceeds its safe limit"
            )
        try:
            total_bytes += len(name.encode("utf-8")) + len(
                raw_value.encode("utf-8")
            )
        except UnicodeEncodeError as exc:
            raise ProviderRuntimeContextError(
                "Hyadex provider runtime environment is not valid UTF-8"
            ) from exc
        if total_bytes > MAX_PROVIDER_RUNTIME_ENV_BYTES:
            raise ProviderRuntimeContextError(
                "Hyadex provider runtime environment exceeds its safe limit"
            )
        clean[name] = raw_value
    return clean


def assert_provider_user_message_unchanged(
    actual: str,
    original: str,
) -> None:
    """Fail closed if runtime assembly mutated provider-visible user text."""

    if actual != original:
        raise ProviderRuntimeContextError(
            "Hyadex runtime context mutated the provider user message"
        )


@dataclass(frozen=True)
class ProviderTurnPayload:
    """Keep provider user input and non-user runtime control structurally split."""

    user_prompt: str
    runtime_context: str
    runtime_env: dict[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.user_prompt, str):
            raise ProviderRuntimeContextError(
                "Hyadex provider user message must be text"
            )
        object.__setattr__(
            self,
            "runtime_context",
            validate_provider_runtime_context(self.runtime_context),
        )
        object.__setattr__(
            self,
            "runtime_env",
            validate_provider_runtime_env(self.runtime_env),
        )
