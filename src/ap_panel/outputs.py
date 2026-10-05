"""Render only simulator feedback into the fixed LCD and lamp inventory."""

from __future__ import annotations

import math

from .config import Hardware
from .models import PanelFrame, TelemetryValue
from .profiles import Profile, interpret


def _integer(value: float | None, width: int, signed: bool = False, degrees: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return "-" * width
    number = round(value)
    if degrees:
        number %= 360
    text = f"{number:+0{width}d}" if signed else f"{number:0{width}d}"
    return text if len(text) <= width else "#" * width


class OutputRenderer:
    def __init__(self, hardware: Hardware):
        self.hardware = hardware
        self._bits = {x.name: x.index for x in hardware.outputs if not x.reserved}
        self.reset()

    def reset(self) -> None:
        self._flap_candidate: str | None = None
        self._flap_since = 0.0

    def render(self, profile: Profile, values: dict[str, TelemetryValue], now: float) -> PanelFrame:
        state = interpret(profile, values)
        lamps = 0
        phase = now % 1.0 < 0.5

        def illuminate(name: str, on: bool) -> None:
            nonlocal lamps
            if on and name in self._bits:
                lamps |= 1 << self._bits[name]

        for name in ("AP", "FD", "HDG", "NAV", "APR", "ALT_SEL", "ALT", "VS", "YD"):
            mode = state.modes.get(name)
            illuminate(name, mode == 2 or (mode == 1 and phase))
        illuminate("GPS", state.source == "GPS")
        illuminate("VLOC", state.source in ("NAV1", "NAV2"))
        illuminate("GS_CAP", state.modes.get("GS") == 2)
        for label, down in state.gear.items():
            illuminate("GEAR_" + label, down is True)
        if state.flaps != self._flap_candidate:
            self._flap_candidate, self._flap_since = state.flaps, now
        if state.flaps is not None and now - self._flap_since >= 0.1:
            illuminate("FLAPS_" + state.flaps, True)
        altitude = (
            state.values["selected_altitude"]
            if profile.altitude_preselection
            else state.values["held_altitude"]
            if state.modes.get("ALT") == 2
            else None
        )
        baro = state.values["baro"]
        pressure = f"{baro:05.2f}" if baro is not None else "-----"
        if len(pressure) > 5:
            pressure = "#####"
        altitude_mode = (
            "ALTSEL ARM"
            if state.modes.get("ALT_SEL") == 1
            else "ALT HOLD"
            if state.modes.get("ALT") == 2
            else "ALTSEL OFF"
            if profile.altitude_preselection
            else "ALT HOLD --"
        )
        gs = {1: "GS ARM", 2: "GS CAP"}.get(state.modes.get("GS"), "GS ---")
        rows = (
            f"HDG {_integer(state.values['heading'], 3, degrees=True)}  CRS {_integer(state.values['course'], 3, degrees=True)}",
            f"ALT {_integer(altitude, 5)} VS {_integer(state.values['vs'], 5, signed=True)}",
            f"BARO {pressure} CDI {state.source}",
            f"{altitude_mode:<11}{gs}",
        )
        return PanelFrame(lamps & self.hardware.assigned_mask, tuple(row.ljust(20) for row in rows))
