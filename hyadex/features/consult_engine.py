"""Chat consults: one Work chat asks another a question, in basic or deep mode.

Basic mode answers from the asked chat's existing context without tools (a
native side question). Deep mode sends a read-only fork of the asked chat to
investigate in its folder. Neither touches the asked chat's transcript, queue
or turn admission, and neither answer can ask another chat in turn.

The engine owns the ledger, who may ask whom, per-turn limits, approvals and
each consult's lifetime. Executors produce the answers; `consult_runtime`
supplies them and wires the engine into the server. Nothing starts on import.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, suppress
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable, Mapping
import uuid

MODES = ("basic", "deep")
INBOUND_POLICIES = ("off", "basic", "deep")
DEFAULT_INBOUND = "deep"
DEFAULT_FOLDER = "General"
TURN_LIMITS = {"basic": 5, "deep": 2}
RUN_TIMEOUTS = {"basic": 120.0, "deep": 300.0}
APPROVAL_TIMEOUT_SECONDS = 600.0
LEASE_SECONDS = 45.0
LEASE_CHECK_SECONDS = 5.0
MAX_WAIT_SECONDS = 25.0
MAX_QUESTION_CHARS = 8000
MAX_ANSWER_CHARS = {"basic": 4000, "deep": 8000}
MAX_STEPS = 200
TARGET_SLOTS = {"basic": 2, "deep": 1}
KEEP_PER_SESSION = 200
LIST_LIMIT = 50
ACTIVE = ("awaiting_approval", "queued", "running")
CANCELLED = ("cancelled", "cancelled", "the consult was cancelled")
TERMINAL = ("completed", "failed", "cancelled", "denied", "expired", "interrupted")
CONFIDENCE_LINE = re.compile(r"^\**\s*CONFIDENCE\s*:\s*(high|low)\s*\**\.?\s*$", re.IGNORECASE)

PROMPTS = {
    "basic": (
        "[Hyadex consult] {asker} asks you a question. Answer only from what you already know in "
        "this conversation. Do not use tools and do not guess: if the answer needs checking files or "
        "running something, say what should be checked. Answer for {reader}: brief and specific. "
        "End with one final line: CONFIDENCE: high or CONFIDENCE: low.\n\nQuestion:\n{question}"
    ),
    "deep": (
        "[Hyadex consult] {asker} asks you a question. You are a temporary, read-only copy of this "
        "conversation, working in the same folder. Investigate with the read-only tools you have "
        "(read files, search the code); never change files, git state or anything else.{busy} "
        "Then answer for {reader}: the answer first, then the evidence (file:line, or the commands "
        "you ran). End with one final line: CONFIDENCE: high or CONFIDENCE: low.\n\nQuestion:\n{question}"
    ),
}
BUSY_NOTE = (" The original chat is working right now, so some files may be mid-change; "
             "say so when it matters.")

Executor = Callable[[str, Mapping[str, Any], str, Callable[[dict[str, str]], None]], Awaitable[dict[str, Any]]]


class ConsultError(Exception):
    """A consult that cannot start or finish, with the HTTP status to report."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def inbound_policy(sess: Mapping[str, Any] | None) -> str:
    """Which modes other chats may use to ask this chat (off, basic, deep)."""
    value = str((sess or {}).get("consult_inbound") or "").strip()
    return value if value in INBOUND_POLICIES else DEFAULT_INBOUND


def deep_needs_approval(source: Mapping[str, Any], target_id: str) -> bool:
    if source.get("consult_deep_approval") is False:
        return False
    return target_id not in (source.get("consult_deep_allowed") or [])


def project_folder(sess: Mapping[str, Any]) -> str:
    folder = str(sess.get("folder") or "").strip()
    return "" if folder == DEFAULT_FOLDER else folder


def normalized_cwd(sess: Mapping[str, Any]) -> str:
    path = os.path.expanduser(str(sess.get("cwd") or "").strip() or "~")
    return os.path.realpath(path).rstrip("/") or "/"


