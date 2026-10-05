"""Lifecycle tests with injected transports; no USB or simulator is accessed."""

import asyncio
import base64
import json
import time

import pytest
import pytest_asyncio

from ap_panel.actions import ALT_ARM, VS_PRESELECT
from ap_panel.board import BoardError, BoardEvent, BoardIdentity
from ap_panel.config import Settings, load_hardware
from ap_panel.models import InputEvent, RuntimeState, TelemetryValue
from ap_panel.profiles import load_profiles
from ap_panel.runtime import Runtime
from ap_panel.xplane import XPlaneAPIError, XPlaneClient


class FakeBoard:
    def __init__(self, hardware, levels=None):
        self.hardware = hardware
        self.connected = False
        self.levels = dict.fromkeys(range(16), False)
        self.levels.update(levels or {})
        self.events = asyncio.Queue()
        self.operations = []
        self.snapshot_started = asyncio.Queue()
        self.snapshot_gate = None
        self.heartbeat_error = None
        self.heartbeat_gate = None
        self.closed = False
        self.names = {pin.name: pin.index for pin in hardware.inputs}

    async def connect(self):
        self.operations.append(("connect",))
        self.connected = True
        return BoardIdentity("MobiFlight RaspiPico2", "Panel", "SN-TEST", "3.1.4", "1")

    async def snapshot(self):
        self.operations.append(("snapshot",))
        self.snapshot_started.put_nowait(None)
        if self.snapshot_gate is not None:
            await self.snapshot_gate.wait()
        return dict(self.levels)

    async def heartbeat(self):
        if self.heartbeat_error:
            raise self.heartbeat_error

    async def read_event(self):
        item = await self.events.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def clear(self, message=""):
        self.operations.append(("clear", message))

    async def write_frame(self, frame, force=False, lcd=True):
        self.operations.append(("frame", frame))

    async def close(self):
        self.connected = False
        self.closed = True
        self.operations.append(("close",))

    def button(self, name, pressed, timestamp=None):
        index = self.names[name]
        self.levels[index] = pressed
        self.events.put_nowait(
            BoardEvent(
                "button",
                "IN",
                index,
                0 if pressed else 1,
                time.monotonic() if timestamp is None else timestamp,
            )
        )

    def encoder(self, name="HDG", clockwise=True, timestamp=None):
        self.events.put_nowait(
            BoardEvent(
                "encoder",
                name,
                None,
                2 if clockwise else 0,
                time.monotonic() if timestamp is None else timestamp,
            )
        )


