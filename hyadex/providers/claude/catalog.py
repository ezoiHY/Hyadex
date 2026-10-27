"""Claude models and options: CLI help parsing, Models API discovery, command lines, account usage."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
import ipaddress
import json
import logging.handlers
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Literal
from urllib.parse import urlparse

from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_BIN,
    CLAUDE_CLI_MODEL_ALIASES,
    CLAUDE_CURRENT_MODEL_FALLBACKS,
    CLAUDE_LEGACY_MODEL_FALLBACKS,
    CLAUDE_MODEL_VALUE_RE,
    CLAUDE_MODELS_API_MAX_BYTES,
    CLAUDE_PROVIDER_SECRET_ENV_NAMES,
    DEFAULT_CWD,
    hyadex_setting,
    PROCESS_STREAM_LIMIT,
    RUNTIME_CATALOG_DEADLINE,
    RUNTIME_CATALOG_TIMEOUT_SECONDS,
    SERVER_VERSION,
)
from hyadex.core.sessions import session_system_prompt
from hyadex.core.util import runner_env
from hyadex.providers.claude.sdk import (
    CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
    ClaudeSDKUnavailable,
    create_claude_agent_options,
    default_claude_sdk_client_factory,
)
from hyadex.providers.runtime import (
    is_scrubbed_provider_runtime_env_name,
    PROVIDER_SECRET_ENV_NAMES,
)
from hyadex.providers import usage as provider_usage

logger = logging.getLogger("hyadex-server")

CLAUDE_ACCOUNT_USAGE_TTL_SECONDS = 120.0
CLAUDE_ACCOUNT_USAGE_FAILURE_TTL_SECONDS = 30.0
CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS = 30.0
CLAUDE_ACCOUNT_USAGE: dict[str, Any] = {"at": 0.0, "value": None, "stale": False}
CLAUDE_ACCOUNT_USAGE_LOCK = asyncio.Lock()


async def probe_claude_account_usage() -> dict[str, Any]:
    """Ask Claude Code for its own ``/usage`` data through the Agent SDK.

    A short-lived CLI connection answers the native ``get_usage`` control
    request from the user's existing login. It sends no prompt, makes no
    model request, loads no project or user settings (so no hooks run), and
    skips the local transcript scan. Only plan windows are kept.
    """
    env = runner_env()
    for inherited_name in tuple(os.environ):
        if inherited_name in PROVIDER_SECRET_ENV_NAMES or is_scrubbed_provider_runtime_env_name(inherited_name):
            env[inherited_name] = ""
    cli_path = claude_sdk_cli_path(env)
    options = create_claude_agent_options(
        cwd=str(Path.home()), cli_path=cli_path, env=env, setting_sources=[],
        max_buffer_size=PROCESS_STREAM_LIMIT, stderr=lambda _line: None,
    )
    client = default_claude_sdk_client_factory(options)
    await client.connect()
    try:
        query = getattr(client, "_query", None)
        if query is None:
            raise ClaudeSDKUnavailable("Claude SDK client did not connect")
        response = await query._send_control_request(
            {"subtype": "get_usage", "skip_behaviors": True},
            timeout=CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS,
        )
    finally:
        with suppress(Exception):
            await client.disconnect()
    return provider_usage.claude_account_snapshot(response, provider_usage.timestamp())


async def read_claude_account_usage(*, refresh: bool = False) -> dict[str, Any]:
    async with CLAUDE_ACCOUNT_USAGE_LOCK:
        cached = CLAUDE_ACCOUNT_USAGE.get("value")
        age = time.monotonic() - float(CLAUDE_ACCOUNT_USAGE.get("at") or 0.0)
        ttl = (CLAUDE_ACCOUNT_USAGE_TTL_SECONDS
               if isinstance(cached, dict) and cached.get("status") == "available"
               else CLAUDE_ACCOUNT_USAGE_FAILURE_TTL_SECONDS)
        if cached is not None and not refresh and not CLAUDE_ACCOUNT_USAGE.get("stale") and age < ttl:
            return copy.deepcopy(cached)
        try:
            result = await asyncio.wait_for(
                probe_claude_account_usage(), CLAUDE_ACCOUNT_USAGE_TIMEOUT_SECONDS + 10,
            )
        except Exception:
            # Never surface provider diagnostics; an old value is not current.
            result = provider_usage.unavailable(BACKEND_CLAUDE, "temporarily_unavailable")
        CLAUDE_ACCOUNT_USAGE.update(at=time.monotonic(), value=result, stale=False)
        return copy.deepcopy(result)


def runtime_option(value: str, label: str | None = None, **extra: Any) -> dict[str, Any]:
    clean = str(value or "").strip()
    return {"value": clean, "label": str(label or clean or "Server default").strip(), **extra}


def server_default_runtime_option(label: str | None = None) -> dict[str, Any]:
    return runtime_option("", label or "Server default")


def title_model_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    known = {
        "fable": "Fable",
        "claude-fable-5-1": "Fable 5.1",
        "claude-fable-5": "Fable 5",
        "claude-mythos-5-1": "Mythos 5.1",
        "claude-mythos-5": "Mythos 5",
        "claude-opus-5": "Opus 5",
        "opus[1m]": "Opus 1M",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-8[1m]": "Opus 4.8 1M",
        "claude-sonnet-5": "Sonnet 5",
        "claude-haiku-4-5-20251001": "Haiku 4.5",
    }
    if clean.lower() in known:
        return known[clean.lower()]
    special = {
        "gpt": "GPT",
        "codex": "Codex",
        "claude": "Claude",
    }
    return " ".join(special.get(part.lower(), part.upper() if part.lower().startswith("gpt") else part.capitalize()) for part in re.split(r"[-_\s]+", clean) if part)


def title_effort_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    if clean.lower() == "xhigh":
        return "XHigh"
    return clean.capitalize()


def unique_runtime_options(options: list[dict[str, Any]], default_label: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in [server_default_runtime_option(default_label), *options]:
        value = str(option.get("value") or "").strip()
        if value in seen:
            continue
        seen.add(value)
        extra = {key: val for key, val in option.items() if key not in {"value", "label"}}
        out.append(runtime_option(value, option.get("label") or None, **extra))
    return out


def run_catalog_command(cmd: list[str]) -> str:
    result = runtime_command(cmd)
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {stderr}")
    return result.stdout


def claude_supports_effort(effort: str) -> bool:
    clean = str(effort or "").strip().lower()
    if not clean:
        return False
    try:
        result = runtime_command([CLAUDE_BIN, "--effort", clean, "--version"])
    except Exception as exc:
        logger.debug("claude effort probe failed effort=%s: %s", clean, exc)
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 0 and "unknown --effort value" not in output


class RuntimeCatalogBudgetExpired(RuntimeError):
    """A refresh ran out of time; this is not evidence of a broken runtime."""


def runtime_catalog_budget_expired() -> bool:
    deadline = RUNTIME_CATALOG_DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline


def runtime_probe_timeout(timeout_seconds: float) -> float:
    deadline = RUNTIME_CATALOG_DEADLINE.get()
    if deadline is None:
        return timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeCatalogBudgetExpired("Runtime catalog refresh budget expired")
    return min(timeout_seconds, remaining)


def runtime_command(
    cmd: list[str], *, timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = runtime_probe_timeout(
        RUNTIME_CATALOG_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    try:
        return subprocess.run(
            cmd,
            cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
            env=runner_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if runtime_catalog_budget_expired():
            raise RuntimeCatalogBudgetExpired("Runtime catalog refresh budget expired") from None
        raise


def parse_claude_help_model_option_groups(
    help_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract alias and full-name groups from Claude Code's public help.

    Claude Code currently documents evergreen aliases and one full-name
    example in the wrapped ``--model`` option. The old parser stopped at the
    first parenthesized clause, so it silently dropped the full model ID.
    Restrict parsing to that option block so quoted examples elsewhere in the
    help cannot become model choices.
    """

    match = re.search(
        r"(?ms)^[ \t]*(?:-[A-Za-z],\s*)?--model\s+<model>(?P<body>.*?)"
        r"(?=^[ \t]*(?:-[A-Za-z],\s*)?--[A-Za-z][A-Za-z0-9-]*(?:[ \t,<]|$)|\Z)",
        str(help_text or ""),
    )
    if match is None:
        return [], []

    body = match.group("body")
    full_name_marker = re.search(
        r"\bor\s+(?:a\s+)?model(?:'s)?\s+full\s+name\b",
        body,
        re.IGNORECASE,
    )
    token_re = re.compile(
        r"(?<![A-Za-z0-9])['\"`]([A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,255})['\"`]"
    )

    def options_from(region: str) -> list[dict[str, Any]]:
        return [
            runtime_option(value, title_model_label(value))
            for value in token_re.findall(region)
            if CLAUDE_MODEL_VALUE_RE.fullmatch(value)
        ]

    if full_name_marker is not None:
        return (
            options_from(body[:full_name_marker.start()]),
            options_from(body[full_name_marker.end():]),
        )

    # Older help has no explicit separator. Short identifier-like values are
    # aliases; provider/platform IDs contain punctuation and remain full names.
    alias_options: list[dict[str, Any]] = []
    full_name_options: list[dict[str, Any]] = []
    for option in options_from(body):
        value = str(option.get("value") or "")
        target = (
            alias_options
            if re.fullmatch(r"[a-z][a-z0-9_]*(?:\[[a-z0-9]+\])?", value)
            else full_name_options
        )
        target.append(option)
    return alias_options, full_name_options


