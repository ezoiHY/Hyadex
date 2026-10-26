"""Resolve scheduled work against the caller's provider configuration."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException


VALID_JOB_CONTEXT_MODES = {"chat", "standalone"}


def job_context_mode(job: dict[str, Any]) -> Literal["chat", "standalone"]:
    mode = str(job.get("context_mode") or "").strip().lower()
    return "standalone" if mode == "standalone" else "chat"


def resolve_job_context_contract(
    parent_session: dict[str, Any],
    context_mode: Any,
    backend: Any,
    *,
    context_mode_explicit: bool,
    enabled_backends: set[str],
    default_backend: str,
) -> tuple[Literal["chat", "standalone"], str | None]:
    """Resolve legacy omission and validate a job's provider context."""

    normalized_backend = str(backend or "").strip().lower() or None
    if normalized_backend is not None and normalized_backend not in enabled_backends:
        raise HTTPException(
            status_code=400,
            detail=f"backend must be one of {sorted(enabled_backends)}",
        )
    mode: Literal["chat", "standalone"] = (
        "standalone"
        if str(context_mode or "").strip().lower() == "standalone"
        else "chat"
    )
    parent_backend = str(
        parent_session.get("backend") or default_backend
    ).strip().lower()
    if parent_backend not in enabled_backends:
        parent_backend = default_backend
    if (
        not context_mode_explicit
        and normalized_backend is not None
        and normalized_backend != parent_backend
    ):
        # Contract-v8 and mobile clients predate context_mode. Their explicit
        # alternate backend meant an independent provider run; preserve that
        # intent instead of turning a formerly accepted request into a 400.
        mode = "standalone"
    if (
        mode == "chat"
        and normalized_backend is not None
        and normalized_backend != parent_backend
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "A job that continues in this chat must use the parent chat "
                f"backend ({parent_backend}); choose standalone context to use "
                f"{normalized_backend}."
            ),
        )
    # None intentionally means "inherit the parent chat backend". Preserve it
    # for older clients and legacy job payloads instead of expanding it into a
    # new required field value.
    return mode, normalized_backend
