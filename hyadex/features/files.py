"""Uploads, artifacts, file records, manifests and per-turn code diffs."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.core.config import (
    CODE_DIFF_DISCOVERY_MAX_BYTES,
    CODE_DIFF_INDEX_MAX_BYTES,
    CODE_DIFF_NUMSTAT_MAX_BYTES,
    CODE_DIFF_PATCH_MAX_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_FILE_BYTES,
    CODE_DIFF_SNAPSHOT_MAX_PATHS,
    CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS,
    FILES_ROOT,
    MAX_ARTIFACT_PUBLISH_FILES,
    MAX_ARTIFACT_TEXT_CHARS,
    MAX_ARTIFACT_TITLE_CHARS,
    MAX_UPLOAD_BYTES,
    MAX_WORKSPACE_PATH_CHARS,
)
from hyadex.core.event_log import _snapshot_object_dir_is_owned, legacy_session_event_file_ids
from hyadex.core.publications import ArtifactPublicationError
from hyadex.core.paths import code_diffs_dir, now_iso, safe_name
from hyadex.core.runs import RUNS
from hyadex.core.sessions import append_event
from hyadex.core.session_files import (
    guess_content_type,
    list_session_file_records,
    read_file_meta_record,
)
from hyadex.core.util import ensure_dirs, merged_file_ids, normalized_file_id
from hyadex.features.history import copy_fork_file_data
from hyadex.features.usage import join_task_despite_caller_cancellation

logger = logging.getLogger("hyadex-server")


def _git_command(
    repo_root: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    stdout: Any = subprocess.PIPE,
    deadline: float,
) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=0)
    return subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.quotePath=false",
            "-C",
            repo_root,
            *args,
        ],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        # Git diagnostics are not user-facing here. Never let a hook, filter,
        # or malformed repository fill an unbounded stderr pipe while this
        # best-effort snapshot blocks provider admission.
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=remaining,
        check=False,
    )


def _code_diff_git_env() -> dict[str, str]:
    """Return a Git environment detached from an invoking shell's repository."""

    env = dict(os.environ)
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_GLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
        "GIT_INDEX_FILE",
        "GIT_LITERAL_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        env.pop(name, None)
    # Every pathspec used by the snapshot pipeline comes from an exact Git
    # filename or a provider-attributed filename. NUL framing prevents record
    # ambiguity, but Git still interprets wildcard and ``:(...)`` magic unless
    # literal mode is explicit. Pin it here so filenames cannot widen the
    # published agent-attributed diff and inherited shell settings cannot
    # alter that boundary.
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def _git_stream_stdout(
    repo_root: str,
    args: list[str],
    *,
    env: dict[str, str],
    max_bytes: int,
    output: Any | None = None,
    deadline: float,
) -> tuple[int, bytes, bool, bool]:
    """Run Git while retaining/writing no more than ``max_bytes`` of stdout.

    ``subprocess.run(capture_output=True)`` is deliberately not used here:
    repository-sized discovery or diff output must never be accumulated before
    its limit is checked. A joined timer bounds a child that stops producing
    output while the blocking reader waits.
    """

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return -1, b"", False, True
    process = subprocess.Popen(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.quotePath=false",
            "-C",
            repo_root,
            *args,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        with suppress(OSError):
            process.kill()

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    collected = bytearray()
    exceeded = False
    total = 0
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                exceeded = True
                with suppress(OSError):
                    process.kill()
                break
            if output is None:
                collected.extend(chunk)
            else:
                output.write(chunk)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        if process.poll() is None:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        timer.cancel()
        timer.join(timeout=1)
    return (
        int(process.returncode if process.returncode is not None else -1),
        bytes(collected),
        exceeded,
        timed_out.is_set(),
    )


def _discover_git_snapshot_paths(
    repo_root: str,
    env: dict[str, str],
    deadline: float,
) -> list[bytes] | None:
    """Return a bounded exact NUL-path list for unstaged and untracked work."""

    paths: dict[bytes, None] = {}
    for args in (
        ["diff", "--no-ext-diff", "--name-only", "-z", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
    ):
        returncode, payload, exceeded, timed_out = _git_stream_stdout(
            repo_root,
            args,
            env=env,
            max_bytes=CODE_DIFF_DISCOVERY_MAX_BYTES,
            deadline=deadline,
        )
        if (
            returncode != 0
            or exceeded
            or timed_out
            or (payload and not payload.endswith(b"\0"))
        ):
            return None
        for raw_path in payload.split(b"\0")[:-1]:
            if not raw_path:
                continue
            paths.setdefault(raw_path, None)
            if len(paths) > CODE_DIFF_SNAPSHOT_MAX_PATHS:
                return None

    aggregate_bytes = 0
    root_bytes = os.fsencode(repo_root)
    for raw_path in paths:
        if time.monotonic() >= deadline:
            return None
        decoded = os.fsdecode(raw_path)
        if (
            os.path.isabs(decoded)
            or any(part == ".." for part in decoded.replace("\\", "/").split("/"))
            or len(raw_path) > MAX_WORKSPACE_PATH_CHARS * 4
        ):
            return None
        try:
            metadata = os.lstat(os.path.join(root_bytes, raw_path))
        except FileNotFoundError:
            # A tracked deletion is a zero-byte snapshot input.
            continue
        except OSError:
            return None
        size = max(0, int(metadata.st_size))
        if size > CODE_DIFF_SNAPSHOT_MAX_FILE_BYTES:
            return None
        aggregate_bytes += size
        if aggregate_bytes > CODE_DIFF_SNAPSHOT_MAX_BYTES:
            return None
    return list(paths)


def _open_private_exclusive(path: Path, mode: str) -> Any:
    flags = os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if "b" in mode:
            return os.fdopen(descriptor, mode)
        return os.fdopen(descriptor, mode, encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        with suppress(OSError):
            path.unlink()
        raise


def _copy_private_bounded_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
    deadline: float,
) -> None:
    """Copy one regular file with exact private mode, size, and time bounds."""

    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ValueError("Git index exceeds the code-diff limit")
        with _open_private_exclusive(destination, "wb") as output:
            copied = 0
            while True:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(
                        cmd="copy Git index",
                        timeout=CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS,
                    )
                chunk = os.read(source_descriptor, min(1024 * 1024, max_bytes + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise ValueError("Git index exceeds the code-diff limit")
                output.write(chunk)
            output.flush()
    except BaseException:
        with suppress(OSError):
            destination.unlink()
        raise
    finally:
        os.close(source_descriptor)


def _write_nul_pathspec(
    path: Path,
    paths: Iterable[str | bytes],
    *,
    deadline: float | None = None,
) -> int:
    total = 0
    count = 0
    with _open_private_exclusive(path, "wb") as stream:
        for raw_path in paths:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd="write Git pathspec",
                    timeout=CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS,
                )
            encoded = raw_path if isinstance(raw_path, bytes) else os.fsencode(raw_path)
            if not encoded or b"\0" in encoded:
                raise ValueError("invalid Git path")
            total += len(encoded) + 1
            count += 1
            if (
                count > CODE_DIFF_SNAPSHOT_MAX_PATHS
                or total > CODE_DIFF_DISCOVERY_MAX_BYTES
            ):
                raise ValueError("Git path list exceeds the code-diff limit")
            stream.write(encoded)
            stream.write(b"\0")
    return count


def discard_git_baseline(session_id: str, baseline: dict[str, Any] | None) -> None:
    """Idempotently remove only the snapshot object directory we created."""

    if not isinstance(baseline, dict):
        return
    raw_path = str(baseline.get("object_dir") or "")
    baseline["object_dir"] = ""
    if not raw_path:
        return
    path = Path(raw_path)
    if not _snapshot_object_dir_is_owned(session_id, path):
        logger.warning(
            "refusing to remove unowned code diff object directory session=%s path=%s",
            session_id,
            raw_path,
        )
        return
    shutil.rmtree(path, ignore_errors=True)


def _capture_git_tree(
    session_id: str,
    run_id: str,
    cwd: str,
    *,
    object_dir: str | None = None,
    deadline: float | None = None,
) -> dict[str, str] | None:
    """Snapshot bounded worktree state without writing into the real object DB."""

    base_env = _code_diff_git_env()
    if deadline is None:
        deadline = time.monotonic() + CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS
    try:
        probe_returncode, probe_payload, probe_exceeded, probe_timed_out = (
            _git_stream_stdout(
                cwd,
                ["rev-parse", "--show-toplevel"],
                env=base_env,
                max_bytes=MAX_WORKSPACE_PATH_CHARS * 4 + 1024,
                deadline=deadline,
            )
        )
    except OSError:
        return None
    if probe_returncode != 0 or probe_exceeded or probe_timed_out:
        return None
    repo_root = os.fsdecode(probe_payload.rstrip(b"\r\n"))
    if not repo_root:
        return None

    ensure_dirs(session_id)
    diff_root = code_diffs_dir(session_id)
    created_object_dir = object_dir is None
    if created_object_dir:
        snapshot_object_dir = Path(tempfile.mkdtemp(
            prefix=f".snapshot-{safe_name(run_id)[:64]}-",
            dir=str(diff_root),
        ))
        with suppress(OSError):
            snapshot_object_dir.chmod(0o700)
    else:
        snapshot_object_dir = Path(str(object_dir))
        if (
            not _snapshot_object_dir_is_owned(session_id, snapshot_object_dir)
            or not snapshot_object_dir.is_dir()
        ):
            return None
    index_path = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.index"
    pathspec_path = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.paths"
    env = dict(base_env)
    env["GIT_INDEX_FILE"] = str(index_path)
    env["GIT_OBJECT_DIRECTORY"] = str(snapshot_object_dir)
    captured = False
    try:
        object_probe = _git_command(
            repo_root,
            ["rev-parse", "--git-path", "objects"],
            env=base_env,
            deadline=deadline,
        )
        object_path = str(object_probe.stdout or "").strip()
        if object_probe.returncode != 0 or not object_path:
            return None
        real_object_dir = Path(object_path)
        if not real_object_dir.is_absolute():
            real_object_dir = Path(repo_root) / real_object_dir
        if not real_object_dir.is_dir():
            return None
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(real_object_dir.resolve())

        index_probe = _git_command(
            repo_root,
            ["rev-parse", "--git-path", "index"],
            env=base_env,
            deadline=deadline,
        )
        index_path_value = str(index_probe.stdout or "").strip()
        source_index = Path(index_path_value) if index_path_value else None
        if source_index is not None and not source_index.is_absolute():
            source_index = Path(repo_root) / source_index
        if (
            index_probe.returncode == 0
            and source_index is not None
            and source_index.is_file()
        ):
            _copy_private_bounded_file(
                source_index,
                index_path,
                max_bytes=CODE_DIFF_INDEX_MAX_BYTES,
                deadline=deadline,
            )
        else:
            head = _git_command(
                repo_root,
                ["rev-parse", "--verify", "HEAD"],
                env=base_env,
                deadline=deadline,
            )
            read_args = ["read-tree", head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else ["read-tree", "--empty"]
            if _git_command(
                repo_root,
                read_args,
                env=env,
                deadline=deadline,
            ).returncode != 0:
                return None
            with suppress(OSError):
                index_path.chmod(0o600)

        snapshot_paths = _discover_git_snapshot_paths(
            repo_root,
            base_env,
            deadline,
        )
        if snapshot_paths is None:
            logger.info(
                "code diff snapshot skipped session=%s run=%s: worktree exceeds a discovery, path, or byte limit",
                session_id,
                run_id,
            )
            return None
        if snapshot_paths:
            _write_nul_pathspec(
                pathspec_path,
                snapshot_paths,
                deadline=deadline,
            )
            staged = _git_command(
                repo_root,
                [
                    "add",
                    "-A",
                    f"--pathspec-from-file={pathspec_path}",
                    "--pathspec-file-nul",
                ],
                env=env,
                deadline=deadline,
            )
            if staged.returncode != 0:
                logger.info(
                    "code diff snapshot skipped session=%s run=%s: git add failed",
                    session_id,
                    run_id,
                )
                return None
            with suppress(OSError):
                index_path.chmod(0o600)
        tree = _git_command(
            repo_root,
            ["write-tree"],
            env=env,
            deadline=deadline,
        )
        tree_id = tree.stdout.strip()
        if tree.returncode != 0 or not tree_id:
            return None
        captured = True
        return {
            "repo_root": repo_root,
            "tree": tree_id,
            "object_dir": str(snapshot_object_dir),
            "alternate_object_dir": str(real_object_dir.resolve()),
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.info("code diff snapshot failed session=%s run=%s error=%s", session_id, run_id, exc)
        return None
    finally:
        with suppress(OSError):
            index_path.unlink()
        with suppress(OSError):
            Path(f"{index_path}.lock").unlink()
        with suppress(OSError):
            pathspec_path.unlink()
        if created_object_dir and not captured:
            discard_git_baseline(
                session_id,
                {"object_dir": str(snapshot_object_dir)},
            )


def _parse_git_numstat(source: bytes) -> list[dict[str, Any]]:
    """Parse ``git diff --numstat -z`` without treating newlines as records."""

    files: list[dict[str, Any]] = []
    records = source.split(b"\0")
    if records and not records[-1]:
        records.pop()
    index = 0
    while index < len(records):
        raw_record = records[index]
        index += 1
        parts = raw_record.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, raw_path = parts
        old_path: str | None = None
        if not raw_path:
            if index + 1 >= len(records):
                break
            old_path = os.fsdecode(records[index])
            raw_path = records[index + 1]
            index += 2
        path = os.fsdecode(raw_path)
        added = int(added_raw) if added_raw.isdigit() else None
        deleted = int(deleted_raw) if deleted_raw.isdigit() else None
        record: dict[str, Any] = {
            "path": path,
            "additions": added,
            "deletions": deleted,
            "binary": added is None or deleted is None,
        }
        if old_path is not None:
            record["old_path"] = old_path
        files.append(record)
    return files


def _normalize_changed_paths(
    paths: Iterable[str],
    repo_root: str,
    cwd: str,
    *,
    deadline: float | None = None,
) -> list[str]:
    root = Path(repo_root).expanduser().resolve()
    working = Path(cwd).expanduser().resolve()
    normalized: set[str] = set()
    for raw_index, raw in enumerate(paths):
        if raw_index >= CODE_DIFF_SNAPSHOT_MAX_PATHS:
            return []
        if deadline is not None and time.monotonic() >= deadline:
            return []
        clean = str(raw or "")
        if (
            len(clean) >= 2
            and clean[0] == clean[-1]
            and clean[0] in {"\"", "'", "`"}
        ):
            clean = clean[1:-1]
        if not clean or clean == "/dev/null":
            continue
        try:
            candidate = Path(clean).expanduser()
        except (OSError, RuntimeError, ValueError):
            # Provider tool paths are untrusted attribution hints. An unknown
            # ``~user`` (or another host-specific expansion failure) must not
            # escape this best-effort finalizer and replace a successful turn
            # with supervisor recovery.
            continue
        candidates = [candidate] if candidate.is_absolute() else [working / candidate, root / candidate]
        for unresolved in candidates:
            try:
                # Resolve every parent component so equivalent filesystem
                # aliases (notably macOS /var -> /private/var) compare against
                # Git's canonical repository root and a symlinked directory
                # cannot escape it. Do not resolve the final component: an
                # agent can legitimately replace a Git-tracked symlink and
                # attribution must retain the link itself.
                absolute = Path(os.path.abspath(os.fspath(unresolved)))
                if not absolute.name or absolute.name in {".", ".."}:
                    continue
                resolved = absolute.parent.resolve() / absolute.name
                relative = resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if relative.parts and ".." not in relative.parts:
                normalized.add(relative.as_posix())
                break
        if len(normalized) > CODE_DIFF_SNAPSHOT_MAX_PATHS:
            return []
    return sorted(normalized)


def _write_turn_code_diff(
    session_id: str,
    run_id: str,
    backend: str,
    baseline: dict[str, Any],
    cwd: str,
    changed_paths: set[str],
) -> dict[str, Any] | None:
    # Finalization is best effort and runs on the provider completion path.
    # Share one wall-clock budget across the worktree capture, filtering,
    # patch, and numstat commands so a slow repository cannot multiply the
    # configured timeout by the number of Git subprocesses.
    deadline = time.monotonic() + CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS
    diff_root = code_diffs_dir(session_id)
    patch_path = diff_root / f"{safe_name(run_id)}.patch"
    patch_tmp = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.patch"
    metadata_tmp = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.json"
    filter_index = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.filter-index"
    filter_pathspec = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.filter-paths"
    try:
        current = _capture_git_tree(
            session_id,
            run_id,
            cwd,
            object_dir=str(baseline.get("object_dir") or ""),
            deadline=deadline,
        )
        if not current or current.get("repo_root") != baseline.get("repo_root"):
            return None
        base_tree = str(baseline.get("tree") or "")
        current_tree = str(current.get("tree") or "")
        if not base_tree or not current_tree or base_tree == current_tree:
            return None

        repo_root = str(current["repo_root"])
        attributed_paths = _normalize_changed_paths(
            changed_paths,
            repo_root,
            cwd,
            deadline=deadline,
        )
        if not attributed_paths:
            logger.info(
                "code diff skipped session=%s run=%s: no agent-owned edit paths",
                session_id,
                run_id,
            )
            return None

        _write_nul_pathspec(
            filter_pathspec,
            attributed_paths,
            deadline=deadline,
        )
        git_env = _code_diff_git_env()
        git_env["GIT_INDEX_FILE"] = str(filter_index)
        git_env["GIT_OBJECT_DIRECTORY"] = str(current["object_dir"])
        git_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(
            current["alternate_object_dir"]
        )
        if _git_command(
            repo_root,
            ["read-tree", base_tree],
            env=git_env,
            deadline=deadline,
        ).returncode != 0:
            return None
        with suppress(OSError):
            filter_index.chmod(0o600)
        if _git_command(
            repo_root,
            [
                "reset",
                "-q",
                current_tree,
                f"--pathspec-from-file={filter_pathspec}",
                "--pathspec-file-nul",
            ],
            env=git_env,
            deadline=deadline,
        ).returncode != 0:
            return None
        with suppress(OSError):
            filter_index.chmod(0o600)
        filtered = _git_command(
            repo_root,
            ["write-tree"],
            env=git_env,
            deadline=deadline,
        )
        attributed_tree = filtered.stdout.strip()
        if filtered.returncode != 0 or not attributed_tree or attributed_tree == base_tree:
            return None

        with _open_private_exclusive(patch_tmp, "wb") as output:
            patch_returncode, _unused, patch_exceeded, patch_timed_out = (
                _git_stream_stdout(
                    repo_root,
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--find-renames",
                        "--unified=3",
                        base_tree,
                        attributed_tree,
                    ],
                    env=git_env,
                    max_bytes=CODE_DIFF_PATCH_MAX_BYTES,
                    output=output,
                    deadline=deadline,
                )
            )
        if (
            patch_returncode != 0
            or patch_exceeded
            or patch_timed_out
            or patch_tmp.stat().st_size == 0
        ):
            if patch_exceeded:
                logger.info(
                    "code diff patch skipped session=%s run=%s: output exceeds %s bytes",
                    session_id,
                    run_id,
                    CODE_DIFF_PATCH_MAX_BYTES,
                )
            return None

        (
            numstat_returncode,
            numstat_payload,
            numstat_exceeded,
            numstat_timed_out,
        ) = _git_stream_stdout(
            repo_root,
            ["diff", "--numstat", "-z", "--find-renames", base_tree, attributed_tree],
            env=git_env,
            max_bytes=CODE_DIFF_NUMSTAT_MAX_BYTES,
            deadline=deadline,
        )
        if numstat_returncode != 0 or numstat_exceeded or numstat_timed_out:
            return None
        files = _parse_git_numstat(numstat_payload)
        additions = sum(int(item["additions"] or 0) for item in files)
        deletions = sum(int(item["deletions"] or 0) for item in files)
        metadata = {
            "run_id": run_id,
            "backend": backend,
            "repository_root": repo_root,
            "diff_files": files,
            "files_changed": len(files),
            "additions": additions,
            "deletions": deletions,
            "byte_count": patch_tmp.stat().st_size,
            "created_at": now_iso(),
            "attribution": "agent_tool_paths",
            "attributed_paths": attributed_paths,
        }
        metadata_path = diff_root / f"{safe_name(run_id)}.json"
        with _open_private_exclusive(metadata_tmp, "w") as output:
            json.dump(metadata, output, indent=2)
            output.write("\n")
            output.flush()
        patch_tmp.replace(patch_path)
        metadata_tmp.replace(metadata_path)
        return metadata
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("code diff capture failed session=%s run=%s error=%s", session_id, run_id, exc)
        return None
    finally:
        for temporary in (
            patch_tmp,
            metadata_tmp,
            filter_index,
            Path(f"{filter_index}.lock"),
            filter_pathspec,
        ):
            with suppress(OSError):
                temporary.unlink()
        discard_git_baseline(session_id, baseline)


GIT_BASELINES_BY_TASK: dict[
    asyncio.Task[Any],
    list[tuple[str, dict[str, Any]]],
] = {}


async def capture_git_baseline(
    session_id: str,
    run_id: str,
    cwd: str,
) -> dict[str, Any] | None:
    """Capture and transfer exact snapshot ownership to the calling task."""

    capture_task = asyncio.create_task(asyncio.to_thread(
        _capture_git_tree,
        session_id,
        run_id,
        cwd,
    ))
    try:
        baseline = await asyncio.shield(capture_task)
    except BaseException:
        baseline: dict[str, Any] | None = None
        try:
            baseline = await join_task_despite_caller_cancellation(capture_task)
        except BaseException:
            pass
        if baseline:
            cleanup_task = asyncio.create_task(asyncio.to_thread(
                discard_git_baseline,
                session_id,
                baseline,
            ))
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
        raise
    owner = asyncio.current_task()
    if baseline and owner is not None:
        GIT_BASELINES_BY_TASK.setdefault(owner, []).append((session_id, baseline))
    return baseline


async def discard_owned_git_baselines(
    owner: asyncio.Task[Any] | None = None,
) -> None:
    """Release every baseline adopted by one provider-runner task."""

    selected = owner or asyncio.current_task()
    if selected is None:
        return
    baselines = GIT_BASELINES_BY_TASK.pop(selected, [])
    if baselines:
        await asyncio.to_thread(
            lambda: [
                discard_git_baseline(session_id, baseline)
                for session_id, baseline in baselines
            ]
        )


async def publish_turn_code_diff(
    session_id: str,
    run_id: str,
    backend: str,
    cwd: str,
    baseline: dict[str, Any] | None,
    changed_paths: set[str],
) -> None:
    if not baseline:
        return

    async def finish_publication() -> None:
        metadata = await asyncio.to_thread(
            _write_turn_code_diff,
            session_id,
            run_id,
            backend,
            baseline,
            cwd,
            changed_paths,
        )
        if metadata:
            await append_event(session_id, "code_diff", metadata)

    publication_task = asyncio.create_task(finish_publication())
    try:
        await asyncio.shield(publication_task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(publication_task)
        raise


ARTIFACT_PUBLICATION_LOCK_STRIPES = tuple(asyncio.Lock() for _ in range(64))


def artifact_publication_lock(
    session_id: str,
    publication_id: str,
) -> asyncio.Lock:
    digest = hashlib.sha256(
        f"{session_id}\0{publication_id}".encode("utf-8")
    ).digest()
    return ARTIFACT_PUBLICATION_LOCK_STRIPES[
        int.from_bytes(digest[:2], "big") % len(ARTIFACT_PUBLICATION_LOCK_STRIPES)
    ]


class PublishArtifactsRequest(BaseModel):
    publication_id: str = Field(min_length=1, max_length=128)
    files: list[Any] = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_PUBLISH_FILES,
    )


def run_event_metadata(run_id: str) -> dict[str, Any]:
    metadata = RUNS.run_metadata.get(run_id) or {}
    return {key: value for key, value in metadata.items() if value is not None}


def file_attachment_prompt_lines(
    session_id: str,
    file_ids: list[str] | tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    for file_id in validate_session_file_ids(session_id, file_ids, reject_invalid=False):
        with suppress(Exception):
            rec = load_file_meta(file_id)
            lines.append(
                f"- {rec.get('path')} ({rec.get('filename')}, {rec.get('content_type')})"
            )
    return lines


def legacy_file_ids_for_paths(paths: list[str]) -> list[str]:
    wanted = {str(path or "").strip() for path in paths if str(path or "").strip()}
    if not wanted or not FILES_ROOT.exists():
        return []
    found: list[str] = []
    for meta_path in FILES_ROOT.glob("*/meta.json"):
        with suppress(Exception):
            record = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(record.get("path") or "").strip() in wanted:
                found.append(meta_path.parent.name)
    return merged_file_ids(found)


def legacy_attachment_paths(lines: str) -> list[str]:
    paths: list[str] = []
    for line in lines.splitlines():
        if not line.startswith("- "):
            continue
        value = line[2:].strip()
        path, separator, _metadata = value.rpartition(" (")
        paths.append(path if separator else value)
    return paths


def split_legacy_scoped_attachments(
    text: str,
    heading: str,
    *,
    recover_file_ids: bool = True,
) -> tuple[str, list[str]]:
    marker = f"\n\n[{heading}]\n"
    end_marker = f"\n[End {heading.lower()}]"
    marker_index = text.rfind(marker)
    if marker_index < 0 or not text.endswith(end_marker):
        return text, []
    attachment_text = text[marker_index + len(marker):-len(end_marker)]
    file_ids = (
        legacy_file_ids_for_paths(legacy_attachment_paths(attachment_text))
        if recover_file_ids
        else []
    )
    return text[:marker_index], file_ids


def split_legacy_attached_files(
    text: str,
    *,
    recover_file_ids: bool = True,
) -> tuple[str, list[str]]:
    marker = "\n\n[Attached files]\n"
    footer = "\nUse these local paths directly when needed."
    marker_index = text.rfind(marker)
    normalized = text[:-1] if text.endswith("\n") else text
    if marker_index < 0 or not normalized.endswith(footer):
        return text, []
    attachment_text = normalized[marker_index + len(marker):-len(footer)]
    file_ids = (
        legacy_file_ids_for_paths(legacy_attachment_paths(attachment_text))
        if recover_file_ids
        else []
    )
    return text[:marker_index], file_ids


def validate_session_file_ids(
    session_id: str,
    file_ids: list[str] | tuple[str, ...],
    *,
    reject_invalid: bool = True,
) -> list[str]:
    """Resolve file IDs only within ``session_id``.

    Modern metadata has an explicit owner. Ownerless legacy metadata is
    accepted only when that chat's own upload/artifact event establishes its
    origin. Missing and foreign IDs share the same 404 to avoid leaking file
    existence across chats.
    """
    valid: list[str] = []
    legacy_membership: set[str] | None = None
    for raw_file_id in merged_file_ids(file_ids):
        file_id = normalized_file_id(raw_file_id)
        record = read_file_meta_record(file_id) if file_id else None
        owner = str((record or {}).get("session_id") or "").strip()
        belongs = bool(file_id and owner == session_id)
        if file_id and not owner:
            if legacy_membership is None:
                legacy_membership = legacy_session_event_file_ids(session_id)
            belongs = file_id in legacy_membership
        if belongs:
            valid.append(file_id)
            continue
        if reject_invalid:
            raise HTTPException(status_code=404, detail="file not found in session")
    return valid


def require_session_file_meta(session_id: str, file_id: str) -> dict[str, Any]:
    clean_file_id = normalized_file_id(file_id)
    validated = validate_session_file_ids(session_id, [clean_file_id])
    if not validated:
        raise HTTPException(status_code=404, detail="file not found in session")
    return load_file_meta(validated[0])


def artifact_publication_id(value: str) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", clean):
        raise ArtifactPublicationError(
            "publication_id must contain only letters, numbers, '.', '_', ':', or '-'"
        )
    return clean


def normalize_artifact_entry_shapes(
    entries: Any,
    *,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Validate request fields without touching potentially remote files."""
    if not isinstance(entries, list):
        raise ArtifactPublicationError("files must be an array")
    if not entries and not allow_empty:
        raise ArtifactPublicationError("files must contain at least one entry")
    if len(entries) > MAX_ARTIFACT_PUBLISH_FILES:
        raise ArtifactPublicationError(
            f"files must contain at most {MAX_ARTIFACT_PUBLISH_FILES} entries"
        )

    normalized: list[dict[str, Any]] = []
    requested_paths: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"files[{index}]"
        if isinstance(entry, str):
            raw_path = entry
            title = None
            text = None
        elif isinstance(entry, dict):
            unknown = set(entry) - {"path", "title", "text"}
            if unknown:
                raise ArtifactPublicationError(
                    f"{label} contains unsupported keys: {', '.join(sorted(unknown))}"
                )
            raw_path = entry.get("path")
            title = entry.get("title")
            text = entry.get("text")
        else:
            raise ArtifactPublicationError(f"{label} must be a path string or object")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ArtifactPublicationError(f"{label}.path must be a non-empty string")
        if "\x00" in raw_path or len(raw_path) > MAX_WORKSPACE_PATH_CHARS:
            raise ArtifactPublicationError(f"{label}.path is invalid or too long")
        source = Path(raw_path)
        if not source.is_absolute():
            raise ArtifactPublicationError(f"{label}.path must be absolute")

        if title is not None and not isinstance(title, str):
            raise ArtifactPublicationError(f"{label}.title must be a string or null")
        if text is not None and not isinstance(text, str):
            raise ArtifactPublicationError(f"{label}.text must be a string or null")
        if isinstance(title, str) and (
            "\x00" in title or len(title) > MAX_ARTIFACT_TITLE_CHARS
        ):
            raise ArtifactPublicationError(f"{label}.title is invalid or too long")
        if isinstance(text, str) and (
            "\x00" in text or len(text) > MAX_ARTIFACT_TEXT_CHARS
        ):
            raise ArtifactPublicationError(f"{label}.text is invalid or too long")

        requested = str(source)
        if requested in requested_paths:
            raise ArtifactPublicationError(f"{label}.path duplicates an earlier file")
        requested_paths.add(requested)
        normalized.append({"path": requested, "title": title, "text": text})
    return normalized


def normalize_artifact_entries(
    entries: Any,
    *,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Validate a complete artifact batch before any destination is created."""
    shapes = normalize_artifact_entry_shapes(entries, allow_empty=allow_empty)
    normalized: list[dict[str, Any]] = []
    canonical_paths: set[str] = set()
    total_size = 0
    for index, entry in enumerate(shapes):
        label = f"files[{index}]"
        try:
            source = Path(str(entry["path"])).resolve(strict=True)
            is_regular_file = source.is_file()
            is_readable = os.access(source, os.R_OK)
            source_size = source.stat().st_size
        except OSError as exc:
            raise ArtifactPublicationError(f"{label}.path is unavailable: {exc}") from exc
        if not is_regular_file:
            raise ArtifactPublicationError(f"{label}.path is not a regular file")
        if not is_readable:
            raise ArtifactPublicationError(f"{label}.path is not readable")
        if source_size > MAX_UPLOAD_BYTES:
            raise ArtifactPublicationError(f"{label}.path exceeds the file size limit")
        total_size += source_size
        if total_size > MAX_UPLOAD_BYTES:
            raise ArtifactPublicationError("artifact batch exceeds the total size limit")
        canonical = str(source)
        if canonical in canonical_paths:
            raise ArtifactPublicationError(f"{label}.path duplicates an earlier file")
        canonical_paths.add(canonical)
        normalized.append({
            "path": canonical,
            "title": entry.get("title"),
            "text": entry.get("text"),
        })
    return normalized


def artifact_entries_digest(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalized_artifact_request(
    publication_id: str,
    entries: Any,
) -> tuple[str, list[dict[str, Any]], str]:
    clean_publication_id = artifact_publication_id(publication_id)
    shapes = normalize_artifact_entry_shapes(entries)
    return clean_publication_id, shapes, artifact_entries_digest(shapes)


def artifact_record(session_id: str, entry: str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(entry, str):
        path = entry
        title = None
        text = None
    elif isinstance(entry, dict):
        path = entry.get("path", "")
        title = entry.get("title")
        text = entry.get("text")
    else:
        return None
    if not path or not os.path.isfile(path):
        return None
    file_id = f"art_{uuid.uuid4().hex[:16]}"
    src = Path(path)
    ext = src.suffix
    dest_dir = FILES_ROOT / file_id
    try:
        source_before = src.stat()
        dest_dir.mkdir(parents=True, exist_ok=False)
        dest = dest_dir / safe_name(src.name)
        if src.resolve() != dest.resolve():
            copy_fork_file_data(src, dest)
        source_after = src.stat()
        if (
            source_before.st_size != source_after.st_size
            or source_before.st_mtime_ns != source_after.st_mtime_ns
        ):
            raise ArtifactPublicationError(
                f"source file changed while it was being copied: {src}"
            )
        rec = {
            "id": file_id,
            "session_id": session_id,
            "kind": "artifact",
            "title": title or src.name,
            "text": text,
            "path": str(dest),
            "source_path": str(src),
            "filename": dest.name,
            "size": dest.stat().st_size,
            "content_type": guess_content_type(dest.name),
            "created_at": now_iso(),
        }
        (dest_dir / "meta.json").write_text(json.dumps(rec, indent=2))
        return rec
    except BaseException:
        with suppress(OSError):
            shutil.rmtree(dest_dir)
        raise


def remove_artifact_records(records: list[dict[str, Any]]) -> None:
    files_root = FILES_ROOT.resolve()
    for record in records:
        artifact_id = str(record.get("id") or "")
        if not re.fullmatch(r"art_[0-9a-f]{16}", artifact_id):
            continue
        artifact_dir = (FILES_ROOT / artifact_id).resolve()
        if artifact_dir.parent != files_root:
            continue
        with suppress(OSError):
            shutil.rmtree(artifact_dir)


async def remove_artifact_records_async(records: list[dict[str, Any]]) -> None:
    cleanup_task = asyncio.create_task(asyncio.to_thread(
        remove_artifact_records,
        records,
    ))
    with suppress(BaseException):
        await join_task_despite_caller_cancellation(cleanup_task)


def prepare_artifact_records(
    session_id: str,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for entry in entries:
            record = artifact_record(session_id, entry)
            if record is None:
                raise ArtifactPublicationError(
                    f"artifact source became unavailable: {entry['path']}"
                )
            records.append(record)
        return records
    except BaseException:
        remove_artifact_records(records)
        raise


def artifact_event_receipt(event: dict[str, Any]) -> dict[str, Any]:
    artifact = dict(event.get("artifact") or {})
    return {
        "artifact_id": artifact.get("id"),
        "event_id": event.get("id"),
        "event_seq": event.get("seq"),
        "run_id": event.get("run_id"),
        "filename": artifact.get("filename"),
        "content_type": artifact.get("content_type"),
        "size": artifact.get("size"),
    }


def path_is_relative_to(path: Path, root: Path) -> bool:
    with suppress(ValueError):
        path.relative_to(root)
        return True
    return False


def normalized_link_target(target: str) -> str:
    clean = target.strip()
    if clean.startswith("file://"):
        parsed = urlparse(clean)
        return unquote(parsed.path)
    return unquote(clean)


def session_file_for_link(session_id: str, target: str) -> dict[str, Any]:
    clean = normalized_link_target(target)
    if not clean:
        raise HTTPException(status_code=400, detail="missing target")
    clean_name = Path(clean).name
    files_root = FILES_ROOT.resolve()
    usable: list[dict[str, Any]] = []
    for rec in list_session_file_records(session_id):
        try:
            path = Path(str(rec.get("path") or "")).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path.is_file() and path_is_relative_to(path, files_root):
            usable.append(rec)

    exact = [
        rec
        for rec in usable
        if clean in {
            str(rec.get("id") or ""),
            str(rec.get("source_path") or ""),
            str(rec.get("path") or ""),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise HTTPException(status_code=409, detail=f"linked file target is ambiguous: {clean}")

    basename_matches = [
        rec
        for rec in usable
        if clean_name in {
            str(rec.get("filename") or ""),
            str(rec.get("title") or ""),
            Path(str(rec.get("source_path") or "")).name,
            Path(str(rec.get("path") or "")).name,
        }
    ]
    if len(basename_matches) == 1:
        return basename_matches[0]
    if len(basename_matches) > 1:
        raise HTTPException(status_code=409, detail=f"linked file name is ambiguous: {clean_name}")
    raise HTTPException(status_code=404, detail=f"linked file is not a registered artifact: {clean}")


def read_artifact_manifest(
    manifest_path: Path,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError:
        return [], None, None
    except OSError as exc:
        return [], None, f"manifest read failed: {exc}"
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"files"}:
            raise ArtifactPublicationError(
                "manifest must be an object containing only a files array"
            )
        entries = normalize_artifact_entries(data.get("files"), allow_empty=True)
        return entries, digest, None
    except (UnicodeDecodeError, json.JSONDecodeError, ArtifactPublicationError) as exc:
        return [], digest, f"manifest validation failed: {exc}"


def unlink_manifest_if_unchanged(manifest_path: Path, expected_digest: str) -> bool:
    try:
        current = manifest_path.read_bytes()
        if hashlib.sha256(current).hexdigest() != expected_digest:
            return False
        manifest_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = Path(path).expanduser().stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def live_manifest_entry_ready(path: str, stable_files: dict[str, tuple[int, int]]) -> bool:
    signature = file_signature(path)
    if signature is None:
        return False
    previous = stable_files.get(path)
    stable_files[path] = signature
    return previous == signature and time.time_ns() - signature[1] > 750_000_000


def live_manifest_batch_ready(
    entries: list[dict[str, Any]],
    stable_files: dict[str, tuple[int, int]],
) -> bool:
    ready = True
    for entry in entries:
        if not live_manifest_entry_ready(str(entry["path"]), stable_files):
            ready = False
    return ready


async def active_artifact_publication_run(
    session_id: str,
) -> tuple[str, dict[str, Any]]:
    async with RUNS.active_lock:
        current = RUNS.current_turns.get(session_id)
        active = RUNS.active.get(session_id)
        run_id = str((current or {}).get("run_id") or "").strip()
        if (
            session_id not in RUNS.busy
            or not isinstance(current, dict)
            or not isinstance(active, dict)
            or not run_id
            or str(active.get("run_id") or "").strip() != run_id
            or active.get("codex_native_operation") is True
            or active.get("stop_requested") is True
            or run_id in RUNS.stopped_runs
        ):
            raise HTTPException(
                status_code=409,
                detail="no active agent turn can receive artifacts for this chat",
            )
        return run_id, run_event_metadata(run_id)


def stage_upload_file_sync(source: Any, destination: Path) -> int:
    """Copy one already-spooled upload with a hard byte bound off-loop."""

    size = 0
    with _open_private_exclusive(destination, "wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="upload too large")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return size


def write_upload_metadata_sync(path: Path, metadata: dict[str, Any]) -> None:
    with _open_private_exclusive(path, "w") as output:
        json.dump(metadata, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


async def remove_upload_tree(path: Path) -> None:
    """Join cleanup even when the request is cancelled repeatedly."""

    cleanup_task = asyncio.create_task(asyncio.to_thread(
        shutil.rmtree,
        path,
        True,
    ))
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(cleanup_task)
        raise


async def run_upload_worker(function: Callable[..., Any], *args: Any) -> Any:
    """Own one upload filesystem worker through repeated cancellation."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(worker)
        raise


def load_file_meta(file_id: str) -> dict[str, Any]:
    clean_file_id = normalized_file_id(file_id)
    if not clean_file_id:
        raise HTTPException(status_code=404, detail="file not found")
    meta_path = FILES_ROOT / clean_file_id / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    file_dir = FILES_ROOT / clean_file_id
    if not file_dir.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    files = [p for p in file_dir.iterdir() if p.is_file() and p.name != "meta.json"]
    if len(files) != 1:
        raise HTTPException(status_code=404, detail="file not found")
    path = files[0]
    meta = {
        "id": clean_file_id,
        "kind": "artifact",
        "filename": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "content_type": guess_content_type(path.name),
        "created_at": now_iso(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta
