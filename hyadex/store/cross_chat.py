"""Durable cross-chat ledger with explicit policy and runtime dependencies.

Connections and transactions remain local to the ledger. Provider runners and
route handlers are not dependencies; existing HTTPException outcomes are retained.
Callers supply their clock, instance
identity, cancellation join and authorization validators.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from hyadex.store import mailbox as chat_mailbox


@dataclass(frozen=True)
class CrossChatBindings:
    now: Callable[[], str]
    join_task: Callable[[asyncio.Task[Any]], Awaitable[Any]]
    route_actions: Callable[[Any], list[str]]
    source_instruction: Callable[[Any], str]
    instance_id: Callable[[], str]
    retention_seconds: Callable[[], int]
    route_id_pattern: re.Pattern[str]
    pair_id_pattern: re.Pattern[str]
    effect_id_pattern: re.Pattern[str]
    exchange_terminal_statuses: frozenset[str]
    leg_terminal_statuses: frozenset[str]


class CrossChatStore:
    """Small durable ledger for exactly-once cross-chat delivery.

    Provider credentials never enter this database.  It contains only the
    immutable routing envelope and its monotonic delivery state.
    """

    TERMINAL_STATUSES = frozenset({"delivered", "failed", "cancelled"})

    def __init__(self, path: Path, bindings: CrossChatBindings) -> None:
        self.path = path
        self._bindings = bindings
        # SQLite fsync/open may stall on external or unhealthy volumes. Keep
        # its serialization in a worker thread so no ledger operation can
        # block the HyadexServer event loop.
        self._thread_lock = threading.RLock()
        self._initialized = False

    def _locked_call(self, callback: Callable[[], Any]) -> Any:
        with self._thread_lock:
            return callback()

    async def _call(self, callback: Callable[[], Any]) -> Any:
        return await asyncio.to_thread(self._locked_call, callback)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        def operation() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS cross_chat_envelopes (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('instruction', 'final_result')),
                        source_session_id TEXT NOT NULL,
                        source_run_id TEXT NOT NULL,
                        target_session_id TEXT NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('instruction', 'final_result')),
                        body TEXT NOT NULL DEFAULT '',
                        source_user_instruction TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL,
                        authorization_kind TEXT NOT NULL DEFAULT 'explicit_prompt',
                        authorization_route_id TEXT,
                        authorization_pair_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        queued_id TEXT,
                        queue_position INTEGER,
                        target_run_id TEXT,
                        result_hash TEXT,
                        error TEXT,
                        lifecycle_status TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(source_run_id, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS cross_chat_source_run
                        ON cross_chat_envelopes(source_run_id, status);
                    CREATE INDEX IF NOT EXISTS cross_chat_target_status
                        ON cross_chat_envelopes(target_session_id, status);

                    CREATE TABLE IF NOT EXISTS cross_chat_exchanges (
                        id TEXT PRIMARY KEY,
                        requester_session_id TEXT NOT NULL,
                        responder_session_id TEXT NOT NULL,
                        authorization_source_run_id TEXT NOT NULL,
                        authorization_kind TEXT NOT NULL DEFAULT 'explicit_prompt',
                        authorization_route_id TEXT,
                        authorization_pair_id TEXT NOT NULL DEFAULT '',
                        reciprocal_route_effect_id TEXT NOT NULL DEFAULT '',
                        reciprocal_route_actions TEXT NOT NULL DEFAULT '',
                        reciprocal_route_state TEXT NOT NULL DEFAULT '',
                        reciprocal_route_id TEXT NOT NULL DEFAULT '',
                        initial_action TEXT NOT NULL DEFAULT 'request_reply'
                            CHECK(initial_action IN ('instruction', 'request_reply')),
                        source_user_instruction TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK(status IN (
                            'waiting_request', 'active', 'completed',
                            'failed', 'cancelled', 'expired'
                        )),
                        max_legs INTEGER NOT NULL CHECK(max_legs > 0 AND max_legs <= 6),
                        used_legs INTEGER NOT NULL DEFAULT 0
                            CHECK(used_legs >= 0 AND used_legs <= max_legs),
                        active_leg_id TEXT,
                        expires_at TEXT NOT NULL,
                        error_code TEXT,
                        error TEXT,
                        lifecycle_status TEXT NOT NULL DEFAULT '',
                        live_response_requested INTEGER NOT NULL DEFAULT 0
                            CHECK(live_response_requested IN (0, 1)),
                        live_response_lease INTEGER NOT NULL DEFAULT 0
                            CHECK(live_response_lease IN (0, 1)),
                        live_response_instance_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK(requester_session_id <> responder_session_id)
                    );
                    CREATE INDEX IF NOT EXISTS cross_chat_exchange_participants
                        ON cross_chat_exchanges(requester_session_id, responder_session_id, status);
                    CREATE INDEX IF NOT EXISTS cross_chat_exchange_expiry
                        ON cross_chat_exchanges(status, expires_at);

                    CREATE TABLE IF NOT EXISTS cross_chat_exchange_legs (
                        id TEXT PRIMARY KEY,
                        exchange_id TEXT NOT NULL,
                        parent_leg_id TEXT,
                        ordinal INTEGER NOT NULL,
                        source_session_id TEXT NOT NULL,
                        source_run_id TEXT NOT NULL,
                        source_owner_run_id TEXT NOT NULL DEFAULT '',
                        target_session_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('request', 'reply', 'status')),
                        expects_reply INTEGER NOT NULL CHECK(expects_reply IN (0, 1)),
                        response_state TEXT NOT NULL CHECK(response_state IN (
                            'open', 'explicit_committed', 'automatic_committed', 'closed'
                        )),
                        body TEXT NOT NULL,
                        body_chars INTEGER NOT NULL,
                        body_sha256 TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'registered', 'submitting', 'queued', 'running',
                            'delivered', 'failed', 'cancelled', 'expired'
                        )),
                        queued_id TEXT,
                        queue_position INTEGER,
                        target_run_id TEXT,
                        lifecycle_status TEXT NOT NULL DEFAULT '',
                        error_code TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(exchange_id) REFERENCES cross_chat_exchanges(id) ON DELETE CASCADE,
                        UNIQUE(exchange_id, ordinal),
                        UNIQUE(parent_leg_id),
                        UNIQUE(exchange_id, source_run_id),
                        UNIQUE(exchange_id, idempotency_key),
                        CHECK(source_session_id <> target_session_id)
                    );
                    CREATE INDEX IF NOT EXISTS cross_chat_exchange_leg_target_status
                        ON cross_chat_exchange_legs(target_session_id, status);
                    CREATE INDEX IF NOT EXISTS cross_chat_exchange_leg_run
                        ON cross_chat_exchange_legs(target_run_id, status);

                    CREATE TABLE IF NOT EXISTS cross_chat_route_rate_events (
                        effect_id TEXT PRIMARY KEY,
                        source_session_id TEXT NOT NULL,
                        target_session_id TEXT NOT NULL,
                        accepted_at_epoch REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS cross_chat_route_rate_source_time
                        ON cross_chat_route_rate_events(
                            source_session_id, accepted_at_epoch
                        );
                    CREATE INDEX IF NOT EXISTS cross_chat_route_rate_target_time
                        ON cross_chat_route_rate_events(
                            target_session_id, accepted_at_epoch
                        );
                    """
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(cross_chat_envelopes)"
                    ).fetchall()
                }
                if "lifecycle_status" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes "
                        "ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT ''"
                    )
                if "queue_position" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes "
                        "ADD COLUMN queue_position INTEGER"
                    )
                if "authorization_kind" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes ADD COLUMN "
                        "authorization_kind TEXT NOT NULL DEFAULT 'explicit_prompt'"
                    )
                if "authorization_route_id" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes ADD COLUMN "
                        "authorization_route_id TEXT"
                    )
                if "source_user_instruction" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes ADD COLUMN "
                        "source_user_instruction TEXT NOT NULL DEFAULT ''"
                    )
                if "source_user_delegation_action" not in columns:
                    # No backfill: old source text or route membership alone
                    # is not proof of an explicit user delegation.
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes ADD COLUMN "
                        "source_user_delegation_action TEXT NOT NULL DEFAULT ''"
                    )
                if "target_body" not in columns:
                    connection.execute("ALTER TABLE cross_chat_envelopes ADD COLUMN target_body TEXT")
                if "message_revision" not in columns:
                    connection.execute("ALTER TABLE cross_chat_envelopes ADD COLUMN message_revision INTEGER NOT NULL DEFAULT 0")
                exchange_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(cross_chat_exchanges)"
                    ).fetchall()
                }
                if "authorization_kind" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "authorization_kind TEXT NOT NULL DEFAULT 'explicit_prompt'"
                    )
                if "authorization_route_id" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "authorization_route_id TEXT"
                    )
                if "authorization_pair_id" not in columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_envelopes ADD COLUMN "
                        "authorization_pair_id TEXT NOT NULL DEFAULT ''"
                    )
                if "authorization_pair_id" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "authorization_pair_id TEXT NOT NULL DEFAULT ''"
                    )
                if "reciprocal_route_effect_id" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "reciprocal_route_effect_id TEXT NOT NULL DEFAULT ''"
                    )
                if "reciprocal_route_actions" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "reciprocal_route_actions TEXT NOT NULL DEFAULT ''"
                    )
                if "reciprocal_route_state" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "reciprocal_route_state TEXT NOT NULL DEFAULT ''"
                    )
                if "reciprocal_route_id" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "reciprocal_route_id TEXT NOT NULL DEFAULT ''"
                    )
                if "initial_action" not in exchange_columns:
                    # Existing exchange rows were all explicit requests for a
                    # response.  Instruction-mode exchanges are additive and
                    # are written only by the newer configured-route path.
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "initial_action TEXT NOT NULL DEFAULT 'request_reply'"
                    )
                if "source_user_instruction" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "source_user_instruction TEXT NOT NULL DEFAULT ''"
                    )
                if "live_response_lease" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "live_response_lease INTEGER NOT NULL DEFAULT 0 "
                        "CHECK(live_response_lease IN (0, 1))"
                    )
                if "live_response_instance_id" not in exchange_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "live_response_instance_id TEXT NOT NULL DEFAULT ''"
                    )
                if "live_response_requested" not in exchange_columns:
                    # Preserve the immutable request shape separately from the
                    # mutable live lease. A restarted live request is resumed
                    # through durable async delivery, but it remains exempt
                    # from elapsed-time expiry and an exact POST replay must
                    # still compare equal to the originally accepted request.
                    connection.execute(
                        "ALTER TABLE cross_chat_exchanges ADD COLUMN "
                        "live_response_requested INTEGER NOT NULL DEFAULT 0 "
                        "CHECK(live_response_requested IN (0, 1))"
                    )
                    connection.execute(
                        "UPDATE cross_chat_exchanges "
                        "SET live_response_requested=live_response_lease"
                    )
                leg_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(cross_chat_exchange_legs)"
                    ).fetchall()
                }
                if "source_owner_run_id" not in leg_columns:
                    connection.execute(
                        "ALTER TABLE cross_chat_exchange_legs ADD COLUMN "
                        "source_owner_run_id TEXT NOT NULL DEFAULT ''"
                    )
                    connection.execute(
                        "UPDATE cross_chat_exchange_legs "
                        "SET source_owner_run_id=source_run_id "
                        "WHERE source_owner_run_id=''"
                    )
                # A short-lived beta interpreted an inline @Chat as an
                # instruction to forward the source prompt verbatim. Current
                # @ mentions are route hints only. Retire any nonterminal
                # envelope created by that UI contract before queue recovery
                # can restore or start its target turn. Configured-route
                # Send/Ask effects and final-result obligations use different
                # authorization/idempotency shapes and are untouched.
                connection.execute(
                    """
                    UPDATE cross_chat_envelopes
                    SET status='failed',
                        error='legacy raw @chat delivery retired during route-hint migration',
                        lifecycle_status='',
                        updated_at=?
                    WHERE kind='instruction'
                      AND authorization_kind='explicit_prompt'
                      AND substr(idempotency_key, 1, 7)='direct:'
                      AND status IN (
                          'waiting_admission', 'ready', 'submitting', 'queued'
                      )
                    """,
                    (self._bindings.now(),),
                )
                connection.execute(
                    "DELETE FROM cross_chat_route_rate_events "
                    "WHERE accepted_at_epoch < ?",
                    (
                        time.time()
                        - self._bindings.retention_seconds(),
                    ),
                )
                mailbox_columns = {str(row["name"]) for row in connection.execute(
                    "PRAGMA table_info(cross_chat_envelopes)"
                )}
                for column, definition in (
                    ("delivery_mode", "TEXT NOT NULL DEFAULT ''"),
                    ("reply_to_message_id", "TEXT"),
                ):
                    if column not in mailbox_columns:
                        connection.execute(f"ALTER TABLE cross_chat_envelopes ADD COLUMN {column} {definition}")
                chat_mailbox.initialize(connection)
        await self._call(operation)
        self._initialized = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def create_final_obligation(
        self,
        *,
        envelope_id: str,
        source_session_id: str,
        source_run_id: str,
        target_session_id: str,
        idempotency_key: str,
        source_user_instruction: str = "",
    ) -> dict[str, Any]:
        source_user_instruction = self._bindings.source_instruction(
            source_user_instruction
        )
        timestamp = self._bindings.now()
        def operation() -> dict[str, Any]:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cross_chat_envelopes
                    (id, kind, source_session_id, source_run_id, target_session_id,
                     action, body, source_user_instruction, idempotency_key,
                     status, created_at, updated_at)
                    VALUES (?, 'final_result', ?, ?, ?, 'final_result', '', ?, ?,
                            'waiting_source', ?, ?)
                    """,
                    (
                        envelope_id,
                        source_session_id,
                        source_run_id,
                        target_session_id,
                        source_user_instruction,
                        idempotency_key,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE source_run_id=? AND idempotency_key=?",
                    (source_run_id, idempotency_key),
                ).fetchone()
            assert row is not None
            return dict(row)
        return await self._call(operation)

    async def create_instruction(
        self,
        *,
        envelope_id: str,
        source_session_id: str,
        source_run_id: str,
        target_session_id: str,
        body: str,
        idempotency_key: str,
        source_user_instruction: str = "",
        source_user_delegation_action: str = "",
        authorization_kind: str = "explicit_prompt",
        authorization_route_id: str | None = None,
        authorization_pair_id: str = "",
        initial_status: str = "ready",
        reply_to_message_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        source_user_instruction = self._bindings.source_instruction(
            source_user_instruction
        )
        if authorization_kind not in {"explicit_prompt", "configured_route"}:
            raise ValueError("invalid cross-chat authorization kind")
        if authorization_kind == "configured_route":
            if not self._bindings.route_id_pattern.fullmatch(
                str(authorization_route_id or "")
            ):
                raise ValueError("configured route authorization requires a route id")
        elif authorization_route_id is not None:
            raise ValueError("explicit prompt authorization cannot carry a route id")
        if authorization_pair_id and (
            authorization_kind != "configured_route"
            or not self._bindings.pair_id_pattern.fullmatch(authorization_pair_id)
        ):
            raise ValueError("invalid permanent pair authorization")
        if initial_status not in {"ready", "waiting_admission", "stored"}:
            raise ValueError("invalid initial cross-chat instruction status")
        if initial_status == "stored" and not authorization_pair_id:
            raise ValueError("mailbox storage requires a permanent pair")
        if source_user_delegation_action and (
            source_user_delegation_action not in {"route", "instruction", "request_reply"}
            or initial_status != "stored" or not source_user_instruction.strip()
        ):
            raise ValueError("user delegation requires an exact source instruction and mailbox message")
        timestamp = self._bindings.now()
        def operation() -> tuple[dict[str, Any], bool]:
            with self._transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE source_run_id=? AND idempotency_key=?",
                    (source_run_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    record = dict(existing)
                    if (
                        record["kind"] != "instruction"
                        or record["target_session_id"] != target_session_id
                        or record["body"] != body
                        or record.get("source_user_instruction", "")
                        != source_user_instruction
                        or record.get("source_user_delegation_action", "")
                        != source_user_delegation_action
                        or record.get("authorization_kind")
                        != authorization_kind
                        or record.get("authorization_route_id")
                        != authorization_route_id
                        or record.get("authorization_pair_id", "") != authorization_pair_id
                        or record.get("reply_to_message_id") != reply_to_message_id
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="idempotency key was already used for a different handoff",
                        )
                    return record, False
                connection.execute(
                    """
                    INSERT INTO cross_chat_envelopes
                    (id, kind, source_session_id, source_run_id, target_session_id,
                     action, body, source_user_instruction, idempotency_key,
                     authorization_kind,
                     authorization_route_id, authorization_pair_id, status, created_at, updated_at)
                    VALUES (?, 'instruction', ?, ?, ?, 'instruction', ?, ?, ?, ?,
                            ?, ?, ?, ?, ?)
                    """,
                    (
                        envelope_id,
                        source_session_id,
                        source_run_id,
                        target_session_id,
                        body,
                        source_user_instruction,
                        idempotency_key,
                        authorization_kind,
                        authorization_route_id,
                        authorization_pair_id,
                        initial_status,
                        timestamp,
                        timestamp,
                    ),
                )
                if initial_status == "stored":
                    connection.execute(
                        "UPDATE cross_chat_envelopes SET delivery_mode='mailbox', reply_to_message_id=?, "
                        "source_user_delegation_action=? WHERE id=?",
                        (reply_to_message_id, source_user_delegation_action, envelope_id),
                    )
                    chat_mailbox.store_message(connection, envelope_id, now=timestamp,
                                               in_reply_to_message_id=reply_to_message_id)
                row = connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE id=?",
                    (envelope_id,),
                ).fetchone()
            assert row is not None
            return dict(row), True
        return await self._call(operation)

    async def pending_mailbox_migration_candidates(self) -> list[dict[str, Any]]:
        """Startup metadata only; bodies remain in the existing envelope ledger."""
        if not self._initialized:
            # Ordinary pre-lifespan queue helpers must not create/open a ledger.
            # Production startup initializes it before opening queue admission.
            return []
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                return [dict(row) for row in connection.execute("""SELECT id,kind,action,
                    source_session_id,target_session_id,authorization_kind,authorization_route_id,
                    authorization_pair_id,status,queued_id,target_run_id,created_at
                    FROM cross_chat_envelopes WHERE kind='instruction'
                    AND authorization_kind='configured_route' AND authorization_pair_id!=''
                    AND delivery_mode='' AND status IN ('ready','submitting','queued')
                    AND target_run_id IS NULL ORDER BY created_at,id""")]
        return await self._call(operation)

    async def migrate_pending_mailbox_message(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        """CAS one history-proven, still-unlaunched pair message into passive storage."""
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute("""UPDATE cross_chat_envelopes
                    SET status='stored',delivery_mode='mailbox',queue_position=NULL,
                        lifecycle_status='mailbox_migration_pending',updated_at=?
                    WHERE id=? AND source_session_id=? AND target_session_id=?
                    AND authorization_kind='configured_route' AND kind='instruction'
                    AND authorization_route_id=? AND authorization_pair_id=?
                    AND status=? AND queued_id IS ? AND target_run_id IS NULL AND delivery_mode=''""",
                    (self._bindings.now(), candidate['id'], candidate['source_session_id'], candidate['target_session_id'],
                     candidate['authorization_route_id'], candidate['authorization_pair_id'],
                     candidate['status'], candidate['queued_id'])).rowcount
                if changed != 1:
                    return None
                chat_mailbox.store_message(connection, candidate['id'], now=(
                    self._bindings.now() if candidate['status'] == 'ready' else candidate['created_at']
                ))
                return dict(connection.execute("SELECT * FROM cross_chat_envelopes WHERE id=?",
                                               (candidate['id'],)).fetchone())
        return await self._call(operation)

    async def mailbox_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """One mailbox transaction on the existing worker-owned ledger."""
        def operation() -> Any:
            with self._transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return getattr(chat_mailbox, name)(connection, *args, **kwargs)
        task = asyncio.create_task(self._call(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._bindings.join_task(task)
            raise

    async def mailbox_envelopes(self, *, pending_only: bool = False, message_id: str = "") -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                where = (
                    " AND e.status='stored' AND e.lifecycle_status != CASE "
                    "WHEN m.excluded_reason='deleted' THEN 'mailbox_deleted' "
                    "WHEN m.excluded_at IS NOT NULL THEN 'mailbox_cancelled' ELSE 'mailbox_received' END"
                ) if pending_only else ""
                if message_id:
                    where += " AND e.id=?"
                return [dict(row) for row in connection.execute(
                    "SELECT e.*, m.read_at, m.stored_at, m.excluded_reason FROM cross_chat_envelopes e "
                    "JOIN chat_mailbox_messages m ON m.message_id=e.id WHERE e.delivery_mode='mailbox'" + where + " LIMIT 100",
                    (message_id,) if message_id else (),
                )]
        return await self._call(operation)

    async def admit_direct_instructions(
        self,
        envelope_ids: list[str],
        *,
        source_run_id: str,
    ) -> list[dict[str, Any]]:
        """Atomically promote one admitted turn's direct messages to ready."""

        normalized_ids = [
            str(envelope_id or "") for envelope_id in envelope_ids
            if str(envelope_id or "")
        ]
        if not normalized_ids:
            return []
        if len(normalized_ids) != len(set(normalized_ids)):
            raise RuntimeError("duplicate direct cross-chat instruction id")
        placeholders = ",".join("?" for _ in normalized_ids)

        def operation() -> list[dict[str, Any]]:
            timestamp = self._bindings.now()
            with self._transaction() as connection:
                rows = connection.execute(
                    f"""
                    SELECT * FROM cross_chat_envelopes
                    WHERE id IN ({placeholders})
                    """,
                    normalized_ids,
                ).fetchall()
                records = {str(row["id"]): dict(row) for row in rows}
                if set(records) != set(normalized_ids):
                    raise RuntimeError("direct cross-chat instruction disappeared before admission")
                for envelope_id in normalized_ids:
                    record = records[envelope_id]
                    if (
                        record.get("kind") != "instruction"
                        or record.get("source_run_id") != source_run_id
                    ):
                        raise RuntimeError("direct cross-chat instruction owner changed before admission")
                    if str(record.get("status") or "") not in {
                        "waiting_admission",
                        "ready",
                        *self.TERMINAL_STATUSES,
                    }:
                        raise RuntimeError("direct cross-chat instruction changed before admission")
                connection.execute(
                    f"""
                    UPDATE cross_chat_envelopes
                    SET status='ready', updated_at=?
                    WHERE id IN ({placeholders}) AND source_run_id=?
                      AND status='waiting_admission'
                    """,
                    [timestamp, *normalized_ids, source_run_id],
                )
                admitted_rows = connection.execute(
                    f"""
                    SELECT * FROM cross_chat_envelopes
                    WHERE id IN ({placeholders})
                    """,
                    normalized_ids,
                ).fetchall()
            admitted = {str(row["id"]): dict(row) for row in admitted_rows}
            return [admitted[envelope_id] for envelope_id in normalized_ids]

        return await self._call(operation)

    async def get(self, envelope_id: str) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE id=?",
                    (envelope_id,),
                ).fetchone())
        return await self._call(operation)

    async def for_source_run(self, run_id: str) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE source_run_id=? ORDER BY created_at, id",
                    (run_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        return await self._call(operation)

    async def edit_exact_queued_message(
        self, *, envelope_id: str, target_session_id: str, queued_id: str,
        pair_id: str, expected_revision: int, body: str,
    ) -> dict[str, Any] | None:
        """Recipient override only; the sender's body and routing stay immutable."""
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """UPDATE cross_chat_envelopes
                       SET target_body=?, message_revision=message_revision+1, updated_at=?
                       WHERE id=? AND target_session_id=? AND queued_id=?
                         AND authorization_kind='configured_route' AND kind='instruction'
                         AND authorization_pair_id=? AND status='queued'
                         AND target_run_id IS NULL AND message_revision=?""",
                    (body, self._bindings.now(), envelope_id, target_session_id, queued_id, pair_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    return None
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE id=?", (envelope_id,),
                ).fetchone())
        return await self._call(operation)

    async def rebind_source_run(
        self,
        envelope_ids: list[str],
        *,
        old_source_run_id: str,
        new_source_run_id: str,
    ) -> None:
        if not envelope_ids:
            return
        placeholders = ",".join("?" for _ in envelope_ids)
        def operation() -> None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE cross_chat_envelopes
                    SET source_run_id=?, updated_at=?
                    WHERE source_run_id=? AND id IN ({placeholders})
                      AND status='waiting_source'
                    """,
                    [new_source_run_id, self._bindings.now(), old_source_run_id, *envelope_ids],
                )
                if int(cursor.rowcount or 0) != len(envelope_ids):
                    raise RuntimeError(
                        "cross-chat obligations changed before source run binding"
                    )
        await self._call(operation)

    async def create_exchange_obligation(
        self,
        *,
        exchange_id: str,
        requester_session_id: str,
        authorization_source_run_id: str,
        responder_session_id: str,
        max_legs: int,
        expires_at: str,
        source_user_instruction: str = "",
    ) -> dict[str, Any]:
        source_user_instruction = self._bindings.source_instruction(
            source_user_instruction
        )
        timestamp = self._bindings.now()

        def operation() -> dict[str, Any]:
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if existing is not None:
                    record = dict(existing)
                    if (
                        record.get("requester_session_id") != requester_session_id
                        or record.get("authorization_source_run_id") != authorization_source_run_id
                        or record.get("responder_session_id") != responder_session_id
                        or record.get("source_user_instruction", "")
                        != source_user_instruction
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="exchange id is already bound to another authorization",
                        )
                    return record
                connection.execute(
                    """
                    INSERT INTO cross_chat_exchanges
                    (id, requester_session_id, responder_session_id,
                     authorization_source_run_id, source_user_instruction, status,
                     max_legs, used_legs, active_leg_id, expires_at, created_at,
                     updated_at)
                    VALUES (?, ?, ?, ?, ?, 'waiting_request', ?, 0, NULL, ?, ?, ?)
                    """,
                    (
                        exchange_id,
                        requester_session_id,
                        responder_session_id,
                        authorization_source_run_id,
                        source_user_instruction,
                        max_legs,
                        expires_at,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
            assert row is not None
            return dict(row)

        return await self._call(operation)

    async def create_route_exchange_request(
        self,
        *,
        exchange_id: str,
        leg_id: str,
        requester_session_id: str,
        authorization_source_run_id: str,
        responder_session_id: str,
        body: str,
        idempotency_key: str,
        max_legs: int,
        expires_at: str,
        authorization_route_id: str,
        authorization_pair_id: str = "",
        reciprocal_route_effect_id: str = "",
        reciprocal_route_actions: list[str] | None = None,
        initial_action: str = "request_reply",
        source_user_instruction: str = "",
        live_response_lease: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically create a lazy configured-route exchange and first leg."""

        source_user_instruction = self._bindings.source_instruction(
            source_user_instruction
        )
        if not self._bindings.route_id_pattern.fullmatch(authorization_route_id):
            raise ValueError("configured route exchange requires a route id")
        if authorization_pair_id and not self._bindings.pair_id_pattern.fullmatch(authorization_pair_id):
            raise ValueError("invalid permanent pair authorization")
        if initial_action not in {"instruction", "request_reply"}:
            raise ValueError("configured route exchange has an invalid initial action")
        frozen_reciprocal_actions = (
            self._bindings.route_actions(
                reciprocal_route_actions
            )
            if reciprocal_route_actions
            else []
        )
        if bool(reciprocal_route_effect_id) != bool(
            frozen_reciprocal_actions
        ) or (
            reciprocal_route_effect_id
            and (
                reciprocal_route_effect_id != exchange_id
                or not self._bindings.effect_id_pattern.fullmatch(
                    reciprocal_route_effect_id
                )
            )
        ):
            raise ValueError("configured route reciprocal effect is invalid")
        reciprocal_actions_value = ",".join(frozen_reciprocal_actions)
        reciprocal_state = "pending" if reciprocal_route_effect_id else ""

        timestamp = self._bindings.now()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        def operation() -> tuple[dict[str, Any], dict[str, Any], bool]:
            with self._transaction() as connection:
                existing_exchange_row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if existing_exchange_row is not None:
                    exchange = dict(existing_exchange_row)
                    existing_leg_row = connection.execute(
                        "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                        (leg_id,),
                    ).fetchone()
                    if existing_leg_row is None:
                        raise HTTPException(
                            status_code=409,
                            detail="route exchange is missing its initial request",
                        )
                    leg = dict(existing_leg_row)
                    if (
                        exchange.get("requester_session_id")
                        != requester_session_id
                        or exchange.get("authorization_source_run_id")
                        != authorization_source_run_id
                        or exchange.get("responder_session_id")
                        != responder_session_id
                        or int(exchange.get("max_legs") or 0) != max_legs
                        or exchange.get("expires_at") != expires_at
                        or exchange.get("authorization_kind")
                        != "configured_route"
                        or exchange.get("authorization_route_id")
                        != authorization_route_id
                        or exchange.get("authorization_pair_id", "") != authorization_pair_id
                        or exchange.get("reciprocal_route_effect_id", "")
                        != reciprocal_route_effect_id
                        or exchange.get("reciprocal_route_actions", "")
                        != reciprocal_actions_value
                        or exchange.get("reciprocal_route_state", "")
                        not in {reciprocal_state, "applied"}
                        or exchange.get("initial_action") != initial_action
                        or exchange.get("source_user_instruction", "")
                        != source_user_instruction
                        or bool(exchange.get("live_response_requested"))
                        != bool(live_response_lease)
                        or leg.get("exchange_id") != exchange_id
                        or int(leg.get("ordinal") or 0) != 1
                        or leg.get("source_session_id") != requester_session_id
                        or leg.get("source_run_id")
                        != authorization_source_run_id
                        or leg.get("target_session_id") != responder_session_id
                        or leg.get("body") != body
                        or leg.get("idempotency_key") != idempotency_key
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "idempotency key was already used for a "
                                "different route exchange request"
                            ),
                        )
                    return exchange, leg, False

                connection.execute(
                    """
                    INSERT INTO cross_chat_exchanges
                    (id, requester_session_id, responder_session_id,
                     authorization_source_run_id, authorization_kind,
                     authorization_route_id, authorization_pair_id, reciprocal_route_effect_id,
                     reciprocal_route_actions, reciprocal_route_state,
                     reciprocal_route_id, initial_action,
                     source_user_instruction, live_response_requested,
                     live_response_lease,
                     live_response_instance_id, status,
                     max_legs, used_legs,
                     active_leg_id, expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'configured_route', ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, 'active',
                            ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        exchange_id,
                        requester_session_id,
                        responder_session_id,
                        authorization_source_run_id,
                        authorization_route_id,
                        authorization_pair_id,
                        reciprocal_route_effect_id,
                        reciprocal_actions_value,
                        reciprocal_state,
                        initial_action,
                        source_user_instruction,
                        1 if live_response_lease else 0,
                        1 if live_response_lease else 0,
                        self._bindings.instance_id() if live_response_lease else "",
                        max_legs,
                        leg_id,
                        expires_at,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO cross_chat_exchange_legs
                    (id, exchange_id, parent_leg_id, ordinal, source_session_id,
                     source_run_id, source_owner_run_id, target_session_id, kind, expects_reply,
                     response_state, body, body_chars, body_sha256,
                     idempotency_key, status, created_at, updated_at)
                    VALUES (?, ?, NULL, 1, ?, ?, ?, ?, 'request', ?, 'open',
                            ?, ?, ?, ?, 'registered', ?, ?)
                    """,
                    (
                        leg_id,
                        exchange_id,
                        requester_session_id,
                        authorization_source_run_id,
                        authorization_source_run_id,
                        responder_session_id,
                        1 if initial_action == "request_reply" else 0,
                        body,
                        len(body),
                        body_hash,
                        idempotency_key,
                        timestamp,
                        timestamp,
                    ),
                )
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())
                leg = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())
            return exchange, leg, True

        return await self._call(operation)

    async def rebind_exchange_source_run(
        self,
        exchange_ids: list[str],
        *,
        old_source_run_id: str,
        new_source_run_id: str,
    ) -> None:
        if not exchange_ids:
            return
        placeholders = ",".join("?" for _ in exchange_ids)

        def operation() -> None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE cross_chat_exchanges
                    SET authorization_source_run_id=?, updated_at=?
                    WHERE authorization_source_run_id=?
                      AND id IN ({placeholders})
                      AND status='waiting_request'
                    """,
                    [new_source_run_id, self._bindings.now(), old_source_run_id, *exchange_ids],
                )
                if int(cursor.rowcount or 0) != len(exchange_ids):
                    raise RuntimeError(
                        "cross-chat exchanges changed before source run binding"
                    )

        await self._call(operation)

    async def get_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())

        return await self._call(operation)

    async def get_exchange_first_leg(
        self,
        exchange_id: str,
    ) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                return self._row(connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE exchange_id=? AND ordinal=1
                    ORDER BY created_at, id LIMIT 1
                    """,
                    (exchange_id,),
                ).fetchone())

        return await self._call(operation)

    async def mark_reciprocal_route_applied(
        self,
        exchange_id: str,
        *,
        effect_id: str,
        route_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Monotonically settle one exact auto-reciprocal route effect."""

        if (
            effect_id != exchange_id
            or not self._bindings.effect_id_pattern.fullmatch(
                effect_id
            )
            or not self._bindings.route_id_pattern.fullmatch(route_id)
        ):
            raise ValueError("invalid reciprocal route settlement identity")

        def operation() -> tuple[dict[str, Any], bool]:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(
                        status_code=410,
                        detail="reciprocal route exchange no longer exists",
                    )
                current = dict(row)
                if (
                    current.get("reciprocal_route_effect_id", "")
                    != effect_id
                    or current.get("reciprocal_route_state", "")
                    not in {"pending", "applied"}
                    or (
                        current.get("reciprocal_route_state") == "applied"
                        and current.get("reciprocal_route_id") != route_id
                    )
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="reciprocal route settlement no longer matches",
                    )
                if current.get("reciprocal_route_state") == "applied":
                    return current, False
                connection.execute(
                    """
                    UPDATE cross_chat_exchanges
                    SET reciprocal_route_state='applied',
                        reciprocal_route_id=?, updated_at=?
                    WHERE id=? AND reciprocal_route_effect_id=?
                      AND reciprocal_route_state='pending'
                    """,
                    (route_id, self._bindings.now(), exchange_id, effect_id),
                )
                applied_row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                assert applied_row is not None
                applied = dict(applied_row)
                if (
                    applied.get("reciprocal_route_state") != "applied"
                    or applied.get("reciprocal_route_id") != route_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="reciprocal route settlement lost its owner",
                    )
                return applied, True

        return await self._call(operation)

    async def pending_reciprocal_route_effects(
        self,
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Return immutable exchange/first-leg pairs awaiting settlement."""

        def operation(
        ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE reciprocal_route_effect_id != ''
                      AND reciprocal_route_state = 'pending'
                    ORDER BY created_at, id
                    """
                ).fetchall()
                effects: list[
                    tuple[dict[str, Any], dict[str, Any] | None]
                ] = []
                for row in rows:
                    exchange = dict(row)
                    leg_row = connection.execute(
                        """
                        SELECT * FROM cross_chat_exchange_legs
                        WHERE exchange_id=? AND ordinal=1
                        ORDER BY created_at, id LIMIT 1
                        """,
                        (exchange["id"],),
                    ).fetchone()
                    effects.append((
                        exchange,
                        dict(leg_row) if leg_row is not None else None,
                    ))
                return effects

        return await self._call(operation)

    async def downgrade_live_exchange(
        self,
        exchange_id: str,
        *,
        active_leg_id: str,
        expected_instance_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically turn a lost synchronous wait into durable async delivery."""

        timestamp = self._bindings.now()

        def operation() -> tuple[dict[str, Any] | None, bool]:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE cross_chat_exchanges
                    SET live_response_lease=0, live_response_instance_id='',
                        updated_at=?
                    WHERE id=? AND status='active' AND active_leg_id=?
                      AND live_response_lease=1
                      AND live_response_instance_id=?
                    """,
                    (
                        timestamp,
                        exchange_id,
                        active_leg_id,
                        expected_instance_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                return self._row(row), int(cursor.rowcount or 0) == 1

        return await self._call(operation)

    async def get_exchange_leg(self, leg_id: str) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())

        return await self._call(operation)

    async def exchange_legs(self, exchange_id: str) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE exchange_id=? ORDER BY ordinal, created_at, id
                    """,
                    (exchange_id,),
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def exchanges_for_authorization_run(
        self,
        run_id: str,
    ) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE authorization_source_run_id=? ORDER BY created_at, id
                    """,
                    (run_id,),
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def create_initial_exchange_leg(
        self,
        *,
        exchange_id: str,
        source_session_id: str,
        source_run_id: str,
        target_session_id: str,
        body: str,
        idempotency_key: str,
        live_response_lease: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        timestamp = self._bindings.now()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        def operation() -> tuple[dict[str, Any], dict[str, Any], bool] | None:
            with self._transaction() as connection:
                exchange_row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if exchange_row is None:
                    raise HTTPException(status_code=404, detail="cross-chat exchange was not found")
                exchange = dict(exchange_row)
                existing = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE exchange_id=? AND source_run_id=?
                    """,
                    (exchange_id, source_run_id),
                ).fetchone()
                if existing is not None:
                    leg = dict(existing)
                    if (
                        int(leg.get("ordinal") or 0) != 1
                        or leg.get("source_session_id") != source_session_id
                        or leg.get("target_session_id") != target_session_id
                        or leg.get("body") != body
                        or leg.get("idempotency_key") != idempotency_key
                        or bool(exchange.get("live_response_requested"))
                        != bool(live_response_lease)
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="idempotency key was already used for a different exchange request",
                        )
                    return exchange, leg, False
                if exchange.get("status") != "waiting_request":
                    raise HTTPException(status_code=409, detail="exchange request was already committed")
                if exchange.get("authorization_source_run_id") != source_run_id:
                    raise HTTPException(status_code=403, detail="exchange is bound to another source run")
                if (
                    exchange.get("requester_session_id") != source_session_id
                    or exchange.get("responder_session_id") != target_session_id
                ):
                    raise HTTPException(status_code=403, detail="exchange participants do not match the grant")
                if str(exchange.get("expires_at") or "") <= timestamp:
                    connection.execute(
                        """
                        UPDATE cross_chat_exchanges
                        SET status='expired', error_code='expired',
                            error='exchange authorization expired before it was sent',
                            lifecycle_status='', updated_at=?
                        WHERE id=? AND status='waiting_request'
                        """,
                        (timestamp, exchange_id),
                    )
                    # Return normally so the transaction commits. The async
                    # boundary below converts this sentinel to the public 410.
                    return None
                leg_id = "leg_" + uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO cross_chat_exchange_legs
                    (id, exchange_id, parent_leg_id, ordinal, source_session_id,
                     source_run_id, source_owner_run_id, target_session_id, kind, expects_reply,
                     response_state, body, body_chars, body_sha256,
                     idempotency_key, status, created_at, updated_at)
                    VALUES (?, ?, NULL, 1, ?, ?, ?, ?, 'request', 1, 'open',
                            ?, ?, ?, ?, 'registered', ?, ?)
                    """,
                    (
                        leg_id,
                        exchange_id,
                        source_session_id,
                        source_run_id,
                        source_run_id,
                        target_session_id,
                        body,
                        len(body),
                        body_hash,
                        idempotency_key,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE cross_chat_exchanges
                    SET status='active', used_legs=1, active_leg_id=?,
                        live_response_requested=?,
                        live_response_lease=?,
                        live_response_instance_id=?,
                        error_code=NULL, error=NULL, updated_at=?
                    WHERE id=? AND status='waiting_request'
                    """,
                    (
                        leg_id,
                        1 if live_response_lease else 0,
                        1 if live_response_lease else 0,
                        self._bindings.instance_id() if live_response_lease else "",
                        timestamp,
                        exchange_id,
                    ),
                )
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())
                leg = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())
            return exchange, leg, True

        result = await self._call(operation)
        if result is None:
            raise HTTPException(status_code=410, detail="cross-chat exchange expired")
        return result

    async def commit_exchange_response(
        self,
        *,
        exchange_id: str,
        inbound_leg_id: str,
        source_session_id: str,
        source_run_id: str,
        body: str,
        request_response: bool,
        idempotency_key: str,
        automatic: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        timestamp = self._bindings.now()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        committed_state = "automatic_committed" if automatic else "explicit_committed"

        def operation() -> tuple[dict[str, Any], dict[str, Any], bool] | None:
            with self._transaction() as connection:
                exchange_row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                inbound_row = connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=? AND exchange_id=?",
                    (inbound_leg_id, exchange_id),
                ).fetchone()
                if exchange_row is None or inbound_row is None:
                    raise HTTPException(status_code=404, detail="cross-chat exchange leg was not found")
                exchange = dict(exchange_row)
                inbound = dict(inbound_row)
                existing_row = connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE parent_leg_id=?",
                    (inbound_leg_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = dict(existing_row)
                    expected_kind = "request" if request_response else "reply"
                    if (
                        str(existing.get("source_owner_run_id") or existing.get("source_run_id") or "")
                        == source_run_id
                        and existing.get("source_session_id") == source_session_id
                        and existing.get("body") == body
                        and existing.get("kind") == expected_kind
                        and existing.get("idempotency_key") == idempotency_key
                    ):
                        return exchange, existing, False
                    raise HTTPException(
                        status_code=409,
                        detail="response_already_committed",
                    )
                if exchange.get("status") != "active":
                    detail = "expired" if exchange.get("status") == "expired" else "response_already_committed"
                    raise HTTPException(status_code=410 if detail == "expired" else 409, detail=detail)
                if (
                    str(exchange.get("expires_at") or "") <= timestamp
                    and not bool(exchange.get("live_response_requested"))
                ):
                    connection.execute(
                        """
                        UPDATE cross_chat_exchanges
                        SET status='expired', error_code='expired', error='exchange expired',
                            lifecycle_status='', updated_at=?
                        WHERE id=? AND status='active'
                        """,
                        (timestamp, exchange_id),
                    )
                    return None
                if inbound.get("response_state") != "open":
                    raise HTTPException(status_code=409, detail="response_already_committed")
                if automatic and not bool(inbound.get("expects_reply")):
                    raise HTTPException(status_code=409, detail="response_already_committed")
                if (
                    inbound.get("target_session_id") != source_session_id
                    or inbound.get("target_run_id") != source_run_id
                    or inbound.get("status") not in {"running", "delivered"}
                ):
                    raise HTTPException(status_code=403, detail="response is not bound to this live delivery run")
                participants = {
                    str(exchange.get("requester_session_id") or ""),
                    str(exchange.get("responder_session_id") or ""),
                }
                expected_target = str(inbound.get("source_session_id") or "")
                if (
                    {source_session_id, expected_target} != participants
                    or expected_target == source_session_id
                ):
                    raise HTTPException(status_code=403, detail="exchange participant pair is invalid")
                used_legs = int(exchange.get("used_legs") or 0)
                max_legs = int(exchange.get("max_legs") or 0)
                required_slots = 2 if request_response else 1
                if max_legs - used_legs < required_slots:
                    raise HTTPException(status_code=409, detail="budget_exhausted")
                next_ordinal = used_legs + 1
                leg_id = "leg_" + uuid.uuid4().hex
                durable_source_run_id = (
                    f"{source_run_id}:live:{next_ordinal}"
                    if bool(exchange.get("live_response_lease"))
                    else source_run_id
                )
                cursor = connection.execute(
                    """
                    UPDATE cross_chat_exchange_legs
                    SET response_state=?, updated_at=?
                    WHERE id=? AND response_state='open'
                    """,
                    (committed_state, timestamp, inbound_leg_id),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise HTTPException(status_code=409, detail="response_already_committed")
                connection.execute(
                    """
                    INSERT INTO cross_chat_exchange_legs
                    (id, exchange_id, parent_leg_id, ordinal, source_session_id,
                     source_run_id, source_owner_run_id, target_session_id, kind, expects_reply,
                     response_state, body, body_chars, body_sha256,
                     idempotency_key, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'registered', ?, ?)
                    """,
                    (
                        leg_id,
                        exchange_id,
                        inbound_leg_id,
                        next_ordinal,
                        source_session_id,
                        durable_source_run_id,
                        source_run_id,
                        expected_target,
                        "request" if request_response else "reply",
                        1 if request_response else 0,
                        "open",
                        body,
                        len(body),
                        body_hash,
                        idempotency_key,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE cross_chat_exchanges
                    SET used_legs=?, active_leg_id=?, updated_at=?
                    WHERE id=? AND status='active'
                    """,
                    (next_ordinal, leg_id, timestamp, exchange_id),
                )
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())
                leg = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())
            return exchange, leg, True

        result = await self._call(operation)
        if result is None:
            raise HTTPException(status_code=410, detail="expired")
        return result

    async def update_exchange_leg(
        self,
        leg_id: str,
        *,
        expected: set[str] | None = None,
        **changes: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "status", "queued_id", "queue_position", "target_run_id",
            "response_state", "lifecycle_status", "error_code", "error",
        }
        patch = {key: value for key, value in changes.items() if key in allowed}
        if not patch:
            return await self.get_exchange_leg(leg_id)
        if (
            patch.get("status") in self._bindings.leg_terminal_statuses
            and "lifecycle_status" not in patch
        ):
            patch["lifecycle_status"] = ""
        patch["updated_at"] = self._bindings.now()
        assignments = ", ".join(f"{key}=?" for key in patch)
        values = list(patch.values())
        where = "id=?"
        values.append(leg_id)
        if expected:
            placeholders = ",".join("?" for _ in expected)
            where += f" AND status IN ({placeholders})"
            values.extend(sorted(expected))

        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    f"UPDATE cross_chat_exchange_legs SET {assignments} WHERE {where}",
                    values,
                )
                if int(cursor.rowcount or 0) == 0:
                    return None
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())

        return await self._call(operation)

    async def update_exchange(
        self,
        exchange_id: str,
        *,
        expected: set[str] | None = None,
        **changes: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "status", "used_legs", "active_leg_id", "lifecycle_status",
            "error_code", "error",
        }
        patch = {key: value for key, value in changes.items() if key in allowed}
        if not patch:
            return await self.get_exchange(exchange_id)
        if (
            patch.get("status") in self._bindings.exchange_terminal_statuses
            and "lifecycle_status" not in patch
        ):
            patch["lifecycle_status"] = ""
        patch["updated_at"] = self._bindings.now()
        assignments = ", ".join(f"{key}=?" for key in patch)
        values = list(patch.values())
        where = "id=?"
        values.append(exchange_id)
        if expected:
            placeholders = ",".join("?" for _ in expected)
            where += f" AND status IN ({placeholders})"
            values.extend(sorted(expected))

        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    f"UPDATE cross_chat_exchanges SET {assignments} WHERE {where}",
                    values,
                )
                if int(cursor.rowcount or 0) == 0:
                    return None
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())

        return await self._call(operation)

    async def cancel_exact_queued_exchange_leg(
        self,
        *,
        exchange_id: str,
        leg_id: str,
        target_session_id: str,
        queued_id: str,
        error_code: str,
        error: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Cancel only the exchange leg that still owns an exact queue row."""

        timestamp = self._bindings.now()

        def operation() -> tuple[dict[str, Any], dict[str, Any]] | None:
            with self._transaction() as connection:
                leg_row = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE id=? AND exchange_id=? AND target_session_id=?
                      AND queued_id=? AND status='queued'
                    """,
                    (leg_id, exchange_id, target_session_id, queued_id),
                ).fetchone()
                if leg_row is None:
                    return None
                leg = dict(leg_row)
                exchange_row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if exchange_row is None:
                    return None
                exchange = dict(exchange_row)
                if (
                    leg.get("kind") != "status"
                    and exchange.get("status") == "active"
                    and str(exchange.get("active_leg_id") or "") != leg_id
                ):
                    return None
                cursor = connection.execute(
                    """
                    UPDATE cross_chat_exchange_legs
                    SET status='cancelled', response_state='closed',
                        error_code=?, error=?, lifecycle_status='', updated_at=?
                    WHERE id=? AND exchange_id=? AND target_session_id=?
                      AND queued_id=? AND status='queued'
                    """,
                    (
                        error_code,
                        error,
                        timestamp,
                        leg_id,
                        exchange_id,
                        target_session_id,
                        queued_id,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    return None
                if leg.get("kind") != "status" and exchange.get("status") == "active":
                    connection.execute(
                        """
                        UPDATE cross_chat_exchanges
                        SET status='cancelled', error_code=?, error=?,
                            lifecycle_status='', updated_at=?
                        WHERE id=? AND status='active' AND active_leg_id=?
                        """,
                        (error_code, error, timestamp, exchange_id, leg_id),
                    )
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())
                leg = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())
                return exchange, leg

        return await self._call(operation)

    async def cancel_exchange(
        self,
        exchange_id: str,
        *,
        status: Literal["failed", "cancelled", "expired"] = "cancelled",
        error_code: str = "cancelled_by_user",
        error: str = "exchange was cancelled by the user",
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        """Atomically close routing and the latest outbound leg."""

        timestamp = self._bindings.now()

        def operation() -> tuple[dict[str, Any], dict[str, Any] | None] | None:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone()
                if row is None:
                    return None
                exchange = dict(row)
                active_leg_id = str(exchange.get("active_leg_id") or "")
                leg_row = (
                    connection.execute(
                        "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                        (active_leg_id,),
                    ).fetchone()
                    if active_leg_id
                    else None
                )
                if exchange.get("status") not in self._bindings.exchange_terminal_statuses:
                    connection.execute(
                        """
                        UPDATE cross_chat_exchanges
                        SET status=?, error_code=?, error=?,
                            lifecycle_status='', updated_at=?
                        WHERE id=? AND status IN ('waiting_request','active')
                        """,
                        (status, error_code, error, timestamp, exchange_id),
                    )
                    if leg_row is not None:
                        leg = dict(leg_row)
                        if leg.get("status") in {"registered", "submitting", "queued"}:
                            connection.execute(
                                """
                                UPDATE cross_chat_exchange_legs
                                SET status=?, response_state='closed',
                                    error_code=?, error=?, lifecycle_status='', updated_at=?
                                WHERE id=? AND status IN ('registered','submitting','queued')
                                """,
                                (status, error_code, error, timestamp, active_leg_id),
                            )
                        elif leg.get("status") == "running":
                            connection.execute(
                                """
                                UPDATE cross_chat_exchange_legs
                                SET response_state='closed', updated_at=?
                                WHERE id=? AND status='running'
                                """,
                                (timestamp, active_leg_id),
                            )
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (exchange_id,),
                ).fetchone())
                current_leg_row = (
                    connection.execute(
                        "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                        (str(exchange.get("active_leg_id") or ""),),
                    ).fetchone()
                    if exchange.get("active_leg_id")
                    else None
                )
                return exchange, self._row(current_leg_row)

        return await self._call(operation)

    async def finish_exchange_leg(
        self,
        leg_id: str,
        *,
        status: Literal["delivered", "failed", "cancelled", "expired"],
        error_code: str | None = None,
        error: str | None = None,
        preserve_committed_response: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        timestamp = self._bindings.now()

        def operation() -> tuple[dict[str, Any], dict[str, Any]] | None:
            with self._transaction() as connection:
                leg_row = connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone()
                if leg_row is None:
                    return None
                leg = dict(leg_row)
                if leg.get("status") in self._bindings.leg_terminal_statuses:
                    exchange_row = connection.execute(
                        "SELECT * FROM cross_chat_exchanges WHERE id=?",
                        (leg["exchange_id"],),
                    ).fetchone()
                    return (dict(exchange_row), leg) if exchange_row is not None else None
                effective_status = status
                effective_error_code = error_code
                effective_error = error
                if (
                    preserve_committed_response
                    and status == "failed"
                    and leg.get("response_state") in {
                        "explicit_committed", "automatic_committed",
                    }
                ):
                    # Response commit is the atomic continuation boundary.
                    # A later failed/stopped parent terminal cannot kill the
                    # already-created child leg or regress the exchange.
                    effective_status = "delivered"
                    effective_error_code = None
                    effective_error = None
                connection.execute(
                    """
                    UPDATE cross_chat_exchange_legs
                    SET status=?, error_code=?, error=?, lifecycle_status='', updated_at=?
                    WHERE id=? AND status IN ('registered','submitting','queued','running')
                    """,
                    (
                        effective_status,
                        effective_error_code,
                        effective_error,
                        timestamp,
                        leg_id,
                    ),
                )
                leg = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone())
                exchange = dict(connection.execute(
                    "SELECT * FROM cross_chat_exchanges WHERE id=?",
                    (leg["exchange_id"],),
                ).fetchone())
                if leg.get("kind") != "status" and exchange.get("status") == "active":
                    exchange_status: str | None = None
                    exchange_error_code = error_code
                    exchange_error = error
                    if effective_status in {"failed", "cancelled", "expired"}:
                        exchange_status = effective_status
                    elif (
                        effective_status == "delivered"
                        and leg.get("response_state") == "open"
                        and not bool(leg.get("expects_reply"))
                    ):
                        connection.execute(
                            """
                            UPDATE cross_chat_exchange_legs
                            SET response_state='closed', updated_at=? WHERE id=?
                            """,
                            (timestamp, leg_id),
                        )
                        leg["response_state"] = "closed"
                        exchange_status = "completed"
                        exchange_error_code = None
                        exchange_error = None
                    if exchange_status is not None:
                        connection.execute(
                            """
                            UPDATE cross_chat_exchanges
                            SET status=?, error_code=?, error=?, lifecycle_status='',
                                updated_at=?
                            WHERE id=? AND status='active'
                            """,
                            (
                                exchange_status,
                                exchange_error_code,
                                exchange_error,
                                timestamp,
                                exchange["id"],
                            ),
                        )
                        exchange = dict(connection.execute(
                            "SELECT * FROM cross_chat_exchanges WHERE id=?",
                            (exchange["id"],),
                        ).fetchone())
            return exchange, leg

        return await self._call(operation)

    async def pending_exchange_legs(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE status IN ('registered','submitting','queued','running')
                    ORDER BY created_at, id
                    """
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def recoverable_active_exchange_legs(self) -> list[dict[str, Any]]:
        """Return the durable active-leg snapshot for every active exchange.

        This deliberately includes terminal legs.  A process can stop after
        committing a leg outcome but before committing its automatic child or
        exchange terminal state, and ordinary pending-leg recovery excludes
        those terminal rows.
        """

        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT legs.*
                    FROM cross_chat_exchanges AS exchanges
                    JOIN cross_chat_exchange_legs AS legs
                      ON legs.id = exchanges.active_leg_id
                    WHERE exchanges.status='active'
                    ORDER BY exchanges.created_at, exchanges.id
                    """
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def terminal_exchange_failures_missing_status(self) -> list[dict[str, Any]]:
        """Find terminal failures whose durable status-wake obligation is absent."""

        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT exchanges.*
                    FROM cross_chat_exchanges AS exchanges
                    WHERE exchanges.status IN ('failed','expired')
                      AND exchanges.active_leg_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM cross_chat_exchange_legs AS status_leg
                          WHERE status_leg.exchange_id = exchanges.id
                            AND status_leg.kind='status'
                      )
                    ORDER BY exchanges.updated_at, exchanges.id
                    """
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def terminal_exchange_legs_with_queue_owner(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE status IN ('delivered','failed','cancelled','expired')
                      AND queued_id IS NOT NULL
                    ORDER BY updated_at, id
                    """
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def pending_exchange_lifecycle(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        def operation() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            with self._transaction() as connection:
                exchanges = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE status IN ('completed','failed','cancelled','expired')
                      AND lifecycle_status != status
                    ORDER BY created_at, id
                    """
                ).fetchall()
                legs = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE status IN ('delivered','failed','cancelled','expired')
                      AND lifecycle_status != status
                    ORDER BY created_at, id
                    """
                ).fetchall()
            return [dict(row) for row in exchanges], [dict(row) for row in legs]

        return await self._call(operation)

    async def nonterminal_exchanges_for_session(self, session_id: str) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE (requester_session_id=? OR responder_session_id=?)
                      AND status IN ('waiting_request','active')
                    ORDER BY created_at, id
                    """,
                    (session_id, session_id),
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def recoverable_exchanges(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE status IN ('waiting_request','active')
                    ORDER BY created_at, id
                    """
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def expirable_exchanges(self, timestamp: str) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchanges
                    WHERE status IN ('waiting_request','active')
                      AND live_response_requested=0 AND expires_at <= ?
                    ORDER BY expires_at, id
                    """,
                    (timestamp,),
                ).fetchall()
            return [dict(row) for row in rows]

        return await self._call(operation)

    async def create_exchange_status_leg(
        self,
        *,
        exchange_id: str,
        source_session_id: str,
        target_session_id: str,
        body: str,
        error_code: str,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = self._bindings.now()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        def operation() -> tuple[dict[str, Any], bool]:
            with self._transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT * FROM cross_chat_exchange_legs
                    WHERE exchange_id=? AND kind='status'
                    """,
                    (exchange_id,),
                ).fetchone()
                if existing is not None:
                    return dict(existing), False
                leg_id = "leg_" + uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO cross_chat_exchange_legs
                    (id, exchange_id, parent_leg_id, ordinal, source_session_id,
                     source_run_id, source_owner_run_id, target_session_id, kind, expects_reply,
                     response_state, body, body_chars, body_sha256,
                     idempotency_key, status, error_code, created_at, updated_at)
                    VALUES (?, ?, NULL, 0, ?, ?, ?, ?, 'status', 0, 'closed',
                            ?, ?, ?, ?, 'registered', ?, ?, ?)
                    """,
                    (
                        leg_id,
                        exchange_id,
                        source_session_id,
                        f"status:{exchange_id}",
                        f"status:{exchange_id}",
                        target_session_id,
                        body,
                        len(body),
                        body_hash,
                        f"status:{error_code}",
                        error_code,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM cross_chat_exchange_legs WHERE id=?",
                    (leg_id,),
                ).fetchone()
            assert row is not None
            return dict(row), True

        return await self._call(operation)

    async def recoverable(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_envelopes
                    WHERE status IN ('waiting_admission', 'waiting_source', 'ready', 'submitting', 'queued', 'running')
                    ORDER BY created_at, id
                    """
                ).fetchall()
            return [dict(row) for row in rows]
        return await self._call(operation)

    async def pending_terminal_lifecycle(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_envelopes
                    WHERE status IN ('delivered', 'failed', 'cancelled')
                      AND lifecycle_status != status
                    ORDER BY created_at, id
                    """
                ).fetchall()
            return [dict(row) for row in rows]
        return await self._call(operation)

    async def nonterminal_for_session(self, session_id: str) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._transaction() as connection:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cross_chat_envelopes'"
                ).fetchone()
                if table_exists is None:
                    # Legacy/unit-test call paths can construct the server
                    # store without running the FastAPI lifespan. An absent
                    # ledger means there is no cross-chat cleanup to perform.
                    return []
                rows = connection.execute(
                    """
                    SELECT * FROM cross_chat_envelopes
                    WHERE (source_session_id=? OR target_session_id=?)
                      AND status IN ('waiting_admission', 'waiting_source', 'ready', 'submitting', 'queued', 'running')
                    ORDER BY created_at, id
                    """,
                    (session_id, session_id),
                ).fetchall()
            return [dict(row) for row in rows]
        return await self._call(operation)

    async def update(
        self,
        envelope_id: str,
        *,
        expected: set[str] | None = None,
        **changes: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "body", "status", "queued_id", "target_run_id", "result_hash",
            "error", "lifecycle_status", "queue_position",
        }
        patch = {key: value for key, value in changes.items() if key in allowed}
        if not patch:
            return await self.get(envelope_id)
        if (
            patch.get("status") in self.TERMINAL_STATUSES
            and "lifecycle_status" not in patch
        ):
            # The terminal ledger transition and its user-visible lifecycle
            # events form a durable outbox. A crash between them is repaired
            # on retry/startup, with append-side event de-duplication.
            patch["lifecycle_status"] = ""
        patch["updated_at"] = self._bindings.now()
        assignments = ", ".join(f"{key}=?" for key in patch)
        values = list(patch.values())
        where = "id=?"
        values.append(envelope_id)
        if expected:
            placeholders = ",".join("?" for _ in expected)
            where += f" AND status IN ({placeholders})"
            values.extend(sorted(expected))
        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    f"UPDATE cross_chat_envelopes SET {assignments} WHERE {where}",
                    values,
                )
                if cursor.rowcount == 0:
                    return None
                row = connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE id=?",
                    (envelope_id,),
                ).fetchone()
            return self._row(row)
        return await self._call(operation)

    async def cancel_exact_queued_envelope(
        self,
        *,
        envelope_id: str,
        target_session_id: str,
        queued_id: str,
        error: str,
    ) -> dict[str, Any] | None:
        """Cancel only the envelope that still owns an exact queue row."""

        timestamp = self._bindings.now()

        def operation() -> dict[str, Any] | None:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE cross_chat_envelopes
                    SET status='cancelled', error=?, lifecycle_status='',
                        updated_at=?
                    WHERE id=? AND target_session_id=? AND queued_id=?
                      AND status='queued'
                    """,
                    (
                        error,
                        timestamp,
                        envelope_id,
                        target_session_id,
                        queued_id,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    return None
                return self._row(connection.execute(
                    "SELECT * FROM cross_chat_envelopes WHERE id=?",
                    (envelope_id,),
                ).fetchone())

        return await self._call(operation)

