"""HTTP admission: route authentication, unsafe-mutation admission and request helpers."""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from datetime import timezone
import hmac
import re
from typing import Any, Literal
import uuid

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.routing import Match

from hyadex.core.config import AGENT_TOKEN, EMERGENCY_MESSAGE_MAX_CHARS, MAX_JOB_TITLE_CHARS
from hyadex.core.server import SERVER
from hyadex.core.sessions import (
    EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
    execution_maintenance_blocks_work,
    managed_server_restart_blocks_work,
)
from hyadex.core.state import RouteAuth, RouteAuthKind, UNSAFE_HTTP_MUTATION_TASKS
from hyadex.core.util import decoded_exact_header_secret
from hyadex.providers.instructions import MAX_JOB_PROMPT_CHARS
from hyadex.providers.registry import network_host_is_loopback


def token_matches(candidate: str | None) -> bool:
    if not AGENT_TOKEN:
        return True
    if not candidate:
        return False
    try:
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            AGENT_TOKEN.encode("utf-8"),
        )
    except (AttributeError, UnicodeError):
        return False


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return None


def request_authorized(request: Request) -> bool:
    if not AGENT_TOKEN:
        return True
    return (
        token_matches(bearer_token(request.headers.get("authorization")))
        or token_matches(request.headers.get("x-hyadex-token"))
        or token_matches(request.headers.get("x-zenithdock-token"))
    )


def request_exact_native_token_header_authorized(request: Request) -> bool:
    """Accept exactly one desktop/mobile native token header.

    Update and local Codex configuration are one privileged realm.  Reserving
    ``Authorization`` for controls that explicitly document it prevents a
    generic bearer, URL credential, or duplicate-header interpretation from
    changing as requests cross HTTP implementations.
    """

    if not AGENT_TOKEN or "token" in request.query_params:
        return False
    raw_headers = request.scope.get("headers", [])
    if any(
        bytes(name).lower() in {b"authorization", b"cookie"}
        for name, _value in raw_headers
    ):
        return False
    values = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower()
        in {b"x-hyadex-token", b"x-zenithdock-token"}
    ]
    if len(values) != 1:
        return False
    candidate = decoded_exact_header_secret(values[0])
    return candidate is not None and token_matches(candidate)


def request_exact_restart_header_authorized(request: Request) -> bool:
    """Accept one, and only one, of the restart control's documented headers."""

    if not AGENT_TOKEN or "token" in request.query_params:
        return False
    recognized = {
        b"authorization",
        b"x-hyadex-token",
        b"x-zenithdock-token",
    }
    values = [
        (bytes(name).lower(), bytes(value))
        for name, value in request.scope.get("headers", [])
        if bytes(name).lower() in recognized
    ]
    if len(values) != 1:
        return False
    name, raw_value = values[0]
    if name == b"authorization":
        try:
            authorization = raw_value.decode("ascii")
        except UnicodeDecodeError:
            return False
        scheme, separator, candidate = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            return False
        candidate = decoded_exact_header_secret(candidate.encode("ascii"))
    else:
        candidate = decoded_exact_header_secret(raw_value)
    return candidate is not None and token_matches(candidate)


def request_exact_bearer_authorized(request: Request) -> bool:
    """Accept exactly one canonical Agent bearer and no alternate realm."""

    if not AGENT_TOKEN or "token" in request.query_params:
        return False
    raw_headers = request.scope.get("headers", [])
    names = [bytes(name).lower() for name, _value in raw_headers]
    if (
        b"x-hyadex-token" in names
        or b"x-zenithdock-token" in names
        or b"cookie" in names
    ):
        return False
    values = [
        value
        for name, value in raw_headers
        if bytes(name).lower() == b"authorization"
    ]
    if len(values) != 1:
        return False
    try:
        authorization = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    if not authorization.startswith("Bearer "):
        return False
    candidate = authorization[7:]
    if (
        not candidate
        or candidate != candidate.strip()
        or any(char.isspace() for char in candidate)
    ):
        return False
    return token_matches(candidate)


def validate_listener_auth() -> None:
    """Require a client credential before exposing any non-loopback listener."""

    if not str(AGENT_TOKEN or "").strip() and not network_host_is_loopback(SERVER.bind_address):
        raise RuntimeError(
            "Non-loopback listeners require HYADEX_AGENT_TOKEN. "
            "Set a token or use --bind 127.0.0.1 for local development."
        )


