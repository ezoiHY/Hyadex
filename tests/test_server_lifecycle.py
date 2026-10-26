"""Common lifecycle protection independent of the retired updater."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import agent_server


class ServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_hyadex_server_systemd_cgroup_handles_unified_and_legacy_paths(self):
        paths = (
            "/user.slice/user-1000.slice/user@1000.service/app.slice/hyadex-server.service",
            "/unrelated",
        )
        with patch.object(agent_server, "process_cgroup_paths", return_value=paths):
            cgroup = agent_server.hyadex_server_systemd_cgroup(123)

        self.assertEqual(cgroup, paths[0])
        self.assertTrue(agent_server.cgroup_is_within(f"{cgroup}/child", cgroup))
        self.assertFalse(agent_server.cgroup_is_within("/hyadex-server.service-old", cgroup))

    def test_linux_runner_environment_restores_the_user_service_bus(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "bus").touch()
            with patch.object(agent_server.sys, "platform", "linux"), \
                 patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}, clear=True):
                environment = agent_server.server_instance_environment()

        self.assertEqual(environment, {
            "HYADEX_SERVER_INSTANCE": "default",
            "HYADEX_SERVER_CONFIG_DIR": str(agent_server.CONFIG_ENV_FILE.parent),
            "HYADEX_STATE_DIR": str(agent_server.STATE_DIR),
            "HYADEX_SERVER_INSTALL_DIR": "",
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        })

    async def test_cancelled_http_mutation_holds_lease_until_thread_finishes(self):
        path = "/api/sessions/chat/unread"
        request = agent_server.Request({
            "type": "http", "app": agent_server.app,
            "asgi": {"version": "3.0"},
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 7870),
        })
        entered = threading.Event()
        release = threading.Event()

        def blocked_write():
            entered.set()
            release.wait()

        async def slow_mutation(_request):
            await asyncio.to_thread(blocked_write)
            return agent_server.JSONResponse({"saved": True})

        with patch.object(agent_server, "AGENT_TOKEN", ""), \
             patch.object(agent_server, "UNSAFE_HTTP_MUTATION_TASKS", {}), \
             patch.object(agent_server, "unsafe_http_mutation_blocked_response", return_value=None):
            mutation = asyncio.create_task(
                agent_server.require_agent_token(request, slow_mutation)
            )
            try:
                entered_ready = await asyncio.wait_for(
                    asyncio.to_thread(entered.wait, 5),
                    timeout=6,
                )
                self.assertTrue(entered_ready)
                mutation.cancel()
                await asyncio.sleep(0)
                mutation.cancel()
                await asyncio.sleep(0)

                # Cancelling the asyncio wrapper does not stop its worker thread.
                # The exact lease must remain until that real mutation settles.
                self.assertFalse(mutation.done())
                self.assertEqual(agent_server.unsafe_http_mutation_count_locked(), 1)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await mutation
                await asyncio.sleep(0)

                self.assertEqual(agent_server.unsafe_http_mutation_count_locked(), 0)
            finally:
                release.set()

    async def test_mutation_registry_prunes_only_finished_tasks(self):
        release = asyncio.Event()
        live = asyncio.create_task(release.wait())
        completed = asyncio.create_task(asyncio.sleep(0))
        cancelled = asyncio.create_task(asyncio.sleep(60))
        await completed
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        registry = {
            "completed": completed,
            "cancelled": cancelled,
            "live": live,
        }
        with patch.object(agent_server, "UNSAFE_HTTP_MUTATION_TASKS", registry):
            self.assertEqual(
                agent_server.live_unsafe_http_mutation_ids_locked(),
                ["live"],
            )
            release.set()
            await live
            self.assertEqual(agent_server.unsafe_http_mutation_count_locked(), 0)
