import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import ap_panel.board as board_module
from ap_panel.board import BoardClient, BoardError
from ap_panel.models import PanelFrame
from ap_panel.protocol import Parser

CONFIG = (
    "8.0.1.0.HDG:8.2.3.0.ALT:8.6.7.0.VS:8.8.9.0.CRS:8.10.11.0.BARO:8.12.13.0.FLAPS:"
    "12.14.15.16.2.IN:10.19.18.17.3.OUT:7.39.20.4.LCD:"
)


def reply(command, *arguments):
    fields = [str(command)]
    fields.extend("".join("/" + c if c in ",;/" else c for c in str(arg)) for arg in arguments)
    return (",".join(fields) + ";\r\n").encode("ascii")


class FakeSerial:
    """Fake port that also enforces the dedicated-thread ownership invariant."""

    def __init__(self, config=CONFIG, firmware="3.1.4", callback=None):
        self.config = config
        self.firmware = firmware
        self.callback = callback
        self.owner = None
        self.writes = []
        self.messages = []
        self._buffer = bytearray(reply(17, "OK"))
        self._condition = threading.Condition()
        self.closed = False
        self.timeout = 0.005
        self.parser = Parser()

    def open(self, *args, **kwargs):
        assert kwargs["baudrate"] == 115200
        assert kwargs["exclusive"] is True
        self.owner = threading.get_ident()
        return self

    def _check_owner(self):
        assert threading.get_ident() == self.owner, "port accessed outside its owner thread"

    @property
    def in_waiting(self):
        self._check_owner()
        with self._condition:
            return len(self._buffer)

    def inject(self, data):
        with self._condition:
            self._buffer.extend(data)
            self._condition.notify_all()

    def read(self, count):
        self._check_owner()
        with self._condition:
            if not self._buffer:
                self._condition.wait(self.timeout)
            data = bytes(self._buffer[:count])
            del self._buffer[:count]
            return data

    def write(self, data):
        self._check_owner()
        self.writes.append(data)
        for message in self.parser.feed(data):
            self.messages.append(message)
            if self.callback and self.callback(self, message):
                continue
            if message.command == 9:
                self.inject(
                    reply(10, "MobiFlight RaspiPico2", "AP Panel", "SN-ABC", self.firmware, "4.7.1")
                )
            elif message.command == 12:
                # Stock OnGetConfig writes raw EEPROM characters, unescaped.
                self.inject(("10," + self.config + ";\r\n").encode("ascii"))
            elif message.command == 13:
                self.config = ""
                self.inject(reply(5, "OK"))
            elif message.command == 11:
                self.config += message.arguments[0]
                self.inject(reply(5, len(self.config)))
            elif message.command == 14:
                self.inject(reply(15, "OK"))
            elif message.command == 16:
                self.inject(reply(17, "OK"))
            elif message.command == 23:
                levels = b"".join(reply(29, "IN", index, 1) for index in range(16) if index != 1)
                self.inject(levels + reply(29, "IN", 1, 0) + reply(34))
        return len(data)

    def close(self):
        self._check_owner()
        self.closed = True


@pytest.fixture
def fake_port(monkeypatch):
    monkeypatch.setattr(board_module, "STARTUP_DELAY", 0)
    monkeypatch.setattr(board_module, "READ_TIMEOUT", 0.005)
    monkeypatch.setattr(board_module, "TRANSACTION_TIMEOUT", 0.08)
    monkeypatch.setattr(board_module, "HEARTBEAT_TIMEOUT", 0.03)

    def install(**kwargs):
        fake = FakeSerial(**kwargs)
        monkeypatch.setattr(board_module.serial, "Serial", fake.open)
        return fake

    return install


@pytest.mark.asyncio
async def test_connect_is_readonly_and_port_has_one_owner(fake_port):
    fake = fake_port()
    client = BoardClient(CONFIG, serial_number="abc", port="fake")
    try:
        identity = await client.connect()
        assert identity.serial == "SN-ABC"
        assert client.connected
        assert [message.command for message in fake.messages] == [9, 12]
        assert fake.owner != threading.get_ident()
    finally:
        await client.close()
    assert fake.closed


