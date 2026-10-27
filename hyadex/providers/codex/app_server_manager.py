"""The shared Codex app-server manager: process lifecycle and per-thread clients."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Sequence

from hyadex.providers.codex.app_server import (
    _OMITTED,
    _OmittedType,
    CodexAppServerClient,
    CodexAppServerSubscription,
    CodexAppServerTurn,
    NotificationHandler,
    ProcessFactory,
    ProcessLifecycleHook,
    ServerRequestHandler,
)


class CodexAppServerManager:
    """Lifecycle façade owning exactly one lazy shared app-server client."""

    def __init__(
        self,
        codex_bin: str,
        *,
        cwd: str,
        env_factory: Callable[[], dict[str, str]],
        app_server_args: Sequence[str] = (),
        request_timeout: float = 30.0,
        lifecycle_timeout: float = 300.0,
        process_stream_limit: int = 16 * 1024 * 1024,
        notification_queue_limit: int = 8192,
        json_parse_thread_threshold: int = 1024 * 1024,
        process_factory: ProcessFactory | None = None,
        before_start: Callable[[], Awaitable[Any]] | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        notification_guard: Callable[[dict[str, Any]], bool] | None = None,
        initialize_params: dict[str, Any] | None = None,
        on_process_started: ProcessLifecycleHook | None = None,
        on_process_exited: ProcessLifecycleHook | None = None,
        sensitive_values: Sequence[str] = (),
        protected_env_keys: Sequence[str] = (),
    ) -> None:
        self.client = CodexAppServerClient(
            codex_bin,
            cwd=cwd,
            env_factory=env_factory,
            app_server_args=app_server_args,
            request_timeout=request_timeout,
            lifecycle_timeout=lifecycle_timeout,
            process_stream_limit=process_stream_limit,
            notification_queue_limit=notification_queue_limit,
            json_parse_thread_threshold=json_parse_thread_threshold,
            process_factory=process_factory,
            before_start=before_start,
            server_request_handler=server_request_handler,
            notification_guard=notification_guard,
            initialize_params=initialize_params,
            on_process_started=on_process_started,
            on_process_exited=on_process_exited,
            sensitive_values=sensitive_values,
            protected_env_keys=protected_env_keys,
        )

    @property
    def ready(self) -> bool:
        return self.client.ready

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self.client.process

    @property
    def generation(self) -> int:
        return self.client.generation

    def is_thread_loaded(self, thread_id: str) -> bool:
        return self.client.is_thread_loaded(thread_id)

    def cached_thread_name(self, thread_id: str) -> str | None:
        return self.client.cached_thread_name(thread_id)

    async def start(self) -> None:
        await self.client.start()

    async def close(self) -> None:
        await self.client.close()

    async def retire_generation(self, expected_generation: int) -> bool:
        return await self.client.retire_generation(expected_generation)

    async def __aenter__(self) -> "CodexAppServerManager":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    def subscribe_thread(self, thread_id: str) -> CodexAppServerSubscription:
        return self.client.subscribe_thread(thread_id)

    def subscribe_turn(
        self,
        thread_id: str,
        turn_id: str,
    ) -> CodexAppServerSubscription:
        return self.client.subscribe_turn(thread_id, turn_id)

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self.client.add_notification_handler(handler)

    def remove_notification_handler(self, handler: NotificationHandler) -> None:
        self.client.remove_notification_handler(handler)

    async def wait_for_notification_handler(
        self,
        handler: NotificationHandler,
        thread_id: str,
    ) -> None:
        await self.client.wait_for_notification_handler(handler, thread_id)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        return await self.client.request(method, params, timeout=timeout)

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        await self.client.notify(method, params)

    async def start_thread(self, params: dict[str, Any]) -> str:
        return await self.client.start_thread(params)

    async def resume_thread(
        self,
        thread_id: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        return await self.client.resume_thread(thread_id, params)

    async def start_or_resume_thread(
        self,
        thread_id: str | None,
        start_params: dict[str, Any],
        resume_params: dict[str, Any] | None = None,
    ) -> str:
        return await self.client.start_or_resume_thread(
            thread_id,
            start_params,
            resume_params,
        )

    async def fork_thread(
        self,
        thread_id: str,
        params: dict[str, Any] | None = None,
        *,
        last_turn_id: str | None = None,
    ) -> str:
        return await self.client.fork_thread(
            thread_id,
            params,
            last_turn_id=last_turn_id,
        )

    async def inject_items(
        self,
        thread_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        await self.client.inject_items(thread_id, items)

    async def inject_items_guarded(
        self,
        thread_id: str,
        items: list[dict[str, Any]],
        *,
        expected_generation: int,
        before_send: Callable[[], bool],
        timeout: float | None = None,
    ) -> None:
        await self.client.inject_items_guarded(
            thread_id, items, expected_generation=expected_generation,
            before_send=before_send, timeout=timeout,
        )

    async def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> dict[str, Any]:
        return await self.client.read_thread(
            thread_id,
            include_turns=include_turns,
        )

    async def delete_thread(self, thread_id: str) -> None:
        await self.client.delete_thread(thread_id)

    async def list_turns(
        self,
        thread_id: str,
        *,
        limit: int = 4,
        items_view: str = "full",
        sort_direction: str = "desc",
    ) -> list[dict[str, Any]]:
        return await self.client.list_turns(
            thread_id,
            limit=limit,
            items_view=items_view,
            sort_direction=sort_direction,
        )

    async def list_descendant_threads(
        self,
        thread_id: str,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.client.list_descendant_threads(
            thread_id,
            page_size=page_size,
        )

    async def list_permission_profiles(
        self,
        *,
        cwd: str | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self.client.list_permission_profiles(
            cwd=cwd,
            page_size=page_size,
        )

    async def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None | _OmittedType = _OMITTED,
        status: str | None | _OmittedType = _OMITTED,
        token_budget: int | None | _OmittedType = _OMITTED,
    ) -> dict[str, Any]:
        return await self.client.set_thread_goal(
            thread_id,
            objective=objective,
            status=status,
            token_budget=token_budget,
        )

    async def get_thread_goal(self, thread_id: str) -> dict[str, Any] | None:
        return await self.client.get_thread_goal(thread_id)

    async def clear_thread_goal(self, thread_id: str) -> bool:
        return await self.client.clear_thread_goal(thread_id)

    async def compact_thread(self, thread_id: str) -> None:
        await self.client.compact_thread(thread_id)

    async def rollback_thread(
        self,
        thread_id: str,
        *,
        num_turns: int,
    ) -> dict[str, Any]:
        return await self.client.rollback_thread(thread_id, num_turns=num_turns)

    async def start_review(
        self,
        thread_id: str,
        target: dict[str, Any],
        *,
        delivery: str = "inline",
    ) -> dict[str, Any]:
        return await self.client.start_review(
            thread_id,
            target,
            delivery=delivery,
        )

    async def run_thread_shell_command(self, thread_id: str, command: str) -> None:
        await self.client.run_thread_shell_command(thread_id, command)

    async def list_background_terminals(
        self,
        thread_id: str,
        *,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self.client.list_background_terminals(
            thread_id,
            page_size=page_size,
        )

    async def terminate_background_terminal(
        self,
        thread_id: str,
        process_id: str,
    ) -> bool:
        return await self.client.terminate_background_terminal(thread_id, process_id)

    async def clean_background_terminals(self, thread_id: str) -> None:
        await self.client.clean_background_terminals(thread_id)

    async def unsubscribe_thread(self, thread_id: str) -> str:
        return await self.client.unsubscribe_thread(thread_id)

    async def start_turn(
        self,
        thread_id: str,
        input_items: list[dict[str, Any]],
        *,
        overrides: dict[str, Any] | None = None,
        retain_thread_stream: bool = False,
    ) -> CodexAppServerTurn:
        return await self.client.start_turn(
            thread_id,
            input_items,
            overrides=overrides,
            retain_thread_stream=retain_thread_stream,
        )

    async def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        input_items: list[dict[str, Any]],
        *,
        client_user_message_id: str | None = None,
    ) -> str:
        return await self.client.steer_turn(
            thread_id,
            turn_id,
            input_items,
            client_user_message_id=client_user_message_id,
        )

    async def steer_turn_with_notification_watermark(
        self,
        thread_id: str,
        turn_id: str,
        input_items: list[dict[str, Any]],
        *,
        client_user_message_id: str | None = None,
        notification_subscription: CodexAppServerSubscription | None = None,
        before_send: Callable[[], bool] | None = None,
    ) -> tuple[str, int]:
        return await self.client.steer_turn_with_notification_watermark(
            thread_id,
            turn_id,
            input_items,
            client_user_message_id=client_user_message_id,
            notification_subscription=notification_subscription,
            before_send=before_send,
        )

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.client.interrupt_turn(thread_id, turn_id)

    def active_turn(self, thread_id: str) -> CodexAppServerTurn | None:
        return self.client.active_turn(thread_id)
