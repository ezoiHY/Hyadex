import asyncio
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from tests import hyadex_server
class IsolatedServerStateMixin:
    def isolated_state(self, root: Path):
        state = root / "state"
        return patch.multiple(
            hyadex_server,
            STATE_DIR=state,
            FILES_ROOT=state / "files",
            CODE_DIFFS_ROOT=state / "code_diffs",
            PROVIDER_AUTHORITY_ROOT=state / "cross_chat_authority",
        )

    def upload(self, content: bytes, filename: str = "notes.txt") -> UploadFile:
        return UploadFile(
            io.BytesIO(content),
            filename=filename,
            headers=Headers({"content-type": "application/octet-stream"}),
        )


class UploadLifecycleTests(IsolatedServerStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_upload_is_atomic_private_and_uses_effective_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploaded = self.upload(b"hello\n")
            old_umask = os.umask(0)
            try:
                with self.isolated_state(root), patch.dict(
                    hyadex_server.STORE.sessions,
                    {"chat": {"id": "chat", "archived": False}},
                    clear=True,
                ), patch.object(
                    hyadex_server,
                    "append_durable_event",
                    AsyncMock(return_value={"seq": 1}),
                ) as append_event:
                    result = await hyadex_server.upload_file("chat", uploaded)
            finally:
                os.umask(old_umask)

            record = result["file"]
            data_path = Path(record["path"])
            file_root = data_path.parent
            metadata_path = file_root / "meta.json"
            self.assertEqual(data_path.read_bytes(), b"hello\n")
            self.assertEqual(record["content_type"], "text/plain")
            self.assertEqual(stat.S_IMODE(file_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(data_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(metadata_path.read_text())["id"], record["id"])
            self.assertFalse(any(path.name.startswith(".file_") for path in file_root.parent.iterdir()))
            append_event.assert_awaited_once()

    async def test_oversize_and_event_failure_leave_no_upload_or_staging_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.isolated_state(root), patch.dict(
                hyadex_server.STORE.sessions,
                {"chat": {"id": "chat", "archived": False}},
                clear=True,
            ), patch.object(hyadex_server, "MAX_UPLOAD_BYTES", 4):
                with self.assertRaises(HTTPException) as oversized:
                    await hyadex_server.upload_file("chat", self.upload(b"too large"))
                self.assertEqual(oversized.exception.status_code, 413)
                self.assertEqual(list(hyadex_server.FILES_ROOT.iterdir()), [])

            with self.isolated_state(root), patch.dict(
                hyadex_server.STORE.sessions,
                {"chat": {"id": "chat", "archived": False}},
                clear=True,
            ), patch.object(
                hyadex_server,
                "append_durable_event",
                AsyncMock(side_effect=OSError("event write failed")),
            ):
                with self.assertRaises(OSError):
                    await hyadex_server.upload_file("chat", self.upload(b"content"))
                self.assertEqual(list(hyadex_server.FILES_ROOT.iterdir()), [])

    async def test_repeated_cancel_during_metadata_worker_leaves_no_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata_started = threading.Event()
            release_metadata = threading.Event()
            real_write = hyadex_server.write_upload_metadata_sync

            def delayed_metadata(path: Path, metadata: dict) -> None:
                metadata_started.set()
                release_metadata.wait(timeout=5)
                real_write(path, metadata)

            with self.isolated_state(root), patch.dict(
                hyadex_server.STORE.sessions,
                {"chat": {"id": "chat", "archived": False}},
                clear=True,
            ), patch.object(
                hyadex_server,
                "write_upload_metadata_sync",
                side_effect=delayed_metadata,
            ):
                request = asyncio.create_task(hyadex_server.upload_file(
                    "chat",
                    self.upload(b"cancel me"),
                ))
                self.assertTrue(await asyncio.to_thread(metadata_started.wait, 2))
                request.cancel()
                await asyncio.sleep(0)
                request.cancel()
                release_metadata.set()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertEqual(list(hyadex_server.FILES_ROOT.iterdir()), [])

    async def test_delete_winning_copy_race_rejects_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied = threading.Event()
            release_copy = threading.Event()
            real_stage = hyadex_server.stage_upload_file_sync

            def delayed_stage(source, destination: Path) -> int:
                size = real_stage(source, destination)
                copied.set()
                release_copy.wait(timeout=5)
                return size

            with self.isolated_state(root), patch.dict(
                hyadex_server.STORE.sessions,
                {"chat": {"id": "chat", "archived": False}},
                clear=True,
            ), patch.object(
                hyadex_server,
                "stage_upload_file_sync",
                side_effect=delayed_stage,
            ), patch.object(
                hyadex_server,
                "append_durable_event",
                AsyncMock(),
            ) as append_event:
                request = asyncio.create_task(hyadex_server.upload_file(
                    "chat",
                    self.upload(b"racing"),
                ))
                self.assertTrue(await asyncio.to_thread(copied.wait, 2))
                async with hyadex_server.session_lifecycle_lock("chat"):
                    hyadex_server.STORE.sessions.pop("chat", None)
                    hyadex_server.DELETING_SESSIONS.add("chat")
                release_copy.set()
                with self.assertRaises(HTTPException):
                    await request
                self.assertEqual(list(hyadex_server.FILES_ROOT.iterdir()), [])
                append_event.assert_not_awaited()
            hyadex_server.DELETING_SESSIONS.discard("chat")


class FileLinkAndForkTests(IsolatedServerStateMixin, unittest.TestCase):
    def test_exact_link_beats_newer_basename_and_ambiguous_basename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.isolated_state(root):
                first = hyadex_server.FILES_ROOT / "art_first" / "report.pdf"
                second = hyadex_server.FILES_ROOT / "art_second" / "report.pdf"
                first.parent.mkdir(parents=True)
                second.parent.mkdir(parents=True)
                first.write_bytes(b"first")
                second.write_bytes(b"second")
                records = [
                    {"id": "art_second", "session_id": "chat", "path": str(second), "source_path": "/work/two/report.pdf", "filename": "report.pdf"},
                    {"id": "art_first", "session_id": "chat", "path": str(first), "source_path": "/work/one/report.pdf", "filename": "report.pdf"},
                ]
                with patch.object(
                    hyadex_server,
                    "list_session_file_records",
                    return_value=records,
                ):
                    exact = hyadex_server.session_file_for_link(
                        "chat",
                        "/work/one/report.pdf",
                    )
                    with self.assertRaises(HTTPException) as ambiguous:
                        hyadex_server.session_file_for_link("chat", "report.pdf")

        self.assertEqual(exact["id"], "art_first")
        self.assertEqual(ambiguous.exception.status_code, 409)

    def test_fork_copies_owned_diff_and_session_cleanup_removes_diff_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.isolated_state(root):
                parent = hyadex_server.code_diffs_dir("parent")
                parent.mkdir(parents=True)
                (parent / "run-1.patch").write_bytes(b"patch")
                (parent / "run-1.json").write_text('{"run_id":"run-1"}')
                copied = hyadex_server.clone_fork_code_diffs(
                    "parent",
                    "child",
                    ["run-1"],
                )
                child = hyadex_server.code_diffs_dir("child")
                self.assertEqual(copied, ["run-1.patch", "run-1.json"])
                self.assertEqual((child / "run-1.patch").read_bytes(), b"patch")
                self.assertEqual(
                    stat.S_IMODE((child / "run-1.patch").stat().st_mode),
                    0o600,
                )
                hyadex_server.session_dir("child").mkdir(parents=True)
                hyadex_server.delete_session_local_roots("child")
                self.assertFalse(child.exists())
                self.assertFalse(hyadex_server.session_dir("child").exists())


class ArtifactPublicationLifecycleTests(
    IsolatedServerStateMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_delete_winning_artifact_copy_race_cannot_recreate_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "artifact.txt"
            source.write_text("artifact\n")
            copied = threading.Event()
            release_copy = threading.Event()
            real_prepare = hyadex_server.prepare_artifact_records

            def delayed_prepare(session_id: str, entries: list[dict]) -> list[dict]:
                records = real_prepare(session_id, entries)
                copied.set()
                release_copy.wait(timeout=5)
                return records

            entries = hyadex_server.normalize_artifact_entries([
                {"path": str(source)},
            ])
            with self.isolated_state(root), patch.dict(
                hyadex_server.STORE.sessions,
                {"chat": {"id": "chat", "archived": False}},
                clear=True,
            ), patch.object(
                hyadex_server,
                "prepare_artifact_records",
                side_effect=delayed_prepare,
            ):
                publication = asyncio.create_task(
                    hyadex_server._publish_artifact_entries_unlocked(
                        "chat",
                        "run-1",
                        entries,
                    )
                )
                self.assertTrue(await asyncio.to_thread(copied.wait, 2))
                async with hyadex_server.session_lifecycle_lock("chat"):
                    hyadex_server.STORE.sessions.pop("chat", None)
                    hyadex_server.DELETING_SESSIONS.add("chat")
                    hyadex_server.DELETED_SESSION_TOMBSTONES.add("chat")
                release_copy.set()
                with self.assertRaises(hyadex_server.ArtifactPublicationError):
                    await publication
                self.assertFalse(hyadex_server.events_path("chat").exists())
                self.assertEqual(list(hyadex_server.FILES_ROOT.iterdir()), [])
            hyadex_server.DELETING_SESSIONS.discard("chat")
            hyadex_server.DELETED_SESSION_TOMBSTONES.discard("chat")


class SubscriberHubCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_per_session_and_global_reservations_are_atomic(self) -> None:
        hub = hyadex_server.SubscriberHub()
        first, second, third = object(), object(), object()
        with patch.object(hyadex_server, "EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL", 2), patch.object(
            hyadex_server,
            "EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION",
            1,
        ):
            same_session = await asyncio.gather(
                hub.reserve("one", first),  # type: ignore[arg-type]
                hub.reserve("one", second),  # type: ignore[arg-type]
            )
            self.assertEqual(sum(same_session), 1)
            winner = first if same_session[0] else second
            self.assertTrue(await hub.reserve("two", third))  # type: ignore[arg-type]
            blocked = object()
            self.assertFalse(await hub.reserve("three", blocked))  # type: ignore[arg-type]
            self.assertTrue(await hub.register_accepted("one", winner))  # type: ignore[arg-type]
            await hub.unsubscribe("one", winner)  # type: ignore[arg-type]
            self.assertTrue(await hub.reserve("three", blocked))  # type: ignore[arg-type]
            await hub.unsubscribe("two", third)  # type: ignore[arg-type]
            await hub.unsubscribe("three", blocked)  # type: ignore[arg-type]

        self.assertEqual(hub._subscribers, {})
        self.assertEqual(hub._reservations, {})

    async def test_endpoint_over_capacity_closes_1013_and_reconnect_releases(self) -> None:
        class Socket:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.query_params: dict[str, str] = {}
                self.accepted = False
                self.closes: list[tuple[int, str]] = []

            async def accept(self, *, subprotocol=None) -> None:
                self.accepted = True

            async def close(self, code: int = 1000, reason: str = "") -> None:
                self.closes.append((code, reason))

            async def receive_text(self) -> str:
                raise hyadex_server.WebSocketDisconnect()

            async def send_json(self, _event: dict) -> None:
                return None

        hub = hyadex_server.SubscriberHub()
        blocker = Socket()
        rejected = Socket()
        reconnect = Socket()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            hyadex_server,
            "HUB",
            hub,
        ), patch.object(
            hyadex_server,
            "EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL",
            1,
        ), patch.object(
            hyadex_server,
            "EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION",
            1,
        ), patch.object(
            hyadex_server,
            "websocket_authorized",
            return_value=True,
        ), patch.dict(
            hyadex_server.STORE.sessions,
            {"chat": {"id": "chat"}},
            clear=True,
        ), patch.object(
            hyadex_server,
            "events_path",
            return_value=Path(temporary) / "events.jsonl",
        ):
            self.assertTrue(await hub.reserve("chat", blocker))  # type: ignore[arg-type]
            await hyadex_server.session_events("chat", rejected)  # type: ignore[arg-type]
            self.assertEqual(rejected.closes[0][0], 1013)
            await hub.unsubscribe("chat", blocker)  # type: ignore[arg-type]
            await hyadex_server.session_events("chat", reconnect)  # type: ignore[arg-type]

        self.assertEqual(hub._subscribers, {})
        self.assertEqual(hub._reservations, {})


class JobDeleteRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_save_failure_restores_exact_popped_job(self) -> None:
        store = hyadex_server.JobStore()
        job = {"id": "job-1", "session_id": "chat", "nested": {"value": 1}}
        store.jobs[job["id"]] = job
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                await store.delete(job["id"])
        self.assertIs(store.jobs[job["id"]], job)

    async def test_cancellation_after_save_commit_keeps_job_deleted(self) -> None:
        store = hyadex_server.JobStore()
        job = {"id": "job-1", "session_id": "chat"}
        store.jobs[job["id"]] = job
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.delete(job["id"])
        self.assertNotIn(job["id"], store.jobs)


class TerminalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_wrapper_serializes_same_session(self) -> None:
        active = 0
        maximum = 0
        guard = threading.Lock()

        def inner(session_id: str, cwd=None, *, columns=None, rows=None) -> dict:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"session_id": session_id}

        with patch.object(
            hyadex_server,
            "_ensure_terminal_session_locked",
            side_effect=inner,
        ):
            await asyncio.gather(
                asyncio.to_thread(hyadex_server.ensure_terminal_session, "chat"),
                asyncio.to_thread(hyadex_server.ensure_terminal_session, "chat"),
            )
        self.assertEqual(maximum, 1)

    async def test_http_controls_do_not_resurrect_missing_terminal(self) -> None:
        with patch.dict(
            hyadex_server.STORE.sessions,
            {"chat": {"id": "chat", "archived": False}},
            clear=True,
        ), patch.object(
            hyadex_server,
            "tmux_session_exists",
            return_value=False,
        ), patch.object(
            hyadex_server,
            "ensure_terminal_session",
        ) as ensure:
            for operation in (
                lambda: hyadex_server.send_terminal_input("chat", "text"),
                lambda: hyadex_server.resize_terminal_pane("chat", 100, 30),
                lambda: hyadex_server.terminal_action("chat", "new-window"),
            ):
                with self.subTest(operation=operation), self.assertRaises(HTTPException) as raised:
                    await asyncio.to_thread(operation)
                self.assertEqual(raised.exception.status_code, 409)
            ensure.assert_not_called()

    async def test_nonblocking_pty_backpressure_does_not_block_event_loop(self) -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        try:
            while True:
                try:
                    os.write(write_fd, b"x" * 65536)
                except BlockingIOError:
                    break
            ticked = asyncio.Event()

            async def ticker() -> None:
                await asyncio.sleep(0.01)
                ticked.set()

            ticker_task = asyncio.create_task(ticker())
            with patch.object(
                hyadex_server,
                "TERMINAL_INPUT_WRITE_TIMEOUT_SECONDS",
                0.05,
            ):
                with self.assertRaises(asyncio.TimeoutError):
                    await hyadex_server.write_terminal_input(write_fd, b"blocked")
            await ticker_task
            self.assertTrue(ticked.is_set())
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_malformed_resize_is_ignored_without_terminal_error(self) -> None:
        class Socket:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.query_params: dict[str, str] = {}
                self.messages = [
                    {"type": "websocket.receive", "text": "[]"},
                    {"type": "websocket.receive", "text": "null"},
                    {"type": "websocket.receive", "text": '"resize"'},
                    {"type": "websocket.receive", "text": "42"},
                    {"type": "websocket.receive", "text": '{"type":"resize","columns":"bad","rows":20}'},
                    {"type": "websocket.disconnect"},
                ]
                self.sent: list[dict] = []

            async def accept(self, *, subprotocol=None) -> None:
                return None

            async def close(self, code: int = 1000) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_bytes(self, _payload: bytes) -> None:
                return None

            async def receive(self) -> dict:
                return self.messages.pop(0)

        socket = Socket()
        attachments = MagicMock()
        attachments.reserve = AsyncMock(return_value=True)
        attachments.spawn = AsyncMock(return_value=(MagicMock(), 99, "hx_chat"))
        attachments.release = AsyncMock()

        async def never_read(_fd: int) -> bytes:
            await asyncio.Event().wait()
            return b""

        with patch.dict(
            hyadex_server.STORE.sessions,
            {"chat": {"id": "chat", "archived": False}},
            clear=True,
        ), patch.object(
            hyadex_server,
            "websocket_authorized",
            return_value=True,
        ), patch.object(
            hyadex_server,
            "TERMINAL_ATTACHMENTS",
            attachments,
        ), patch.object(
            hyadex_server,
            "read_terminal_output",
            side_effect=never_read,
        ), patch.object(
            hyadex_server,
            "set_pty_dimensions",
        ) as set_dimensions, patch.object(
            hyadex_server,
            "resize_terminal_window",
        ) as resize, patch.object(
            hyadex_server,
            "stop_terminal_client",
        ):
            await hyadex_server.session_terminal("chat", socket)  # type: ignore[arg-type]

        self.assertEqual(socket.sent[0]["type"], "ready")
        self.assertFalse(any(item.get("type") == "error" for item in socket.sent))
        set_dimensions.assert_not_called()
        resize.assert_not_called()
        attachments.release.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
