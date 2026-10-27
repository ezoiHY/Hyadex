"""Construct an ASGI application from explicit runtime bindings.

Importing this module does not load the legacy server or initialize stores,
provider processes, state directories, background tasks or environment settings.
The caller owns those dependencies and their lifecycle.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

HTTPHandler = Callable[[Request], Awaitable[Response]]
HTTPAuthorization = Callable[[Request, HTTPHandler], Awaitable[Response]]
ApplicationLifespan = Callable[[FastAPI], AbstractAsyncContextManager[Mapping[str, Any] | None]]


@dataclass(frozen=True)
class ApplicationBindings:
    """Dependencies required to create one application; authorization is mandatory."""

    router: APIRouter
    authorize_http: HTTPAuthorization
    lifespan: ApplicationLifespan | None = None


def create_app(bindings: ApplicationBindings) -> FastAPI:
    """Create a distinct application while preserving its supplied route order."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Keep the production lifecycle outside included-router lifecycles,
        # including startup/shutdown hooks and their yielded request state.
        async with AsyncExitStack() as stack:
            runtime_state = (await stack.enter_async_context(bindings.lifespan(app))
                             if bindings.lifespan is not None else None)
            route_state = await stack.enter_async_context(bindings.router.lifespan_context(app))
            if runtime_state is None and route_state is None:
                yield None
            else:
                yield {**(route_state or {}), **(runtime_state or {})}

    # Preserve directly declared top-level routes. FastAPI's include_router may add a
    # lazy wrapper; the native mutation guard inspects body_field before it
    # acquires an admission lease. The supplied routers are already complete.
    app = FastAPI(title="HyadexServer", routes=list(bindings.router.routes),
                  lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=[], allow_headers=[])
    app.middleware("http")(bindings.authorize_http)
    return app
