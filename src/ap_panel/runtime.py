"""Connection lifecycle, physical input dispatch and feedback-only panel rendering."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .actions import ActionError, ActionExecutor
from .board import BoardClient
from .config import Hardware, Settings
from .inputs import InputProcessor, action_name
from .models import InputEvent, PanelFrame, RuntimeState
from .outputs import OutputRenderer
from .profiles import Profile, aircraft_path
from .xplane import XPlaneClient

LOG = logging.getLogger(__name__)
MODES = {"run", "observe", "commission", "bench", "demo"}
RECONNECT_DELAY = 2.0
SIMULATOR_HEALTH_LEASE = 2.0
HEALTH_LEASE_POLL = 0.05


class Runtime:
    """Own all dispatch decisions on the asyncio thread.

    Each Start establishes a new physical baseline. A connection or aircraft
    change invalidates queued actions and always requires another Start.
    """

    def __init__(
        self,
        hardware: Hardware,
        profiles: list[Profile],
        settings: Settings,
        mode: str = "run",
        record_path: Path | None = None,
        board_factory: Callable[..., Any] = BoardClient,
        simulator_factory: Callable[..., Any] = XPlaneClient,
    ):
        if mode not in MODES or not profiles:
            raise ValueError("A supported runtime mode and at least one profile are required")
        self.hardware, self.profiles, self.settings, self.mode = hardware, profiles, settings, mode
        self.board_factory, self.simulator_factory = board_factory, simulator_factory
        self.board: Any = None
        self.simulator: Any = None
        self.identity: Any = None
        self.profile: Profile | None = None
        self.aircraft: str | None = None
        self.ap_type: int | None = None
        self.state = RuntimeState.STOPPED
        self.running = False
        self._display_enabled = False
        self.ready = False
        self.error: str | None = None
        self.configuration_ok = False
        self.processor = InputProcessor(hardware)
        self.renderer = OutputRenderer(hardware)
        self.panel = PanelFrame.message()
        self.events: deque[dict] = deque(maxlen=100)
        self.encoder_counts = {e.name: 0 for e in hardware.encoders}
        self._subscribers: set[asyncio.Queue] = set()
        self._commands: asyncio.Queue[tuple[int, InputEvent]] = asyncio.Queue(maxsize=32)
        self._epoch = 0
        self._baseline_at = 0.0
        self._simulator_last_ack: float | None = None
        self._closed = False
        self._tasks: list[asyncio.Task] = []
        self._session_tasks: list[asyncio.Task] = []
        self._session_fault: asyncio.Future | None = None
        self._bench_task: asyncio.Task | None = None
        self._output_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._record = None
        if record_path is not None:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents losing an earlier diagnostic session.
            self._record = record_path.open("x", encoding="utf-8")
            self._record_line(
                "metadata",
                {
                    "schema_version": 1,
                    "mode": mode,
                    "configuration_hash": hardware.configuration_hash,
                    "hardware": hardware.model_dump(),
                },
            )

    async def launch(self) -> None:
        if self._tasks or self._closed:
            raise RuntimeError("Runtime has already been launched or closed")
        self._tasks = [
            asyncio.create_task(self._supervise()),
            asyncio.create_task(self._render_loop()),
            asyncio.create_task(self._status_loop()),
        ]

    def _invalidate_dispatch(self) -> None:
        self.running = False
        self._display_enabled = False
        self._epoch += 1
        while not self._commands.empty():
            self._commands.get_nowait()
        self.processor.pending.clear()

    def _fault(self, reason: str) -> None:
        # This happens synchronously before any subsequent await or command.
        self._invalidate_dispatch()
        self.ready = False
        self.error = reason
        self.state = RuntimeState.FAULT
        if self._session_fault is not None and not self._session_fault.done():
            self._session_fault.set_result(reason)

    async def _clear(self, message: str = "") -> None:
        async with self._output_lock:
            self.panel = PanelFrame.message(message)
            if self.board is not None and self.board.connected:
                await self.board.clear(message)
        self._record_line("output", asdict(self.panel))

    async def _supervise(self) -> None:
        if self.mode == "demo":
            await self._demo()
            return
        while not self._closed:
            self._session_fault = asyncio.get_running_loop().create_future()
            try:
                self.error = None
                self.state = RuntimeState.CONNECTING_BOARD
                self.board = self.board_factory(
                    expected_config=self.hardware.raw_config,
                    serial_number=self.settings.serial_number,
                    port=self.settings.port,
                )
                self.state = RuntimeState.VERIFYING_CONFIGURATION
                self.identity = await self.board.connect()
                self.configuration_ok = True
                await self._clear(
                    "BENCH DIAGNOSTICS" if self.mode == "bench" else "WAITING FOR X-PLANE"
                )
                self.state = RuntimeState.SNAPSHOTTING_INPUTS
                await self._baseline()
                self._watch(self._board_inputs())
                self._watch(self._periodic(self.board.heartbeat, 1.0))
                if self.mode == "bench":
                    self.ready = True
                    self.state = RuntimeState.READY
                    self._event("connection", "Board ready for bench diagnostics")
                else:
                    self.state = RuntimeState.CONNECTING_SIMULATOR
                    self.simulator = self.simulator_factory(self.settings.simulator_url)
                    await self.simulator.connect()
                    self._simulator_last_ack = time.monotonic()
                    simulator, generation = self.simulator, self.simulator.generation
                    self._watch(self._simulator_heartbeat(simulator, generation))
                    self._watch(self._simulator_lease(simulator, generation))
                    await self._bind()
                    self._watch(self._simulator_events())
                    self._watch(self._periodic(self.simulator.resync, 5.0))
                    self._watch(self._action_worker())
                reason = await self._session_fault
                raise RuntimeError(reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fault(str(exc))
                self._event("fault", str(exc))
                with contextlib.suppress(Exception):
                    await self._clear("CONNECTION LOST")
            finally:
                await self._cancel_bench()
                for task in self._session_tasks:
                    task.cancel()
                await asyncio.gather(*self._session_tasks, return_exceptions=True)
                self._session_tasks.clear()
                self._invalidate_dispatch()
                self.ready = False
                self.configuration_ok = False
                for transport in (self.simulator, self.board):
                    if transport is not None:
                        with contextlib.suppress(Exception):
                            await transport.close()
                self.board = self.simulator = None
                self._simulator_last_ack = None
                self.profile = None
            if not self._closed:
                await asyncio.sleep(RECONNECT_DELAY)

    def _watch(self, coroutine: Any) -> None:
        # Own the actual coroutine immediately. A wrapper cancelled before
        # its first turn would leave the supplied coroutine unawaited.
        task = asyncio.create_task(coroutine)

        def finished(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            error = completed.exception()
            if (
                not self._closed
                and completed in self._session_tasks
                and (self._session_fault is None or not self._session_fault.done())
            ):
                self._fault(str(error) if error else "A connection task stopped unexpectedly")

        task.add_done_callback(finished)
        self._session_tasks.append(task)

    async def _periodic(self, operation: Callable, interval: float) -> None:
        # Fixed deadlines avoid extending a lease by the request's duration.
        deadline = time.monotonic()
        while True:
            await operation()
            deadline += interval
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))

    async def _simulator_heartbeat(self, simulator: Any, generation: int) -> None:
        async def acknowledge() -> None:
            await simulator.heartbeat()
            if (
                self.simulator is simulator
                and simulator.generation == generation
                and simulator.healthy
                and not self._closed
            ):
                self._simulator_last_ack = time.monotonic()

        await self._periodic(acknowledge, 1.0)

    async def _simulator_lease(self, simulator: Any, generation: int) -> None:
        while True:
            await asyncio.sleep(HEALTH_LEASE_POLL)
            if (
                self.simulator is not simulator
                or simulator.generation != generation
                or self._closed
            ):
                return
            last_ack = self._simulator_last_ack
            if last_ack is None or time.monotonic() - last_ack > SIMULATOR_HEALTH_LEASE:
                reason = "Simulator acknowledged health lease expired"
                # Dispatch and render stop before socket cleanup can await I/O.
                self._fault(reason)
                await simulator.expire_health(reason, generation=generation)
                return

    async def _baseline(self) -> None:
        self._invalidate_dispatch()
        epoch, board = self._epoch, self.board
        async with self._input_lock:
            snapshot = await board.snapshot()
            if (
                self._closed
                or epoch != self._epoch
                or self.board is not board
                or not board.connected
                or (self._session_fault is not None and self._session_fault.done())
            ):
                raise ValueError("Connection or control state changed during the input snapshot")
            # Edges following command 34 can arrive in the same serial read,
            # before snapshot() resumes on the host thread. Keep those edges.
            self._baseline_at = getattr(board, "snapshot_completed_at", 0.0) or time.monotonic()
            self.processor.reset(snapshot, epoch)
            self.renderer.reset()
        self._event("snapshot", "All 16 inputs baselined; held buttons require release", snapshot)

    async def _bind(self, selected: Profile | None = None) -> None:
        self.state = RuntimeState.RESOLVING_PROFILE
        simulator, generation, epoch = self.simulator, self.simulator.generation, self._epoch

        def check_session() -> None:
            if (
                self._closed
                or self.simulator is not simulator
                or not simulator.healthy
                or simulator.generation != generation
                or self._epoch != epoch
                or (self._session_fault is not None and self._session_fault.done())
            ):
                raise ValueError("Connection or control state changed while resolving the profile")

        base = self.profiles[0]
        keys = ("aircraft", "paused", "ap_type")
        found = await self.simulator.resolve_datarefs({k: base.datarefs[k].path for k in keys})
        check_session()
        available = self._validate_types(base, found)
        if set(available) != set(keys):
            raise RuntimeError("Missing aircraft identity, pause or autopilot-type DataRef")
        await self.simulator.subscribe(list(keys))
        check_session()
        if any(not self._valid_initial_value(key) for key in keys):
            raise RuntimeError(
                "Incomplete initial aircraft identity, pause or autopilot-type telemetry"
            )
        self.aircraft = aircraft_path(self.simulator.get("aircraft"))
        self.ap_type = self.simulator.get("ap_type")
        if not self.aircraft:
            raise RuntimeError("X-Plane supplied no valid aircraft path")
        profile = selected or next(
            (p for p in self.profiles if p.compatible(self.aircraft, self.ap_type)), None
        )
        if profile is None or not profile.compatible(self.aircraft, self.ap_type):
            self.profile = None
            self.error = "No compatible profile for the loaded aircraft/autopilot"
            self.ready = False
            self.state = RuntimeState.READY
            await self._clear("PROFILE UNAVAILABLE")
            self._event("profile", self.error)
            return
        found = await self.simulator.resolve_datarefs(
            {k: v.path for k, v in profile.datarefs.items()}
        )
        check_session()
        available = self._validate_types(profile, found)
        missing = [k for k, ref in profile.datarefs.items() if ref.required and k not in available]
        if missing:
            raise RuntimeError(
                "Missing or incorrectly typed required DataRefs: " + ", ".join(missing)
            )
        await self.simulator.resolve_commands(profile.command_paths())
        check_session()
        await self.simulator.subscribe(available)
        check_session()
        if (
            aircraft_path(simulator.get("aircraft")) != self.aircraft
            or simulator.get("ap_type") != self.ap_type
        ):
            raise RuntimeError("Aircraft/autopilot changed while resolving the profile")
        invalid = [
            k
            for k, ref in profile.datarefs.items()
            if ref.required and not self._valid_initial_value(k)
        ]
        if invalid:
            raise RuntimeError("Incomplete initial telemetry: " + ", ".join(invalid))
        self.profile = profile
        self._display_enabled = self.mode == "observe"
        self._record_line("profile", profile.model_dump())
        self.ready = True
        self.state = RuntimeState.READY
        self.error = None
        self._event("profile", f"Resolved {profile.name}; Start is required for command dispatch")

    def _valid_initial_value(self, key: str) -> bool:
        value = self.simulator.get(key)
        kind = self.simulator.dataref_types.get(key)
        if kind in {"float", "double", "int"}:
            return (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and (kind != "int" or value == int(value))
            )
        if kind in {"float_array", "int_array"}:
            return isinstance(value, list) and all(
                not isinstance(v, bool)
                and isinstance(v, (int, float))
                and math.isfinite(v)
                and (kind != "int_array" or v == int(v))
                for v in value
            )
        return isinstance(value, str) if kind == "data" else False

    def _validate_types(self, profile: Profile, found: dict) -> list[str]:
        available = []
        for key in found:
            expected = profile.datarefs[key].value_type
            actual = self.simulator.dataref_types.get(key)
            if actual == expected or (
                expected == "numeric" and actual in {"int", "float", "double"}
            ):
                available.append(key)
            else:
                self._event(
                    "binding", f"Wrong type for {key}: expected {expected}, received {actual}"
                )
        return available

    async def _board_inputs(self) -> None:
        while True:
            async with self._input_lock:
                try:
                    event = await asyncio.wait_for(self.board.read_event(), 0.01)
                except TimeoutError:
                    event = None
                if event is not None and event.timestamp >= self._baseline_at:
                    self._record_line("physical_input", asdict(event))
                    if event.kind == "encoder" and event.name in self.encoder_counts:
                        self.encoder_counts[event.name] += -1 if event.value in (0, 1) else 1
                        self._event(
                            "encoder", f"{event.name}: firmware event {event.value}", asdict(event)
                        )
                    elif event.kind == "button":
                        self._event(
                            "input",
                            f"IN {event.index}: {'LOW / closed' if event.value == 0 else 'HIGH / open'}",
                        )
                    for logical in self.processor.feed(event):
                        self._accept_input(logical)
                for logical in self.processor.flush(time.monotonic()):
                    self._accept_input(logical)

    def _accept_input(self, event: InputEvent) -> None:
        if not self.running or event.generation != self._epoch:
            return
        name = action_name(event)
        reason = (
            self.profile.missing_for(name, self.simulator)
            if self.profile
            else "No aircraft profile"
        )
        if reason:
            self._event("unavailable", f"{name}: {reason}")
            return
        try:
            self._commands.put_nowait((self._epoch, event))
        except asyncio.QueueFull:
            self._fault("Command queue overflow; dispatch stopped to prevent delayed actions")

    async def _action_worker(self) -> None:
        while True:
            epoch, event = await self._commands.get()
            profile = self.profile
            if not self.running or epoch != self._epoch or profile is None:
                continue
            if time.monotonic() - event.timestamp > 0.5:
                self._fault("Command queue latency exceeded 500 ms; pending actions discarded")
                continue

            def guard() -> bool:
                return (
                    self.running
                    and self._epoch == epoch
                    and self.profile is profile
                    and self.simulator is not None
                    and self.simulator.healthy
                    and aircraft_path(self.simulator.get("aircraft")) == self.aircraft
                    and self.simulator.get("ap_type") == self.ap_type
                )

            name = action_name(event)
            executor = ActionExecutor(self.simulator, profile, guard=guard)
            try:
                await executor.execute(name)
                self._event(
                    "command", f"{name}: request completed; lamps follow simulator feedback"
                )
            except Exception as exc:
                if (
                    isinstance(exc, ActionError)
                    and not executor.effects_attempted
                    and self.simulator is not None
                    and self.simulator.healthy
                ):
                    self._event("unavailable", f"{name}: {exc}")
                    continue
                self._event("command_error", f"{name}: {exc}")
                # No automatic replay or rollback, including partially completed sequences.
                if epoch == self._epoch:
                    await self.stop()
                    self.error = f"{name} failed: {exc}; inspect the simulator before Start"

    async def _simulator_events(self) -> None:
        while True:
            event = await self.simulator.read_event()
            if event.get("kind") == "lost":
                raise RuntimeError(event.get("reason", "Simulator connection lost"))
            if not self.simulator.healthy:
                raise RuntimeError("Simulator health lease expired")
            current = aircraft_path(self.simulator.get("aircraft"))
            if current != self.aircraft or self.simulator.get("ap_type") != self.ap_type:
                self._fault("Aircraft/autopilot changed; rebuilding session and requiring Start")
                return
            values = {
                k: asdict(self.simulator.values[k])
                for k in event.get("keys", [])
                if k in self.simulator.values
            }
            self._record_line("telemetry", values)

    async def start(self) -> None:
        async with self._control_lock:
            if self.mode in {"bench", "observe", "demo"}:
                raise ValueError("Start is available only in run or commissioning mode")
            if self.running:
                return
            if (
                not self.ready
                or not self.profile
                or not self.simulator
                or not self.simulator.healthy
            ):
                raise ValueError("Board and compatible simulator profile must be ready")
            if self.mode == "run" and not (
                self.hardware.bench_verified and self.profile.simulator_tested
            ):
                raise ValueError(
                    "Commissioning is incomplete; use run --commission and record the required checks"
                )
            simulator, profile, board = self.simulator, self.profile, self.board
            generation = simulator.generation
            await self._baseline()
            if (
                self._closed
                or not self.ready
                or self.simulator is not simulator
                or self.profile is not profile
                or self.board is not board
                or not simulator.healthy
                or simulator.generation != generation
                or aircraft_path(simulator.get("aircraft")) != self.aircraft
                or simulator.get("ap_type") != self.ap_type
            ):
                raise ValueError("Connection changed during the input snapshot")
            self.running = True
            self._display_enabled = True
            self.state = RuntimeState.RUNNING
            self.error = None
            self._event("dispatch", "Physical command dispatch enabled")

    async def stop(self) -> None:
        # Disable synchronously, even if an output write or action is awaiting I/O.
        self._invalidate_dispatch()
        await self._cancel_bench()
        if self.state != RuntimeState.FAULT:
            self.state = RuntimeState.READY if self.ready else RuntimeState.STOPPED
        await self._clear()
        self._event("dispatch", "Stopped; queued actions discarded and outputs cleared")

    async def select_profile(self, profile_id: str) -> None:
        async with self._control_lock:
            profile = next((p for p in self.profiles if p.id == profile_id), None)
            if profile is None:
                raise KeyError(profile_id)
            if self.running:
                raise ValueError("Stop before selecting a profile")
            if self.mode in {"bench", "demo"} or not self.simulator or not self.simulator.healthy:
                raise ValueError("A connected simulator is required for profile selection")
            if not self.aircraft or not profile.compatible(self.aircraft, self.ap_type):
                raise ValueError("Profile is incompatible with the loaded aircraft/autopilot")
            await self.stop()
            self.ready = False
            try:
                await self._baseline()
                await self._bind(profile)
            except Exception as exc:
                self._fault(str(exc))
                raise

    async def _render_loop(self) -> None:
        lcd_at = 0.0
        while True:
            await asyncio.sleep(0.05)
            if (
                not self.ready
                or not self.profile
                or not self.simulator
                or not self.simulator.healthy
            ):
                continue
            if not self._display_enabled:
                continue
            try:
                now = time.monotonic()
                epoch, profile, simulator, board = (
                    self._epoch,
                    self.profile,
                    self.simulator,
                    self.board,
                )
                frame = self.renderer.render(profile, simulator.values, now)
                async with self._output_lock:
                    # Stop may have happened while waiting for a board write lock.
                    if (
                        self._closed
                        or not self.ready
                        or not self._display_enabled
                        or epoch != self._epoch
                        or self.profile is not profile
                        or self.simulator is not simulator
                        or self.board is not board
                        or not simulator.healthy
                    ):
                        continue
                    changed = frame != self.panel
                    self.panel = frame
                    if self.board is not None and self.board.connected:
                        await self.board.write_frame(frame, lcd=now - lcd_at >= 0.2)
                    if now - lcd_at >= 0.2:
                        lcd_at = now
                    if changed:
                        self._record_line("output", asdict(frame))
            except Exception as exc:
                self._fault(str(exc))

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            self._publish({"type": "status", "data": self.status()})

    async def _cancel_bench(self) -> None:
        if self._bench_task is not None:
            self._bench_task.cancel()
            await asyncio.gather(self._bench_task, return_exceptions=True)
            self._bench_task = None

    def _require_bench(self) -> None:
        if self.mode != "bench" or not self.ready or not self.board or not self.board.connected:
            raise ValueError("A connected board in bench mode is required")

    async def _bench_frame(self, lamps: int, rows: tuple) -> bool:
        epoch, board = self._epoch, self.board
        async with self._output_lock:
            if (
                self._closed
                or epoch != self._epoch
                or self.board is not board
                or not self.ready
                or board is None
                or not board.connected
            ):
                return False
            self.panel = PanelFrame(lamps, rows)
            await board.write_frame(self.panel, force=True)
        self._record_line("output", asdict(self.panel))
        return True

    async def bench_lamp(self, index: int | None, duration: float = 0.5) -> None:
        self._require_bench()
        if index is not None and (type(index) is not int or not 0 <= index < 24):
            raise ValueError("Lamp index must be 0..23")
        if not 0.1 <= duration <= 5:
            raise ValueError("Lamp duration must be 0.1..5 seconds")
        await self._cancel_bench()
        if not await self._bench_frame(0 if index is None else 1 << index, self.panel.rows):
            return
        if index is not None:

            async def expire() -> None:
                try:
                    await asyncio.sleep(duration)
                    await self._bench_frame(0, self.panel.rows)
                except Exception as exc:
                    self._fault(str(exc))

            self._bench_task = asyncio.create_task(expire())
        self._event("bench", f"Lamp {'off' if index is None else index}")

    async def bench_walk(self) -> None:
        self._require_bench()
        await self._cancel_bench()

        async def walk() -> None:
            try:
                for output in sorted(self.hardware.outputs, key=lambda x: x.index):
                    if output.reserved:
                        continue
                    if not await self._bench_frame(
                        1 << output.index,
                        PanelFrame.message(f"LAMP {output.index:02} {output.name}").rows,
                    ):
                        return
                    self._event("bench", f"Walking lamp {output.index}: {output.name}")
                    await asyncio.sleep(0.5)
                await self._bench_frame(0, self.panel.rows)
            except Exception as exc:
                self._fault(str(exc))

        self._bench_task = asyncio.create_task(walk())

    async def bench_lcd(self, text: str) -> None:
        self._require_bench()
        if len(text) > 80 or any(not 32 <= ord(c) <= 126 for c in text):
            raise ValueError("LCD test requires at most 80 printable ASCII characters")
        padded = text.ljust(80)
        if not await self._bench_frame(
            self.panel.lamps, tuple(padded[i : i + 20] for i in range(0, 80, 20))
        ):
            return
        self._event("bench", "LCD test written")

    def _event(self, kind: str, message: str, data: Any = None) -> None:
        event = {"time": time.time(), "kind": kind, "message": message}
        if data is not None:
            event["data"] = data
        self.events.append(event)
        LOG.info("%s: %s", kind, message)
        self._record_line("event", event)
        self._publish({"type": "event", "data": event})

    def _record_line(self, kind: str, data: Any) -> None:
        if self._record:
            self._record.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "monotonic": time.monotonic(),
                        "kind": kind,
                        "data": data,
                    },
                    allow_nan=False,
                )
                + "\n"
            )
            self._record.flush()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, item: dict) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    def profile_summaries(self) -> list[dict]:
        return [
            {
                "id": p.id,
                "name": p.name,
                "compatible": bool(self.aircraft and p.compatible(self.aircraft, self.ap_type)),
                "verification": "simulator_tested" if p.simulator_tested else "reference_verified",
            }
            for p in self.profiles
        ]

    def status(self) -> dict:
        simulator = self.simulator
        healthy = bool(simulator is not None and simulator.healthy)
        profile = self.profile
        capabilities = {}
        if profile and simulator:
            for name in sorted(set(profile.actions) | set(profile.disabled)):
                reason = (
                    profile.missing_for(name, simulator)
                    if healthy
                    else "Simulator feedback is invalid"
                )
                capabilities[name] = {"available": reason is None, "reason": reason}
            for name, verified in (
                ("YD_LAMP", profile.yd_engaged_verified),
                ("GEAR_LAMPS", profile.gear_verified),
                ("FLAP_LAMPS", profile.flaps_verified),
            ):
                capabilities[name] = {
                    "available": verified and healthy,
                    "reason": None if verified else "Actual-aircraft feedback is not commissioned",
                }
        values = simulator.values if simulator else {}
        wall_offset = time.time() - time.monotonic()
        return {
            "state": self.state.value,
            "mode": self.mode,
            "running": self.running,
            "ready": self.ready,
            "board": {
                "connected": bool(self.board and self.board.connected),
                "identity": asdict(self.identity) if self.identity is not None else None,
                "configuration_ok": self.configuration_ok,
            },
            "simulator": {
                "connected": healthy,
                "healthy": healthy,
                "version": (
                    simulator.capabilities.get("x-plane", {}).get("version")
                    or simulator.capabilities.get("simulator", {}).get("version")
                )
                if simulator
                else None,
                "aircraft": self.aircraft,
            },
            "profile": (
                {
                    "id": profile.id,
                    "name": profile.name,
                    "compatible": True,
                    "verification": "simulator_tested"
                    if profile.simulator_tested
                    else "reference_verified",
                }
                if profile
                else None
            ),
            "capabilities": capabilities,
            "panel": {
                "lamps": self.panel.lamps,
                "rows": list(self.panel.rows),
                "indicators": [
                    {
                        "index": o.index,
                        "name": o.name,
                        "on": bool(self.panel.lamps & (1 << o.index)),
                    }
                    for o in sorted(self.hardware.outputs, key=lambda x: x.index)
                ],
            },
            "telemetry": {
                k: {
                    "value": v.value,
                    "valid": v.valid and healthy,
                    "updated_at": v.updated_at + wall_offset if v.updated_at else None,
                }
                for k, v in values.items()
            },
            "inputs": dict(self.processor.states),
            "encoder_counts": dict(self.encoder_counts),
            "events": list(self.events),
            "error": self.error,
            "commissioning": {
                "hardware_verified": self.hardware.bench_verified,
                "aircraft_verified": bool(profile and profile.simulator_tested),
            },
        }

    async def _demo(self) -> None:
        from .demo import DemoSimulator

        self.profile = next((p for p in self.profiles if p.id == "c90b"), self.profiles[0])
        self.simulator = DemoSimulator(self.profile)
        self.aircraft = self.profile.aircraft_paths[0]
        self.ap_type = 0
        self.ready = True
        self._display_enabled = True
        self.state = RuntimeState.READY
        self._record_line("profile", self.profile.model_dump())
        self._record_line(
            "telemetry", {key: asdict(value) for key, value in self.simulator.values.items()}
        )
        self._event("demo", "Offline sample data; no board or simulator connection")
        while True:
            self.simulator.tick()
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._invalidate_dispatch()
        self.ready = False
        await self._cancel_bench()
        with contextlib.suppress(Exception):
            await self._clear()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.mode == "demo":
            self.simulator = None
        self.state = RuntimeState.STOPPED
        if self._record:
            self._record.close()
            self._record = None