@pytest.mark.asyncio
async def test_no_user_selected_identity_never_opens(fake_port):
    fake = fake_port()
    with pytest.raises(BoardError, match="Select a board"):
        await BoardClient(CONFIG).connect()
    assert fake.owner is None


@pytest.mark.asyncio
async def test_firmware_and_config_mismatch_do_not_write_eeprom(fake_port):
    fake = fake_port(firmware="3.1.3")
    client = BoardClient(CONFIG, port="fake")
    with pytest.raises(BoardError, match="Expected stock firmware 3.1.4"):
        await client.connect()
    await client.close()
    assert [message.command for message in fake.messages] == [9]
    fake = fake_port(config="")
    client = BoardClient(CONFIG, port="fake")
    with pytest.raises(BoardError, match="explicit provisioning"):
        await client.connect()
    await client.close()
    assert [message.command for message in fake.messages] == [9, 12]


@pytest.mark.asyncio
async def test_snapshot_suppresses_retrigger_events(fake_port):
    fake = fake_port()
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        snapshot = await client.snapshot()
        assert snapshot == {index: index == 1 for index in range(16)}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.read_event(), 0.02)
        fake.inject(reply(29, "IN", 14, 0))
        event = await asyncio.wait_for(client.read_event(), 0.1)
        assert (event.kind, event.name, event.index, event.value) == ("button", "IN", 14, 0)
        assert event.timestamp <= time.monotonic()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_interleave_with_acknowledged_heartbeat(fake_port):
    def callback(fake, message):
        if message.command == 9 and len(fake.messages) > 2:
            fake.inject(reply(29, "IN", 0, 0) + reply(6, "HDG", 3))
        return False

    fake = fake_port(callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        await client.heartbeat()
        button = await asyncio.wait_for(client.read_event(), 0.1)
        encoder = await asyncio.wait_for(client.read_event(), 0.1)
        assert button.kind == "button"
        assert encoder.value == 3
        assert fake.writes[-1] == b"18,0;"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_absolute_frames_cache_and_force_refresh(fake_port):
    fake = fake_port()
    client = BoardClient(CONFIG, port="fake")
    frame = PanelFrame(0x010204, ("A" * 20, "B" * 20, "C" * 20, "D" * 20))
    try:
        await client.connect()
        await client.write_frame(frame)
        assert fake.writes[-3:] == [
            b"27,0,3,0,254,253,251;",
            b"27,0,3,1,1,2,4;",
            b"25,0," + frame.text.encode() + b";",
        ]
        count = len(fake.writes)
        await client.write_frame(frame)
        assert len(fake.writes) == count
        await client.write_frame(frame, force=True, lcd=False)
        assert len(fake.writes) == count + 2
        await client.clear("DISCONNECTED")
        assert fake.writes[-2] == b"27,0,3,0,255,255,255;"
        assert fake.writes[-1].startswith(b"25,0,DISCONNECTED")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_timeout_poison_closes_connection_and_never_retries(fake_port):
    def callback(fake, message):
        return message.command == 9 and len(fake.messages) > 2

    fake = fake_port(callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        with pytest.raises(BoardError, match="timed out"):
            await client.heartbeat()
        fake.inject(reply(10, "stale-config"))
        with pytest.raises(BoardError):
            await client.heartbeat()
        assert [message.command for message in fake.messages] == [9, 12, 9]
    finally:
        await client.close()
    assert fake.closed


@pytest.mark.asyncio
async def test_provision_backup_precedes_mutation_and_readback_verifies(fake_port, tmp_path):
    backup_path = tmp_path / "backups" / "board.json"
    previous = "12.14.15.16.2.OTHER:"

    def callback(fake, message):
        if message.command == 12 and fake.config == previous:
            fake.inject(reply(10, previous) + reply(29, "OTHER", 0, 0))
            return True
        if message.command == 13:
            assert json.loads(backup_path.read_text())["config"] == previous
        return False

    fake = fake_port(config=previous, callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        result = await client.provision(backup_path)
        assert result["verified"] is True
        assert fake.config == CONFIG
        commands = [message.command for message in fake.messages]
        assert commands[:3] == [9, 12, 13]
        assert commands[-3:] == [14, 16, 12]
        assert commands.count(11) == len(CONFIG[:-1].split(":"))
        assert json.loads(backup_path.read_text())["identity"]["serial"] == "SN-ABC"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_existing_backup_prevents_any_config_mutation(fake_port, tmp_path):
    fake = fake_port(config="")
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("existing recovery backup")
    client = BoardClient(CONFIG, port="fake")
    try:
        with pytest.raises(BoardError, match="Serial operation failed"):
            await client.provision(backup_path)
        assert [message.command for message in fake.messages] == [9, 12]
        assert backup_path.read_text() == "existing recovery backup"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_append_rejection_is_not_retried(fake_port, tmp_path):
    def callback(fake, message):
        if message.command == 11:
            fake.inject(reply(5, -1))
            return True
        return False

    fake = fake_port(config="", callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        with pytest.raises(BoardError, match="Malformed"):
            await client.provision(tmp_path / "backup.json")
        assert [message.command for message in fake.messages] == [9, 12, 13, 11]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_serial_selection_normalizes_stock_sn_prefix(fake_port, monkeypatch):
    fake = fake_port()
    monkeypatch.setattr(
        board_module.list_ports,
        "comports",
        lambda: [
            SimpleNamespace(
                device="/dev/cu.fake",
                serial_number="ABC",
                vid=0x2E8A,
                pid=0xF00A,
                manufacturer="MobiFlight",
                product="Pico 2",
                description="Pico 2",
                hwid="USB",
            )
        ],
    )
    client = BoardClient(CONFIG, serial_number="SN-abc")
    try:
        await client.connect()
        assert fake.owner is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_wakes_waiting_event_reader(fake_port):
    fake_port()
    client = BoardClient(CONFIG, port="fake")
    await client.connect()
    reader = asyncio.create_task(client.read_event())
    await asyncio.sleep(0)
    await client.close()
    with pytest.raises(BoardError, match="closed"):
        await asyncio.wait_for(reader, 0.1)


@pytest.mark.asyncio
async def test_provision_cancel_stops_before_next_append(fake_port, tmp_path):
    first_append = threading.Event()

    def callback(fake, message):
        if message.command == 11:
            first_append.set()
            return True  # Intentionally leave this append outcome unknown.
        return False

    fake = fake_port(config="", callback=callback)
    client = BoardClient(CONFIG, port="fake")
    task = asyncio.create_task(client.provision(tmp_path / "backup.json"))
    await asyncio.wait_for(asyncio.to_thread(first_append.wait, 0.2), 0.3)
    assert first_append.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()
    assert [message.command for message in fake.messages] == [9, 12, 13, 11]
    assert fake.closed


@pytest.mark.asyncio
async def test_activation_memory_error_fails_even_if_ok_follows(fake_port, tmp_path):
    def callback(fake, message):
        if message.command == 16:
            fake.inject(reply(5, "LCD", "does not fit in Memory") + reply(17, "OK"))
            return True
        return False

    fake = fake_port(config="", callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        with pytest.raises(BoardError, match="Unexpected firmware reply"):
            await client.provision(tmp_path / "backup.json")
        assert [message.command for message in fake.messages][-1] == 16
        assert not client.connected
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reconnect_does_not_receive_previous_session_callbacks(fake_port):
    fake_port()
    client = BoardClient(CONFIG, port="fake")
    await client.connect()
    old_queue = client._events
    old_generation = client._generation
    reader = asyncio.create_task(client.read_event())
    await asyncio.sleep(0)
    await client.close()
    with pytest.raises(BoardError):
        await reader
    fake = fake_port()
    try:
        await client.connect()
        client._deliver_failure(BoardError("old failure"), old_queue)
        client._deliver_event(
            board_module.BoardEvent("button", "IN", 0, 0, time.monotonic()),
            old_queue,
            old_generation,
        )
        fake.inject(reply(29, "IN", 2, 0))
        assert (await asyncio.wait_for(client.read_event(), 0.1)).index == 2
        assert client.connected
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_after_snapshot_completion_in_same_read_are_not_lost(fake_port):
    def callback(fake, message):
        if message.command == 23:
            levels = b"".join(reply(29, "IN", index, 1) for index in range(16))
            fake.inject(levels + reply(34) + reply(29, "IN", 15, 0) + reply(6, "HDG", 2))
            return True
        return False

    fake_port(callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        assert await client.snapshot() == dict.fromkeys(range(16), False)
        gear = await asyncio.wait_for(client.read_event(), 0.1)
        encoder = await asyncio.wait_for(client.read_event(), 0.1)
        assert (gear.kind, gear.index, gear.value) == ("button", 15, 0)
        assert (encoder.kind, encoder.name, encoder.value) == ("encoder", "HDG", 2)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_snapshot_complete_marker_does_not_hide_missing_levels(fake_port):
    def callback(fake, message):
        if message.command == 23:
            # Physical motion between firmware's release and press polls can omit
            # a channel entirely.  Completion alone does not establish a baseline.
            fake.inject(b"".join(reply(29, "IN", i, 1) for i in range(1, 16)) + reply(34))
            return True
        return False

    fake_port(callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        with pytest.raises(BoardError, match="all sixteen"):
            await client.snapshot()
        assert not client.connected
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_snapshot_without_completion_times_out_and_is_not_retried(fake_port):
    def callback(fake, message):
        if message.command == 23:
            fake.inject(b"".join(reply(29, "IN", i, 1) for i in range(16)))
            return True
        return False

    fake = fake_port(callback=callback)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        with pytest.raises(BoardError, match="timed out"):
            await client.snapshot()
        assert [message.command for message in fake.messages].count(23) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provision_append_timeout_has_durable_backup_and_no_retry(fake_port, tmp_path):
    def callback(fake, message):
        if message.command == 11:
            fake.config += message.arguments[0]  # Firmware may already have applied it.
            return True  # But the acknowledgement never reaches the host.
        return False

    fake = fake_port(config="", callback=callback)
    client = BoardClient(CONFIG, port="fake")
    backup = tmp_path / "before.json"
    try:
        with pytest.raises(BoardError, match="timed out"):
            await client.provision(backup)
        assert json.loads(backup.read_text())["config"] == ""
        assert [message.command for message in fake.messages] == [9, 12, 13, 11]
        assert fake.config == CONFIG.split(":")[0] + ":"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_provision_preserves_original_raw_config_slashes(fake_port, tmp_path):
    previous = "12.14.15.16.2.OTHER//INPUT:"

    def callback(fake, message):
        if message.command == 12 and fake.config == previous:
            fake.inject(("10," + previous + ";\r\n").encode("ascii") + reply(7, "OLD_BUTTON", 0))
            return True
        return False

    fake_port(config=previous, callback=callback)
    client = BoardClient(CONFIG, port="fake")
    backup = tmp_path / "raw.json"
    try:
        result = await client.provision(backup)
        assert result["previous_config"] == previous
        assert json.loads(backup.read_text())["config"] == previous
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_heartbeat_uses_shorter_deadline_than_provisioning(fake_port, monkeypatch):
    def callback(fake, message):
        return message.command == 9 and len(fake.messages) > 2

    fake_port(callback=callback)
    monkeypatch.setattr(board_module, "TRANSACTION_TIMEOUT", 0.5)
    monkeypatch.setattr(board_module, "HEARTBEAT_TIMEOUT", 0.025)
    client = BoardClient(CONFIG, port="fake")
    try:
        await client.connect()
        started = time.monotonic()
        with pytest.raises(BoardError, match="timed out"):
            await client.heartbeat()
        assert time.monotonic() - started < 0.2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "invalid",
    [
        CONFIG.replace("10.19.18.17.3.OUT:", "10.19.18.17.4.OUT:"),
        CONFIG.replace("7.39.20.4.LCD:", "7.39.16.2.LCD:"),
        CONFIG.replace("8.0.1.0.HDG:", ""),
        CONFIG + "2.20.EXTRA:",
    ],
)
def test_raw_config_cannot_create_unsafe_module_count_mismatch(fake_port, invalid):
    fake = fake_port()
    with pytest.raises(BoardError, match="Panel|panel"):
        BoardClient(invalid, port="fake")
    assert fake.owner is None
