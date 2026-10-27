"""Chat terminals: tmux session control, windows, panes and the terminal WebSocket."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import json
import logging.handlers
import shutil
import subprocess
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hyadex.core.config import TERMINAL_WEBSOCKET_PROTOCOL
from hyadex.core.http import (
    router,
    TerminalActionRequest,
    TerminalInputRequest,
    TerminalOpenRequest,
    TerminalResizeRequest,
    websocket_authorized,
    websocket_endpoint_subprotocol,
)
from hyadex.core.processes import run_tmux
from hyadex.core.sessions import STORE
from hyadex.core.util import terminal_dimensions
from hyadex.features.terminal import (
    capture_tmux_pane,
    ensure_terminal_session,
    kill_terminal_session,
    read_terminal_output,
    require_existing_terminal_session,
    resize_terminal_window,
    scroll_terminal_history,
    send_terminal_input,
    set_pty_dimensions,
    stop_terminal_client,
    terminal_action,
    TERMINAL_ATTACHMENTS,
    terminal_session_lock,
    terminal_snapshot,
    terminal_windows_snapshot,
    TerminalAttachmentAdmissionClosed,
    tmux_panes_snapshot,
    tmux_session_exists,
    write_terminal_input,
)
from hyadex.providers.authority import terminal_session_name

logger = logging.getLogger("hyadex-server")

router = APIRouter()


def resize_terminal_pane(session_id: str, columns: int, rows: int) -> dict[str, Any]:
    with terminal_session_lock(session_id):
        require_existing_terminal_session(session_id)
        _cols, line_count = resize_terminal_window(session_id, columns, rows)
        return terminal_snapshot(session_id, lines=line_count)


def exit_terminal_auto_scroll(session_id: str) -> None:
    if shutil.which("tmux") is None:
        return
    name = terminal_session_name(session_id)
    if tmux_session_exists(name):
        run_tmux(["send-keys", "-X", "-t", name, "cancel"], check=False)


@router.get("/api/sessions/{session_id}/terminal")
async def get_session_terminal(session_id: str, lines: int = 240) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_snapshot, session_id, lines=lines)


@router.post("/api/sessions/{session_id}/terminal/open")
async def open_session_terminal(session_id: str, req: TerminalOpenRequest) -> dict[str, Any]:
    return await asyncio.to_thread(ensure_terminal_session, session_id, req.cwd)


@router.post("/api/sessions/{session_id}/terminal/input")
async def input_session_terminal(session_id: str, req: TerminalInputRequest) -> dict[str, Any]:
    return await asyncio.to_thread(send_terminal_input, session_id, req.text, enter=req.enter, key=req.key)


@router.post("/api/sessions/{session_id}/terminal/resize")
async def resize_session_terminal(session_id: str, req: TerminalResizeRequest) -> dict[str, Any]:
    return await asyncio.to_thread(resize_terminal_pane, session_id, req.columns, req.rows)


@router.get("/api/sessions/{session_id}/terminal/windows")
async def get_session_terminal_windows(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_windows_snapshot, session_id)


@router.post("/api/sessions/{session_id}/terminal/action")
async def run_session_terminal_action(session_id: str, req: TerminalActionRequest) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_action, session_id, req.action, req.target)


@router.delete("/api/sessions/{session_id}/terminal")
async def delete_session_terminal(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(kill_terminal_session, session_id)


@router.get("/api/sessions/{session_id}/tmux")
async def get_session_tmux_panes(session_id: str, include_all: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(tmux_panes_snapshot, session_id, include_all=include_all)


@router.get("/api/sessions/{session_id}/tmux/capture")
async def capture_session_tmux_pane(session_id: str, pane_id: str, lines: int = 500) -> dict[str, Any]:
    return await asyncio.to_thread(capture_tmux_pane, session_id, pane_id, lines=lines)


@router.websocket("/api/sessions/{session_id}/terminal/ws")
async def session_terminal(
    session_id: str,
    ws: WebSocket,
    columns: int = 120,
    rows: int = 36,
    cwd: str | None = None,
) -> None:
    selected_subprotocol = websocket_endpoint_subprotocol(
        ws,
        TERMINAL_WEBSOCKET_PROTOCOL,
    )
    if not websocket_authorized(ws):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4404)
        return
    if bool(STORE.sessions[session_id].get("archived")):
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=4409)
        return

    cols, lines = terminal_dimensions(columns, rows)
    reserved = await TERMINAL_ATTACHMENTS.reserve(session_id, ws)
    if not reserved:
        await ws.accept(subprotocol=selected_subprotocol)
        await ws.close(code=1012)
        return
    process: subprocess.Popen[bytes] | None = None
    master_fd: int | None = None
    try:
        await ws.accept(subprotocol=selected_subprotocol)
        process, master_fd, name = await TERMINAL_ATTACHMENTS.spawn(
            session_id,
            ws,
            cwd,
            cols,
            lines,
        )
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "name": name,
            "columns": cols,
            "rows": lines,
        })

        async def pump_output() -> None:
            assert master_fd is not None
            while True:
                try:
                    data = await read_terminal_output(master_fd)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        return
                    raise
                if not data:
                    return
                await ws.send_bytes(data)

        async def receive_input() -> None:
            assert master_fd is not None
            auto_scroll_mode = False
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data:
                        if auto_scroll_mode:
                            await asyncio.to_thread(exit_terminal_auto_scroll, session_id)
                            auto_scroll_mode = False
                        await write_terminal_input(master_fd, data)
                        continue
                    text = message.get("text")
                    if not text:
                        continue
                    try:
                        control = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(control, dict):
                        continue
                    if control.get("type") == "resize":
                        # Control frames are untrusted and may come from a
                        # mixed-version or partially restored renderer. Match
                        # scroll handling: discard malformed geometry without
                        # turning it into a fatal WebSocket error.
                        with suppress(TypeError, ValueError):
                            next_cols, next_rows = terminal_dimensions(control.get("columns"), control.get("rows"))
                            set_pty_dimensions(master_fd, next_cols, next_rows)
                            await asyncio.to_thread(resize_terminal_window, session_id, next_cols, next_rows)
                    elif control.get("type") == "scroll":
                        with suppress(TypeError, ValueError):
                            auto_scroll_mode = await asyncio.to_thread(
                                scroll_terminal_history,
                                session_id,
                                int(control.get("delta") or 0),
                                managed=auto_scroll_mode,
                            )
            finally:
                if auto_scroll_mode:
                    await asyncio.to_thread(exit_terminal_auto_scroll, session_id)

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(receive_input())
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                task.result()
    except (WebSocketDisconnect, TerminalAttachmentAdmissionClosed):
        pass
    except Exception as exc:
        logger.warning("terminal websocket failed session=%s error=%s", session_id, exc)
        with suppress(RuntimeError):
            await ws.send_json({"type": "error", "message": str(exc)[:1000]})
    finally:
        if process is not None and master_fd is not None:
            await asyncio.to_thread(stop_terminal_client, process, master_fd)
        await TERMINAL_ATTACHMENTS.release(ws)
        with suppress(RuntimeError):
            await ws.close()