def websocket_token_subprotocol(ws: WebSocket) -> str | None:
    """Return the first exact token protocol that authenticates this socket."""

    if not AGENT_TOKEN:
        return None
    for value in websocket_requested_protocols(ws):
        if not value.startswith("hyadex-token."):
            continue
        encoded = value[len("hyadex-token."):]
        if not encoded:
            continue
        with suppress(Exception):
            padding = "=" * (-len(encoded) % 4)
            candidate = base64.urlsafe_b64decode(
                encoded + padding
            ).decode("utf-8")
            if token_matches(candidate):
                return value
    return None


def websocket_authorized(ws: WebSocket) -> bool:
    if not AGENT_TOKEN:
        return True
    return (
        token_matches(bearer_token(ws.headers.get("authorization")))
        or token_matches(ws.headers.get("x-hyadex-token"))
        or token_matches(ws.headers.get("x-zenithdock-token"))
        or websocket_token_subprotocol(ws) is not None
    )


def websocket_endpoint_subprotocol(
    ws: WebSocket,
    endpoint_protocol: str,
) -> str | None:
    """Select a fixed non-secret protocol, with token-only compatibility."""

    if endpoint_protocol in websocket_requested_protocols(ws):
        return endpoint_protocol
    return websocket_token_subprotocol(ws)


def websocket_requested_protocols(ws: WebSocket) -> list[str]:
    return [
        value.strip()
        for value in str(ws.headers.get("sec-websocket-protocol") or "").split(",")
        if value.strip()
    ]


class AgentNotificationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    level: Literal["normal", "important", "emergency"]
    message: str = Field(min_length=1, max_length=EMERGENCY_MESSAGE_MAX_CHARS)


class TerminalOpenRequest(BaseModel):
    cwd: str | None = None


class TerminalInputRequest(BaseModel):
    text: str | None = None
    enter: bool = True
    key: str | None = None


class TerminalResizeRequest(BaseModel):
    columns: int = 100
    rows: int = 30


class TerminalActionRequest(BaseModel):
    action: str
    target: str | None = None


class JobCreateFields(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_JOB_TITLE_CHARS)
    prompt: str = Field(min_length=1, max_length=MAX_JOB_PROMPT_CHARS)
    schedule_kind: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    rrule: str | None = None
    timezone: str | None = None
    first_run_at: str | None = None
    loop: bool = False
    max_runs: int | None = None
    enabled: bool = True
    backend: str | None = None
    context_mode: Literal["chat", "standalone"] = "chat"
    # Saved @Chat and Team targets were retired; reject rather than drop them.
    chat_references: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    team_references: list[dict[str, Any]] = Field(default_factory=list, max_length=0)


class CreateJobRequest(JobCreateFields):
    session_id: str


class CreateScopedJobRequest(JobCreateFields):
    pass


class AgentCreateScopedJobRequest(JobCreateFields):
    model_config = {"extra": "forbid"}

    chat_routes: list[dict[str, Any]] = Field(default_factory=list, max_length=0)


class UpdateJobRequest(BaseModel):
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_JOB_TITLE_CHARS,
    )
    prompt: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_JOB_PROMPT_CHARS,
    )
    schedule_kind: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    rrule: str | None = None
    timezone: str | None = None
    next_run_at: str | None = None
    loop: bool | None = None
    max_runs: int | None = None
    enabled: bool | None = None
    backend: str | None = None
    context_mode: Literal["chat", "standalone"] | None = None
    # Saved @Chat and Team targets were retired. Only an empty list (which
    # clears a legacy job's saved targets) is accepted.
    chat_references: list[dict[str, Any]] | None = Field(default=None, max_length=0)
    team_references: list[dict[str, Any]] | None = Field(default=None, max_length=0)


class AgentUpdateJobRequest(UpdateJobRequest):
    model_config = {"extra": "forbid"}

    chat_routes: list[dict[str, Any]] | None = Field(default=None, max_length=0)


SERVER_RESTART_MAX_BODY_BYTES = 2_048