def parse_claude_help_model_options(help_text: str) -> list[dict[str, Any]]:
    aliases, full_names = parse_claude_help_model_option_groups(help_text)
    return [*aliases, *full_names]


def parse_claude_models_api_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize the account-scoped response from Anthropic's Models API."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    options: list[dict[str, Any]] = []
    for raw_model in payload["data"]:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get("id") or "").strip()
        if not CLAUDE_MODEL_VALUE_RE.fullmatch(model_id):
            continue
        display_name = str(raw_model.get("display_name") or "").strip()
        options.append(
            runtime_option(
                model_id,
                display_name[:160] or title_model_label(model_id),
            )
        )
    return options


def claude_models_api_base_url_allowed(parsed_base: Any) -> bool:
    """Allow credentials over HTTPS, or plain HTTP only to loopback."""

    if parsed_base.scheme == "https" and parsed_base.hostname:
        return True
    if parsed_base.scheme != "http" or not parsed_base.hostname:
        return False
    host = parsed_base.hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def discover_claude_provider_models(
    *, timeout_seconds: float = 6.0,
) -> tuple[list[dict[str, Any]], Literal["unavailable", "success", "failed"]]:
    """Return models available to the configured Anthropic API account.

    Claude subscription/OAuth installs fall through without network access
    here; their native picker comes from SDK initialization. API-key installs
    can use the official account-scoped Models API; failures remain non-fatal
    and never log the credential or a response body.
    """

    api_key = str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return [], "unavailable"
    if "\r" in api_key or "\n" in api_key:
        logger.warning("claude provider model discovery skipped: invalid credential format")
        return [], "failed"
    base_url = str(
        os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    ).strip().rstrip("/")
    parsed_base = urlparse(base_url)
    if not claude_models_api_base_url_allowed(parsed_base):
        logger.warning("claude provider model discovery skipped: insecure or invalid base URL")
        return [], "failed"
    endpoint = (
        f"{base_url}/models?limit=1000"
        if parsed_base.path.rstrip("/").endswith("/v1")
        else f"{base_url}/v1/models?limit=1000"
    )
    curl_bin = shutil.which("curl", path=runner_env().get("PATH"))
    if not curl_bin:
        logger.warning("claude provider model discovery skipped: curl unavailable")
        return [], "unavailable"
    try:
        timeout_seconds = runtime_probe_timeout(
            max(0.5, min(RUNTIME_CATALOG_TIMEOUT_SECONDS, timeout_seconds))
        )
        # Keep the key out of argv, the child environment, and disk by feeding
        # curl's header-file syntax through stdin. curl's total-time limit is
        # resistant to a peer that trickles bytes forever; subprocess.run's
        # independent deadline kills and reaps curl if its own timer fails.
        header_bytes = (
            "anthropic-version: 2023-06-01\n"
            f"x-api-key: {api_key}\n"
            "accept: application/json\n"
        ).encode("utf-8")
        child_env = runner_env()
        for secret_name in CLAUDE_PROVIDER_SECRET_ENV_NAMES:
            child_env.pop(secret_name, None)
        curl_args = [curl_bin, "--disable"]
        if parsed_base.scheme == "http":
            # Plain HTTP is allowed only for loopback. Never let inherited
            # proxy settings send that credential-bearing request elsewhere.
            curl_args.extend(["--noproxy", "*"])
        curl_args.extend(
            [
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                f"{timeout_seconds:g}",
                "--max-time",
                f"{timeout_seconds:g}",
                "--max-filesize",
                str(CLAUDE_MODELS_API_MAX_BYTES),
                "--header",
                "@-",
                "--user-agent",
                f"HyadexServer/{SERVER_VERSION}",
                endpoint,
            ]
        )
        result = subprocess.run(
            curl_args,
            cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
            env=child_env,
            input=header_bytes,
            text=False,
            capture_output=True,
            timeout=runtime_probe_timeout(timeout_seconds + 1.0),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl exited {result.returncode}")
        raw = result.stdout
        if len(raw) > CLAUDE_MODELS_API_MAX_BYTES:
            raise ValueError("Claude Models API response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Claude Models API returned an invalid payload")
        return parse_claude_models_api_payload(payload), "success"
    except Exception as exc:
        logger.warning(
            "claude provider model discovery failed error_type=%s",
            type(exc).__name__,
        )
        return [], "failed"


def claude_fallback_model_options() -> list[dict[str, Any]]:
    return [
        runtime_option(
            value,
            label,
            **({"availability": "limited"} if limited else {}),
        )
        for value, label, limited in (
            *CLAUDE_CURRENT_MODEL_FALLBACKS,
            *CLAUDE_LEGACY_MODEL_FALLBACKS,
        )
    ]


def discover_claude_native_models() -> tuple[list[dict[str, Any]], str]:
    from hyadex.providers.claude.model_catalog import cached_native_models

    try:
        models = cached_native_models(CLAUDE_BIN, env=runner_env())
        return (models, "success") if models is not None else ([], "unavailable")
    except Exception as exc:
        # Optional metadata must neither poison readiness nor leak the init
        # response (which contains account data), stderr, or subprocess args.
        logger.debug("claude native model discovery unavailable error_type=%s", type(exc).__name__)
        return [], "unavailable"


def parse_claude_help_catalog() -> dict[str, Any]:
    model_options: list[dict[str, Any]] = []
    effort_options: list[dict[str, Any]] = []
    default_model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or hyadex_setting("CLAUDE_MODEL", "")
        or "sonnet"
    )
    default_effort = os.environ.get("CLAUDE_EFFORT") or hyadex_setting("CLAUDE_EFFORT", "")
    supports_ultracode = claude_supports_effort("ultracode")
    # Optional candidate discovery must leave time for the native picker,
    # especially if a configured API endpoint is slow or unreachable.
    provider_options, provider_status = discover_claude_provider_models(timeout_seconds=2.0)
    native_options, native_status = discover_claude_native_models()
    help_text = ""
    help_available = False
    try:
        help_text = run_catalog_command([CLAUDE_BIN, "--help"])
        help_available = True
    except Exception as exc:
        logger.warning(
            "claude help model discovery failed error_type=%s",
            type(exc).__name__,
        )

    discovered_help_aliases, discovered_help_full_names = (
        parse_claude_help_model_option_groups(help_text)
    )
    if native_status == "success":
        # Native values preserve alias semantics; native labels identify the
        # actual version (e.g. Opus 5.5). Legacy candidates are included only
        # after the native picker applies account/organization restrictions.
        # Never append unfiltered static pins to a successful native result.
        model_options.extend(native_options)
    else:
        model_options.extend(discovered_help_aliases)
        model_options.extend(runtime_option(value, label) for value, label in CLAUDE_CLI_MODEL_ALIASES)
        if provider_status == "success":
            # The API is account-scoped; don't add static pins absent from it.
            model_options.extend(provider_options)
        else:
            model_options.extend(claude_fallback_model_options())
            model_options.extend(discovered_help_full_names)
    if native_status != "success" and provider_status != "success" and default_model and not any(
        str(option.get("value") or "") == default_model
        for option in model_options
    ):
        model_options.insert(0, runtime_option(default_model, title_model_label(default_model)))

    effort_match = re.search(r"--effort\s+<level>.*?\(([^)]+)\)", help_text, re.IGNORECASE | re.DOTALL)
    if effort_match:
        for effort in re.split(r"[,/\s]+", effort_match.group(1)):
            clean = effort.strip()
            if clean and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", clean):
                effort_options.append(runtime_option(clean, title_effort_label(clean)))
    if not effort_options:
        effort_options.extend(
            runtime_option(effort, title_effort_label(effort))
            for effort in ("low", "medium", "high", "xhigh", "max")
        )
    if supports_ultracode:
        effort_options.append(runtime_option("ultracode", "Ultracode"))

    model_sources = []
    if native_status == "success":
        model_sources.append("Cached Claude SDK initialize")
    elif provider_status == "success":
        model_sources.append("Anthropic Models API")
    if native_status != "success":
        model_sources.append("claude --help" if help_available else "claude --help failed")
    if native_status != "success" and provider_status != "success":
        if provider_status == "failed":
            model_sources.append("Anthropic Models API failed")
        model_sources.append("current fallback")
    effort_source = "claude --help" if help_available else "claude --help failed"
    default_model_label = next(
        (option["label"] for option in model_options if option["value"] == default_model),
        title_model_label(default_model),
    )
    # Label the "" option as the server default, like Codex, so it is not
    # mistaken for the explicit model or effort of the same name.
    return {
        "models": unique_runtime_options(model_options, f"Server default ({default_model_label})"),
        "efforts": unique_runtime_options(
            effort_options,
            f"Server default ({title_effort_label(default_effort)})" if default_effort else "Server default",
        ),
        "model_source": " + ".join(model_sources),
        "effort_source": effort_source,
        "default_model": default_model,
        "default_effort": default_effort or None,
    }


