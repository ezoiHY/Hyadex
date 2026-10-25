"""Exercise listener, HTTP and WebSocket security against an isolated dev server.

Run with .venv/bin/python scripts/check_network_security.py. No installed
service, provider credential or existing state is used.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[1]


async def check_server(port: int, token: str) -> None:
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=5) as client:
        for _ in range(300):
            try:
                response = await client.get("/api/health", headers={"Authorization": "Bearer " + token})
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        else:
            raise AssertionError("isolated server did not become healthy")
        health = response.json()
        assert health["auth_required"] is True
        guard = health["job_guard"]
        assert guard["job_min_available_mem_mb"] == 1536
        assert guard["effective_min_available_mem_mb"] == 2048
        assert isinstance(guard["resource_blocked"], bool)
        assert "resource_blocking_reason" in guard
        for path in ("/api/sessions", "/api/sessions?token=" + token):
            assert (await client.get(path)).status_code == 401
        for headers in ({"Authorization": "Bearer " + token}, {"x-hyadex-token": token}):
            assert (await client.get("/api/sessions", headers=headers)).status_code == 200
        origin = {"Origin": "https://untrusted.example"}
        response = await client.get("/api/sessions", headers={**origin, "Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
        response = await client.options("/api/sessions", headers={
            **origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers
    endpoint = f"ws://127.0.0.1:{port}/api/emergency-alerts/events"
    async with connect(endpoint + "?token=" + token, subprotocols=["hyadex-emergency-v1"]) as ws:
        try:
            await ws.recv()
        except ConnectionClosed as exc:
            assert exc.rcvd is not None and exc.rcvd.code == 4401
        else:
            raise AssertionError("query-only WebSocket credential accepted")
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    for _ in range(2):
        async with connect(endpoint, subprotocols=["hyadex-emergency-v1", "hyadex-token." + encoded]) as ws:
            assert ws.subprotocol == "hyadex-emergency-v1"
            assert json.loads(await ws.recv())["type"] == "emergency_snapshot"
    print("PASS HTTP headers, query rejection, CORS, WebSocket auth/reconnect, health diagnostics")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hyadex-network-smoke-") as directory:
        home = Path(directory).resolve()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        denied = subprocess.run([str(ROOT / ".venv/bin/python"), "agent_server.py", "--bind", "0.0.0.0"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert denied.returncode == 2 and "HYADEX_AGENT_TOKEN" in denied.stderr
        print("PASS unauthenticated non-loopback startup rejected")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        with (home / "server.log").open("w+") as log:
            process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "scripts/dev_server.py",
                                        "--instance", "network-smoke", "--port", str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                token_path = home / ".config/hyadex-server-instances/network-smoke/dev-token"
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("isolated server exited")
                    if token_path.exists() and token_path.read_text().strip():
                        break
                    time.sleep(.1)
                asyncio.run(check_server(port, token_path.read_text().strip()))
            except Exception:
                log.flush()
                log.seek(0)
                print("Isolated server diagnostics:\n" + log.read()[-5000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
