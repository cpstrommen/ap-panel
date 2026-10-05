"""HTTP/WS boundary tests use an in-memory runtime and never open hardware."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from ap_panel.dashboard import create_app

HOST = "127.0.0.1:8787"
ORIGIN = "http://" + HOST
ASSETS = Path(__file__).resolve().parents[1] / "web"


class FakeRuntime:
    def __init__(self) -> None:
        self.mode = "run"
        self.running = False
        self.ready = True
        self.calls: list[tuple] = []
        self.queues: list[asyncio.Queue] = []
        self.fail_start = False

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "state": "running" if self.running else "idle",
            "running": self.running,
            "ready": self.ready,
            "board": {"connected": True, "configuration_ok": True},
            "simulator": {"connected": True, "healthy": True},
            "panel": {"lamps": 0, "rows": [" " * 20] * 4},
            "events": [],
            "error": "Commissioning is required" if self.fail_start else None,
        }

    def profile_summaries(self) -> list:
        return [
            {
                "id": "c172-analog",
                "name": "C172 Analog",
                "compatible": True,
                "verification": "reference_verified",
            }
        ]

    async def start(self) -> bool:
        self.calls.append(("start",))
        if self.fail_start:
            return False
        self.running = True
        return True

    async def stop(self) -> None:
        self.calls.append(("stop",))
        self.running = False

    async def select_profile(self, profile_id: str) -> None:
        if profile_id != "c172-analog":
            raise KeyError(profile_id)
        self.calls.append(("profile", profile_id))

    async def bench_lamp(self, index: int | None, duration: float) -> None:
        self.calls.append(("lamp", index, duration))

    async def bench_walk(self) -> None:
        self.calls.append(("walk",))

    async def bench_lcd(self, text: str) -> None:
        self.calls.append(("lcd", text))

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.queues.remove(queue)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = FakeRuntime()
        self.client = TestClient(TestServer(create_app(self.runtime, assets_path=ASSETS)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def get(self, path: str, **kwargs):
        return await self.client.get(path, headers={"Host": HOST}, **kwargs)

    async def post(self, path: str, data: dict | None = None, **kwargs):
        return await self.client.post(
            path, json=data or {}, headers={"Host": HOST, "Origin": ORIGIN}, **kwargs
        )

    async def test_status_profiles_and_assets_are_read_only(self):
        response = await self.get("/api/status")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["mode"], "run")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response = await self.get("/api/profiles")
        self.assertEqual((await response.json())["profiles"][0]["id"], "c172-analog")
        for path, content_type in [
            ("/", "text/html"),
            ("/app.css", "text/css"),
            ("/app.js", "javascript"),
        ]:
            response = await self.get(path)
            self.assertEqual(response.status, 200)
            self.assertIn(content_type, response.headers["Content-Type"])
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.runtime.calls, [])

    async def test_run_stop_and_readiness_error(self):
        response = await self.post("/api/run")
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["status"]["running"])
        response = await self.post("/api/stop")
        self.assertFalse((await response.json())["status"]["running"])
        self.runtime.fail_start = True
        response = await self.post("/api/run")
        self.assertEqual(response.status, 400)
        self.assertIn("Commissioning", (await response.json())["error"])

    async def test_profile_validation(self):
        response = await self.post("/api/profile", {"profile_id": "c172-analog"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.runtime.calls[-1], ("profile", "c172-analog"))
        for value in ["", 3, [], "x" * 81]:
            response = await self.post("/api/profile", {"profile_id": value})
            self.assertEqual(response.status, 400)
        response = await self.post("/api/profile", {"profile_id": "unknown"})
        self.assertEqual(response.status, 404)

    async def test_bench_mode_gate_and_valid_output_actions(self):
        response = await self.post("/api/bench/lamp", {"index": 0})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.runtime.calls, [])
        self.runtime.mode = "bench"
        for path, data in [
            ("/api/bench/lamp", {"index": 23, "duration": 0.5}),
            ("/api/bench/lamp", {"index": None}),
            ("/api/bench/walk", {}),
            ("/api/bench/lcd", {"text": "AP PANEL TEST"}),
        ]:
            response = await self.post(path, data)
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(
            self.runtime.calls,
            [("lamp", 23, 0.5), ("lamp", None, 0.5), ("walk",), ("lcd", "AP PANEL TEST")],
        )

    async def test_invalid_bench_data_never_reaches_runtime(self):
        self.runtime.mode = "bench"
        for data in [
            {"index": -1},
            {"index": 24},
            {"index": True},
            {"index": 1.5},
            {"index": 0, "duration": 0},
            {"index": 0, "duration": 6},
            {"index": 0, "duration": True},
            {"index": 0, "duration": float("nan")},
            {"index": 0, "duration": "1"},
            {"index": 0, "unexpected": "field"},
        ]:
            response = await self.post("/api/bench/lamp", data)
            self.assertEqual(response.status, 400, str(data))
        for value in [None, 42, "x" * 81, "LINE\nTWO", "29.92°"]:
            response = await self.post("/api/bench/lcd", {"text": value})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.runtime.calls, [])

    async def test_json_shape_content_type_and_size_limits(self):
        response = await self.client.post(
            "/api/run", data="{}", headers={"Host": HOST, "Origin": ORIGIN}
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/run", json=[], headers={"Host": HOST, "Origin": ORIGIN}
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/run",
            data="{",
            headers={"Host": HOST, "Origin": ORIGIN, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 400)
        response = await self.post("/api/run", {"large": "x" * 5000})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.runtime.calls, [])

    async def test_host_and_origin_checks(self):
        for host in [
            "attacker.example:8787",
            "localhost:9999",
            "127.0.0.1",
            "127.0.0.1:8787.attacker.example",
            "evil@127.0.0.1:8787",
        ]:
            response = await self.client.get("/api/status", headers={"Host": host})
            self.assertEqual(response.status, 403, host)
        for origin in [
            "http://attacker.example",
            "null",
            "http://127.0.0.1:9999",
            "http://localhost:8787",
            "http://127.0.0.1:8787/path",
            "https://127.0.0.1:8787",
        ]:
            response = await self.client.post(
                "/api/run", json={}, headers={"Host": HOST, "Origin": origin}
            )
            self.assertEqual(response.status, 403, origin)
        for host in ["localhost:8787", "[::1]:8787"]:
            response = await self.client.get("/api/status", headers={"Host": host})
            self.assertEqual(response.status, 200, host)
        response = await self.client.post("/api/stop", json={}, headers={"Host": HOST})
        self.assertEqual(response.status, 200)  # Native loopback clients need no Origin header.

    async def test_websocket_initial_state_updates_and_unsubscribe(self):
        socket = await self.client.ws_connect(
            "/api/events", headers={"Host": HOST, "Origin": ORIGIN}
        )
        initial = await socket.receive_json(timeout=1)
        self.assertEqual(initial["type"], "status")
        self.assertEqual(initial["data"]["mode"], "run")
        self.assertEqual(len(self.runtime.queues), 1)
        await self.runtime.queues[0].put(
            {"type": "event", "data": {"kind": "input", "message": "AP pressed"}}
        )
        self.assertEqual((await socket.receive_json(timeout=1))["data"]["message"], "AP pressed")
        await socket.close()
        for _ in range(20):
            if not self.runtime.queues:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.runtime.queues, [])

    async def test_websocket_rejects_cross_origin_before_subscribing(self):
        response = await self.client.get(
            "/api/events", headers={"Host": HOST, "Origin": "http://attacker.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.runtime.queues, [])

    async def test_unknown_routes_do_not_expose_flight_actions(self):
        response = await self.post("/api/flight/ap")
        self.assertEqual(response.status, 404)
        self.assertIn("error", await response.json())
        response = await self.get("/api/run")
        self.assertEqual(response.status, 405)
        self.assertIn("error", await response.json())
        for path in ["/settings.local.yaml", "/electronics/pinmap.yaml", "/api/missing"]:
            response = await self.get(path)
            self.assertEqual(response.status, 404)
        self.assertEqual(self.runtime.calls, [])

    async def test_missing_assets_have_a_json_error(self):
        client = TestClient(TestServer(create_app(self.runtime, assets_path=ASSETS / "missing")))
        await client.start_server()
        try:
            response = await client.get("/", headers={"Host": HOST})
            self.assertEqual(response.status, 404)
            self.assertEqual((await response.json())["error"], "Dashboard asset is unavailable")
        finally:
            await client.close()

    async def test_bench_runtime_fault_is_reported_without_success(self):
        self.runtime.mode = "bench"
        self.runtime.bench_lamp = AsyncMock(
            side_effect=RuntimeError("Board disconnected during the test")
        )
        response = await self.post("/api/bench/lamp", {"index": 0})
        self.assertEqual(response.status, 400)
        data = await response.json()
        self.assertIn("Board disconnected", data["error"])
        self.assertNotIn("ok", data)


if __name__ == "__main__":
    unittest.main()