class UnsafeMutationAdmissionRejected(RuntimeError):
    """A body finished only after lifecycle admission had closed."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__("unsafe HTTP mutation admission closed")
        self.response = response


def live_unsafe_http_mutation_ids_locked() -> list[str]:
    """Return exact live mutation leases, pruning only tasks already finished.

    Callers hold ``UNSAFE_HTTP_MUTATION_ADMISSION_LOCK`` in production. A task
    with cancellation requested remains live until it is actually done because
    request code may suppress cancellation and continue committing a change.
    """

    for mutation_id, task in tuple(UNSAFE_HTTP_MUTATION_TASKS.items()):
        if task is None:
            continue
        try:
            done = task.done()
        except Exception:
            done = False
        if done and UNSAFE_HTTP_MUTATION_TASKS.get(mutation_id) is task:
            UNSAFE_HTTP_MUTATION_TASKS.pop(mutation_id, None)
    return sorted(UNSAFE_HTTP_MUTATION_TASKS)


def unsafe_http_mutation_count_locked() -> int:
    return len(live_unsafe_http_mutation_ids_locked())


def register_unsafe_http_mutation_locked(
    task: asyncio.Task[Any],
) -> tuple[str, asyncio.Task[Any] | None, dict[str, asyncio.Task[Any] | None]]:
    """Register one downstream mutation and its exact completion fallback."""

    mutation_id = uuid.uuid4().hex
    registry = UNSAFE_HTTP_MUTATION_TASKS
    registry[mutation_id] = task
    def release_finished_mutation(completed: asyncio.Task[Any]) -> None:
        if registry.get(mutation_id) is completed:
            registry.pop(mutation_id, None)

    task.add_done_callback(release_finished_mutation)
    return mutation_id, task, registry


def release_unsafe_http_mutation(
    mutation_id: str,
    task: asyncio.Task[Any] | None,
    registry: dict[str, asyncio.Task[Any] | None],
) -> None:
    """Release only the exact completed downstream lease, without awaiting."""

    if (
        registry.get(mutation_id) is task
        and (task is None or task.done())
    ):
        registry.pop(mutation_id, None)


def request_route_parses_body(request: Request) -> bool:
    """Return whether FastAPI consumes the body before invoking the handler."""

    for route in request.app.router.routes:
        try:
            match, _child_scope = route.matches(request.scope)
        except Exception:
            continue
        if match == Match.FULL:
            return getattr(route, "body_field", None) is not None
    return False


def unsafe_http_mutation_blocked_response() -> JSONResponse | None:
    if managed_server_restart_blocks_work():
        return JSONResponse(
            {
                "detail": server_restart_error_detail(
                    "server_restarting",
                    (
                        "HyadexServer is restarting and is not accepting "
                        "server changes."
                    ),
                    action="Wait for the server to reconnect, then retry.",
                    retryable=True,
                )
            },
            status_code=409,
        )
    if execution_maintenance_blocks_work():
        return JSONResponse(
            {"detail": EXECUTION_MAINTENANCE_ACTIVE_DETAIL},
            status_code=409,
        )
    return None


def server_restart_error_detail(
    code: str,
    message: str,
    *,
    action: str | None = None,
    retryable: bool = False,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "message": message[:500],
        "retryable": retryable,
    }
    if action:
        result["action"] = action[:500]
    result.update(details)
    return result


router = APIRouter()


def route_auth(kind: RouteAuthKind, body_limit: int | None = None) -> list[Any]:
    """``dependencies=route_auth(...)`` for a route decorator."""
    return [Depends(RouteAuth(kind, body_limit))]


def declared_route_auth(route: Any) -> RouteAuth | None:
    for dependency in getattr(route, "dependencies", None) or ():
        if isinstance(getattr(dependency, "dependency", None), RouteAuth):
            return dependency.dependency
    return None


def declared_auth_routes(routes: Any) -> tuple[tuple[Any, RouteAuth], ...]:
    """Every route with a non-default declaration, including included routers."""
    found: list[tuple[Any, RouteAuth]] = []
    for route in routes:
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested is not None:
            found.extend(declared_auth_routes(nested))
            continue
        auth = declared_route_auth(route)
        if auth is not None and auth.kind != "user":
            found.append((route, auth))
    return tuple(found)


def privileged_native_browser_request_forbidden(request: Request) -> bool:
    """Keep native settings authority out of ambient browser requests."""

    return any(
        bytes(name).lower() in {b"origin", b"cookie"}
        or bytes(name).lower().startswith(b"sec-fetch-")
        for name, _value in request.scope.get("headers", [])
    )


def agent_helper_browser_request_forbidden(request: Request) -> bool:
    """Reject browser ambient authority before inspecting provider secrets."""

    return privileged_native_browser_request_forbidden(request)


def bounded_request_content_length(
    request: Request,
    *,
    max_body_bytes: int,
) -> tuple[int | None, tuple[int, str] | None]:
    """Parse one optional canonical Content-Length and reject framing conflicts."""

    raw_headers = request.scope.get("headers", [])
    lengths = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower() == b"content-length"
    ]
    transfer_encoded = any(
        bytes(name).lower() == b"transfer-encoding"
        for name, _value in raw_headers
    )
    if len(lengths) > 1 or (lengths and transfer_encoded):
        return None, (400, "request body framing is invalid")
    if not lengths:
        return None, None
    try:
        raw_length = lengths[0].decode("ascii")
        size = int(raw_length, 10)
    except (UnicodeDecodeError, ValueError):
        return None, (400, "request content length is invalid")
    if raw_length != str(size) or size < 0:
        return None, (400, "request content length is invalid")
    if size > max_body_bytes:
        return None, (413, "request body is too large")
    return size, None


async def prebuffer_bounded_request_body(
    request: Request,
    *,
    max_body_bytes: int,
    declared_size: int | None,
) -> tuple[int, str] | None:
    """Read at most one route ceiling before body parsing and cache the result."""

    if hasattr(request, "_body"):
        body = bytes(request._body)
        if len(body) > max_body_bytes:
            return 413, "request body is too large"
        if declared_size is not None and len(body) != declared_size:
            return 400, "request content length does not match the body"
        request.state.bounded_body_prebuffered = True
        return None

    body = bytearray()
    while True:
        message = await request._receive()
        if message.get("type") != "http.request":
            return 400, "request body was interrupted"
        chunk = message.get("body", b"")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            return 400, "request body framing is invalid"
        if len(body) + len(chunk) > max_body_bytes:
            return 413, "request body is too large"
        body.extend(chunk)
        if not message.get("more_body", False):
            break
    if declared_size is not None and len(body) != declared_size:
        return 400, "request content length does not match the body"
    request._body = bytes(body)
    request._stream_consumed = True
    request.state.bounded_body_prebuffered = True
    return None


def server_restart_browser_request_forbidden(request: Request) -> bool:
    """Reject browser ambient-authority requests to the restart control."""

    if str(request.headers.get("origin") or "").strip():
        return True
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    return bool(fetch_site and fetch_site != "none")


def server_restart_post_transport_error(request: Request) -> tuple[int, str] | None:
    """Validate bounded JSON transport metadata before route body parsing."""

    media_type = str(request.headers.get("content-type") or "").partition(";")[0]
    if media_type.strip().lower() != "application/json":
        return 415, "restart requests require application/json"

    raw_headers = request.scope.get("headers", [])
    if any(
        bytes(name).lower() == b"transfer-encoding"
        for name, _value in raw_headers
    ):
        return 400, "restart requests do not accept transfer encoding"
    content_lengths = [
        value.decode("latin-1").strip()
        for name, value in raw_headers
        if bytes(name).lower() == b"content-length"
    ]
    if not content_lengths:
        return 411, "restart request content length is required"
    if len(content_lengths) != 1:
        return 400, "restart request content length is invalid"
    content_length = content_lengths[0]
    if re.fullmatch(r"[0-9]+", content_length) is None:
        return 400, "restart request content length is invalid"
    size = int(content_length, 10)
    if size <= 0:
        return 400, "restart request content length is invalid"
    if size > SERVER_RESTART_MAX_BODY_BYTES:
        return 413, "restart request body is too large"
    return None


def privileged_native_json_transport(
    request: Request,
    *,
    max_body_bytes: int,
    label: str,
    require_content_length: bool,
) -> tuple[int | None, tuple[int, str] | None]:
    """Validate privileged JSON metadata before any model/body allocation."""

    raw_headers = request.scope.get("headers", [])
    content_types = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower() == b"content-type"
    ]
    if len(content_types) != 1:
        return None, (415, f"{label} requests require application/json")
    try:
        media_type = content_types[0].decode("ascii").partition(";")[0]
    except UnicodeDecodeError:
        return None, (415, f"{label} requests require application/json")
    if media_type.strip().lower() != "application/json":
        return None, (415, f"{label} requests require application/json")
    transfer_encoded = any(
        bytes(name).lower() == b"transfer-encoding"
        for name, _value in raw_headers
    )
    if require_content_length and transfer_encoded:
        return None, (400, f"{label} requests do not accept transfer encoding")
    declared_size, framing_error = bounded_request_content_length(
        request,
        max_body_bytes=max_body_bytes,
    )
    if framing_error is not None:
        return None, framing_error
    if require_content_length and declared_size is None:
        return None, (411, f"{label} request content length is required")
    if require_content_length and not declared_size:
        return None, (400, f"{label} request content length is invalid")
    return declared_size, None


async def prebuffer_declared_body(request: Request, body_limit: int | None) -> JSONResponse | None:
    """Reject a body over the route's declared limit before reading it."""
    if body_limit is None:
        return None
    declared_size, framing_error = bounded_request_content_length(request, max_body_bytes=body_limit)
    if framing_error is not None:
        status_code, detail = framing_error
        return JSONResponse({"detail": detail}, status_code=status_code)
    body_error = await prebuffer_bounded_request_body(request, max_body_bytes=body_limit, declared_size=declared_size)
    if body_error is not None:
        status_code, detail = body_error
        return JSONResponse({"detail": detail}, status_code=status_code)
    return None