class FakeSimulator:
    def __init__(self, profiles, aircraft_profile):
        self.healthy = False
        self.generation = 0
        self.capabilities = {"api": {"versions": ["v2"]}, "x-plane": {"version": "12.1.4"}}
        self.events = asyncio.Queue()
        self.operations = []
        self.dataref_ids = {}
        self.dataref_types = {}
        self.command_ids = {}
        self.values = {}
        self.backend = dict.fromkeys(aircraft_profile.datarefs, 0)
        self.backend.update(
            aircraft=self.encoded_path(aircraft_profile.aircraft_paths[0]),
            ap_type=aircraft_profile.expected_ap_types[0]
            if aircraft_profile.expected_ap_types
            else 0,
            heading=42,
            vs=600,
            selected_altitude=8000,
            held_altitude=3000,
            pilot_altitude=3000,
            gear=[0.0] * 10,
            baro=29.92,
            course=90,
            nav1_bearing=123,
            nav1_flag=1,
            nav1_type=4,
        )
        self.catalog = {
            spec.path: ("float" if spec.value_type == "numeric" else spec.value_type)
            for p in profiles
            for spec in p.datarefs.values()
        }
        self.missing_keys = set()
        self.wrong_types = {}
        self.subscribed = set()
        self.heartbeat_error = None
        self.heartbeat_gate = None
        self.command_gate = None
        self.command_started = asyncio.Queue()
        self.command_failures = {}
        self.command_hook = None
        self.subscribe_hook = None
        self.closed = False

    @staticmethod
    def encoded_path(path):
        return base64.b64encode(path.encode()).decode()

    async def connect(self):
        self.operations.append(("connect",))
        self.healthy = True
        self.generation += 1
        return self.capabilities

    async def resolve_datarefs(self, specs):
        found = {}
        for key, path in specs.items():
            if key not in self.missing_keys and path in self.catalog:
                found[key] = len(self.dataref_ids) + 1
                self.dataref_ids[key] = found[key]
                self.dataref_types[key] = self.wrong_types.get(key, self.catalog[path])
        return found

    async def resolve_commands(self, paths):
        self.command_ids.update({path: i + 1 for i, path in enumerate(paths)})
        return dict(self.command_ids)

    async def subscribe(self, keys):
        self.operations.append(("subscribe", tuple(keys)))
        self.subscribed = set(keys)
        if self.subscribe_hook:
            self.subscribe_hook(keys)
        await self.resync()

    async def resync(self):
        for key in self.subscribed:
            previous = self.values.get(key)
            self.values[key] = TelemetryValue(
                self.backend.get(key),
                revision=previous.revision + 1 if previous else 1,
                generation=self.generation,
                updated_at=time.monotonic(),
            )

    async def heartbeat(self):
        if self.heartbeat_gate:
            await self.heartbeat_gate.wait()
        if self.heartbeat_error:
            self.healthy = False
            raise self.heartbeat_error

    async def expire_health(self, reason, *, generation=None):
        if generation is not None and generation != self.generation:
            return
        self.healthy = False
        self.generation += 1
        self.values = {
            key: TelemetryValue(
                value.value,
                valid=False,
                generation=self.generation,
                revision=value.revision + 1,
                updated_at=value.updated_at,
            )
            for key, value in self.values.items()
        }
        self.events.put_nowait({"kind": "lost", "reason": reason})

    def get(self, key, default=None):
        entry = self.values.get(key)
        return (
            entry.value
            if entry and entry.valid and entry.generation == self.generation
            else default
        )

    async def read_event(self):
        return await self.events.get()

    async def command(self, path):
        self.operations.append(("command", path))
        self.command_started.put_nowait(path)
        if self.command_gate:
            await self.command_gate.wait()
        if path in self.command_failures:
            raise self.command_failures[path]
        if path == VS_PRESELECT:
            self.backend["vs_status"] = 2
        elif path == ALT_ARM:
            self.backend["alt_armed"] = 1
        if self.command_hook:
            self.command_hook(path)

    async def write(self, key, value):
        self.operations.append(("write", key, value))
        self.backend[key] = value

    async def wait_value(self, key, predicate, timeout=2.0, min_revision=None):
        if not predicate(self.get(key)):
            raise RuntimeError("Missing expected aircraft feedback")
        return self.get(key)

    async def close(self):
        self.healthy = False
        self.closed = True
        self.operations.append(("close",))

    async def push(self, **values):
        self.backend.update(values)
        await self.resync()
        self.events.put_nowait(
            {"kind": "update", "keys": list(values), "generation": self.generation}
        )

    @property
    def mutations(self):
        return [event for event in self.operations if event[0] in ("command", "write")]


class Harness:
    def __init__(self, mode="commission", verified=False, levels=None, aircraft="c90b"):
        self.hardware = load_hardware()
        self.hardware.bench_verified = verified
        self.profiles = load_profiles()
        self.profile = next(p for p in self.profiles if p.id == aircraft)
        if verified:
            self.profile.simulator_tested = True
            self.profile.tested_aircraft_path = self.profile.aircraft_paths[0]
        self.boards = []
        self.simulators = []
        self.levels = levels
        self.board_setup = None
        self.simulator_setup = None
        self.runtime = Runtime(
            self.hardware,
            self.profiles,
            Settings(),
            mode=mode,
            board_factory=self.board_factory,
            simulator_factory=self.simulator_factory,
        )

    def board_factory(self, **kwargs):
        assert kwargs["expected_config"] == self.hardware.raw_config
        board = FakeBoard(self.hardware, self.levels)
        if self.board_setup:
            self.board_setup(board)
        self.boards.append(board)
        return board

    def simulator_factory(self, url):
        sim = FakeSimulator(self.profiles, self.profile)
        if self.simulator_setup:
            self.simulator_setup(sim)
        self.simulators.append(sim)
        return sim

    async def launch_ready(self):
        await self.runtime.launch()
        await eventually(lambda: self.runtime.ready)


