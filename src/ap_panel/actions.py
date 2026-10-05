"""Guarded aircraft actions; simulator feedback is the source of truth.

An acknowledgement proves acceptance, not the resulting autopilot mode.
Multi-step actions therefore refresh and check feedback between operations.
An interrupted or partially completed action is never retried or undone.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

from .profiles import Action, Profile
from .xplane import XPlaneError

VS_PRESELECT = "sim/autopilot/vertical_speed_pre_sel"
ALT_ARM = "sim/autopilot/altitude_arm"
NAV1_SELECT = "sim/autopilot/hsi_select_nav_1"
GPS_SELECT = "sim/autopilot/hsi_select_gps"
NUMERIC_TYPES = {"int", "float", "double"}
STATUS_DOMAINS = {
    "ap": (0, 1),
    "source": (0, 1, 2),
    "alt_armed": (0, 1),
    "fd_mode": (0, 1, 2),
    "fd_selected": (0, 1),
    "yd_selected": (0, 1),
    **dict.fromkeys(
        (
            "hdg_status",
            "nav_status",
            "gpss_status",
            "apr_status",
            "alt_status",
            "vs_status",
            "gs_status",
        ),
        (0, 1, 2),
    ),
}


class ActionError(RuntimeError):
    """A control is unavailable, interrupted, or lacked confirming feedback."""


class ActionExecutor:
    """Execute one control against an unchanged aircraft/connection context.

    ``guard`` should check the caller's running state and aircraft/profile
    epoch. It is checked before every effect and after every asynchronous
    operation. The executor also checks connection generation itself.
    """

    def __init__(self, simulator: Any, profile: Profile, guard: Callable[[], bool] = lambda: True):
        self.simulator = simulator
        self.profile = profile
        self.guard = guard
        self.effects_attempted = False
        self._lock = asyncio.Lock()

    async def execute(self, name: str) -> None:
        async with self._lock:
            self.effects_attempted = False
            generation = self.simulator.generation
            profile = self.profile
            self._check(generation, profile)
            reason = profile.missing_for(name, self.simulator)
            if reason:
                raise ActionError(reason)
            action = profile.actions[name]
            self._require_keys(action.requires, generation)
            try:
                if action.kind == "command":
                    await self._command(action.command, generation, profile)
                elif action.kind == "write":
                    await self._write_confirm(action.dataref, action.value, generation, profile)
                elif action.kind == "alt_sel":
                    await self._alt_sel(action, generation, profile)
                elif action.kind == "cdi":
                    await self._cdi(action, generation, profile)
                elif action.kind == "alt_sync":
                    await self._alt_sync(action, generation, profile)
                elif action.kind == "course_sync":
                    await self._course_sync(action, generation, profile)
                else:
                    raise ActionError(f"Unsupported action kind: {action.kind}")
            except XPlaneError as error:
                raise ActionError(f"{name} failed: {error}") from error
            except (KeyError, ValueError) as error:
                raise ActionError(f"{name} failed: {error}") from error

    def _check(self, generation: int, profile: Profile) -> None:
        if not self.guard() or self.profile is not profile:
            raise ActionError("Control interrupted: panel or aircraft profile changed")
        if not self.simulator.healthy or self.simulator.generation != generation:
            raise ActionError("Control interrupted: simulator connection changed")

    def _require_keys(self, keys: list[str], generation: int) -> None:
        for key in keys:
            spec = self.profile.datarefs.get(key)
            if spec is None or key not in self.simulator.dataref_ids:
                raise ActionError(f"Unavailable simulator setting: {key}")
            actual_type = self.simulator.dataref_types.get(key)
            if (spec.value_type == "numeric" and actual_type not in NUMERIC_TYPES) or (
                spec.value_type != "numeric" and actual_type != spec.value_type
            ):
                raise ActionError(f"Unexpected simulator type for {key}: {actual_type}")
            entry = self.simulator.values.get(key)
            if not entry or not entry.valid or entry.generation != generation:
                raise ActionError(f"Unavailable simulator feedback: {key}")
            if actual_type in NUMERIC_TYPES:
                self._number(key, generation)
            if key in STATUS_DOMAINS:
                self._status(key, generation, STATUS_DOMAINS[key])

    def _number(self, key: str, generation: int) -> float:
        entry = self.simulator.values.get(key)
        if not entry or not entry.valid or entry.generation != generation:
            raise ActionError(f"Unavailable simulator feedback: {key}")
        value = entry.value
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ActionError(f"Invalid numeric simulator feedback: {key}")
        return float(value)

    def _status(self, key: str, generation: int, allowed: tuple[int, ...]) -> int:
        value = self._number(key, generation)
        if value not in allowed:
            raise ActionError(f"Unknown simulator status for {key}: {value}")
        return int(value)

    async def _refresh(self, keys: list[str], generation: int, profile: Profile) -> None:
        self._check(generation, profile)
        started = time.monotonic()
        await self.simulator.resync()
        self._check(generation, profile)
        self._require_keys(keys, generation)
        # Unsubscribed or stale fields must never be copied into a selector.
        for key in keys:
            if self.simulator.values[key].updated_at < started:
                raise ActionError(f"Fresh simulator feedback is unavailable: {key}")

    async def _command(self, path: str | None, generation: int, profile: Profile) -> None:
        self._check(generation, profile)
        if path is None or path not in self.simulator.command_ids:
            raise ActionError("Required aircraft command is unavailable")
        self.effects_attempted = True
        await self.simulator.command(path)
        self._check(generation, profile)

    async def _wait(
        self, key: str, predicate: Callable[[Any], bool], generation: int, profile: Profile
    ) -> None:
        self._check(generation, profile)
        await self.simulator.wait_value(key, predicate, timeout=2.0)
        self._check(generation, profile)

    async def _write_confirm(
        self,
        key: str | None,
        value: float | None,
        generation: int,
        profile: Profile,
        *,
        angular: bool = False,
        tolerance: float = 1.0,
    ) -> None:
        self._check(generation, profile)
        spec = profile.datarefs.get(key) if key else None
        if spec is None or not spec.writable:
            raise ActionError(f"Simulator setting is not declared writable: {key}")
        self._require_keys([key], generation)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ActionError("A simulator write requires a finite numeric value")
        if self.simulator.dataref_types[key] == "int" and value != int(value):
            raise ActionError(f"Integer simulator setting requires an integer: {key}")
        self.effects_attempted = True
        await self.simulator.write(key, value)
        self._check(generation, profile)
        await self._refresh([key], generation, profile)
        await self._wait(
            key, lambda actual: self._close(actual, value, tolerance, angular), generation, profile
        )

    @staticmethod
    def _close(actual: Any, expected: float, tolerance: float, angular: bool = False) -> bool:
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(actual)
        ):
            return False
        error = abs((actual - expected + 180) % 360 - 180) if angular else abs(actual - expected)
        return error <= tolerance

    async def _alt_sel(self, action: Action, generation: int, profile: Profile) -> None:
        if not profile.altitude_preselection:
            raise ActionError("This aircraft has no selected-altitude capture mode")
        keys = list(dict.fromkeys(action.requires + ["held_altitude", "pilot_altitude"]))
        await self._refresh(keys, generation, profile)
        selected_vs = self._number("vs", generation)
        selected_altitude = self._number("selected_altitude", generation)
        self._status("alt_armed", generation, (0, 1))
        self._status("vs_status", generation, (0, 1, 2))
        self._status("alt_status", generation, (0, 1, 2))

        def preserved() -> None:
            if not self._close(self._number("vs", generation), selected_vs, 1.0):
                raise ActionError("Selected vertical speed changed during ALT SEL")
            if not self._close(
                self._number("selected_altitude", generation), selected_altitude, 1.0
            ):
                raise ActionError("Selected altitude changed during ALT SEL")

        def captured() -> bool:
            return (
                self._status("alt_status", generation, (0, 1, 2)) == 2
                and self._close(self._number("held_altitude", generation), selected_altitude, 100.0)
                and self._close(
                    self._number("pilot_altitude", generation), selected_altitude, 100.0
                )
            )

        await self._command(VS_PRESELECT, generation, profile)
        await self._refresh(keys, generation, profile)
        preserved()
        await self._wait(
            "vs_status",
            lambda _: self._status("vs_status", generation, (0, 1, 2)) == 2 or captured(),
            generation,
            profile,
        )
        preserved()
        if captured() or self._status("alt_armed", generation, (0, 1)) == 1:
            return
        # altitude_arm may toggle; only issue it after observing disarmed.
        await self._command(ALT_ARM, generation, profile)
        await self._refresh(keys, generation, profile)
        preserved()
        await self._wait(
            "alt_armed",
            lambda _: self._status("alt_armed", generation, (0, 1)) == 1 or captured(),
            generation,
            profile,
        )
        preserved()

    async def _cdi(self, action: Action, generation: int, profile: Profile) -> None:
        await self._refresh(action.requires, generation, profile)
        source = self._status("source", generation, (0, 1, 2))
        target = 0 if source == 2 else 2
        await self._command(NAV1_SELECT if target == 0 else GPS_SELECT, generation, profile)
        await self._refresh(["source"], generation, profile)
        await self._wait("source", lambda value: self._close(value, target, 0), generation, profile)

    async def _alt_sync(self, action: Action, generation: int, profile: Profile) -> None:
        await self._refresh(action.requires, generation, profile)
        altitude = self._number("pilot_altitude", generation)
        if profile.alt_sync_use_write:
            await self._write_confirm("selected_altitude", altitude, generation, profile)
            return
        await self._command(action.command, generation, profile)
        await self._refresh(["selected_altitude"], generation, profile)
        # A selector may quantize to the aircraft's hundred-foot increment.
        await self._wait(
            "selected_altitude",
            lambda value: self._close(value, altitude, 100.0),
            generation,
            profile,
        )

    async def _course_sync(self, action: Action, generation: int, profile: Profile) -> None:
        await self._refresh(action.requires, generation, profile)
        source = self._status("source", generation, (0, 1, 2))
        bearing = None
        if profile.id == "c172-analog":
            if source != 0:
                raise ActionError("C172 course sync requires NAV1 selected")
            self._status("nav1_flag", generation, (1, 2))
            nav_type = self._number("nav1_type", generation)
            # XPLMNavType uses bit flags: VOR=4, ILS=8, Localizer=16.
            if (
                nav_type != int(nav_type)
                or nav_type < 0
                or not int(nav_type) & 4
                or int(nav_type) & 24
            ):
                raise ActionError("C172 course sync requires a received VOR")
            bearing = self._number("nav1_bearing", generation) % 360
        elif source not in (0, 1):
            raise ActionError("Course sync requires a NAV receiver selected")
        if profile.course_sync_use_write:
            if bearing is None:
                raise ActionError("No verified receiver bearing is available for course sync")
            await self._write_confirm("course", bearing, generation, profile, angular=True)
            if self._status("source", generation, (0, 1, 2)) != source:
                raise ActionError("NAV source changed during course sync")
            return
        await self._command(action.command, generation, profile)
        await self._refresh(["course", "source"], generation, profile)
        if self._status("source", generation, (0, 1, 2)) != source:
            raise ActionError("NAV source changed during course sync")
        if bearing is not None:
            await self._wait(
                "course", lambda value: self._close(value, bearing, 1.0, True), generation, profile
            )
        else:
            # C90's native HSI command handles NAV1/NAV2. No guessed bearing
            # or GPS direct-to is substituted; retain its observed OBS value.
            self._number("course", generation)
