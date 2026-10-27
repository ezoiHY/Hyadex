"""Helpers shared by several route modules."""
from __future__ import annotations

from fastapi import HTTPException, Request

from hyadex.core.config import AGENT_TOKEN
from hyadex.core.http import (
    privileged_native_browser_request_forbidden,
    request_exact_native_token_header_authorized,
)


def require_native_admin_control(request: Request) -> None:
    """Repeat privileged native auth at the endpoint invocation boundary."""

    if privileged_native_browser_request_forbidden(request):
        raise HTTPException(status_code=403, detail="forbidden")
    if not AGENT_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "This administrative control requires authenticated "
                "HyadexServer mode"
            ),
        )
    if not request_exact_native_token_header_authorized(request):
        raise HTTPException(status_code=401, detail="unauthorized")
