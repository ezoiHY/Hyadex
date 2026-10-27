"""Publishing turn artifacts from manifests into chat records."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import logging.handlers
from pathlib import Path
import time
from typing import Any

from fastapi import HTTPException

from hyadex.core.event_log import (
    ArtifactPublicationError,
    load_publication_events,
    store_publication_events,
    verified_publication_events,
)
from hyadex.core.history_sync import (
    append_durable_event,
    append_durable_event_batch,
    append_durable_event_batch_locked,
)
from hyadex.core.paths import manifests_dir
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    ensure_session_not_deleting,
    event_delivery_lock,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS
from hyadex.features.files import (
    artifact_entries_digest,
    artifact_publication_lock,
    live_manifest_batch_ready,
    prepare_artifact_records,
    read_artifact_manifest,
    remove_artifact_records_async,
    remove_upload_tree,
    run_event_metadata,
    run_upload_worker,
    unlink_manifest_if_unchanged,
)
from hyadex.features.usage import join_task_despite_caller_cancellation

logger = logging.getLogger("hyadex-server")


async def persist_artifact_records(
    session_id: str,
    run_id: str,
    records: list[dict[str, Any]],
    *,
    publication_id: str | None = None,
    publication_digest: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fsync artifact events as one batch and deduplicate verified retries."""
    async with event_delivery_lock(session_id):
        if publication_id:
            existing = await asyncio.to_thread(
                load_publication_events,
                session_id,
                publication_id,
            )
            verified = await asyncio.to_thread(
                verified_publication_events,
                existing,
                run_id=run_id,
                publication_id=publication_id,
                publication_digest=str(publication_digest or ""),
                expected_count=len(records),
            )
            if verified:
                return verified, True

        stored_metadata = dict(metadata) if metadata is not None else run_event_metadata(run_id)
        event_specs: list[tuple[str, dict[str, Any]]] = []
        for index, record in enumerate(records):
            payload: dict[str, Any] = {
                "run_id": run_id,
                "artifact": record,
                **stored_metadata,
            }
            if publication_id:
                payload.update({
                    "publication_id": publication_id,
                    "publication_digest": publication_digest,
                    "publication_index": index,
                    "publication_count": len(records),
                })
            event_specs.append(("artifact_created", payload))
        events = await append_durable_event_batch_locked(session_id, event_specs)
        if publication_id:
            sidecar_task = asyncio.create_task(asyncio.to_thread(
                store_publication_events,
                session_id,
                publication_id,
                events,
            ))
            try:
                await asyncio.shield(sidecar_task)
            except BaseException as exc:
                try:
                    await join_task_despite_caller_cancellation(sidecar_task)
                except BaseException as sidecar_exc:
                    exc = sidecar_exc
                # The event batch is already durable and authoritative. A CLI
                # retry explicitly asks the endpoint to recover it from the
                # bounded JSONL tail and repair this O(1) sidecar.
                logger.warning(
                    "publication receipt sidecar failed session=%s publication=%s: %s",
                    session_id,
                    publication_id,
                    concise_error_message(exc),
                )
        return events, False


