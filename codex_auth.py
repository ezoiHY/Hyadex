"""Read-only observation of the user's native Codex login.

Hyadex never signs in, refreshes or stores credentials. It reads only the
login kind (for model policy) and a keyed revision of the login files, so a
`codex login` in the user's terminal moves new turns to a fresh app-server.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tomllib



HANDOFF_MESSAGE = (
    "Codex sign-in changed. This chat is waiting for "
    "its running work, approvals, goals or background terminals before switching. "
    "Your running work has not been stopped and this message has not been sent. "
    "Retry after that work finishes."
)
_REVISION_KEY = secrets.token_bytes(32)
_MAX_AUTH_BYTES = 128 * 1024

# Native ChatGPT sign-in only. API-key and custom-provider availability is
# provider-owned. Keep old IDs in saved conversations; this is picker/admission
# policy, not transcript cleanup. Source: https://learn.chatgpt.com/docs/models
CHATGPT_RETIRED_MODELS = frozenset({
    "gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.3-codex-spark",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.5",
})


def native_model_auth_mode(env: dict[str, str], *, cwd: str, observed_mode: str = "") -> str:
    """Read only the auth kind for model policy, never authenticate or refresh.

    An opaque store may use the existing native login-status probe. A file
    store is checked directly so a switch to an API key cannot inherit a cached
    ChatGPT restriction. Unknown/managed providers are never globally banned.
    """
    try:
        root, config = _native_login_config(env, cwd)
        profile = config.get("profile")
        profiles = config.get("profiles")
        selected_profile = profiles.get(profile, {}) if isinstance(profiles, dict) and isinstance(profile, str) else {}
        if not isinstance(selected_profile, dict):
            return "unknown"
        provider = selected_profile.get("model_provider", config.get("model_provider", "openai"))
        if provider != "openai":
            return "custom"
        providers = config.get("model_providers") or {}
        override = providers.get("openai") if isinstance(providers, dict) else None
        if env.get("OPENAI_BASE_URL") or isinstance(override, dict) and override.get("base_url"):
            return "custom"
        if any(env.get(key) for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_IDENTITY_TOKEN_FILE")):
            return "apiKey"
        if env.get("CODEX_ACCESS_TOKEN"):
            return "unknown"
        store = config.get("cli_auth_credentials_store", "file")
        if store != "file":
            return observed_mode if store in {"keyring", "auto"} and observed_mode in {"chatgpt", "apiKey"} else "unknown"
        data = json.loads(_bounded_file(root / "auth.json"))
        if not isinstance(data, dict):
            return "unknown"
        if isinstance(data.get("OPENAI_API_KEY"), str) and data["OPENAI_API_KEY"]:
            return "apiKey"
        if data.get("auth_mode") in (None, "chatgpt") and isinstance(data.get("tokens"), dict):
            return "chatgpt"
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return "unknown"


@dataclass(frozen=True)
class LoginRevision:
    # Process-private change detection only, never authentication evidence.
    # Do not put paths, account IDs, credentials or digests in repr/logs/API.
    value: bytes = field(repr=False)


def _bounded_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_AUTH_BYTES:
            raise ValueError("unsupported credential file")
        raw = stream.read(_MAX_AUTH_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > _MAX_AUTH_BYTES or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("credential file changed during read")
        return raw


def _native_login_config(env: dict[str, str], cwd: str) -> tuple[Path, dict]:
    home = env.get("HOME")
    location = env.get("CODEX_HOME")
    if not location and not home:
        raise ValueError("native home unavailable")
    if location and location.startswith("~/"):
        if not home:
            raise ValueError("native home unavailable")
        location = str(Path(home) / location[2:])
    root = Path(location) if location else Path(home) / ".codex"
    if not root.is_absolute():
        root = Path(cwd) / root
    try:
        config = tomllib.loads(_bounded_file(root / "config.toml").decode())
    except FileNotFoundError:
        config = {}
    return root, config


def native_login_handoff_supported(env: dict[str, str], *, cwd: str) -> bool:
    try:
        _, config = _native_login_config(env, cwd)
        # A process-only login cannot be recovered by starting a replacement.
        # A malformed config must not retire an otherwise working generation.
        return config.get("cli_auth_credentials_store", "file") in {"file", "keyring", "auto"}
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def native_login_revision(env: dict[str, str], *, cwd: str) -> LoginRevision | None:
    """Conservative file-store signal; native Codex still owns all credentials.

    Ignore rotating access/refresh tokens and last_refresh. OIDC auth_time is
    stable across refresh but changes on an interactive authentication. Without
    it we can detect an account switch, not prove a same-account re-login.
    Opaque/keyring stores yield no revision, so they never trigger a handoff.
    Missing/malformed files are not evidence of a new login. No secret survives
    this bounded read except a process-keyed comparison digest.
    """
    try:
        root, config = _native_login_config(env, cwd)
        if config.get("cli_auth_credentials_store", "file") != "file":
            return None  # Never infer keyring/auto/ephemeral state from a leftover file.
        if any(env.get(key) for key in ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_IDENTITY_TOKEN_FILE")):
            return None
        data = json.loads(_bounded_file(root / "auth.json"))
        if not isinstance(data, dict):
            return None
        key = data.get("OPENAI_API_KEY")
        if isinstance(key, str) and key:
            identity = ("apiKey", key)
        else:
            tokens = data.get("tokens")
            if not isinstance(tokens, dict) or data.get("auth_mode") not in (None, "chatgpt"):
                return None
            encoded = tokens.get("id_token")
            if not isinstance(encoded, str) or len(encoded) > 32768:
                return None
            parts = encoded.split(".")
            if len(parts) != 3:
                return None
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(claims, dict):
                return None
            details = claims.get("https://api.openai.com/auth") or {}
            if not isinstance(details, dict):
                return None
            account = tokens.get("account_id") or details.get("chatgpt_account_id")
            subject = claims.get("sub")
            if not isinstance(account, str) or not account or not isinstance(subject, str) or not subject:
                return None
            auth_time = claims.get("auth_time")
            if auth_time is not None and (isinstance(auth_time, bool) or not isinstance(auth_time, int)):
                return None
            # Unverified claims are a change hint, never permission/account proof.
            identity = ("chatgpt", account, subject, auth_time)
        raw = json.dumps((str(root.resolve()), identity), separators=(",", ":")).encode()
        return LoginRevision(hmac.new(_REVISION_KEY, raw, hashlib.sha256).digest())
    except (OSError, ValueError, TypeError, RecursionError):
        return None


