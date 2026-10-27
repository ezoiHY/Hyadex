"""HTTP and WebSocket routes, one module per area of the API.

Each module declares its own ``router``; the server appends their routes to
the application router in a fixed order (``mount_routes``), so the routes
stay plain FastAPI routes rather than lazily included sub-routers.
"""
from __future__ import annotations

from fastapi import APIRouter


def mount_routes(parent: APIRouter, child: APIRouter) -> None:
    parent.routes.extend(child.routes)
