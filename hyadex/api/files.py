"""Chat files: upload, list, linked files and downloads."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
from typing import Any
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from hyadex.core.config import FILES_ROOT
from hyadex.core.http import router
from hyadex.core.paths import now_iso, safe_name
from hyadex.core.queue import commit_staged_upload
from hyadex.core.sessions import (
    effective_content_type,
    ensure_session_not_deleting,
    list_session_file_records,
    STORE,
)
from hyadex.core.util import ensure_dirs
from hyadex.features.files import (
    remove_upload_tree,
    require_session_file_meta,
    run_upload_worker,
    session_file_for_link,
    stage_upload_file_sync,
    write_upload_metadata_sync,
)
from hyadex.features.usage import join_task_despite_caller_cancellation

router = APIRouter()


def file_response_media_type(meta: dict[str, Any]) -> str:
    filename = str(meta.get("filename") or Path(str(meta.get("path") or "")).name)
    return effective_content_type(filename, str(meta.get("content_type") or ""))


@router.post("/api/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_deleting(session_id)
    if bool(session.get("archived")):
        raise HTTPException(status_code=409, detail="archived chats cannot accept uploads")

    ensure_dirs()
    file_id = f"file_{uuid.uuid4().hex[:16]}"
    filename = safe_name(file.filename or "upload")
    final_root = FILES_ROOT / file_id
    staging_root = Path(tempfile.mkdtemp(
        prefix=f".{file_id}-upload-",
        dir=str(FILES_ROOT),
    ))
    staged_file = staging_root / filename
    copy_task = asyncio.create_task(asyncio.to_thread(
        stage_upload_file_sync,
        file.file,
        staged_file,
    ))
    try:
        size = await asyncio.shield(copy_task)
    except BaseException:
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(copy_task)
        with suppress(BaseException):
            await remove_upload_tree(staging_root)
        raise

    dest = final_root / filename
    meta = {
        "id": file_id,
        "session_id": session_id,
        "kind": "upload",
        "filename": filename,
        "path": str(dest),
        "size": size,
        "content_type": effective_content_type(filename, file.content_type),
        "created_at": now_iso(),
    }
    try:
        await run_upload_worker(
            write_upload_metadata_sync,
            staging_root / "meta.json",
            meta,
        )
        commit_task = asyncio.create_task(commit_staged_upload(
            session_id,
            staging_root,
            final_root,
            meta,
        ))
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(commit_task)
            raise
        return {"file": meta}
    finally:
        await remove_upload_tree(staging_root)


@router.get("/api/sessions/{session_id}/files")
async def list_session_files(
    session_id: str,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    content_prefix: str | None = Query(default=None),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    records = await asyncio.to_thread(list_session_file_records, session_id)
    if content_prefix:
        prefix = content_prefix.strip().lower()
        records = [
            rec for rec in records
            if str(rec.get("content_type") or "").lower().startswith(prefix)
        ]
    total = len(records)
    if limit is None:
        return {
            "files": records,
            "total": total,
            "offset": 0,
            "limit": total,
            "has_more": False,
        }
    page = records[offset:offset + limit]
    return {
        "files": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }


@router.get("/api/sessions/{session_id}/links/file")
@router.head("/api/sessions/{session_id}/links/file")
async def get_session_linked_file(session_id: str, target: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await asyncio.to_thread(session_file_for_link, session_id, target)
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )


@router.get("/api/sessions/{session_id}/files/{file_id}")
@router.head("/api/sessions/{session_id}/files/{file_id}")
async def get_session_file(session_id: str, file_id: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await asyncio.to_thread(
        require_session_file_meta,
        session_id,
        file_id,
    )
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )
