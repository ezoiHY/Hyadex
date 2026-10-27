"""Verified artifact publication events."""
from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

from hyadex.core.paths import artifact_publications_dir, events_path
from hyadex.core.session_files import read_file_meta_record


class ArtifactPublicationError(RuntimeError):
    pass


def publication_receipt_path(session_id: str, publication_id: str) -> Path:
    filename = hashlib.sha256(publication_id.encode("utf-8")).hexdigest() + ".json"
    return artifact_publications_dir(session_id) / filename


def load_publication_events(
    session_id: str,
    publication_id: str,
) -> list[dict[str, Any]]:
    path = publication_receipt_path(session_id, publication_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactPublicationError(
            "the durable publication receipt is unreadable"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("publication_id") != publication_id
        or not isinstance(data.get("events"), list)
        or not data.get("events")
    ):
        raise ArtifactPublicationError("the durable publication receipt is invalid")
    return list(data["events"])


def recover_publication_events_from_tail(
    session_id: str,
    publication_id: str,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Rare recovery path for a lost response before its sidecar was written."""
    path = events_path(session_id)
    if not path.is_file():
        return []
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        start = max(0, size - max_bytes)
        stream.seek(start)
        raw = stream.read()
    if start:
        _, _, raw = raw.partition(b"\n")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        with suppress(Exception):
            event = json.loads(line.decode("utf-8", errors="replace"))
            if (
                event.get("type") == "artifact_created"
                and event.get("publication_id") == publication_id
            ):
                events.append(event)
    return events


def store_publication_events(
    session_id: str,
    publication_id: str,
    events: list[dict[str, Any]],
) -> None:
    directory = artifact_publications_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = publication_receipt_path(session_id, publication_id)
    temp = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = {
        "version": 1,
        "session_id": session_id,
        "publication_id": publication_id,
        "events": events,
    }
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(OSError):
            temp.unlink()


def verified_publication_events(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    publication_id: str,
    publication_digest: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    if not events:
        return []
    by_index: dict[int, dict[str, Any]] = {}
    for event in events:
        if (
            event.get("publication_id") != publication_id
            or event.get("publication_digest") != publication_digest
            or event.get("run_id") != run_id
            or event.get("publication_count") != expected_count
        ):
            raise ArtifactPublicationError(
                "publication_id is already bound to another artifact batch or run"
            )
        index = event.get("publication_index")
        artifact = event.get("artifact")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= expected_count
            or index in by_index
            or not isinstance(artifact, dict)
            or not str(artifact.get("id") or "").strip()
            or not isinstance(event.get("seq"), int)
            or int(event["seq"]) <= 0
        ):
            raise ArtifactPublicationError(
                "publication_id has an incomplete or invalid durable receipt"
            )
        artifact_id = str(artifact["id"])
        metadata = read_file_meta_record(artifact_id)
        artifact_path = Path(str(artifact.get("path") or ""))
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("session_id") or "")
            != str(event.get("session_id") or "")
            or str(metadata.get("path") or "") != str(artifact_path)
            or not artifact_path.is_file()
        ):
            raise ArtifactPublicationError(
                "publication_id references an unavailable artifact"
            )
        by_index[index] = event
    if set(by_index) != set(range(expected_count)):
        raise ArtifactPublicationError(
            "publication_id has an incomplete durable receipt"
        )
    return [by_index[index] for index in range(expected_count)]
