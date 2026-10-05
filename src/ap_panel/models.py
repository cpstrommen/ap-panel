"""Transport-neutral values shared by the board, simulator and renderer."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING_BOARD = "CONNECTING_BOARD"
    VERIFYING_CONFIGURATION = "VERIFYING_CONFIGURATION"
    SNAPSHOTTING_INPUTS = "SNAPSHOTTING_INPUTS"
    CONNECTING_SIMULATOR = "CONNECTING_SIMULATOR"
    RESOLVING_PROFILE = "RESOLVING_PROFILE"
    READY = "READY"
    RUNNING = "RUNNING"
    FAULT = "FAULT"


@dataclass(frozen=True)
class TelemetryValue:
    value: Any
    valid: bool = True
    revision: int = 0
    generation: int = 0
    updated_at: float = 0.0


@dataclass(frozen=True)
class InputEvent:
    control: str
    kind: str
    value: bool | int
    timestamp: float
    generation: int


@dataclass(frozen=True)
class PanelFrame:
    lamps: int
    rows: tuple[str, str, str, str]

    def __post_init__(self) -> None:
        if not 0 <= self.lamps < (1 << 24):
            raise ValueError("Lamp word must fit in 24 bits")
        if len(self.rows) != 4 or any(len(row) != 20 for row in self.rows):
            raise ValueError("LCD frame must have four rows of exactly 20 characters")
        if any(
            not row.isascii() or any(ord(c) < 32 or ord(c) > 126 for c in row) for row in self.rows
        ):
            raise ValueError("LCD rows must contain printable ASCII")

    @property
    def text(self) -> str:
        return "".join(self.rows)

    @classmethod
    def message(cls, text: str = "") -> "PanelFrame":
        rows = [line[:20].ljust(20) for line in text.splitlines()[:4]]
        rows.extend([" " * 20] * (4 - len(rows)))
        return cls(0, tuple(rows))


@dataclass
class AircraftState:
    profile_id: str
    values: dict[str, float | None] = field(default_factory=dict)
    modes: dict[str, int | None] = field(default_factory=dict)
    source: str = "----"
    gear: dict[str, bool | None] = field(default_factory=dict)
    flaps: str | None = None
    yd_selection: bool | None = None
