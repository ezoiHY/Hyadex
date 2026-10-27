"""Provider access to jobs: what an agent may see and change."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Literal

from fastapi import HTTPException, Request

from hyadex.core.session_settings import effective_provider_jobs_access
from hyadex.core.sessions import STORE
from hyadex.features.job_context import job_context_mode
from hyadex.features.job_schedules import job_schedule_kind
from hyadex.providers.registry import authorize_provider_action


async def authorize_provider_jobs_operation(
    request: Request,
    *,
    session_id: str,
    operation: Literal["read", "write"],
) -> dict[str, Any]:
    """Intersect the turn's issued Jobs ceiling with the chat's live policy."""

    capability = await authorize_provider_action(
        request,
        action="jobs",
        session_id=session_id,
    )
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    issued_access = effective_provider_jobs_access({
        "provider_jobs_access": capability.get("provider_jobs_access"),
    })
    live_access = effective_provider_jobs_access(session)
    if issued_access == "blocked" or live_access == "blocked":
        raise HTTPException(
            status_code=403,
            detail="agent scheduled-jobs access is blocked for this chat",
        )
    if operation == "write" and (
        issued_access != "full" or live_access != "full"
    ):
        raise HTTPException(
            status_code=403,
            detail="agent scheduled-jobs access is read-only for this chat",
        )
    return capability


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if not str(k).startswith("_")}
    out["manual_run_pending"] = bool(job.get("manual_run_pending"))
    out["schedule_kind"] = job_schedule_kind(out)
    out["context_mode"] = job_context_mode(out)
    out["timezone"] = str(out.get("timezone") or "UTC")
    for key in ("next_run_at", "scheduled_run_at", "schedule_start_at", "last_scheduled_run_at"):
        if out.get(key) is not None:
            out[f"{key}_iso"] = datetime.fromtimestamp(
                float(out[key]),
                tz=timezone.utc,
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return out


def provider_public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Project a job without disclosing saved target identities.

    Jobs saved before @Chat and Team targets were retired may still carry
    them. Agents see how many saved chat targets keep a job paused, never the
    targets or their chat ids.
    """

    out = public_job(job)
    raw_references = out.pop("chat_references", None)
    references = raw_references if isinstance(raw_references, list) else []
    retired_references = out.pop("team_references", None)
    target_ids: set[str] = set()
    for reference in [
        *references,
        *(retired_references if isinstance(retired_references, list) else []),
    ]:
        if isinstance(reference, dict):
            target_ids.update(
                str(reference[key]).strip()
                for key in ("session_id", "team_id", "target_id")
                if str(reference.get(key) or "").strip()
            )
    out["chat_target_count"] = len(references)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [redact(nested) for nested in value]
        if isinstance(value, str):
            result = value
            for target_id in sorted(target_ids, key=len, reverse=True):
                result = result.replace(target_id, "saved chat target")
            return result
        return value

    return redact(out)


def provider_public_job_run(run: dict[str, Any]) -> dict[str, Any]:
    """Project scheduled-run history without exposing saved target identities.

    Runs recorded by older servers can carry cross-chat routing identities;
    they stay private to the user-facing timeline.
    """

    private_fields = {
        "chat_references",
        "team_references",
        "target_session_id",
        "source_session_id",
        "source_run_id",
        "authorization_source_run_id",
        "authorization_route_id",
        "cross_chat_envelope_id",
        "cross_chat_exchange_id",
        "cross_chat_exchange_leg_id",
        "exchange_id",
        "exchange_leg_id",
        "inbound_leg_id",
        "envelope_id",
        "leg_id",
        "parent_leg_id",
        "cross_chat_obligation_ids",
        "cross_chat_exchange_ids",
        "cross_chat_direct_message_ids",
        "secure_peer_envelope_id",
        "requester_session_id",
        "responder_session_id",
        "target_chat_id",
    }
    private_ids: set[str] = set()

    def collect_private_values(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect_private_values(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_private_values(nested)
        elif isinstance(value, str) and value:
            private_ids.add(value)

    def collect_private_ids(value: Any) -> None:
        if isinstance(value, dict):
            raw_references = value.get("chat_references")
            if isinstance(raw_references, list):
                for raw_reference in raw_references:
                    if not isinstance(raw_reference, dict):
                        continue
                    clean_target_id = str(raw_reference.get("session_id") or "").strip()
                    if clean_target_id:
                        private_ids.add(clean_target_id)
            for key, nested in value.items():
                if key in private_fields and key != "chat_references":
                    collect_private_values(nested)
                if (
                    isinstance(nested, str)
                    and re.fullmatch(r"(?:handoff|exchange|leg)_[A-Za-z0-9_-]+", nested)
                ):
                    private_ids.add(nested)
                collect_private_ids(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_private_ids(nested)

    collect_private_ids(run)
    out = dict(run)
    raw_references = out.pop("chat_references", None)
    if isinstance(raw_references, list):
        out["chat_target_count"] = len(raw_references)
    out.pop("team_references", None)
    if isinstance(out.get("job"), dict):
        out["job"] = provider_public_job(out["job"])

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    nested
                    if key == "session_id"
                    else redact(nested)
                )
                for key, nested in value.items()
                if key not in private_fields
                and not (
                    key == "id"
                    and isinstance(nested, str)
                    and re.fullmatch(
                        r"(?:handoff|exchange|leg)_[A-Za-z0-9_-]+",
                        nested,
                    )
                )
            }
        if isinstance(value, list):
            return [redact(nested) for nested in value]
        if isinstance(value, str):
            result = value
            for private_id in sorted(private_ids, key=len, reverse=True):
                result = result.replace(private_id, "private cross-chat reference")
            return result
        return value

    return redact(out)
