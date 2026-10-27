"""The Hyadex server application: request authentication, route assembly and the entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress
import os
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.routing import Match
import uvicorn

from hyadex.api import (
    admin as admin_routes,
    agent as agent_routes,
    files as files_routes,
    health as health_routes,
    jobs as jobs_routes,
    mount_routes,
    provider_mcp as provider_mcp_routes,
    providers as providers_routes,
    search as search_routes,
    sessions as sessions_routes,
    terminal as terminal_routes,
    turns as turns_routes,
)
from hyadex.api.common import require_native_admin_control
from hyadex.api.app import ApplicationBindings, create_app
from hyadex.core.config import (
    AGENT_TOKEN,
    EVENTS_WEBSOCKET_PROTOCOL,
    hyadex_setting,
    STATE_DIR,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from hyadex.core.http import (
    agent_helper_browser_request_forbidden,
    declared_auth_routes,
    prebuffer_bounded_request_body,
    prebuffer_declared_body,
    privileged_native_browser_request_forbidden,
    privileged_native_json_transport,
    register_unsafe_http_mutation_locked,
    release_unsafe_http_mutation,
    request_authorized,
    request_exact_native_token_header_authorized,
    request_exact_restart_header_authorized,
    request_route_parses_body,
    router,
    server_restart_browser_request_forbidden,
    server_restart_error_detail,
    server_restart_post_transport_error,
    unsafe_http_mutation_blocked_response,
    UnsafeMutationAdmissionRejected,
    validate_listener_auth,
    websocket_authorized,
    websocket_endpoint_subprotocol,
)
from hyadex.core.server import SERVER
from hyadex.core.server_logging import configure_server_logging
from hyadex.core.sessions import native_session_available
from hyadex.core.state import (
    ROUTE_AUTH_INDEX,
    RouteAuth,
    UNSAFE_HTTP_MUTATION_ADMISSION_LOCK,
    UNSAFE_HTTP_MUTATION_METHODS,
    UNSAFE_HTTP_MUTATION_TASKS,
    USER_ROUTE,
)
from hyadex.features.group_runtime import GROUPS
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.ops.lifecycle import AUTOPILOT, lifespan, uvicorn_graceful_shutdown_seconds
from hyadex.providers.authority import request_exact_registered_provider_capability
from hyadex.providers.codex.commands import SIDE_QUESTIONS
from hyadex.providers.codex.tools import (
    request_client_is_codex_provider_mcp_local,
    request_exact_codex_provider_mcp_secret,
)
from hyadex.providers.registry import request_client_is_provider_helper_local
from hyadex.api.autopilot import create_autopilot_router
from hyadex.api.groups import create_group_router
from hyadex.ops import instances as server_instances
from hyadex.features import side_question_service as side_questions


# Provider runners register themselves with the turn engine on import.
import hyadex.providers.claude.turns  # noqa: E402,F401
import hyadex.providers.codex.turns  # noqa: E402,F401

logger = logging.getLogger("hyadex-server")

# Fail before reading another instance's config or resolving/migrating state.
server_instances.validate_runtime_environment(os.environ, Path.home())



def route_auth_for_scope(app: Any, method: str, path: str, *, any_method: bool = False) -> RouteAuth:
    """The declaration of the route a request would reach (default ``user``).

    Only the few declared routes are matched, so ordinary requests pay one
    short scan. ``any_method`` also accepts a path match with another method,
    for preflight requests.
    """
    index = ROUTE_AUTH_INDEX.get(app)
    if index is None:
        index = ROUTE_AUTH_INDEX[app] = declared_auth_routes(app.router.routes)
    scope = {"type": "http", "method": str(method or "").upper(), "path": path, "root_path": ""}
    for route, auth in index:
        match, _child = route.matches(scope)
        if match == Match.FULL or (any_method and match == Match.PARTIAL):
            return auth
    return USER_ROUTE


def agent_helper_route_body_limit(method: str, path: str) -> int | None:
    auth = route_auth_for_scope(app, method, path)
    return auth.body_limit if auth.kind == "agent_helper" else None


def is_agent_helper_route(method: str, path: str) -> bool:
    return route_auth_for_scope(app, method, path).kind == "agent_helper"


async def require_agent_token(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        # Preflight never reaches native controls or the provider channel.
        preflight = route_auth_for_scope(request.app, "OPTIONS", path, any_method=True)
        if preflight.kind in {"native_admin", "provider_mcp"}:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    auth = route_auth_for_scope(request.app, request.method, path)

    if auth.kind == "provider_mcp":
        if (
            request.method.upper() != "POST"
            or agent_helper_browser_request_forbidden(request)
            or not request_client_is_codex_provider_mcp_local(request)
            or not request_exact_codex_provider_mcp_secret(request)
        ):
            return JSONResponse(
                {"detail": "Codex provider MCP transport is forbidden"},
                status_code=403,
            )
        if (rejected := await prebuffer_declared_body(request, auth.body_limit)) is not None:
            return rejected
        request.state.codex_provider_mcp_authenticated = True
    elif auth.kind == "agent_helper":
        # Helpers use a per-run provider capability and validate their action
        # and session in-route. The client bearer is never a substitute.
        if (
            agent_helper_browser_request_forbidden(request)
            or not request_client_is_provider_helper_local(request)
        ):
            return JSONResponse(
                {"detail": "agent helper route is restricted to local provider clients"},
                status_code=403,
            )
        if not await request_exact_registered_provider_capability(request):
            return JSONResponse(
                {"detail": "provider capability is invalid"},
                status_code=403,
            )
        if request.method.upper() in UNSAFE_HTTP_MUTATION_METHODS:
            if (rejected := await prebuffer_declared_body(request, auth.body_limit)) is not None:
                return rejected
    elif auth.kind == "native_admin":
        if privileged_native_browser_request_forbidden(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if not AGENT_TOKEN:
            return JSONResponse(
                {
                    "detail": (
                        "This administrative control requires authenticated "
                        "HyadexServer mode"
                    )
                },
                status_code=503,
            )
        if not request_exact_native_token_header_authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if request.method.upper() == "PUT" and auth.body_limit is not None:
            declared_size, transport_error = privileged_native_json_transport(
                request,
                max_body_bytes=auth.body_limit,
                label="Codex settings",
                require_content_length=True,
            )
            if transport_error is not None:
                status_code, detail = transport_error
                return JSONResponse({"detail": detail}, status_code=status_code)
            body_error = await prebuffer_bounded_request_body(
                request,
                max_body_bytes=auth.body_limit,
                declared_size=declared_size,
            )
            if body_error is not None:
                status_code, detail = body_error
                return JSONResponse({"detail": detail}, status_code=status_code)

    # Restart has a stricter header-only check below, including its structured
    # error contract; URL credentials must reach that rejection.
    if auth.kind in {"user", "native_admin"} and not request_authorized(request):
        logger.warning("unauthorized request method=%s path=%s host=%s", request.method, path, request.client.host if request.client else "-")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    if auth.kind == "restart":
        if server_restart_browser_request_forbidden(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        # Enforce the privileged route's header-only authentication before
        # FastAPI parses a request body. The route repeats these checks for
        # direct invocation safety and performs the managed-service proof.
        if not AGENT_TOKEN:
            return JSONResponse(
                {
                    "detail": server_restart_error_detail(
                        "server_restart_auth_required",
                        "Server restart requires authenticated HyadexServer mode.",
                        action=(
                            "Install or configure HyadexServer with an access token."
                        ),
                    )
                },
                status_code=503,
            )
        if not request_exact_restart_header_authorized(request):
            return JSONResponse(
                {
                    "detail": server_restart_error_detail(
                        "server_restart_unauthorized",
                        (
                            "Server restart requires an access token in an "
                            "authorization header."
                        ),
                    )
                },
                status_code=401,
            )
        if request.method.upper() == "POST":
            transport_error = server_restart_post_transport_error(request)
            if transport_error is not None:
                status_code, detail = transport_error
                return JSONResponse({"detail": detail}, status_code=status_code)

    # Restart drives the lifecycle fence itself, so it is not admitted as an
    # ordinary mutation.
    unsafe_mutation = (
        request.method.upper() in UNSAFE_HTTP_MUTATION_METHODS
        and auth.kind != "restart"
    )
    if not unsafe_mutation:
        return await call_next(request)
    mutation_id = ""
    mutation_task: asyncio.Task[Any] | None = None
    mutation_registry = UNSAFE_HTTP_MUTATION_TASKS
    admission_rejected_response: JSONResponse | None = None
    downstream_task: asyncio.Task[Response]
    original_receive = getattr(request, "_receive", None)
    defer_admission_until_body_received = (
        original_receive is not None
        and request_route_parses_body(request)
        and not getattr(request.state, "bounded_body_prebuffered", False)
    )

    async def admit_unsafe_http_mutation(
        task: asyncio.Task[Any],
    ) -> None:
        nonlocal admission_rejected_response, mutation_id
        nonlocal mutation_task, mutation_registry
        if mutation_id:
            return
        async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
            if mutation_id:
                return
            blocked = unsafe_http_mutation_blocked_response()
            if blocked is not None:
                admission_rejected_response = blocked
                raise UnsafeMutationAdmissionRejected(blocked)
            (
                mutation_id,
                mutation_task,
                mutation_registry,
            ) = register_unsafe_http_mutation_locked(task)

    try:
        if defer_admission_until_body_received:
            # Authentication and transport validation above are header-only.
            # Do not let a client reserve the global lifecycle fence merely
            # by opening a request and withholding its body. FastAPI always
            # consumes a route's declared body before invoking the handler, so
            # atomically acquire the lease at the final request-body frame.
            async def receive_after_mutation_admission() -> dict[str, Any]:
                message = await original_receive()
                if (
                    message.get("type") == "http.request"
                    and not message.get("more_body", False)
                ):
                    # Starlette reads each frame in a short-lived task used to
                    # race the request against disconnect. Registering that
                    # receiver would release the lease as soon as it returns,
                    # before validation or the handler can mutate anything.
                    # The enclosing call_next task instead owns the complete
                    # downstream request through its response boundary.
                    await admit_unsafe_http_mutation(downstream_task)
                return message

            request._receive = receive_after_mutation_admission
            downstream_task = asyncio.create_task(call_next(request))
        else:
            async with UNSAFE_HTTP_MUTATION_ADMISSION_LOCK:
                blocked = unsafe_http_mutation_blocked_response()
                if blocked is not None:
                    return blocked
                # Own the downstream operation independently from the HTTP
                # caller. Cancelling an await on asyncio.to_thread only cancels
                # its Future; the worker keeps mutating state. Shielding this
                # child means the lease remains authoritative until the actual
                # handler settles.
                downstream_task = asyncio.create_task(call_next(request))
                (
                    mutation_id,
                    mutation_task,
                    mutation_registry,
                ) = register_unsafe_http_mutation_locked(downstream_task)
        try:
            response = await asyncio.shield(downstream_task)
            # FastAPI deliberately normalizes exceptions raised while parsing
            # a declared request body into a 400 response. Preserve the exact
            # admission result recorded before the final body frame instead of
            # exposing that parser fallback to the client.
            if admission_rejected_response is not None:
                return admission_rejected_response
            return response
        except UnsafeMutationAdmissionRejected as exc:
            return exc.response
        except asyncio.CancelledError:
            # A disconnected/re-cancelled request cannot release admission
            # while its downstream thread or coroutine still owns a mutation.
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(downstream_task)
            raise
    finally:
        if defer_admission_until_body_received:
            request._receive = original_receive
        if mutation_id:
            release_unsafe_http_mutation(
                mutation_id,
                mutation_task,
                mutation_registry,
            )


router.include_router(side_questions.create_side_question_router(
    authorize=require_native_admin_control,
    session_exists=native_session_available,
    runtime=SIDE_QUESTIONS,
))


router.include_router(create_autopilot_router(AUTOPILOT))


router.include_router(create_group_router(
    GROUPS,
    websocket_authorized=websocket_authorized,
    reserve=HUB.reserve,
    register=HUB.register_accepted,
    unsubscribe=HUB.unsubscribe,
    subprotocol=lambda ws: websocket_endpoint_subprotocol(ws, EVENTS_WEBSOCKET_PROTOCOL),
    send_timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
))


def main() -> int:
    configure_server_logging(STATE_DIR)
    parser = argparse.ArgumentParser(description="HyadexServer")
    parser.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--bind", default=hyadex_setting("AGENT_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(hyadex_setting("AGENT_PORT", "7870")))
    args = parser.parse_args()
    # Downstream helper environments and the required Codex MCP transport
    # must target the socket selected by the CLI, not the import-time default.
    SERVER.bind_address = str(args.bind)
    SERVER.port = int(args.port)
    try:
        validate_listener_auth()
    except RuntimeError as exc:
        parser.error(str(exc))
    uvicorn.run(
        app,
        host=args.bind,
        port=args.port,
        log_level="info",
        # Route uvicorn's own loggers through the root handlers above (stderr
        # plus the rotating file) instead of its private stdout/stderr config,
        # so access and error lines are rotated and filtered as well.
        log_config=None,
        # The local-only Hub gate relies on the actual socket peer. This
        # preview does not support header-trusting proxy operation.
        proxy_headers=False,
        server_header=False,
        # Without this uvicorn waits forever for open websockets/background
        # work after SIGTERM, and the restart watchdog is the only way out.
        timeout_graceful_shutdown=uvicorn_graceful_shutdown_seconds(),
    )
    return 0


# Route modules, in the order their routes are matched.
mount_routes(router, provider_mcp_routes.router)
mount_routes(router, health_routes.router)
mount_routes(router, admin_routes.router)
mount_routes(router, terminal_routes.router)
mount_routes(router, sessions_routes.router)
mount_routes(router, search_routes.router)
mount_routes(router, providers_routes.router)
mount_routes(router, turns_routes.router)
mount_routes(router, jobs_routes.router)
mount_routes(router, agent_routes.router)
mount_routes(router, files_routes.router)

app = create_app(ApplicationBindings(
    router=router, authorize_http=require_agent_token, lifespan=lifespan,
))


if __name__ == "__main__":
    raise SystemExit(main())