async def _publish_artifact_entries_unlocked(
    session_id: str,
    run_id: str,
    entries: list[dict[str, Any]],
    *,
    publication_id: str | None = None,
    publication_digest: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if publication_id:
        existing = await asyncio.to_thread(
            load_publication_events,
            session_id,
            publication_id,
        )
        verified = await asyncio.to_thread(
            verified_publication_events,
            existing,
            run_id=run_id,
            publication_id=publication_id,
            publication_digest=str(publication_digest or ""),
            expected_count=len(entries),
        )
        if verified:
            return verified
    copy_task = asyncio.create_task(asyncio.to_thread(
        prepare_artifact_records,
        session_id,
        entries,
    ))
    try:
        records = await asyncio.shield(copy_task)
    except BaseException:
        completed_records: list[dict[str, Any]] = []
        try:
            completed_records = await join_task_despite_caller_cancellation(copy_task)
        except BaseException:
            pass
        if completed_records:
            await remove_artifact_records_async(completed_records)
        raise
    try:
        # Copying can be expensive, so stage it outside the chat lifecycle
        # lock. Publication itself shares deletion/archive ordering: either
        # this durable event batch commits first and retirement cleans it, or
        # retirement wins and the staged registry files are rolled back.
        async with session_lifecycle_lock(session_id):
            session = STORE.sessions.get(session_id)
            if not session:
                raise ArtifactPublicationError("chat no longer exists")
            if (
                session.get("_fork_initializing")
                or session_id in DELETING_SESSIONS
                or session_id in DELETED_SESSION_TOMBSTONES
            ):
                raise ArtifactPublicationError("chat is being deleted")
            if bool(session.get("archived")):
                raise ArtifactPublicationError("archived chats cannot publish artifacts")
            events, duplicate = await persist_artifact_records(
                session_id,
                run_id,
                records,
                publication_id=publication_id,
                publication_digest=publication_digest,
                metadata=metadata,
            )
    except BaseException:
        await remove_artifact_records_async(records)
        raise
    if duplicate:
        await remove_artifact_records_async(records)
    return events


async def publish_artifact_entries(
    session_id: str,
    run_id: str,
    entries: list[dict[str, Any]],
    *,
    publication_id: str | None = None,
    publication_digest: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if publication_id:
        async with artifact_publication_lock(session_id, publication_id):
            return await _publish_artifact_entries_unlocked(
                session_id,
                run_id,
                entries,
                publication_id=publication_id,
                publication_digest=publication_digest,
                metadata=metadata,
            )
    return await _publish_artifact_entries_unlocked(
        session_id,
        run_id,
        entries,
        metadata=metadata,
    )


async def append_manifest_artifact_error(
    session_id: str,
    run_id: str,
    manifest_path: Path,
    error: str,
) -> None:
    await append_durable_event_batch(session_id, [("artifact_error", {
        "run_id": run_id,
        "path": str(manifest_path),
        "error": error,
        **run_event_metadata(run_id),
    })])


async def collect_manifest(
    session_id: str,
    run_id: str,
    manifest_path: Path,
    *,
    seen_artifacts: set[str] | None = None,
    stable_files: dict[str, tuple[int, int]] | None = None,
    final: bool = True,
) -> None:
    if not manifest_path.exists():
        return
    entries, manifest_digest, validation_error = await asyncio.to_thread(
        read_artifact_manifest,
        manifest_path,
    )
    if manifest_digest is None:
        return
    if validation_error:
        if final:
            await append_manifest_artifact_error(
                session_id,
                run_id,
                manifest_path,
                validation_error,
            )
            await asyncio.to_thread(
                unlink_manifest_if_unchanged,
                manifest_path,
                manifest_digest,
            )
        return

    seen = seen_artifacts if seen_artifacts is not None else set()
    unseen = [entry for entry in entries if str(entry["path"]) not in seen]
    if not unseen:
        if final:
            await asyncio.to_thread(
                unlink_manifest_if_unchanged,
                manifest_path,
                manifest_digest,
            )
        return
    if not final and stable_files is not None:
        if not await asyncio.to_thread(
            live_manifest_batch_ready,
            unseen,
            stable_files,
        ):
            return

    try:
        publication_digest = await asyncio.to_thread(
            artifact_entries_digest,
            unseen,
        )
        legacy_publication_id = "legacy_" + hashlib.sha256(
            (
                f"{session_id}\0{run_id}\0{manifest_path}\0{manifest_digest}"
            ).encode("utf-8")
        ).hexdigest()
        publish_task = asyncio.create_task(publish_artifact_entries(
            session_id,
            run_id,
            unseen,
            publication_id=legacy_publication_id,
            publication_digest=publication_digest,
            metadata=run_event_metadata(run_id),
        ))
        cancelled_after_commit = False
        try:
            await asyncio.shield(publish_task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(publish_task)
            cancelled_after_commit = True
        published_paths = {str(entry["path"]) for entry in unseen}
        seen.update(published_paths)
        if stable_files is not None:
            for path in published_paths:
                stable_files.pop(path, None)
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        logger.warning(
            "artifact manifest publication failed session=%s run=%s path=%s: %s",
            session_id,
            run_id,
            manifest_path,
            concise_error_message(exc),
        )
        if final:
            await append_manifest_artifact_error(
                session_id,
                run_id,
                manifest_path,
                f"artifact publication failed: {concise_error_message(exc)}",
            )
            await asyncio.to_thread(
                unlink_manifest_if_unchanged,
                manifest_path,
                manifest_digest,
            )
        return
    if final:
        await asyncio.to_thread(
            unlink_manifest_if_unchanged,
            manifest_path,
            manifest_digest,
        )
    if cancelled_after_commit:
        raise asyncio.CancelledError


async def watch_manifest_artifacts(session_id: str, run_id: str, manifest_path: Path, seen_artifacts: set[str]) -> None:
    stable_files: dict[str, tuple[int, int]] = {}
    while True:
        try:
            await collect_manifest(
                session_id,
                run_id,
                manifest_path,
                seen_artifacts=seen_artifacts,
                stable_files=stable_files,
                final=False,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning(
                "artifact manifest watcher recovered session=%s run=%s path=%s: %s",
                session_id,
                run_id,
                manifest_path,
                concise_error_message(exc),
            )
        await asyncio.sleep(1.0)


async def collect_recent_leftover_manifests(
    session_id: str,
    run_id: str,
    primary_manifest_path: Path,
    *,
    seen_artifacts: set[str],
    max_age_seconds: int = 6 * 60 * 60,
) -> None:
    """Recover artifacts when an agent writes to a stale run manifest path."""
    root = manifests_dir(session_id)
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    try:
        candidates = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    for candidate in candidates:
        if candidate == primary_manifest_path:
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        logger.info(
            "collecting leftover manifest session=%s run=%s path=%s",
            session_id,
            run_id,
            candidate,
        )
        await collect_manifest(session_id, run_id, candidate, seen_artifacts=seen_artifacts, final=True)


async def commit_staged_upload(
    session_id: str,
    staging_root: Path,
    final_root: Path,
    metadata: dict[str, Any],
) -> None:
    """Atomically expose file+metadata and commit their owning event."""

    async with session_lifecycle_lock(session_id):
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        ensure_session_not_deleting(session_id)
        if bool(session.get("archived")):
            raise HTTPException(status_code=409, detail="archived chats cannot accept uploads")
        await run_upload_worker(staging_root.replace, final_root)
        try:
            await append_durable_event(
                session_id,
                "file_uploaded",
                {"file": metadata},
            )
        except BaseException:
            with suppress(BaseException):
                await remove_upload_tree(final_root)
            raise