def same_project(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Chats may ask each other inside one project: the same project folder,
    or (for chats in none) the same working directory."""
    folder_a, folder_b = project_folder(a), project_folder(b)
    if folder_a and folder_a == folder_b:
        return True
    return normalized_cwd(a) == normalized_cwd(b)


def split_confidence(text: str) -> tuple[str, bool | None]:
    """Remove the closing CONFIDENCE line; True when high, None when absent."""
    lines = str(text or "").rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        match = CONFIDENCE_LINE.match(lines[-1])
        if match:
            return "\n".join(lines[:-1]).strip(), match.group(1).lower() == "high"
    return "\n".join(lines).strip(), None


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def consult_prompt(record: Mapping[str, Any], *, target_busy: bool) -> str:
    source = record.get("source") or {}
    if source.get("kind") == "chat" and source.get("by_user"):
        asker, reader = f'The user, writing in another chat ("{source.get("title") or "untitled"}"),', "the user"
    elif source.get("kind") == "chat":
        asker, reader = f'Another chat, "{source.get("title") or "untitled"}",', "another agent"
    else:
        asker, reader = "The user", "the user"
    return PROMPTS[record["mode"]].format(
        asker=asker, reader=reader, question=record["question"],
        busy=BUSY_NOTE if target_busy else "",
    )


def chat_label(session_id: str, sess: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": session_id, "title": str(sess.get("title") or "未命名聊天"),
            "backend": str(sess.get("backend") or "")}


class ChatConsults:
    """The consult ledger and the lifetime of every consult."""

    def __init__(self, path: str | Path | None, *, sessions: Callable[[], Mapping[str, Mapping[str, Any]]],
                 execute: Executor, readiness: Callable[[Mapping[str, Any]], str | None] | None = None,
                 remember_deep_allowed: Callable[[str, str], Awaitable[None]] | None = None,
                 notify: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
                 record_question: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self.sessions = sessions
        self.execute = execute
        self.readiness = readiness or (lambda _sess: None)
        self.remember_deep_allowed = remember_deep_allowed
        self.notify = notify
        # Marks a question the user asked from a chat (@ in its composer) in that chat's timeline.
        self.record_question = record_question
        self.clock = clock
        self.records: dict[str, dict[str, Any]] = {}   # active consults
        self.tasks: dict[str, asyncio.Task] = {}
        self.decisions: dict[str, asyncio.Future] = {}
        self.finished: dict[str, asyncio.Event] = {}
        self.leases: dict[str, float] = {}
        # Why a consult was cancelled: (status, error code, message).
        self.cancel_reasons: dict[str, tuple[str, str, str]] = {}
        self.turn_counts: dict[str, dict[str, int]] = {}
        self.slots: dict[tuple[str, str], asyncio.Semaphore] = {}
        self.notices: set[asyncio.Task] = set()
        self.last_step_save: dict[str, float] = {}
        self.watchdog: asyncio.Task | None = None
        self.stopping = False
        self._db: sqlite3.Connection | None = None

    # MARK: Storage

    def _database(self) -> sqlite3.Connection:
        if self._db is None:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path) if self.path is not None else ":memory:")
            if self.path is not None:
                with suppress(OSError):
                    os.chmod(self.path, 0o600)
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS consults (
                    id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS consults_source ON consults(source_id, created_at);
                CREATE INDEX IF NOT EXISTS consults_target ON consults(target_id, created_at);
            """)
        return self._db

    def _save(self, record: dict[str, Any]) -> None:
        db = self._database()
        with db:
            db.execute(
                "INSERT OR REPLACE INTO consults (id, source_id, target_id, created_at, status, record) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], (record.get("source") or {}).get("id"), record["target"]["id"],
                 record["created_at"], record["status"], json.dumps(record, ensure_ascii=False)),
            )

    def _prune(self, *session_ids: str) -> None:
        db = self._database()
        with db:
            for session_id in session_ids:
                for column in ("source_id", "target_id"):
                    db.execute(
                        f"DELETE FROM consults WHERE {column} = ? AND status NOT IN (?, ?, ?) AND id NOT IN "
                        f"(SELECT id FROM consults WHERE {column} = ? ORDER BY created_at DESC LIMIT ?)",
                        (session_id, *ACTIVE, session_id, KEEP_PER_SESSION),
                    )

    def _load(self, consult_id: str) -> dict[str, Any] | None:
        active = self.records.get(consult_id)
        if active is not None:
            return active
        row = self._database().execute("SELECT record FROM consults WHERE id = ?", (consult_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self) -> int:
        """At startup, a consult that was answering when the server stopped ends as interrupted."""
        db = self._database()
        rows = db.execute(
            "SELECT record FROM consults WHERE status IN (?, ?, ?)", ACTIVE).fetchall()
        now = self.clock()
        for (raw,) in rows:
            record = json.loads(raw)
            record.update(status="interrupted", finished_at=now, updated_at=now,
                          error={"code": "interrupted", "message": "the server restarted while this consult ran"},
                          revision=int(record.get("revision") or 0) + 1)
            self._save(record)
        return len(rows)

    # MARK: Who may ask whom

    def _source(self, source_id: str, sessions: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
        source = sessions.get(source_id)
        if source is None:
            raise ConsultError(404, "chat not found")
        if source.get("archived"):
            raise ConsultError(409, "an archived chat cannot ask other chats")
        if source.get("group_member"):
            raise ConsultError(409, "group members cannot ask other chats")
        return source

    def _askable(self, source_id: str, source: Mapping[str, Any], target_id: str,
                 target: Mapping[str, Any]) -> bool:
        return (target_id != source_id and not target.get("archived") and not target.get("group_member")
                and same_project(source, target) and inbound_policy(target) != "off")

    def targets(self, source_id: str) -> list[dict[str, Any]]:
        """The chats this chat may ask, most recently active first."""
        sessions = self.sessions()
        source = self._source(source_id, sessions)
        found = []
        for target_id, target in sessions.items():
            if not self._askable(source_id, source, target_id, target):
                continue
            found.append({
                **chat_label(target_id, target),
                "project": project_folder(target) or None,
                "cwd": target.get("cwd"),
                "working": isinstance(target.get("active_run"), dict),
                "modes": ["basic"] if inbound_policy(target) == "basic" else list(MODES),
                "ready": self.readiness(target) is None,
                "updated_at": target.get("updated_at"),
            })
        found.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return found[:LIST_LIMIT]

    def _resolve(self, source_id: str, source: Mapping[str, Any], reference: str,
                 sessions: Mapping[str, Mapping[str, Any]]) -> tuple[str, Mapping[str, Any]]:
        reference = str(reference or "").strip()
        if not reference:
            raise ConsultError(422, "name the chat to ask (its title or id)")
        askable = {sid: sess for sid, sess in sessions.items() if self._askable(source_id, source, sid, sess)}
        if reference in askable:
            return reference, askable[reference]
        if reference in sessions:
            raise ConsultError(403, "that chat is not in this chat's project, or does not accept questions")
        matches = [(sid, sess) for sid, sess in askable.items()
                   if str(sess.get("title") or "").strip().casefold() == reference.casefold()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            ids = ", ".join(f'{sid} ("{sess.get("title")}")' for sid, sess in matches)
            raise ConsultError(409, f"several chats are titled that; ask by id: {ids}")
        titles = ", ".join(f'"{sess.get("title")}"' for sess in list(askable.values())[:10]) or "none"
        raise ConsultError(404, f"no chat in this project is titled that; chats you can ask: {titles}")

    # MARK: Lifecycle of one consult

    async def start(self, *, source_id: str | None, target: str, mode: str, question: str,
                    run_id: str | None = None, request_id: str | None = None,
                    lease: bool = False, by_user: bool = False) -> dict[str, Any]:
        """Admit a consult and start answering it. `source_id` None means the user asked
        the target directly; `by_user` means the user asked from the source chat
        (@ in its composer): no approval and no turn limit, and the answer stays
        with the user rather than the source chat's agent."""
        if self.stopping:
            raise ConsultError(503, "the server is shutting down")
        mode = str(mode or "basic").strip().lower()
        if mode not in MODES:
            raise ConsultError(422, "mode must be basic or deep")
        question = str(question or "").strip()
        if not question:
            raise ConsultError(422, "the question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ConsultError(422, f"the question is longer than {MAX_QUESTION_CHARS} characters")
        sessions = self.sessions()
        if source_id is None:
            target_id = str(target or "")
            target_sess = sessions.get(target_id)
            if target_sess is None:
                raise ConsultError(404, "chat not found")
            source = None
        else:
            source = self._source(source_id, sessions)
            target_id, target_sess = self._resolve(source_id, source, target, sessions)
            if mode == "deep" and inbound_policy(target_sess) == "basic":
                raise ConsultError(403, f'"{target_sess.get("title")}" accepts only basic questions')
        reason = self.readiness(target_sess)
        if reason:
            raise ConsultError(409, reason)
        if request_id:
            for record in self.records.values():
                if record.get("request_id") == request_id and record["source"].get("id") == source_id:
                    return self.public(record)
        if run_id and not by_user:
            counts = self.turn_counts.setdefault(run_id, {key: 0 for key in MODES})
            if counts[mode] >= TURN_LIMITS[mode]:
                raise ConsultError(429, f"this turn already asked {TURN_LIMITS[mode]} {mode} questions")
            counts[mode] += 1
        now = self.clock()
        approval = source is not None and not by_user and mode == "deep" and deep_needs_approval(source, target_id)
        record = {
            "id": f"cns_{uuid.uuid4().hex[:20]}",
            "source": ({"kind": "chat", **chat_label(source_id, source), **({"by_user": True} if by_user else {})}
                       if source is not None else {"kind": "user"}),
            "source_run_id": None if by_user else run_id,
            "request_id": request_id,
            "target": chat_label(target_id, target_sess),
            "mode": mode,
            "question": question,
            "status": "awaiting_approval" if approval else "queued",
            "answer": None,
            "confident": None,
            "suggest_deep": False,
            "steps": [],
            "usage": None,
            "error": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
            "approval_deadline": now + APPROVAL_TIMEOUT_SECONDS if approval else None,
            "revision": 1,
        }
        consult_id = record["id"]
        self.records[consult_id] = record
        self.finished[consult_id] = asyncio.Event()
        if approval:
            # Ready before the consult is announced: an approval can arrive at once.
            self.decisions[consult_id] = asyncio.get_running_loop().create_future()
        if lease:
            self.leases[consult_id] = now
        self._save(record)
        self._prune(*(sid for sid in (source_id, target_id) if sid))
        task = asyncio.create_task(self._run(consult_id), name=f"chat-consult-{consult_id}")
        self.tasks[consult_id] = task
        task.add_done_callback(lambda done, cid=consult_id: self._task_done(cid, done))
        self._ensure_watchdog()
        if by_user and self.record_question is not None:
            # Only the timeline mark; the consult runs whether or not it lands.
            with suppress(Exception):
                await self.record_question(record)
        self._notice(record)
        return self.public(record)

    def _task_done(self, consult_id: str, task: asyncio.Task) -> None:
        if self.tasks.get(consult_id) is task:
            self.tasks.pop(consult_id, None)
        if consult_id in self.records:
            # Cancelled before it ever ran: end it the way _run would have.
            status, code, message = self.cancel_reasons.pop(consult_id, CANCELLED)
            self.decisions.pop(consult_id, None)
            self._finish(consult_id, status, error=(code, message))
        if not task.cancelled():
            task.exception()

    def _slot(self, target_id: str, mode: str) -> asyncio.Semaphore:
        return self.slots.setdefault((target_id, mode), asyncio.Semaphore(TARGET_SLOTS[mode]))

    async def _run(self, consult_id: str) -> None:
        record = self.records[consult_id]
        mode = record["mode"]
        try:
            if record["status"] == "awaiting_approval":
                decision = self.decisions[consult_id]
                try:
                    # Shielded: an expired wait must not cancel a decision that is arriving.
                    answer = await asyncio.wait_for(asyncio.shield(decision), APPROVAL_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    self._finish(consult_id, "expired", error=("approval_expired",
                                 "nobody approved the deep consult in time"))
                    return
                finally:
                    self.decisions.pop(consult_id, None)
                if answer == "deny":
                    self._finish(consult_id, "denied", error=("denied", "the user declined this deep consult"))
                    return
                self._update(consult_id, status="queued", approval_deadline=None)
            async with self._slot(record["target"]["id"], mode):
                target = self.sessions().get(record["target"]["id"])
                if target is None:
                    raise ConsultError(404, "the asked chat no longer exists")
                self._update(consult_id, status="running", started_at=self.clock())
                prompt = consult_prompt(record, target_busy=isinstance(target.get("active_run"), dict))
                result = await asyncio.wait_for(
                    self.execute(mode, target, prompt, lambda step: self._step(consult_id, step)),
                    RUN_TIMEOUTS[mode],
                )
            answer, confident = split_confidence(str((result or {}).get("answer") or ""))
            if not answer:
                raise ConsultError(502, "the asked chat returned no answer")
            self._finish(consult_id, "completed", answer=clip(answer, MAX_ANSWER_CHARS[mode]),
                         confident=confident, suggest_deep=mode == "basic" and confident is False,
                         usage=(result or {}).get("usage") or None)
        except asyncio.TimeoutError:
            minutes = int(RUN_TIMEOUTS[mode] // 60)
            self._finish(consult_id, "failed", error=("timeout", f"no answer within {minutes} minutes"))
        except asyncio.CancelledError:
            status, code, message = self.cancel_reasons.pop(consult_id, CANCELLED)
            self._finish(consult_id, status, error=(code, message))
            raise
        except ConsultError as exc:
            self._finish(consult_id, "failed", error=("failed", exc.message))
        except Exception as exc:  # noqa: BLE001 - every provider failure ends this consult only
            self._finish(consult_id, "failed", error=("failed", f"the consult failed ({type(exc).__name__})"))

    def _update(self, consult_id: str, **changes: Any) -> None:
        record = self.records.get(consult_id)
        if record is None:
            return
        record.update(changes, updated_at=self.clock())
        record["revision"] += 1
        self._save(record)
        self._notice(record)

    def _step(self, consult_id: str, step: dict[str, str]) -> None:
        record = self.records.get(consult_id)
        if record is None or record["status"] != "running" or len(record["steps"]) >= MAX_STEPS:
            return
        record["steps"].append({"kind": str(step.get("kind") or "other"),
                                "title": clip(str(step.get("title") or ""), 300), "at": self.clock()})
        record["updated_at"] = self.clock()
        record["revision"] += 1
        # Progress is shown live; save and announce it at most once a second.
        if self.clock() - self.last_step_save.get(consult_id, 0.0) >= 1.0:
            self.last_step_save[consult_id] = self.clock()
            self._save(record)
            self._notice(record)

    def _finish(self, consult_id: str, status: str, *, error: tuple[str, str] | None = None,
                **result: Any) -> None:
        record = self.records.pop(consult_id, None)
        if record is None:
            return
        now = self.clock()
        record.update(result, status=status, finished_at=now, updated_at=now, approval_deadline=None,
                      error={"code": error[0], "message": error[1]} if error else None)
        record["revision"] += 1
        self.leases.pop(consult_id, None)
        self.last_step_save.pop(consult_id, None)
        with suppress(sqlite3.Error, OSError):
            self._save(record)
        self._notice(record)
        event = self.finished.pop(consult_id, None)
        if event is not None:
            event.set()

    # MARK: Requests from the asking chat and the user

    async def wait(self, consult_id: str, *, source_id: str, timeout: float = 0.0) -> dict[str, Any]:
        """The consult's current state, waiting up to `timeout` for it to end.
        Each call keeps the asking turn's claim on the consult alive."""
        record = self._load(consult_id)
        if record is None or record["source"].get("id") != source_id:
            raise ConsultError(404, "consult not found")
        if consult_id in self.leases:
            self.leases[consult_id] = self.clock()
        event = self.finished.get(consult_id)
        if event is not None and timeout > 0:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), min(timeout, MAX_WAIT_SECONDS))
        return self.public(self._load(consult_id) or record)

    async def decide(self, consult_id: str, *, source_id: str, decision: str) -> dict[str, Any]:
        """Answer a deep consult's approval: allow, always (this chat), or deny."""
        if decision not in {"allow", "always", "deny"}:
            raise ConsultError(422, "decision must be allow, always or deny")
        record = self.records.get(consult_id)
        if record is None or record["source"].get("id") != source_id:
            raise ConsultError(404, "consult not found")
        future = self.decisions.get(consult_id)
        if record["status"] != "awaiting_approval" or future is None or future.done():
            raise ConsultError(409, "this consult is not waiting for approval")
        if decision == "always" and self.remember_deep_allowed is not None:
            await self.remember_deep_allowed(source_id, record["target"]["id"])
        future.set_result(decision)
        return self.public(record)

    async def cancel(self, consult_id: str, *, session_id: str | None = None,
                     reason: tuple[str, str, str] = CANCELLED) -> dict[str, Any]:
        record = self.records.get(consult_id)
        if record is None:
            stored = self._load(consult_id)
            if stored is None or (session_id and session_id not in (
                    stored["source"].get("id"), stored["target"]["id"])):
                raise ConsultError(404, "consult not found")
            return self.public(stored)
        if session_id and session_id not in (record["source"].get("id"), record["target"]["id"]):
            raise ConsultError(404, "consult not found")
        task = self.tasks.get(consult_id)
        if task is not None and not task.done():
            self.cancel_reasons[consult_id] = reason
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        return self.public(self._load(consult_id) or record)

    def run_ended(self, session_id: str, run_id: str | None) -> None:
        """The asking turn ended (finished or stopped): its consults end with it."""
        if not run_id:
            return
        self.turn_counts.pop(run_id, None)
        for consult_id, record in list(self.records.items()):
            if record["source"].get("id") == session_id and record.get("source_run_id") == run_id:
                self._cancel_soon(consult_id, ("cancelled", "turn_ended", "the asking turn ended"))

    def _cancel_soon(self, consult_id: str, reason: tuple[str, str, str]) -> None:
        task = self.tasks.get(consult_id)
        if task is not None and not task.done():
            self.cancel_reasons[consult_id] = reason
            task.cancel()

    def _ensure_watchdog(self) -> None:
        if self.watchdog is None or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watch_leases(), name="chat-consult-leases")

    async def _watch_leases(self) -> None:
        # A helper that stops polling (its turn was stopped or crashed) gives
        # up the consult; the answer would have no one to receive it.
        while self.records:
            await asyncio.sleep(LEASE_CHECK_SECONDS)
            now = self.clock()
            for consult_id, seen in list(self.leases.items()):
                if now - seen > LEASE_SECONDS:
                    self.leases.pop(consult_id, None)
                    self._cancel_soon(consult_id, ("cancelled", "abandoned", "the asking turn stopped waiting"))

    # MARK: Lists

    def list(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        """Consults this chat asked (outgoing) and was asked (incoming), newest first."""
        db = self._database()
        result = {}
        for direction, column in (("outgoing", "source_id"), ("incoming", "target_id")):
            rows = db.execute(f"SELECT id, record FROM consults WHERE {column} = ? "
                              "ORDER BY created_at DESC LIMIT ?", (session_id, LIST_LIMIT)).fetchall()
            result[direction] = [self.public(self.records.get(cid) or json.loads(raw)) for cid, raw in rows]
        return result

    def active(self) -> list[dict[str, Any]]:
        return [{"id": record["id"], "source_id": record["source"].get("id"),
                 "target_id": record["target"]["id"], "mode": record["mode"], "status": record["status"]}
                for record in self.records.values()]

    @staticmethod
    def public(record: Mapping[str, Any]) -> dict[str, Any]:
        public = json.loads(json.dumps(record))
        started, finished = record.get("started_at"), record.get("finished_at")
        public["duration_seconds"] = round(finished - started, 1) if started and finished else None
        return public

    def _notice(self, record: Mapping[str, Any]) -> None:
        if self.notify is None:
            return
        event = {"type": "chat_consult_updated", "consult_id": record["id"],
                 "status": record["status"], "revision": record["revision"]}
        for session_id in {record["source"].get("id"), record["target"]["id"]} - {None}:
            # Invalidation only: clients reread the list.
            task = asyncio.ensure_future(self.notify(session_id, {**event, "session_id": session_id}))
            self.notices.add(task)
            task.add_done_callback(lambda done: (self.notices.discard(done),
                                                 None if done.cancelled() else done.exception()))

    # MARK: Chats and the server going away

    async def delete_session(self, session_id: str) -> None:
        for consult_id, record in list(self.records.items()):
            if session_id in (record["source"].get("id"), record["target"]["id"]):
                await self.cancel(consult_id, reason=("cancelled", "chat_deleted", "a chat of this consult was deleted"))
        db = self._database()
        with db:
            db.execute("DELETE FROM consults WHERE source_id = ? OR target_id = ?", (session_id, session_id))

    async def close(self) -> None:
        self.stopping = True
        for consult_id in list(self.records):
            self._cancel_soon(consult_id, ("interrupted", "interrupted", "the server stopped while this consult ran"))
        pending = [task for task in self.tasks.values() if not task.done()]
        if self.watchdog is not None:
            self.watchdog.cancel()
            pending.append(self.watchdog)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.notices:
            await asyncio.gather(*self.notices, return_exceptions=True)
        if self._db is not None:
            with closing(self._db):
                pass
            self._db = None


__all__ = [
    "ACTIVE", "ChatConsults", "ConsultError", "DEFAULT_INBOUND", "INBOUND_POLICIES", "MODES",
    "TERMINAL", "TURN_LIMITS", "consult_prompt", "deep_needs_approval", "inbound_policy",
    "same_project", "split_confidence",
]
