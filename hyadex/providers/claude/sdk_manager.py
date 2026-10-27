"""The Claude SDK supervisor manager: one supervisor per chat, created, reused and retired."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from typing import Any, Awaitable, Callable

from hyadex.providers.claude.sdk import (
    bind_permission_owner,
    ClaudeSDKConfigurationConflict,
    ClaudeSDKControlTimeout,
    ClaudeSDKGenerationChanged,
    ClaudeSDKLoopError,
    ClaudeSDKQueryError,
    ClaudeSDKRunActive,
    ClaudeSDKRunHandle,
    ClaudeSDKSupervisor,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorSnapshot,
    ClaudeSDKUnavailable,
    ClientFactory,
    default_claude_sdk_client_factory,
    default_is_result_message,
    ResultPredicate,
    SupervisorReadyCallback,
)


class ClaudeSDKSupervisorManager:
    """Lazy, bounded registry of independent chat-scoped supervisors."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = default_claude_sdk_client_factory,
        is_result_message: ResultPredicate = default_is_result_message,
        max_clients: int = 12,
        idle_ttl_seconds: float | None = 15 * 60,
        connect_timeout_seconds: float = 30.0,
        disconnect_timeout_seconds: float = 2.0,
        ack_timeout_seconds: float = 60.0,
        query_delivery_timeout_seconds: float = 10.0,
        control_timeout_seconds: float = 15.0,
        usage_observer: Callable[[str, str, Any], Awaitable[None]] | None = None,
    ) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        if idle_ttl_seconds is not None and idle_ttl_seconds < 0:
            raise ValueError("idle_ttl_seconds must be non-negative or None")
        self._client_factory = client_factory
        self._is_result_message = is_result_message
        self._usage_observer = usage_observer
        self._max_clients = int(max_clients)
        self._idle_ttl_seconds = idle_ttl_seconds
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        if disconnect_timeout_seconds <= 0:
            raise ValueError("disconnect_timeout_seconds must be positive")
        self._disconnect_timeout_seconds = float(disconnect_timeout_seconds)
        if ack_timeout_seconds <= 0:
            raise ValueError("ack_timeout_seconds must be positive")
        self._ack_timeout_seconds = float(ack_timeout_seconds)
        if query_delivery_timeout_seconds <= 0:
            raise ValueError("query_delivery_timeout_seconds must be positive")
        self._query_delivery_timeout_seconds = float(query_delivery_timeout_seconds)
        if control_timeout_seconds <= 0:
            raise ValueError("control_timeout_seconds must be positive")
        self._control_timeout_seconds = float(control_timeout_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._supervisors: OrderedDict[str, ClaudeSDKSupervisor] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._evicting: dict[str, asyncio.Task[None]] = {}
        self._retired_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ClaudeSDKLoopError("Claude SDK manager requires an event loop") from exc
        if self._loop is None:
            self._loop = loop
            self._lock = asyncio.Lock()
        elif loop is not self._loop:
            raise ClaudeSDKLoopError("Claude SDK manager used from a different event loop")
        if self._closed:
            raise ClaudeSDKSupervisorClosed("Claude SDK manager is closed")
        if (
            self._reaper_task is None
            and self._idle_ttl_seconds is not None
            and self._idle_ttl_seconds > 0
        ):
            self._reaper_task = loop.create_task(
                self._reaper_loop(),
                name="claude-sdk-idle-reaper",
            )
        return loop

    async def _reaper_loop(self) -> None:
        interval = min(
            60.0,
            max(1.0, float(self._idle_ttl_seconds or 1.0) / 2.0),
        )
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                await self.evict_idle()
        except (asyncio.CancelledError, ClaudeSDKSupervisorClosed):
            return

    async def _wait_for_eviction(self, chat_id: str) -> None:
        """Serialize replacement behind complete teardown of the old client."""

        assert self._lock is not None
        while True:
            async with self._lock:
                task = self._evicting.get(chat_id)
            if task is None:
                return
            await asyncio.shield(task)
            async with self._lock:
                if self._evicting.get(chat_id) is task and task.done():
                    self._evicting.pop(chat_id, None)

    async def _get_locked(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> tuple[ClaudeSDKSupervisor, ClaudeSDKSupervisor | None]:
        old_to_close: ClaudeSDKSupervisor | None = None
        supervisor = self._supervisors.get(chat_id)
        if supervisor is not None and supervisor.closed:
            if self._pins.get(chat_id, 0):
                raise ClaudeSDKSupervisorClosed(
                    f"Claude SDK chat {chat_id} is retiring"
                )
            self._supervisors.pop(chat_id, None)
            old_to_close = supervisor
            supervisor = None
        if supervisor is not None and supervisor.configuration_key != configuration_key:
            if supervisor.is_active or self._pins.get(chat_id, 0):
                raise ClaudeSDKConfigurationConflict(
                    f"Claude SDK chat {chat_id} is active with another configuration"
                )
            self._supervisors.pop(chat_id, None)
            old_to_close = supervisor
            supervisor = None
        if supervisor is None:
            supervisor = ClaudeSDKSupervisor(
                chat_id,
                options=options,
                configuration_key=configuration_key,
                client_factory=self._client_factory,
                is_result_message=self._is_result_message,
                connect_timeout_seconds=self._connect_timeout_seconds,
                disconnect_timeout_seconds=self._disconnect_timeout_seconds,
                ack_timeout_seconds=self._ack_timeout_seconds,
                query_delivery_timeout_seconds=self._query_delivery_timeout_seconds,
                control_timeout_seconds=self._control_timeout_seconds,
                usage_observer=self._usage_observer,
            )
            self._supervisors[chat_id] = supervisor
        else:
            # A disconnected actor has no live provider state. Refresh its
            # lazy reconnect options so a newly persisted resume/fork binding
            # is honored after a stream failure without changing the stable
            # process-configuration key used by healthy persistent clients.
            if not supervisor.connected and not supervisor.is_active:
                supervisor.options = options
                bind_permission_owner(
                    supervisor.options,
                    supervisor.ownership_token,
                )
            self._supervisors.move_to_end(chat_id)
        return supervisor, old_to_close

    async def get(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> ClaudeSDKSupervisor:
        """Return the chat supervisor, replacing an idle stale configuration."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        await self._wait_for_eviction(clean_chat_id)
        async with self._lock:
            supervisor, old_to_close = await self._get_locked(
                clean_chat_id,
                options=options,
                configuration_key=str(configuration_key),
            )
        if old_to_close is not None:
            await old_to_close.close()
        await self.evict_idle(exclude={clean_chat_id})
        return supervisor

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: Any,
        configuration_key: str,
        query_session_id: str | None = None,
        validated_provider_command_name: str | None = None,
        expected_provider_command_generation: str | None = None,
        on_supervisor_ready: SupervisorReadyCallback | None = None,
        background_task_reconciliation: dict[str, Any] | None = None,
    ) -> ClaudeSDKRunHandle:
        """Pin a chat through query acceptance, then return its run handle."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        await self._wait_for_eviction(clean_chat_id)
        old_to_close: ClaudeSDKSupervisor | None = None
        async with self._lock:
            supervisor, old_to_close = await self._get_locked(
                clean_chat_id,
                options=options,
                configuration_key=str(configuration_key),
            )
            self._pins[clean_chat_id] = self._pins.get(clean_chat_id, 0) + 1
        if old_to_close is not None:
            await old_to_close.close()
        retire = False
        try:
            return await supervisor.start_run(
                prompt,
                run_id=run_id,
                query_session_id=query_session_id,
                validated_provider_command_name=validated_provider_command_name,
                expected_provider_command_generation=(
                    expected_provider_command_generation
                ),
                on_supervisor_ready=on_supervisor_ready,
                background_task_reconciliation=background_task_reconciliation,
            )
        except (ClaudeSDKUnavailable, asyncio.CancelledError):
            # A cold-connect failure or a Stop that cancels start_run before
            # on_supervisor_ready has published an ownership token must evict
            # this exact owner. Otherwise its actor can retain a wedged
            # connect command and every later turn queues behind it forever.
            retire = True
            raise
        finally:
            if retire:
                await self._retire_exact_supervisor(
                    clean_chat_id,
                    supervisor,
                    task_name_prefix="claude-sdk-start-retire",
                )
            else:
                async with self._lock:
                    count = self._pins.get(clean_chat_id, 0) - 1
                    if count > 0:
                        self._pins[clean_chat_id] = count
                    else:
                        self._pins.pop(clean_chat_id, None)
                    if self._supervisors.get(clean_chat_id) is supervisor:
                        self._supervisors.move_to_end(clean_chat_id)
                await self.evict_idle(exclude={clean_chat_id})

    def owns_active_run(
        self,
        chat_id: str,
        ownership_token: str,
        run_id: str,
    ) -> bool:
        """Return whether the current registry owner is running this query."""

        supervisor = self._supervisors.get(str(chat_id))
        return bool(
            supervisor is not None
            and not supervisor.closed
            and supervisor.ownership_token == str(ownership_token)
            and supervisor.active_run_id == str(run_id)
        )

    async def interrupt(self, chat_id: str, *, run_id: str | None = None) -> bool:
        self._bind_loop()
        assert self._lock is not None
        async with self._lock:
            supervisor = self._supervisors.get(str(chat_id))
        if supervisor is None:
            return False
        return await supervisor.interrupt(run_id=run_id)

    async def clear_goal(
        self, chat_id: str, *, run_id: str,
        expected_generation: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Send a native clear only to the existing owner of the exact run."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            supervisor = self._supervisors.get(clean_chat_id)
            if (supervisor is None or supervisor.closed or not supervisor.connected
                    or supervisor.active_run_id != str(run_id)):
                raise ClaudeSDKGenerationChanged(
                    "The Claude run changed before its goal could be cleared"
                )
            self._pins[clean_chat_id] = self._pins.get(clean_chat_id, 0) + 1
            generation = supervisor.snapshot().generation
        retire = False
        try:
            value, revision = await supervisor.clear_goal(
                run_id=str(run_id), expected_generation=expected_generation,
            )
            async with self._lock:
                if (self._supervisors.get(clean_chat_id) is not supervisor
                        or supervisor.closed or supervisor.snapshot().generation != generation):
                    raise ClaudeSDKGenerationChanged(
                        "The Claude owner changed while clearing its goal"
                    )
            return value, revision
        except (ClaudeSDKControlTimeout, ClaudeSDKQueryError, asyncio.CancelledError):
            retire = True
            raise
        finally:
            if retire:
                await self._retire_exact_supervisor(
                    clean_chat_id, supervisor, task_name_prefix="claude-sdk-goal-retire",
                )
            await self._unpin_mcp_supervisor(clean_chat_id, supervisor)

    async def get_context_usage(
        self,
        chat_id: str,
        *,
        ownership_token: str | None = None,
    ) -> tuple[dict[str, Any], int] | None:
        """Sample one chat's connected client with registry/generation fencing."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            supervisor = self._supervisors.get(clean_chat_id)
            if (
                supervisor is None
                or supervisor.closed
                or not supervisor.connected
                or (
                    ownership_token is not None
                    and supervisor.ownership_token != str(ownership_token)
                )
            ):
                return None
            self._pins[clean_chat_id] = self._pins.get(clean_chat_id, 0) + 1
            generation = supervisor.snapshot().generation
        try:
            usage = await supervisor.get_context_usage()
            if usage is None:
                return None
            async with self._lock:
                if (
                    self._supervisors.get(clean_chat_id) is not supervisor
                    or supervisor.closed
                    or supervisor.snapshot().generation != generation
                    or (
                        ownership_token is not None
                        and supervisor.ownership_token != str(ownership_token)
                    )
                ):
                    return None
                self._supervisors.move_to_end(clean_chat_id)
            return dict(usage), generation
        finally:
            async with self._lock:
                count = self._pins.get(clean_chat_id, 0) - 1
                if count > 0:
                    self._pins[clean_chat_id] = count
                else:
                    self._pins.pop(clean_chat_id, None)

    async def _pin_mcp_supervisor(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
        reuse_connected: bool = False,
    ) -> ClaudeSDKSupervisor:
        """Return and pin the exact supervisor used by one MCP HTTP request."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        await self._wait_for_eviction(clean_chat_id)
        async with self._lock:
            supervisor = self._supervisors.get(clean_chat_id)
            old_to_close = None
            if not (reuse_connected and supervisor is not None
                    and supervisor.connected and not supervisor.closed):
                supervisor, old_to_close = await self._get_locked(
                    clean_chat_id,
                    options=options,
                    configuration_key=str(configuration_key),
                )
            self._pins[clean_chat_id] = self._pins.get(clean_chat_id, 0) + 1
        if old_to_close is not None:
            try:
                await old_to_close.close()
            except BaseException:
                await self._unpin_mcp_supervisor(clean_chat_id, supervisor)
                raise
        return supervisor

    async def _unpin_mcp_supervisor(
        self,
        chat_id: str,
        supervisor: ClaudeSDKSupervisor,
    ) -> None:
        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            if self._supervisors.get(clean_chat_id) is not supervisor:
                # Exact-owner retirement already removed this pin. Never let
                # an old request decrement a replacement owner's count.
                return
            count = self._pins.get(clean_chat_id, 0) - 1
            if count > 0:
                self._pins[clean_chat_id] = count
            else:
                self._pins.pop(clean_chat_id, None)
            self._supervisors.move_to_end(clean_chat_id)

    async def _retire_exact_supervisor(
        self,
        chat_id: str,
        supervisor: ClaudeSDKSupervisor,
        *,
        task_name_prefix: str = "claude-sdk-mcp-retire",
    ) -> None:
        """Remove and abort only the owner involved in a failed operation."""

        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            if self._supervisors.get(clean_chat_id) is not supervisor:
                return
            self._supervisors.pop(clean_chat_id, None)
            self._pins.pop(clean_chat_id, None)
            close_task = asyncio.create_task(
                supervisor.abort(),
                name=f"{task_name_prefix}:{clean_chat_id}",
            )
            self._evicting[clean_chat_id] = close_task

        def track_late_cleanup(_completed: asyncio.Task[None]) -> None:
            for cleanup_task in supervisor.late_connect_cleanup_tasks():
                self._retired_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(
                    self._retired_cleanup_tasks.discard
                )

        close_task.add_done_callback(track_late_cleanup)
        try:
            await asyncio.shield(close_task)
        finally:
            if close_task.done():
                async with self._lock:
                    if self._evicting.get(clean_chat_id) is close_task:
                        self._evicting.pop(clean_chat_id, None)

    async def ask_side_question(
        self,
        chat_id: str,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        options: Any,
        configuration_key: str,
        expected_provider_id: str | None = None,
    ) -> dict[str, Any]:
        """Lease native parent context without occupying its main-turn actor.

        Only connection admission is actor-serialized. The adapter owns its
        separate control request and cancellation; side failures never stop,
        disconnect, or retire the parent's supervisor.
        """
        from claude_side_question import ask_native_side_question

        clean_chat_id = str(chat_id or "").strip()
        supervisor = await self._pin_mcp_supervisor(
            clean_chat_id,
            options=options,
            configuration_key=configuration_key,
            # /btw reads the live parent's context and settings. Saved settings
            # may already describe the next main turn; applying them here can
            # reject an active parent or replace an idle native conversation.
            reuse_connected=True,
        )
        side_task: asyncio.Task[dict[str, Any]] | None = None
        retired_task: asyncio.Task[bool] | None = None
        try:
            lease = await supervisor.get_side_question_client(
                expected_provider_id=expected_provider_id,
            )

            async def check_owner() -> None:
                assert self._lock is not None
                async with self._lock:
                    if (
                        self._supervisors.get(clean_chat_id) is not supervisor
                        or supervisor.closed
                        or supervisor._client is not lease.client
                        or supervisor.control_generation != lease.generation
                        or lease.retired.is_set()
                    ):
                        raise ClaudeSDKGenerationChanged(
                            f"Claude SDK side-question connection changed for {clean_chat_id}"
                        )

            await check_owner()
            side_task = asyncio.create_task(
                ask_native_side_question(
                    lease.client, question, history=history,
                ),
                name=f"claude-sdk-side-question:{clean_chat_id}",
            )
            retired_task = asyncio.create_task(lease.retired.wait())
            await asyncio.wait(
                {side_task, retired_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            await check_owner()
            result = await side_task
            await check_owner()
            return result
        finally:
            try:
                tasks = [task for task in (side_task, retired_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await self._unpin_mcp_supervisor(clean_chat_id, supervisor)

    async def get_mcp_status(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> tuple[dict[str, Any], str]:
        """Query MCP state through an exact, pinned chat/profile owner."""

        clean_chat_id = str(chat_id)
        supervisor = await self._pin_mcp_supervisor(
            clean_chat_id,
            options=options,
            configuration_key=configuration_key,
        )
        retire = False
        try:
            if supervisor.is_active:
                raise ClaudeSDKRunActive(
                    f"Claude SDK chat {clean_chat_id} has an active run"
                )
            try:
                status, generation = await supervisor.get_mcp_status()
            except ClaudeSDKControlTimeout:
                retire = True
                raise
            except asyncio.CancelledError:
                retire = True
                raise
            assert self._lock is not None
            async with self._lock:
                if (
                    self._supervisors.get(clean_chat_id) is not supervisor
                    or supervisor.closed
                    or supervisor.control_generation != generation
                ):
                    raise ClaudeSDKGenerationChanged(
                        f"Claude SDK MCP generation changed for chat {clean_chat_id}"
                    )
                self._supervisors.move_to_end(clean_chat_id)
            return dict(status), str(generation)
        finally:
            if retire:
                await self._retire_exact_supervisor(
                    clean_chat_id,
                    supervisor,
                )
            await self._unpin_mcp_supervisor(clean_chat_id, supervisor)

    async def get_server_info(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> tuple[dict[str, Any], str]:
        """Read a bounded provider-command snapshot from an exact owner."""

        clean_chat_id = str(chat_id)
        supervisor = await self._pin_mcp_supervisor(
            clean_chat_id,
            options=options,
            configuration_key=configuration_key,
        )
        retire = False
        try:
            try:
                info, generation = await supervisor.get_server_info()
            except ClaudeSDKControlTimeout:
                # Never retire or interrupt a supervisor that owns a live run.
                retire = not supervisor.is_active
                raise
            except asyncio.CancelledError:
                retire = not supervisor.is_active
                raise
            assert self._lock is not None
            async with self._lock:
                if (
                    self._supervisors.get(clean_chat_id) is not supervisor
                    or supervisor.closed
                    or supervisor.control_generation != generation
                ):
                    raise ClaudeSDKGenerationChanged(
                        "Claude SDK provider-command generation changed for chat "
                        f"{clean_chat_id}"
                    )
                self._supervisors.move_to_end(clean_chat_id)
            return dict(info), str(generation)
        finally:
            if retire:
                await self._retire_exact_supervisor(
                    clean_chat_id,
                    supervisor,
                    task_name_prefix="claude-sdk-provider-command-retire",
                )
            await self._unpin_mcp_supervisor(clean_chat_id, supervisor)

    async def mutate_mcp_server(
        self,
        chat_id: str,
        *,
        action: str,
        server_name: str | None,
        expected_generation: str,
        options: Any,
        configuration_key: str,
    ) -> tuple[dict[str, Any], str]:
        """Apply one exact-generation MCP mutation and return refreshed state."""

        clean_chat_id = str(chat_id)
        supervisor = await self._pin_mcp_supervisor(
            clean_chat_id,
            options=options,
            configuration_key=configuration_key,
        )
        retire = False
        try:
            if supervisor.is_active:
                raise ClaudeSDKRunActive(
                    f"Claude SDK chat {clean_chat_id} has an active run"
                )
            try:
                status, generation = await supervisor.mutate_mcp_server(
                    action=action,
                    server_name=server_name,
                    expected_generation=expected_generation,
                )
            except (ClaudeSDKControlTimeout, asyncio.CancelledError):
                # Mutation delivery is uncertain at this boundary. Remove the
                # exact owner before a replacement can be created.
                retire = True
                raise
            assert self._lock is not None
            async with self._lock:
                if (
                    self._supervisors.get(clean_chat_id) is not supervisor
                    or supervisor.closed
                    or supervisor.control_generation != generation
                ):
                    raise ClaudeSDKGenerationChanged(
                        f"Claude SDK MCP generation changed for chat {clean_chat_id}"
                    )
                self._supervisors.move_to_end(clean_chat_id)
            return dict(status), str(generation)
        finally:
            if retire:
                await self._retire_exact_supervisor(
                    clean_chat_id,
                    supervisor,
                )
            await self._unpin_mcp_supervisor(clean_chat_id, supervisor)

    def is_loaded(self, chat_id: str) -> bool:
        """Return whether this manager currently owns a connected SDK client."""

        supervisor = self._supervisors.get(str(chat_id))
        return bool(
            supervisor is not None
            and supervisor.connected
            and not supervisor.closed
        )

    def usage_generation(self, chat_id: str, *, run_id: str | None = None) -> str | None:
        """Identify the existing native owner without connecting or issuing RPCs."""
        supervisor = self._supervisors.get(str(chat_id))
        if (supervisor is None or supervisor.closed or not supervisor.connected
                or (run_id is not None and supervisor.active_run_id != run_id)):
            return None
        return supervisor.control_generation

    async def evict(
        self,
        chat_id: str,
        *,
        force: bool = False,
        ownership_token: str | None = None,
    ) -> bool:
        """Disconnect one idle chat, or an active chat only when ``force`` is true."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            existing_close = self._evicting.get(clean_chat_id)
            supervisor = self._supervisors.get(clean_chat_id)
            if existing_close is not None:
                close_task = existing_close
            elif supervisor is None:
                return False
            else:
                if (
                    ownership_token is not None
                    and supervisor.ownership_token != str(ownership_token)
                ):
                    # A delayed finalizer from an older generation must not
                    # evict the replacement client now registered for chat.
                    return False
                if not force and (
                    supervisor.is_active or self._pins.get(clean_chat_id, 0)
                ):
                    return False
                self._supervisors.pop(clean_chat_id, None)
                self._pins.pop(clean_chat_id, None)
                close_task = asyncio.create_task(
                    supervisor.abort() if force else supervisor.close(),
                    name=f"claude-sdk-evict:{clean_chat_id}",
                )
                self._evicting[clean_chat_id] = close_task
        try:
            await asyncio.shield(close_task)
        finally:
            if close_task.done():
                async with self._lock:
                    if self._evicting.get(clean_chat_id) is close_task:
                        self._evicting.pop(clean_chat_id, None)
        return True

    async def evict_idle(self, *, exclude: set[str] | None = None) -> list[str]:
        """Apply TTL and LRU limits without ever evicting an active/pinned chat."""

        self._bind_loop()
        assert self._lock is not None
        excluded = {str(value) for value in (exclude or set())}
        now = time.monotonic()
        selected_ids: set[str] = set()
        async with self._lock:
            idle_candidates = [
                (chat_id, supervisor)
                for chat_id, supervisor in self._supervisors.items()
                if (
                    chat_id not in excluded
                    and not supervisor.is_active
                    and not self._pins.get(chat_id, 0)
                )
            ]
            idle_candidates.sort(key=lambda item: item[1].last_used_at)
            if self._idle_ttl_seconds is not None:
                for chat_id, supervisor in idle_candidates:
                    if now - supervisor.last_used_at >= self._idle_ttl_seconds:
                        selected_ids.add(chat_id)
            overflow = max(0, len(self._supervisors) - self._max_clients)
            if overflow:
                for chat_id, _supervisor in idle_candidates:
                    if len(selected_ids) >= overflow:
                        break
                    selected_ids.add(chat_id)
        if selected_ids:
            await asyncio.gather(
                *(self.evict(chat_id) for chat_id in selected_ids),
                return_exceptions=False,
            )
        return list(selected_ids)

    async def close_all(self) -> None:
        """Disconnect every per-chat process; used by HyadexServer shutdown."""

        try:
            self._bind_loop()
        except ClaudeSDKSupervisorClosed:
            return
        assert self._lock is not None
        async with self._lock:
            supervisors = list(self._supervisors.values())
            eviction_tasks = list(self._evicting.values())
            self._supervisors.clear()
            self._pins.clear()
            self._closed = True
            reaper_task = self._reaper_task
            self._reaper_task = None
        if reaper_task is not None and reaper_task is not asyncio.current_task():
            reaper_task.cancel()
            await asyncio.gather(reaper_task, return_exceptions=True)
        if supervisors:
            await asyncio.gather(
                *(supervisor.abort() for supervisor in supervisors),
                return_exceptions=False,
            )
        if eviction_tasks:
            await asyncio.gather(*eviction_tasks, return_exceptions=False)
        for supervisor in supervisors:
            for cleanup_task in supervisor.late_connect_cleanup_tasks():
                self._retired_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(
                    self._retired_cleanup_tasks.discard
                )
        retired_cleanup_tasks = tuple(self._retired_cleanup_tasks)
        if retired_cleanup_tasks:
            await asyncio.wait(
                retired_cleanup_tasks,
                timeout=self._disconnect_timeout_seconds,
            )
        async with self._lock:
            self._evicting.clear()

    def snapshots(self) -> list[ClaudeSDKSupervisorSnapshot]:
        return [supervisor.snapshot() for supervisor in self._supervisors.values()]