def build_claude_cmd(
    session_id: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    provider_id: str | None = None,
    no_session_persistence: bool = False,
    disable_provider_subagents: bool = False,
) -> list[str]:
    if sess.get("fork_resume_session_at") and not (provider_id and sess.get("fork_from")):
        raise ValueError("The completed Claude fork snapshot is unavailable; refusing an empty resume.")
    system_prompt = session_system_prompt(
        session_id,
        sess,
        manifest_path,
    )
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_prompt,
        "--system-prompt-snapshot", "off",
        "--disallowedTools",
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        *CLAUDE_NON_DURABLE_SCHEDULER_TOOLS,
        *(["Agent", "Task"] if disable_provider_subagents else []),
    ]
    if sess.get("model"):
        cmd.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        cmd.extend(["--effort", str(sess["effort"])])
    subagent_limit = sess.get("subagent_limit")
    if type(subagent_limit) is int and subagent_limit > 0:
        # Flag settings override inherited user/project env without writing
        # shared Claude settings or changing another chat's process.
        cmd.extend(["--settings", json.dumps({"env": {
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(subagent_limit),
        }}, separators=(",", ":"))])
    if no_session_persistence and not provider_id:
        cmd.append("--no-session-persistence")
    if provider_id:
        cmd.extend(["--resume", provider_id])
        if sess.get("fork_from"):
            cmd.append("--fork-session")
            cmd.extend(["--name", f"Fork: {sess.get('title') or sess['id']}"])
            if sess.get("fork_resume_session_at"):
                cmd.extend(["--resume-session-at", str(sess["fork_resume_session_at"])])
    return cmd


def claude_sdk_cli_path(env: dict[str, str]) -> str:
    """Resolve the user's existing Claude CLI so SDK mode keeps its auth."""

    configured = str(CLAUDE_BIN or "claude").strip() or "claude"
    expanded = str(Path(configured).expanduser())
    if os.path.sep in expanded:
        if Path(expanded).is_file():
            return expanded
        raise ClaudeSDKUnavailable(
            f"Claude CLI was not found at the configured path: {expanded}"
        )
    resolved = shutil.which(configured, path=env.get("PATH"))
    if not resolved:
        raise ClaudeSDKUnavailable(
            "Claude CLI is not available in the HyadexServer runtime PATH"
        )
    return resolved