async def eventually(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def harnesses(monkeypatch):
    monkeypatch.setattr("ap_panel.runtime.RECONNECT_DELAY", 0.03)
    created = []

    def make(**kwargs):
        harness = Harness(**kwargs)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        await harness.runtime.close()


async def test_startup_is_read_only_and_held_buttons_and_gear_are_baselined(harnesses):
    h = harnesses()
    ap = next(p.index for p in h.hardware.inputs if p.name == "AP")
    gear = next(p.index for p in h.hardware.inputs if p.name == "GEAR")
    h.levels = {ap: True, gear: True}
    await h.launch_ready()
    runtime, board, sim = h.runtime, h.boards[0], h.simulators[0]
    assert runtime.state == RuntimeState.READY and not runtime.running
    assert not sim.mutations
    assert not [op for op in board.operations if op[0] == "frame"]
    assert runtime.status()["simulator"]["version"] == "12.1.4"
    await runtime.start()
    assert [op[0] for op in board.operations].count("snapshot") == 2
    board.button("AP", True)
    board.button("GEAR", True)
    await asyncio.sleep(0.08)
    assert not sim.mutations
    board.button("AP", False)
    await asyncio.sleep(0.05)
    board.button("AP", True)
    await eventually(lambda: bool(sim.mutations))
    assert sim.mutations == [("command", "sim/autopilot/servos_toggle")]
    board.button("GEAR", False)
    await eventually(lambda: len(sim.mutations) == 2)
    assert sim.mutations[-1] == ("command", "sim/flight_controls/landing_gear_up")


async def test_start_discards_old_events_and_captures_new_held_baseline(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, board, sim = h.runtime, h.boards[0], h.simulators[0]
    board.encoder(timestamp=time.monotonic() - 10)
    board.levels[board.names["HDG_PUSH"]] = True
    await runtime.start()
    board.button("HDG_PUSH", True)
    await asyncio.sleep(0.08)
    assert not sim.mutations
    board.encoder()
    await eventually(lambda: bool(sim.mutations))
    assert sim.mutations == [("command", "sim/autopilot/heading_up")]


async def test_stop_drops_queue_clears_outputs_and_never_replays(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, board, sim = h.runtime, h.boards[0], h.simulators[0]
    await runtime.start()
    sim.command_gate = asyncio.Event()
    board.encoder()
    await sim.command_started.get()
    board.encoder("BARO")
    await eventually(lambda: runtime._commands.qsize() == 1)
    await runtime.stop()
    assert not runtime.running and runtime._commands.empty()
    assert runtime.panel.lamps == 0 and not runtime.panel.text.strip()
    assert board.operations[-1] == ("clear", "")
    sim.command_gate.set()
    await asyncio.sleep(0.08)
    assert len(sim.mutations) == 1
    await runtime.start()
    await asyncio.sleep(0.08)
    assert len(sim.mutations) == 1


async def test_stop_during_start_snapshot_cannot_reenable_dispatch(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, board = h.runtime, h.boards[0]
    await board.snapshot_started.get()
    board.snapshot_gate = asyncio.Event()
    start = asyncio.create_task(runtime.start())
    await board.snapshot_started.get()
    await runtime.stop()
    board.snapshot_gate.set()
    with pytest.raises(ValueError, match="changed during the input snapshot"):
        await start
    assert not runtime.running
    assert not h.simulators[0].mutations


async def test_start_rechecks_aircraft_after_awaited_snapshot(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, board, sim = h.runtime, h.boards[0], h.simulators[0]
    await board.snapshot_started.get()
    board.snapshot_gate = asyncio.Event()
    start = asyncio.create_task(runtime.start())
    await board.snapshot_started.get()
    sim.backend["aircraft"] = sim.encoded_path("Aircraft/Other.acf")
    await sim.resync()
    board.snapshot_gate.set()
    with pytest.raises(ValueError, match="Connection changed"):
        await start
    assert not runtime.running and not sim.mutations


async def test_stop_invalidates_render_frame_waiting_for_output_lock(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, board = h.runtime, h.boards[0]
    await runtime.start()
    await runtime._output_lock.acquire()
    rendered = asyncio.Event()
    original = runtime.renderer.render

    def render(*args):
        result = original(*args)
        rendered.set()
        return result

    runtime.renderer.render = render
    await asyncio.wait_for(rendered.wait(), 1)
    stop = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    runtime._output_lock.release()
    await stop
    assert not [op for op in board.operations if op[0] == "frame"]
    assert runtime.panel.lamps == 0 and not runtime.panel.text.strip()


async def test_run_requires_both_certifications_while_commission_allows_dispatch(harnesses):
    unverified = harnesses(mode="run")
    await unverified.launch_ready()
    with pytest.raises(ValueError, match="Commissioning is incomplete"):
        await unverified.runtime.start()
    assert not unverified.runtime.running
    verified = harnesses(mode="run", verified=True)
    await verified.launch_ready()
    await verified.runtime.start()
    assert verified.runtime.running
    commissioning = harnesses(mode="commission")
    await commissioning.launch_ready()
    await commissioning.runtime.start()
    assert commissioning.runtime.running


async def test_observe_renders_feedback_but_never_dispatches_and_stop_stays_clear(harnesses):
    h = harnesses(mode="observe")
    await h.launch_ready()
    runtime, board, sim = h.runtime, h.boards[0], h.simulators[0]
    await sim.push(ap=1, hdg_status=2)
    board.encoder()
    await eventually(lambda: runtime.panel.lamps != 0)
    assert not sim.mutations
    with pytest.raises(ValueError, match="Start is available only"):
        await runtime.start()
    await runtime.stop()
    count = len(board.operations)
    await asyncio.sleep(0.12)
    assert len(board.operations) == count
    assert runtime.panel.lamps == 0 and not runtime.panel.text.strip()


@pytest.mark.parametrize(
    "missing,type_key", [("heading", None), (None, "heading"), ("aircraft", None)]
)
async def test_required_binding_failure_never_allows_start(harnesses, missing, type_key):
    h = harnesses()

    def setup(sim):
        if missing:
            sim.missing_keys.add(missing)
        if type_key:
            sim.wrong_types[type_key] = "data"

    h.simulator_setup = setup
    await h.runtime.launch()
    await eventually(lambda: any(e["kind"] == "fault" for e in h.runtime.events))
    assert not h.runtime.running and not h.runtime.ready
    with pytest.raises(ValueError):
        await h.runtime.start()
    assert all(not sim.mutations for sim in h.simulators)


@pytest.mark.parametrize(
    "key,value", [("heading", float("nan")), ("ap_type", True), ("heading", None)]
)
async def test_invalid_required_initial_telemetry_is_rejected(harnesses, key, value):
    h = harnesses()
    h.simulator_setup = lambda sim: sim.backend.update({key: value})
    await h.runtime.launch()
    await eventually(lambda: any(e["kind"] == "fault" for e in h.runtime.events))
    assert not h.runtime.running and not h.runtime.ready


async def test_missing_optional_binding_does_not_block_other_controls(harnesses):
    h = harnesses()
    h.simulator_setup = lambda sim: sim.missing_keys.add("yd_selected")
    await h.launch_ready()
    await h.runtime.start()
    h.boards[0].encoder()
    await eventually(lambda: bool(h.simulators[0].mutations))
    assert h.runtime.status()["capabilities"]["YD"]["available"] is False


async def test_incompatible_aircraft_stays_unavailable_without_commands(harnesses):
    h = harnesses()
    h.simulator_setup = lambda sim: sim.backend.update(
        aircraft=sim.encoded_path("Aircraft/Other.acf")
    )
    await h.runtime.launch()
    await eventually(lambda: h.runtime.error is not None)
    assert h.runtime.profile is None and not h.runtime.ready
    assert "No compatible profile" in h.runtime.error
    with pytest.raises(ValueError):
        await h.runtime.start()
    assert not h.simulators[0].mutations


async def test_profile_selection_requires_stop_and_takes_another_baseline(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    with pytest.raises(ValueError, match="Stop before"):
        await h.runtime.select_profile("c90b")
    await h.runtime.stop()
    with pytest.raises(ValueError, match="incompatible"):
        await h.runtime.select_profile("c172-analog")
    await h.runtime.select_profile("c90b")
    assert h.runtime.ready and not h.runtime.running
    assert [op[0] for op in h.boards[0].operations].count("snapshot") == 3
    assert not h.simulators[0].mutations


async def test_aircraft_change_discards_queue_and_reconnect_requires_new_start(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime, sim = h.runtime, h.simulators[0]
    await runtime.start()
    sim.command_gate = asyncio.Event()
    h.boards[0].encoder()
    await sim.command_started.get()
    h.boards[0].encoder("BARO")
    await eventually(lambda: runtime._commands.qsize() == 1)
    await sim.push(ap_type=77)
    await eventually(lambda: len(h.simulators) == 2 and runtime.ready)
    assert not runtime.running and runtime._commands.empty()
    assert sim.closed and h.boards[0].closed
    assert len(sim.mutations) == 1 and not h.simulators[1].mutations
    await runtime.start()
    h.boards[1].encoder()
    await eventually(lambda: bool(h.simulators[1].mutations))


async def test_aircraft_change_mid_binding_cannot_publish_old_profile_ready(harnesses):
    h = harnesses()

    def setup(sim):
        def change(keys):
            if "heading" in keys:
                sim.backend["aircraft"] = sim.encoded_path("Aircraft/Other.acf")

        sim.subscribe_hook = change

    h.simulator_setup = setup
    await h.runtime.launch()
    await eventually(
        lambda: any("changed while resolving" in e["message"] for e in h.runtime.events)
    )
    assert not h.runtime.ready and not h.runtime.running


async def test_simulator_lost_event_clears_panel_and_dispatch(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    sim, board = h.simulators[0], h.boards[0]
    await sim.push(ap=1)
    await eventually(lambda: h.runtime.panel.lamps != 0)
    sim.healthy = False
    sim.events.put_nowait({"kind": "lost", "reason": "Socket disconnected"})
    await eventually(lambda: board.closed)
    assert not h.runtime.running
    assert h.runtime.panel.lamps == 0
    assert ("clear", "CONNECTION LOST") in board.operations
    assert any("Socket disconnected" in e["message"] for e in h.runtime.events)


async def test_failed_simulator_heartbeat_lease_clears_outputs(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    sim, board = h.simulators[0], h.boards[0]
    sim.heartbeat_error = RuntimeError("Heartbeat acknowledgement timed out")
    await eventually(lambda: board.closed, timeout=1.5)
    assert not h.runtime.running and h.runtime.panel.lamps == 0
    assert ("clear", "CONNECTION LOST") in board.operations


async def test_command_backlog_is_bounded_and_overflow_fault_discards_it(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime = h.runtime
    await runtime.start()
    epoch = runtime._epoch
    for _ in range(33):
        runtime._accept_input(InputEvent("HDG", "encoder", 1, time.monotonic(), epoch))
    assert not runtime.running and runtime._commands.empty()
    assert runtime.state == RuntimeState.FAULT
    assert "overflow" in runtime.error
    assert not h.simulators[0].mutations


async def test_stale_queued_event_faults_instead_of_delayed_command(harnesses):
    h = harnesses()
    await h.launch_ready()
    runtime = h.runtime
    await runtime.start()
    runtime._accept_input(InputEvent("HDG", "encoder", 1, time.monotonic() - 1, runtime._epoch))
    await eventually(lambda: not runtime.running)
    assert not h.simulators[0].mutations
    assert any("latency exceeded" in e["message"] for e in runtime.events)


async def test_failed_multi_step_action_stops_and_clears_without_replay(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    sim, board = h.simulators[0], h.boards[0]
    sim.command_failures[ALT_ARM] = XPlaneAPIError("rejected", "Native arm rejected")
    board.button("ALT_SEL", True)
    await eventually(lambda: not h.runtime.running)
    assert sim.mutations == [("command", VS_PRESELECT), ("command", ALT_ARM)]
    assert "inspect the simulator" in h.runtime.error
    assert board.operations[-1] == ("clear", "")
    await asyncio.sleep(0.08)
    assert len(sim.mutations) == 2 and h.runtime.ready


@pytest.mark.parametrize("aircraft", ["c90b", "c172-analog"])
async def test_crs_push_on_gps_is_unavailable_but_other_controls_keep_running(harnesses, aircraft):
    h = harnesses(aircraft=aircraft)
    await h.launch_ready()
    await h.runtime.start()
    sim, board = h.simulators[0], h.boards[0]
    await sim.push(source=2)
    board.button("CRS_PUSH", True)
    await eventually(
        lambda: any(
            e["kind"] == "unavailable" and "CRS_PUSH" in e["message"] for e in h.runtime.events
        )
    )
    assert h.runtime.running and not sim.mutations
    board.encoder()
    await eventually(lambda: bool(sim.mutations))
    assert sim.mutations == [("command", "sim/autopilot/heading_up")]


async def test_invalid_feedback_without_effect_does_not_stop_other_controls(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    sim, board = h.simulators[0], h.boards[0]
    await sim.push(hdg_status=3)
    board.button("HDG", True)
    await eventually(
        lambda: any(e["kind"] == "unavailable" and "HDG" in e["message"] for e in h.runtime.events)
    )
    assert h.runtime.running and not sim.mutations
    board.encoder("BARO")
    await eventually(lambda: bool(sim.mutations))
    assert sim.mutations == [("command", "sim/instruments/barometer_up")]


async def test_bench_connects_only_board_and_stop_cancels_walk(harnesses):
    h = harnesses(mode="bench")
    await h.launch_ready()
    assert not h.simulators
    with pytest.raises(ValueError):
        await h.runtime.start()
    await h.runtime.bench_walk()
    await eventually(lambda: any(op[0] == "frame" for op in h.boards[0].operations))
    await h.runtime.stop()
    count = len(h.boards[0].operations)
    await asyncio.sleep(0.08)
    assert len(h.boards[0].operations) == count
    assert h.runtime.panel.lamps == 0 and h.runtime._bench_task is None


async def test_stop_prevents_bench_frame_queued_behind_output_lock(harnesses):
    h = harnesses(mode="bench")
    await h.launch_ready()
    runtime, board = h.runtime, h.boards[0]
    await runtime._output_lock.acquire()
    lamp = asyncio.create_task(runtime.bench_lamp(3))
    await asyncio.sleep(0)
    stop = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    runtime._output_lock.release()
    await asyncio.gather(lamp, stop)
    assert not [op for op in board.operations if op[0] == "frame"]
    assert runtime._bench_task is None and runtime.panel.lamps == 0


async def test_shutdown_clears_closes_transports_and_all_tasks(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    board, sim = h.boards[0], h.simulators[0]
    await h.runtime.close()
    assert board.closed and sim.closed
    assert not h.runtime.running and not h.runtime.ready
    assert h.runtime.board is None and h.runtime.simulator is None
    assert not h.runtime._tasks and not h.runtime._session_tasks
    assert h.runtime.state == RuntimeState.STOPPED
    assert ("clear", "") in board.operations
    await h.runtime.close()
    with pytest.raises(RuntimeError):
        await h.runtime.launch()


async def test_board_read_loss_clears_and_invalidates_dispatch(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    board = h.boards[0]
    board.events.put_nowait(BoardError("USB disconnected"))
    await eventually(lambda: board.closed)
    assert not h.runtime.running
    assert h.runtime.panel.lamps == 0
    assert any("USB disconnected" in e["message"] for e in h.runtime.events)


async def test_demo_never_opens_transports_and_records_initial_replay_state(tmp_path):
    hardware, profiles = load_hardware(), load_profiles()

    def forbidden(*args, **kwargs):
        raise AssertionError("Demo must not connect to a transport")

    path = tmp_path / "demo.jsonl"
    runtime = Runtime(
        hardware,
        profiles,
        Settings(),
        mode="demo",
        record_path=path,
        board_factory=forbidden,
        simulator_factory=forbidden,
    )
    try:
        await runtime.launch()
        await eventually(lambda: runtime.ready)
        await eventually(lambda: runtime.panel.lamps != 0)
        assert not runtime.running and runtime.board is None
    finally:
        await runtime.close()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert {"metadata", "profile", "telemetry", "output"} <= {r["kind"] for r in records}
    telemetry = next(r["data"] for r in records if r["kind"] == "telemetry")
    assert telemetry["aircraft"]["valid"] and telemetry["ap"]["value"] == 1


async def test_independent_lease_expires_hung_heartbeat_without_waiting_ack_timeout(harnesses):
    h = harnesses()
    await h.launch_ready()
    await h.runtime.start()
    runtime, sim, board = h.runtime, h.simulators[0], h.boards[0]
    last_ack = runtime._simulator_last_ack
    sim.heartbeat_gate = asyncio.Event()
    await eventually(lambda: board.closed, timeout=2.5)
    elapsed = time.monotonic() - last_ack
    assert 2.0 <= elapsed <= 2.5
    assert not runtime.running and not sim.healthy
    assert all(not value.valid for value in sim.values.values())
    assert ("clear", "CONNECTION LOST") in board.operations


async def test_real_websocket_silent_ack_expires_lease_while_tcp_stays_open(harnesses):
    from test_xplane import SimulatorServer

    server = SimulatorServer()
    await server.start()
    h = harnesses()
    fake_values = FakeSimulator(h.profiles, h.profile).backend
    server.datarefs = {
        spec.path: {
            "id": index + 1,
            "value_type": "float" if spec.value_type == "numeric" else spec.value_type,
            "value": fake_values[key],
        }
        for index, (key, spec) in enumerate(h.profile.datarefs.items())
    }
    server.commands = {path: i + 100 for i, path in enumerate(h.profile.command_paths())}
    h.runtime.settings.simulator_url = server.base_url
    real_clients = []

    def real_factory(url):
        client = XPlaneClient(url, timeout=2.0)
        real_clients.append(client)
        return client

    h.runtime.simulator_factory = real_factory
    try:
        await h.launch_ready()
        await h.runtime.start()
        runtime, sim, board = h.runtime, real_clients[0], h.boards[0]
        last_ack = runtime._simulator_last_ack
        server.ignore_ack.add("dataref_subscribe_values")
        assert server.sockets  # TCP/WS remains connected; only ACKs are withheld.
        await eventually(lambda: board.closed, timeout=2.5)
        elapsed = time.monotonic() - last_ack
        assert 2.0 <= elapsed <= 2.5
        assert not runtime.running and not sim.healthy
        assert all(not value.valid for value in sim.values.values())
        assert ("clear", "CONNECTION LOST") in board.operations
        assert any("lease expired" in event["message"] for event in runtime.events)
    finally:
        await h.runtime.close()
        await server.close()


async def test_real_serial_edge_after_snapshot_reply_in_same_chunk_uses_new_baseline(
    harnesses, monkeypatch
):
    from test_board import FakeSerial, reply

    import ap_panel.board as board_module
    from ap_panel.board import BoardClient

    h = harnesses()
    gear_index = next(pin.index for pin in h.hardware.inputs if pin.name == "GEAR")

    def callback(fake, message):
        if message.command != 23:
            return False
        released = b"".join(reply(29, "IN", index, 1) for index in range(16) if index != 1)
        # The retrigger boundary and a real new gear transition share one read.
        fake.inject(released + reply(29, "IN", 1, 0) + reply(34) + reply(29, "IN", gear_index, 0))
        return True

    fake = FakeSerial(config=h.hardware.raw_config, callback=callback)
    monkeypatch.setattr(board_module.serial, "Serial", fake.open)
    monkeypatch.setattr(board_module, "STARTUP_DELAY", 0)
    monkeypatch.setattr(board_module, "READ_TIMEOUT", 0.005)
    monkeypatch.setattr(board_module, "TRANSACTION_TIMEOUT", 0.08)
    monkeypatch.setattr(board_module, "HEARTBEAT_TIMEOUT", 0.03)
    h.runtime.settings.port = "fake"

    def factory(**kwargs):
        board = BoardClient(**kwargs)
        h.boards.append(board)
        return board

    h.runtime.board_factory = factory
    await h.launch_ready()
    await eventually(lambda: h.runtime.processor.states.get("GEAR") is True)
    assert not h.simulators[0].mutations
    assert h.boards[0].snapshot_completed_at == h.runtime._baseline_at
    await h.runtime.start()
    await eventually(lambda: bool(h.simulators[0].mutations))
    assert h.simulators[0].mutations == [("command", "sim/flight_controls/landing_gear_down")]
    assert h.runtime.processor.states["GEAR"] is True
    await asyncio.sleep(0.08)
    assert len(h.simulators[0].mutations) == 1
