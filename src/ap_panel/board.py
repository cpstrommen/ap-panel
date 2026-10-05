"""Async interface to stock firmware; one dedicated thread owns the USB port.

Firmware replies lack transaction IDs.  Requests are strictly serialized and
any timeout poisons the connection, preventing a late ID-10 reply from satisfying
a later identity/configuration request.  Ordinary connection never writes EEPROM.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import serial
from serial.tools import list_ports

from .protocol import (
    Message,
    Parser,
    ProtocolError,
    config_records,
    encode,
    lamp_commands,
    lcd_command,
)

if TYPE_CHECKING:
    from .models import PanelFrame

FIRMWARE_VERSION = "3.1.4"
BOARD_TYPE = "MobiFlight RaspiPico2"
USB_VID = 0x2E8A
USB_PID = 0xF00A
STARTUP_DELAY = 1.25
TRANSACTION_TIMEOUT = 2.5
HEARTBEAT_TIMEOUT = 0.25
READ_TIMEOUT = 0.05
MAX_EVENT_QUEUE = 512


class BoardError(RuntimeError):
    """Board selection, serial transport, identity or configuration failure."""


@dataclass(frozen=True)
class BoardIdentity:
    board_type: str
    name: str
    serial: str
    firmware: str
    core: str


@dataclass(frozen=True)
class BoardEvent:
    kind: str
    name: str
    index: int | None
    value: int
    timestamp: float


def list_devices() -> list[dict]:
    """Enumerate metadata without opening a serial port or probing a device."""
    result = []
    for item in list_ports.comports():
        result.append(
            {
                "port": item.device,
                "device": item.device,
                "serial_number": item.serial_number,
                "vid": item.vid,
                "pid": item.pid,
                "manufacturer": item.manufacturer,
                "product": item.product,
                "description": item.description,
                "hwid": item.hwid,
                "mobiflight_pico2": item.vid == USB_VID and item.pid == USB_PID,
            }
        )
    return sorted(result, key=lambda item: item["port"])


def _serial_key(value: str) -> str:
    return value.upper().removeprefix("SN-")


@dataclass
class _Job:
    operation: str
    arguments: tuple
    future: concurrent.futures.Future


class BoardClient:
    def __init__(
        self, expected_config: str, serial_number: str | None = None, port: str | None = None
    ) -> None:
        try:
            self._records = config_records(expected_config)
        except ProtocolError as exc:
            raise BoardError(str(exc)) from exc
        self.expected_config = expected_config
        self.serial_number = serial_number
        self.port = port
        self.identity: BoardIdentity | None = None
        self._input_name, self._input_count, self._encoder_names = self._device_names()
        self._operation_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._jobs: queue.Queue[_Job] = queue.Queue()
        self._events: asyncio.Queue[BoardEvent | BoardError] = asyncio.Queue(MAX_EVENT_QUEUE)
        self._generation = 0
        self._stop = threading.Event()
        self._error: BoardError | None = None
        self._serial = None
        self._parser = Parser()
        self._verified = False
        self._reply_command: int | None = None
        self._reply_validator: Callable[[Message], bool] | None = None
        self._reply: Message | None = None
        self._snapshot: dict[int, bool] | None = None
        self._snapshot_complete = False
        self._snapshot_completed_at = 0.0
        self._last_lamps: int | None = None
        self._last_text: str | None = None

    @property
    def connected(self) -> bool:
        return bool(
            self._verified and self._thread and self._thread.is_alive() and not self._stop.is_set()
        )

    @property
    def snapshot_completed_at(self) -> float:
        """Monotonic serial-thread boundary of the last completed retrigger."""
        with self._state_lock:
            return self._snapshot_completed_at

    def _device_names(self) -> tuple[str, int, frozenset[str]]:
        inputs = []
        encoders = []
        outputs = []
        displays = []
        for record in self._records:
            fields = record[:-1].split(".")
            if fields[0] == "12" and len(fields) == 6:
                try:
                    inputs.append((fields[-1], int(fields[-2]) * 8))
                except ValueError as exc:
                    raise BoardError("Input shifter count must be numeric") from exc
            elif fields[0] == "8" and len(fields) == 5:
                encoders.append(fields[-1])
            elif fields[0] == "10" and len(fields) == 6:
                outputs.append((fields[-2], fields[-1]))
            elif fields[0] == "7" and len(fields) == 5:
                displays.append((fields[-3], fields[-2], fields[-1]))
            else:
                raise BoardError("Expected only the panel's encoder, shifter and LCD records")
        if len(inputs) != 1 or inputs[0][1] != 16:
            raise BoardError("Panel configuration requires one chain of two input shifters")
        if len(encoders) != 6 or set(encoders) != {"HDG", "ALT", "VS", "CRS", "BARO", "FLAPS"}:
            raise BoardError("Panel configuration requires exactly six named encoders")
        if outputs != [("3", "OUT")]:
            raise BoardError("Panel configuration requires one chain of three output shifters")
        if displays != [("20", "4", "LCD")]:
            raise BoardError("Panel configuration requires one 20x4 LCD")
        return inputs[0][0], inputs[0][1], frozenset(encoders)

    def _select_port(self) -> str:
        if self.port:
            return self.port
        if not self.serial_number:
            raise BoardError("Select a board port or serial number before connecting")
        matches = [
            item["port"]
            for item in list_devices()
            if item["mobiflight_pico2"]
            and item["serial_number"]
            and _serial_key(item["serial_number"]) == _serial_key(self.serial_number)
        ]
        # macOS can expose both tty and cu paths; prefer the outgoing cu path.
        outgoing = [port for port in matches if Path(port).name.startswith("cu.")]
        matches = outgoing or matches
        if len(matches) != 1:
            raise BoardError(
                f"Selected serial {self.serial_number!r} matched {len(matches)} USB ports; "
                "select an explicit port"
            )
        return matches[0]

    async def connect(self) -> BoardIdentity:
        async with self._operation_lock:
            try:
                return await self._submit("connect", start=True)
            except asyncio.CancelledError:
                self._stop.set()
                raise

    async def snapshot(self) -> dict[int, bool]:
        """Retrigger physical levels without putting retrigger events on the event queue."""
        async with self._operation_lock:
            return await self._submit("snapshot")

    async def read_event(self) -> BoardEvent:
        if self._error is not None:
            raise self._error
        if not self.connected:
            raise BoardError("Board is not connected")
        item = await self._events.get()
        if isinstance(item, BoardError):
            raise item
        return item

    async def write_frame(self, frame: PanelFrame, force: bool = False, lcd: bool = True) -> None:
        try:
            outputs = lamp_commands(frame.lamps)
            display = lcd_command(frame.rows) if lcd else None
        except ProtocolError as exc:
            raise BoardError(str(exc)) from exc
        async with self._operation_lock:
            await self._submit("frame", frame.lamps, "".join(frame.rows), outputs, display, force)

    async def clear(self, message: str = "") -> None:
        # Error strings should never make the shutdown frame exceed the tiny RX buffer.
        text = "".join(
            char if 32 <= ord(char) <= 126 and char not in ",;/" else " " for char in message
        )
        rows = (text[:20].ljust(20), " " * 20, " " * 20, " " * 20)
        async with self._operation_lock:
            await self._submit("frame", 0, "".join(rows), lamp_commands(0), lcd_command(rows), True)

    async def heartbeat(self) -> None:
        """An acknowledged identity probe, followed by the unacknowledged power heartbeat."""
        async with self._operation_lock:
            await self._submit("heartbeat")

    async def provision(self, backup_path: Path) -> dict:
        """Explicit EEPROM provisioning, with durable backup before the first mutation."""
        async with self._operation_lock:
            return await self._submit("provision", Path(backup_path), start=True)

    async def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, TRANSACTION_TIMEOUT + 1)
            if thread.is_alive():
                raise BoardError("Serial worker did not close within its bounded timeout")
        self._verified = False
        self._thread = None

    async def _submit(self, operation: str, *arguments: object, start: bool = False):
        future: concurrent.futures.Future = concurrent.futures.Future()
        job = _Job(operation, tuple(arguments), future)
        if start and (self._thread is None or not self._thread.is_alive()):
            selected = self._select_port()
            self._loop = asyncio.get_running_loop()
            self._events = asyncio.Queue(MAX_EVENT_QUEUE)
            self._generation += 1
            self._jobs = queue.Queue()
            self._stop = threading.Event()
            self._error = None
            self._verified = False
            self.identity = None
            self._parser = Parser()
            self._last_lamps = None
            self._last_text = None
            self._thread = threading.Thread(
                target=self._worker, args=(selected,), name="ap-panel-serial", daemon=True
            )
            self._jobs.put(job)
            self._thread.start()
        else:
            # Queueing and terminal draining must not race and strand a future.
            with self._state_lock:
                if self._thread is None or not self._thread.is_alive() or self._stop.is_set():
                    raise self._error or BoardError("Board is not connected")
                self._jobs.put(job)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # Especially important for provisioning: cancellation must not continue
            # EEPROM mutations after the caller has abandoned the operation.
            self._stop.set()
            raise

    def _worker(self, selected: str) -> None:
        error = BoardError("Board connection closed")
        try:
            # Opening, waiting, reads, writes and closing happen on this thread only.
            self._serial = serial.Serial(
                selected,
                baudrate=115200,
                timeout=READ_TIMEOUT,
                write_timeout=TRANSACTION_TIMEOUT,
                exclusive=True,
            )
            if self._stop.wait(STARTUP_DELAY):
                return
            self._pump()
            while not self._stop.is_set():
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    self._pump()
                    continue
                if not job.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self._execute(job.operation, job.arguments, selected)
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, BoardError)
                        else BoardError(f"Serial operation failed: {exc}")
                    )
                    job.future.set_exception(error)
                    raise error
                job.future.set_result(result)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, BoardError)
                else BoardError(f"Serial connection failed: {exc}")
            )
        finally:
            error = self._error or error
            with self._state_lock:
                self._error = error
                self._verified = False
                self._stop.set()
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if not job.future.done():
                        job.future.set_exception(error)
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._notify_failure(error)

    def _execute(self, operation: str, arguments: tuple, selected: str):
        if operation in ("connect", "provision"):
            self._verified = False
            identity = self._identify()
            current = self._get_config()
            if operation == "connect":
                if current != self.expected_config:
                    raise BoardError(
                        "Board configuration does not match this panel; run explicit provisioning "
                        "with a backup before connecting"
                    )
                self._verified = True
                return identity
            return self._provision(arguments[0], identity, current, selected)
        if not self._verified:
            raise BoardError("Board has not passed identity and configuration verification")
        if operation == "snapshot":
            # Discard previously queued edges; this new baseline starts a new input epoch.
            self._discard_events()
            self._snapshot = {}
            self._snapshot_complete = False
            with self._state_lock:
                self._snapshot_completed_at = 0.0
            try:
                self._transact(encode(23), 34, lambda message: not message.arguments)
                snapshot = self._snapshot.copy()
                if set(snapshot) != set(range(self._input_count)):
                    raise BoardError("Input retrigger did not return all sixteen physical levels")
                return snapshot
            finally:
                self._snapshot = None
                self._snapshot_complete = False
        if operation == "heartbeat":
            previous = self.identity
            identity = self._identify(timeout=HEARTBEAT_TIMEOUT)
            if previous != identity:
                raise BoardError("Board identity changed during the connection")
            self._write(encode(18, 0))
            return None
        if operation == "frame":
            lamps, text, outputs, display, force = arguments
            if force or lamps != self._last_lamps:
                for output in outputs:
                    self._write(output)
                self._last_lamps = lamps
            if display is not None and (force or text != self._last_text):
                self._write(display)
                self._last_text = text
            return None
        raise BoardError(f"Unknown serial worker operation: {operation}")

    def _identify(self, timeout: float | None = None) -> BoardIdentity:
        reply = self._transact(
            encode(9), 10, lambda message: len(message.arguments) == 5, timeout=timeout
        )
        identity = BoardIdentity(*reply.arguments)
        if identity.board_type != BOARD_TYPE:
            raise BoardError(f"Expected {BOARD_TYPE}; board reports {identity.board_type!r}")
        if identity.firmware != FIRMWARE_VERSION:
            raise BoardError(
                f"Expected stock firmware {FIRMWARE_VERSION}; board reports {identity.firmware!r}"
            )
        if self.serial_number and _serial_key(identity.serial) != _serial_key(self.serial_number):
            raise BoardError(
                f"Selected serial {self.serial_number!r}; board reports {identity.serial!r}"
            )
        if not identity.serial:
            raise BoardError("Board did not report a serial identity")
        self.identity = identity
        return identity

    def _get_config(self) -> str:
        reply = self._transact(encode(12), 10, lambda message: len(message.arguments) <= 1)
        # Config.cpp streams EEPROM characters verbatim, unlike escaped LCD text.
        # Retain the original bytes so recovery backups never remove old slashes.
        if reply.wire is None:
            raise BoardError("Configuration reply did not retain its raw wire payload")
        return reply.wire[3:-1].decode("ascii") if reply.wire.startswith(b"10,") else ""

    def _provision(
        self, backup_path: Path, identity: BoardIdentity, current: str, selected: str
    ) -> dict:
        backup = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "identity": asdict(identity),
            "port": selected,
            "config": current,
        }
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents destroying an earlier recovery copy.
        with backup_path.open("x", encoding="utf-8") as handle:
            json.dump(backup, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._transact(encode(13), 5, lambda message: message.arguments == ("OK",))
        length = 0
        for record in self._records:
            length += len(record.encode("ascii"))
            # These writes append.  Never retry a failed or timed-out append.
            self._transact(
                encode(11, record),
                5,
                lambda message, length=length: message.arguments == (str(length),),
            )
        self._transact(encode(14), 15, lambda message: message.arguments == ("OK",))
        self._transact(encode(16), 17, lambda message: message.arguments == ("OK",))
        if self._get_config() != self.expected_config:
            raise BoardError(f"Provisioned config readback differs; recovery backup: {backup_path}")
        self._verified = True
        self._last_lamps = None
        self._last_text = None
        return {
            "identity": asdict(identity),
            "backup_path": str(backup_path),
            "previous_config": current,
            "config": self.expected_config,
            "verified": True,
        }

    def _write(self, frame: bytes) -> None:
        if self._stop.is_set():
            raise BoardError("Board connection closed")
        if self._serial.write(frame) != len(frame):
            raise BoardError("Serial write was incomplete; connection cannot be reused")

    def _transact(
        self,
        frame: bytes,
        command: int,
        validator: Callable[[Message], bool],
        timeout: float | None = None,
    ) -> Message:
        self._reply_command, self._reply_validator, self._reply = command, validator, None
        try:
            deadline = time.monotonic() + (TRANSACTION_TIMEOUT if timeout is None else timeout)
            self._write(frame)
            while self._reply is None:
                if self._stop.is_set():
                    raise BoardError("Board connection closed")
                if time.monotonic() >= deadline:
                    raise BoardError(
                        "Firmware reply timed out; close and reconnect before another request"
                    )
                self._pump()
            return self._reply
        finally:
            self._reply_command, self._reply_validator, self._reply = None, None, None

    def _pump(self) -> None:
        data = self._serial.read(min(max(getattr(self._serial, "in_waiting", 0), 1), 4096))
        if data:
            try:
                messages = self._parser.feed(data)
            except ProtocolError as exc:
                raise BoardError(f"Invalid firmware framing: {exc}") from exc
            for message in messages:
                self._route(message)

    def _route(self, message: Message) -> None:
        if message.command in (7, 28, 30) and not self._verified and self._snapshot is None:
            return  # Old configurations can report direct buttons, analogs and multiplexers.
        if message.command in (6, 29):
            self._route_event(message)
            return
        if message.command == self._reply_command:
            if self._reply is not None or not self._reply_validator(message):
                raise BoardError(f"Malformed or repeated firmware reply: {message}")
            self._reply = message
            if message.command == 34 and self._snapshot is not None:
                self._snapshot_complete = True
                with self._state_lock:
                    self._snapshot_completed_at = time.monotonic()
            return
        if message.command == 255:
            return
        if message.command == 17 and message.arguments == ("OK",) and self.identity is None:
            return  # Startup announcement, never an acknowledgement of a later request.
        raise BoardError(f"Unexpected firmware reply: {message}; connection cannot be reused")

    def _route_event(self, message: Message) -> None:
        # A board being provisioned can have completely different existing input
        # devices.  Their unsolicited events are irrelevant until config verifies.
        if not self._verified and self._snapshot is None:
            return
        try:
            if message.command == 29:
                if len(message.arguments) != 3:
                    raise ValueError("input event needs name, index and value")
                name, index_text, value_text = message.arguments
                index, value = int(index_text), int(value_text)
                if (
                    name != self._input_name
                    or not 0 <= index < self._input_count
                    or value not in (0, 1)
                ):
                    raise ValueError("unexpected input name/index/value")
                event = BoardEvent("button", name, index, value, time.monotonic())
            else:
                if len(message.arguments) != 2:
                    raise ValueError("encoder event needs name and value")
                name, value_text = message.arguments
                value = int(value_text)
                if name not in self._encoder_names or value not in (0, 1, 2, 3):
                    raise ValueError("unexpected encoder name/value")
                event = BoardEvent("encoder", name, None, value, time.monotonic())
        except ValueError as exc:
            raise BoardError(f"Malformed firmware input event: {message}: {exc}") from exc
        if self._snapshot is not None and not self._snapshot_complete:
            if event.kind == "button":
                self._snapshot[event.index] = event.value == 0
            return
        if self._verified:
            self._loop.call_soon_threadsafe(
                self._deliver_event, event, self._events, self._generation
            )

    def _deliver_event(
        self, event: BoardEvent, event_queue: asyncio.Queue, generation: int
    ) -> None:
        if generation != self._generation or self._stop.is_set():
            return
        try:
            event_queue.put_nowait(event)
        except asyncio.QueueFull:
            error = BoardError("Input queue overflow; reconnect to establish a new baseline")
            self._error = error
            self._stop.set()
            self._deliver_failure(error, event_queue)

    def _discard_events(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._clear_event_queue, self._events)

    def _clear_event_queue(self, event_queue: asyncio.Queue) -> None:
        while not event_queue.empty():
            event_queue.get_nowait()

    def _notify_failure(self, error: BoardError) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._deliver_failure, error, self._events)

    def _deliver_failure(self, error: BoardError, event_queue: asyncio.Queue) -> None:
        # Capture the failed session's queue: a delayed callback must wake old
        # readers without poisoning the queue of a newly connected session.
        self._clear_event_queue(event_queue)
        event_queue.put_nowait(error)
