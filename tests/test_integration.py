"""Exercise the runtime through the real serial worker and HTTP/WS transport."""

import asyncio
import base64
import time

import pytest
from test_board import FakeSerial, reply
from test_xplane import SimulatorServer

import ap_panel.board as board_module
from ap_panel.config import Settings, load_hardware
from ap_panel.profiles import load_profiles
from ap_panel.runtime import Runtime


async def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Integration condition did not arrive")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_usb_event_to_native_request_to_actual_feedback_and_loss(monkeypatch):
    hardware = load_hardware()
    profiles = load_profiles()
    profile = next(p for p in profiles if p.id == "c90b")
    server = SimulatorServer()
    samples = dict.fromkeys(profile.datarefs, 0)
    samples.update(
        aircraft=base64.b64encode(profile.aircraft_paths[0].encode()).decode(),
        heading=274,
        selected_altitude=5500,
        held_altitude=4000,
        pilot_altitude=4000,
        vs=700,
        baro=29.92,
        course=143,
        gear=[0.0] * 10,
        source=2,
    )
    server.datarefs = {
        ref.path: {
            "id": index + 100,
            "value_type": "float" if ref.value_type == "numeric" else ref.value_type,
            "value": samples[key],
        }
        for index, (key, ref) in enumerate(profile.datarefs.items())
    }
    server.commands = {path: index + 1000 for index, path in enumerate(profile.command_paths())}
    await server.start()
    fake = FakeSerial(config=hardware.raw_config)
    monkeypatch.setattr(board_module, "STARTUP_DELAY", 0)
    monkeypatch.setattr(board_module, "READ_TIMEOUT", 0.005)
    monkeypatch.setattr(board_module.serial, "Serial", fake.open)
    runtime = Runtime(
        hardware, profiles, Settings(port="fake", simulator_url=server.base_url), mode="commission"
    )
    try:
        await runtime.launch()
        await until(lambda: runtime.ready)
        assert runtime.status()["simulator"]["version"] == "12.1.4"
        assert not [m for m in server.requests if m["type"] == "command_set_is_active"]
        await runtime.start()
        await until(lambda: runtime.panel.rows[0].startswith("HDG 274"))
        fake.inject(reply(6, "HDG", 3))  # Fast event replaces an ordinary detent.
        await until(lambda: any(m["type"] == "command_set_is_active" for m in server.requests))
        commands = [m for m in server.requests if m["type"] == "command_set_is_active"]
        assert len(commands) == 1
        assert commands[0]["params"]["commands"] == [
            {"id": server.commands["sim/autopilot/heading_up"], "is_active": True, "duration": 0}
        ]
        assert runtime.panel.rows[0].startswith("HDG 274")  # ACK did not change the selector.
        heading = server.datarefs[profile.datarefs["heading"].path]
        heading["value"] = 275
        await server.push({str(heading["id"]): 275})
        await until(lambda: runtime.panel.rows[0].startswith("HDG 275"))
        await until(
            lambda: any(
                m.command == 25 and m.arguments[1].startswith("HDG 275") for m in fake.messages
            )
        )
        assert not any(m.command in (11, 13, 14, 16) for m in fake.messages)
        await server.drop()
        await until(lambda: not runtime.ready)
        await until(lambda: fake.closed)
        assert not runtime.running and runtime.panel.lamps == 0
        assert b"27,0,3,0,255,255,255;" in fake.writes[-3:]
        assert len([m for m in server.requests if m["type"] == "command_set_is_active"]) == 1
    finally:
        await runtime.close()
        await server.close()
