"""Bounded, incremental CmdMessenger framing for stock MobiFlight 3.1.4.

The firmware's receive buffer is considerably smaller than its configuration
storage.  The transmit limit applies *after* escaping, including the semicolon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_TX_FRAME_BYTES = 94
MAX_RX_FRAME_BYTES = 4096
MAX_CONFIG_BYTES = 1494  # Firmware checks config_length + chunk_length + 1 < 1496.


class ProtocolError(ValueError):
    """A malformed or oversized serial message."""


@dataclass(frozen=True)
class Message:
    command: int
    arguments: tuple[str, ...] = ()
    wire: bytes | None = field(default=None, compare=False, repr=False)


def encode(command: int, *arguments: object) -> bytes:
    """Encode a command without firmware-overflowing its 96-byte buffer."""
    if type(command) is not int or not 0 <= command <= 255:
        raise ProtocolError("Command must be an integer between 0 and 255")
    fields = [str(command)]
    for argument in arguments:
        value = str(argument)
        if any(not 32 <= ord(char) <= 126 for char in value):
            raise ProtocolError("CmdMessenger arguments must be printable ASCII")
        fields.append("".join("/" + char if char in ",;/" else char for char in value))
    frame = (",".join(fields) + ";").encode("ascii")
    if len(frame) > MAX_TX_FRAME_BYTES:
        raise ProtocolError(
            f"Escaped command is {len(frame)} bytes; stock firmware allows at most "
            f"{MAX_TX_FRAME_BYTES} bytes"
        )
    return frame


class Parser:
    """Parse fragmented/coalesced replies, including escaped delimiters.

    CR/LF is tolerated only between commands, where firmware appends it.  A
    framing error resets the parser and raises: callers must close the session
    instead of guessing where the next reply starts.
    """

    def __init__(self, max_frame_bytes: int = MAX_RX_FRAME_BYTES) -> None:
        if max_frame_bytes < 2:
            raise ValueError("Receive limit must be at least two bytes")
        self.max_frame_bytes = max_frame_bytes
        self.reset()

    def reset(self) -> None:
        self._fields: list[str] = []
        self._field = bytearray()
        self._escaped = False
        self._length = 0
        self._raw = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        messages: list[Message] = []
        try:
            for byte in data:
                if self._length == 0 and byte in (10, 13):
                    continue
                self._length += 1
                if self._length > self.max_frame_bytes:
                    raise ProtocolError("Firmware reply exceeded the receive frame limit")
                if not 32 <= byte <= 126:
                    raise ProtocolError("Non-ASCII/control byte inside firmware reply")
                self._raw.append(byte)
                if self._escaped:
                    self._field.append(byte)
                    self._escaped = False
                elif byte == ord("/"):
                    self._escaped = True
                elif byte == ord(","):
                    self._fields.append(self._field.decode("ascii"))
                    self._field.clear()
                elif byte == ord(";"):
                    fields = self._fields + [self._field.decode("ascii")]
                    if not fields[0].isascii() or not fields[0].isdecimal():
                        raise ProtocolError("Firmware command ID is not a decimal integer")
                    command = int(fields[0])
                    if command > 255:
                        raise ProtocolError("Firmware command ID is out of range")
                    messages.append(Message(command, tuple(fields[1:]), bytes(self._raw)))
                    self.reset()
                else:
                    self._field.append(byte)
        except ProtocolError:
            self.reset()
            raise
        return messages


def config_records(config: str) -> tuple[str, ...]:
    """Validate and split raw EEPROM config into independently uploadable records.

    Config.cpp reads raw arguments, without CmdMessenger unescaping.  Delimiter
    or escape characters would therefore change the stored configuration.
    """
    if not config or not config.endswith(":"):
        raise ProtocolError("Board configuration must contain colon-terminated records")
    if any(not 32 <= ord(char) <= 126 or char in ",;/" for char in config):
        raise ProtocolError("Raw configuration contains unsafe characters")
    if len(config.encode("ascii")) > MAX_CONFIG_BYTES:
        raise ProtocolError(f"Raw configuration exceeds {MAX_CONFIG_BYTES} bytes")
    records = tuple(part + ":" for part in config[:-1].split(":"))
    for record in records:
        fields = record[:-1].split(".")
        if len(fields) < 3 or not all(field for field in fields):
            raise ProtocolError("Malformed raw configuration record")
        if not fields[0].isdecimal():
            raise ProtocolError("Configuration device type must be numeric")
        encode(11, record)
    return records


def lamp_commands(lamps: int) -> tuple[bytes, ...]:
    """Absolute 24-bit output update, farthest byte first; bit order unchanged."""
    if type(lamps) is not int or not 0 <= lamps <= 0xFFFFFF:
        raise ProtocolError("Lamp bitmap must be a 24-bit nonnegative integer")

    def command(value: int, mask: int) -> bytes:
        return encode(27, 0, 3, value, (mask >> 16) & 255, (mask >> 8) & 255, mask & 255)

    frames = [command(0, 0xFFFFFF ^ lamps)]
    if lamps:
        frames.append(command(1, lamps))
    return tuple(frames)


def lcd_command(rows: tuple[str, str, str, str]) -> bytes:
    if len(rows) != 4 or any(not isinstance(row, str) or len(row) != 20 for row in rows):
        raise ProtocolError("LCD requires exactly four rows of twenty characters")
    return encode(25, 0, "".join(rows))
