"""Transport tests against a local server implementing Laminar's v2 framing."""

import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from ap_panel.xplane import (
    XPlaneAPIError,
    XPlaneClient,
    XPlaneConnectionError,
    XPlaneTimeoutError,
)


class SimulatorServer:
    def __init__(self):
        self.versions = ["v1", "v2", "v3"]
        self.datarefs = {
            "sim/test/heading": {"id": 101, "value_type": "float", "value": 270.0},
            "sim/test/gear": {"id": 102, "value_type": "float_array", "value": [1, 0, 1]},
            "sim/aircraft/view/acf_relative_path": {
                "id": 103,
                "value_type": "data",
                "value": "QWlyY3JhZnQvYzE3Mi5hY2YA",
            },
            "sim/time/paused": {"id": 104, "value_type": "int", "value": 0},
        }
        self.commands = {"sim/test/command": 201}
        self.requests = []
        self.lookups = []
        self.sockets = set()
        self.initial_snapshots = True
        self.ignore_ack = set()
        self.reject = set()
        self.read_gate = None
        self.read_started = asyncio.Queue()
        self.base_url = ""
        self.runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/capabilities", self.capabilities)
        app.router.add_get("/api/v2/datarefs", self.lookup_datarefs)
        app.router.add_get("/api/v2/commands", self.lookup_commands)
        app.router.add_get("/api/v2/datarefs/{identifier}/value", self.get_value)
        app.router.add_get("/api/v2", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def close(self):
        await self.drop()
        if self.read_gate is not None:
            self.read_gate.set()
        await self.runner.cleanup()

    async def capabilities(self, request):
        return web.json_response(
            {
                "api": {"versions": self.versions},
                "x-plane": {"version": "12.1.4"},
            }
        )

    async def lookup_datarefs(self, request):
        paths = request.query.getall("filter[name]", [])
        self.lookups.append(("datarefs", paths))
        if any(path not in self.datarefs for path in paths):
            return web.json_response({"error_code": "invalid_dataref_name"}, status=404)
        return web.json_response(
            {
                "data": [
                    {
                        "id": self.datarefs[path]["id"],
                        "name": path,
                        "value_type": self.datarefs[path]["value_type"],
                    }
                    for path in paths
                ]
            }
        )

    async def lookup_commands(self, request):
        paths = request.query.getall("filter[name]", [])
        self.lookups.append(("commands", paths))
        if any(path not in self.commands for path in paths):
            return web.json_response({"error_code": "invalid_command_name"}, status=404)
        return web.json_response(
            {
                "data": [
                    {"id": self.commands[path], "name": path, "description": "Test command"}
                    for path in paths
                ]
            }
        )

    async def get_value(self, request):
        identifier = int(request.match_info["identifier"])
        for record in self.datarefs.values():
            if record["id"] == identifier:
                snapshot = json.loads(json.dumps(record["value"]))
                if self.read_gate is not None:
                    self.read_started.put_nowait(identifier)
                    await self.read_gate.wait()
                return web.json_response({"data": snapshot})
        return web.json_response({"error_code": "invalid_dataref_id"}, status=404)

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                message = json.loads(frame.data)
                self.requests.append(message)
                kind = message["type"]
                if kind in self.ignore_ack:
                    continue
                if kind in self.reject:
                    await ws.send_json(
                        {
                            "type": "result",
                            "req_id": message["req_id"],
                            "success": False,
                            "error_code": "dataref_is_readonly",
                            "error_message": "Read only",
                        }
                    )
                    continue
                await ws.send_json(
                    {
                        "type": "result",
                        "req_id": message["req_id"],
                        "success": True,
                    }
                )
                if kind == "dataref_subscribe_values" and self.initial_snapshots:
                    identifiers = {item["id"] for item in message["params"]["datarefs"]}
                    await ws.send_json(
                        {
                            "type": "dataref_update_values",
                            "data": {
                                str(record["id"]): record["value"]
                                for record in self.datarefs.values()
                                if record["id"] in identifiers
                            },
                        }
                    )
        finally:
            self.sockets.discard(ws)
        return ws

    async def push(self, data):
        for socket in list(self.sockets):
            await socket.send_json({"type": "dataref_update_values", "data": data})

    async def drop(self):
        for socket in list(self.sockets):
            await socket.close()


@pytest_asyncio.fixture
async def simulator():
    server = SimulatorServer()
    await server.start()
    yield server
    await server.close()


@pytest_asyncio.fixture
async def client(simulator):
    connection = XPlaneClient(simulator.base_url, timeout=0.2)
    yield connection
    await connection.close()


async def wait_lost(client):
    while True:
        event = await asyncio.wait_for(client.read_event(), 1)
        if event["kind"] == "lost":
            return event


async def subscribe_heading(client):
    await client.connect()
    await client.resolve_datarefs({"heading": "sim/test/heading"})
    await client.subscribe(["heading"])


async def test_requires_v2_and_cleans_failed_connection(simulator, client):
    simulator.versions = ["v1"]
    with pytest.raises(XPlaneAPIError, match="unsupported_api"):
        await client.connect()
    assert not client.healthy
    assert client._session is None
    assert client._ws is None


async def test_exact_resolution_splits_missing_names_and_merges_keys(client, simulator):
    capabilities = await client.connect()
    assert capabilities["x-plane"]["version"] == "12.1.4"
    assert await client.resolve_datarefs(
        {
            "heading": "sim/test/heading",
            "missing": "sim/not/registered",
        }
    ) == {"heading": 101}
    assert await client.resolve_datarefs({"gear": "sim/test/gear"}) == {"gear": 102}
    assert await client.resolve_commands(
        [
            "sim/test/command",
            "sim/missing/command",
        ]
    ) == {"sim/test/command": 201}
    assert client._ids == {"heading": 101, "gear": 102}
    assert client.dataref_ids == {"heading": 101, "gear": 102}
    assert client.dataref_types == {"heading": "float", "gear": "float_array"}
    assert client.command_ids == {"sim/test/command": 201}
    # Runtime/profile validation can inspect metadata without mutating the
    # transport's resolved IDs or types.
    client.dataref_ids.clear()
    client.dataref_types.clear()
    client.command_ids.clear()
    assert client.dataref_ids == {"heading": 101, "gear": 102}
    assert client.dataref_types["heading"] == "float"
    assert client.command_ids == {"sim/test/command": 201}
    assert all(paths for _, paths in simulator.lookups)
    with pytest.raises(KeyError, match="Unresolved"):
        await client.subscribe(["missing"])


async def test_initial_snapshot_static_values_full_arrays_and_raw_data(simulator, client):
    simulator.initial_snapshots = False
    await client.connect()
    await client.resolve_datarefs(
        {
            "heading": "sim/test/heading",
            "gear": "sim/test/gear",
            "identity": "sim/aircraft/view/acf_relative_path",
        }
    )
    await client.subscribe(["heading", "gear", "identity"])
    assert client.get("heading") == 270
    assert client.get("gear") == [1, 0, 1]
    assert client.get("identity") == "QWlyY3JhZnQvYzE3Mi5hY2YA"
    assert client.values["gear"].generation == client.generation
    subscription = simulator.requests[-1]
    assert subscription["params"]["datarefs"] == [{"id": 101}, {"id": 102}, {"id": 103}]


async def test_sparse_updates_preserve_other_cached_values(simulator, client):
    await client.connect()
    await client.resolve_datarefs({"heading": "sim/test/heading", "gear": "sim/test/gear"})
    await client.subscribe(["heading", "gear"])
    gear = client.values["gear"]
    revision = client.values["heading"].revision
    await simulator.push({"101": 271})
    assert (
        await client.wait_value("heading", lambda value: value == 271, min_revision=revision + 1)
        == 271
    )
    assert client.values["gear"] == gear


async def test_independent_health_expiry_invalidates_pending_ack_and_cache(simulator, client):
    await subscribe_heading(client)
    await client.resolve_commands(["sim/test/command"])
    simulator.ignore_ack.add("command_set_is_active")
    pending = asyncio.create_task(client.command("sim/test/command"))
    while not any(r["type"] == "command_set_is_active" for r in simulator.requests):
        await asyncio.sleep(0)
    generation = client.generation
    await client.expire_health("Independent lease expired", generation=generation)
    with pytest.raises(XPlaneConnectionError, match="Independent lease expired"):
        await pending
    assert not client.healthy and client.generation == generation + 1
    assert not client.values["heading"].valid and client.get("heading") is None
    assert len([r for r in simulator.requests if r["type"] == "command_set_is_active"]) == 1
    assert (await wait_lost(client))["reason"] == "Independent lease expired"


async def test_expiry_from_an_old_generation_cannot_close_reconnected_session(client):
    await subscribe_heading(client)
    old_generation = client.generation
    await client.connect()
    await client.resolve_datarefs({"heading": "sim/test/heading"})
    await client.subscribe(["heading"])
    generation = client.generation
    await client.expire_health("Old lease expired", generation=old_generation)
    assert client.healthy and client.generation == generation
    assert client.get("heading") == 270


async def test_late_rest_snapshot_cannot_overwrite_newer_websocket_value(simulator, client):
    await subscribe_heading(client)
    simulator.read_gate = asyncio.Event()
    resync = asyncio.create_task(client.resync())
    assert await asyncio.wait_for(simulator.read_started.get(), 1) == 101
    await simulator.push({"101": 280})
    await client.wait_value("heading", lambda value: value == 280)
    revision = client.values["heading"].revision
    simulator.read_gate.set()
    await resync
    assert client.get("heading") == 280
    assert client.values["heading"].revision == revision


async def test_command_and_write_ack_are_not_optimistic_feedback(simulator, client):
    await subscribe_heading(client)
    await client.resolve_commands(["sim/test/command"])
    await client.command("sim/test/command")
    assert simulator.requests[-1]["params"]["commands"] == [
        {"id": 201, "is_active": True, "duration": 0},
    ]
    await client.write("heading", 90)
    assert client.get("heading") == 270
    assert simulator.requests[-1]["params"]["datarefs"] == [{"id": 101, "value": 90}]
    simulator.reject.add("dataref_set_values")
    with pytest.raises(XPlaneAPIError, match="dataref_is_readonly"):
        await client.write("heading", 100)
    assert client.get("heading") == 270
    assert client.healthy


async def test_ack_timeout_faults_connection_and_never_replays_action(simulator, client):
    await subscribe_heading(client)
    await client.resolve_commands(["sim/test/command"])
    generation = client.generation
    simulator.ignore_ack.add("command_set_is_active")
    with pytest.raises(XPlaneTimeoutError, match="not retried"):
        await client.command("sim/test/command")
    assert not client.healthy
    assert client.generation > generation
    assert client.get("heading") is None
    assert not client.values["heading"].valid
    with pytest.raises(XPlaneConnectionError):
        await client.command("sim/test/command")
    assert len([r for r in simulator.requests if r["type"] == "command_set_is_active"]) == 1
    event = await wait_lost(client)
    assert "timed out" in event["reason"]


async def test_heartbeat_requires_ack_even_when_simulator_values_are_unchanged(simulator, client):
    simulator.initial_snapshots = False
    await client.connect()
    await client.heartbeat()
    assert simulator.requests[-1]["params"]["datarefs"] == [{"id": 104}]
    assert client.healthy
    simulator.ignore_ack.add("dataref_subscribe_values")
    with pytest.raises(XPlaneTimeoutError):
        await client.heartbeat()
    assert not client.healthy


async def test_disconnect_wakes_feedback_waiters_and_reconnect_resolves_new_ids(simulator, client):
    await subscribe_heading(client)
    await client.resolve_commands(["sim/test/command"])
    old_generation = client.generation
    waiter = asyncio.create_task(client.wait_value("heading", lambda value: value == 99))
    await simulator.drop()
    await wait_lost(client)
    with pytest.raises(XPlaneConnectionError):
        await waiter
    assert client.get("heading", "missing") == "missing"
    simulator.datarefs["sim/test/heading"]["id"] = 901
    simulator.commands["sim/test/command"] = 902
    await client.connect()
    assert client.generation > old_generation
    assert client.get("heading") is None
    with pytest.raises(KeyError, match="Unresolved command"):
        await client.command("sim/test/command")
    assert await client.resolve_datarefs({"heading": "sim/test/heading"}) == {"heading": 901}
    assert await client.resolve_commands(["sim/test/command"]) == {"sim/test/command": 902}
    await client.subscribe(["heading"])
    await client.command("sim/test/command")
    assert simulator.requests[-1]["params"]["commands"][0]["id"] == 902


async def test_replace_subscription_invalidates_removed_keys(simulator, client):
    await client.connect()
    await client.resolve_datarefs({"heading": "sim/test/heading", "gear": "sim/test/gear"})
    await client.subscribe(["heading", "gear"])
    await client.subscribe(["heading"])
    assert client.get("gear") is None
    assert client.get("heading") == 270
    assert any(
        r["type"] == "dataref_unsubscribe_values" and r["params"] == {"datarefs": "all"}
        for r in simulator.requests
    )
    # An already-in-flight update for the old subscription must not revive it.
    await simulator.push({"102": [1, 1, 1], "101": 275})
    await client.wait_value("heading", lambda value: value == 275)
    assert client.get("gear") is None


async def test_revision_wait_requires_new_feedback_and_rejects_nonfinite_values(simulator, client):
    await subscribe_heading(client)
    revision = client.values["heading"].revision
    waiter = asyncio.create_task(
        client.wait_value(
            "heading",
            lambda value: value == 270,
            min_revision=revision + 1,
        )
    )
    await client.resync()
    assert await waiter == 270
    with pytest.raises(ValueError, match="finite"):
        await client.write("heading", float("nan"))
    await simulator.push({"101": None})
    while client.values["heading"].valid:
        await asyncio.wait_for(client.read_event(), 1)
    assert client.get("heading") is None
    with pytest.raises(XPlaneTimeoutError, match="confirming"):
        await client.wait_value("heading", lambda value: True, timeout=0.01)
    # A feedback predicate timeout alone is not proof of transport failure.
    assert client.healthy


async def test_malformed_websocket_frame_faults_once(simulator, client):
    await subscribe_heading(client)
    for socket in list(simulator.sockets):
        await socket.send_str("invalid json")
    event = await wait_lost(client)
    assert "Invalid WebSocket JSON" in event["reason"]
    assert not client.healthy
    assert client.get("heading") is None
    assert not any(event["kind"] == "lost" for event in list(client.updates._queue))


async def test_http_resync_timeout_invalidates_feedback(simulator, client):
    await subscribe_heading(client)
    simulator.read_gate = asyncio.Event()
    with pytest.raises(XPlaneTimeoutError, match="HTTP response timed out"):
        await client.resync()
    assert not client.healthy
    assert client.get("heading") is None
    await wait_lost(client)


async def test_disconnect_fails_pending_ack_without_replaying(simulator, client):
    await subscribe_heading(client)
    await client.resolve_commands(["sim/test/command"])
    simulator.ignore_ack.add("command_set_is_active")
    operation = asyncio.create_task(client.command("sim/test/command"))
    while not any(r["type"] == "command_set_is_active" for r in simulator.requests):
        await asyncio.sleep(0)
    await simulator.drop()
    with pytest.raises(XPlaneConnectionError):
        await operation
    assert len([r for r in simulator.requests if r["type"] == "command_set_is_active"]) == 1


async def test_closing_with_inflight_rest_does_not_fault_new_generation(simulator, client):
    await subscribe_heading(client)
    simulator.read_gate = asyncio.Event()
    snapshot = asyncio.create_task(client.resync())
    await asyncio.wait_for(simulator.read_started.get(), 1)
    await client.connect()
    with pytest.raises(XPlaneConnectionError):
        await snapshot
    assert client.healthy
    assert client.get("heading") is None
    simulator.read_gate.set()
