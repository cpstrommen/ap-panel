"""Aircraft capabilities and reference-verified command/DataRef manifests."""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .config import RESOURCE_ROOT, StrictModel, read_yaml
from .models import AircraftState, TelemetryValue


class DataRefSpec(StrictModel):
    path: str
    value_type: Literal["numeric", "float", "double", "int", "float_array", "int_array", "data"] = (
        "numeric"
    )
    required: bool = True
    writable: bool = False
    reference_verified: bool = True
    simulator_tested: bool = False


class Action(StrictModel):
    kind: Literal["command", "write", "alt_sel", "cdi", "alt_sync", "course_sync"] = "command"
    command: str | None = None
    dataref: str | None = None
    value: float | None = None
    requires: list[str] = Field(default_factory=list)
    reference_verified: bool = True
    simulator_tested: bool = False

    @model_validator(mode="after")
    def check_action(self) -> "Action":
        if self.kind in {"command", "alt_sync", "course_sync"} and not self.command:
            raise ValueError("Action requires a command")
        if self.kind == "write" and (self.dataref is None or self.value is None):
            raise ValueError("Write requires a DataRef key and finite value")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("Write value must be finite")
        return self


class Profile(StrictModel):
    schema_version: Literal[1]
    id: str
    name: str
    aircraft_paths: list[str]
    expected_ap_types: list[int] = Field(default_factory=list)
    altitude_preselection: bool = False
    simulator_tested: bool = False
    tested_aircraft_path: str | None = None
    datarefs: dict[str, DataRefSpec]
    actions: dict[str, Action]
    disabled: dict[str, str] = Field(default_factory=dict)
    gear_indices: dict[str, int] = Field(default_factory=dict)
    gear_verified: bool = False
    flap_detents: dict[str, float] = Field(default_factory=dict)
    flaps_verified: bool = False
    yd_engaged_key: str | None = None
    yd_engaged_verified: bool = False
    alt_sync_use_write: bool = False
    course_sync_use_write: bool = False
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_profile(self) -> "Profile":
        if self.simulator_tested and not self.tested_aircraft_path:
            raise ValueError("A tested profile requires the exact tested aircraft path")
        if self.gear_indices and (
            set(self.gear_indices) != {"L", "N", "R"}
            or len(set(self.gear_indices.values())) != 3
            or any(v < 0 or v > 9 for v in self.gear_indices.values())
        ):
            raise ValueError("Gear mapping requires three distinct L/N/R array indices")
        if self.gear_verified and not self.gear_indices:
            raise ValueError("Verified gear requires measured array indices")
        if self.gear_verified and (
            "gear" not in self.datarefs
            or self.datarefs["gear"].value_type not in {"float_array", "int_array"}
        ):
            raise ValueError("Verified gear requires an array feedback DataRef")
        if not set(self.flap_detents) <= {"UP", "1", "2", "FULL"}:
            raise ValueError("Unknown flap detent")
        values = sorted(self.flap_detents.values())
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise ValueError("Flap ratios must be finite and normalized")
        if any(b - a <= 0.04 for a, b in zip(values, values[1:])):
            raise ValueError("Flap detent tolerances overlap")
        if self.flaps_verified and not {"UP", "1", "FULL"} <= set(self.flap_detents):
            raise ValueError("Verified flaps require at least UP, 1 and FULL")
        if self.id == "c90b" and "2" in self.flap_detents:
            raise ValueError("C90 has three flap positions; Flaps 2 must remain unused")
        if (
            self.flaps_verified
            and self.id == "c172-analog"
            and set(self.flap_detents) != {"UP", "1", "2", "FULL"}
        ):
            raise ValueError("Verified C172 flaps require all four measured positions")
        ordered = [
            self.flap_detents[label]
            for label in ("UP", "1", "2", "FULL")
            if label in self.flap_detents
        ]
        if any(b - a <= 0.04 for a, b in zip(ordered, ordered[1:])):
            raise ValueError("Measured flap positions must increase from UP to FULL")
        if self.flaps_verified and (
            "flap_actual" not in self.datarefs
            or self.datarefs["flap_actual"].value_type not in {"numeric", "float", "double", "int"}
        ):
            raise ValueError("Verified flaps require numeric actual-position feedback")
        if self.yd_engaged_verified and (
            not self.yd_engaged_key or self.yd_engaged_key not in self.datarefs
        ):
            raise ValueError("Verified YD requires a declared engaged-feedback DataRef")
        if not self.altitude_preselection and any(
            name in {"ALT_SEL", "ALT_CW", "ALT_CCW", "ALT_PUSH"}
            or action.kind in {"alt_sel", "alt_sync"}
            for name, action in self.actions.items()
        ):
            raise ValueError("Altitude selection controls require an altitude preselector")
        if set(self.disabled) & set(self.actions):
            raise ValueError("An action cannot also be declared disabled")
        for key, ref in self.datarefs.items():
            if not ref.path or not ref.reference_verified:
                raise ValueError(f"DataRef {key} must have reference provenance")
        for name, action in self.actions.items():
            if any(key not in self.datarefs for key in action.requires):
                raise ValueError(f"Unknown required DataRef in {name}")
            if action.dataref and (
                action.dataref not in self.datarefs or not self.datarefs[action.dataref].writable
            ):
                raise ValueError(f"{name} writes an undeclared/read-only DataRef")
        return self

    def matches(self, aircraft: str) -> bool:
        path = normalize_aircraft_path(aircraft)
        paths = [self.tested_aircraft_path] if self.simulator_tested else self.aircraft_paths
        return any(path == normalize_aircraft_path(p) for p in paths if p)

    def compatible(self, aircraft: str, ap_type: Any) -> bool:
        return self.matches(aircraft) and (
            not self.expected_ap_types or ap_type in self.expected_ap_types
        )

    def command_paths(self) -> list[str]:
        paths = {
            a.command
            for a in self.actions.values()
            if a.command
            and not (a.kind == "alt_sync" and self.alt_sync_use_write)
            and not (a.kind == "course_sync" and self.course_sync_use_write)
        }
        if any(a.kind == "alt_sel" for a in self.actions.values()):
            paths.update(["sim/autopilot/vertical_speed_pre_sel", "sim/autopilot/altitude_arm"])
        if any(a.kind == "cdi" for a in self.actions.values()):
            paths.update(["sim/autopilot/hsi_select_gps", "sim/autopilot/hsi_select_nav_1"])
        return sorted(paths)

    def missing_for(self, name: str, simulator: Any) -> str | None:
        action = self.actions.get(name)
        if not action:
            return self.disabled.get(name, "This aircraft does not support this control")
        if not action.reference_verified:
            return "Action has not been reference-verified"
        for key in action.requires:
            if key not in simulator.values or not simulator.values[key].valid:
                return f"Unavailable simulator feedback: {key}"
        write_target = action.dataref
        if action.kind == "alt_sync" and self.alt_sync_use_write:
            write_target = "selected_altitude"
        if action.kind == "course_sync" and self.course_sync_use_write:
            write_target = "course"
        commands = [action.command] if action.command and write_target is None else []
        if action.kind == "alt_sel":
            commands = ["sim/autopilot/vertical_speed_pre_sel", "sim/autopilot/altitude_arm"]
        if action.kind == "cdi":
            commands = ["sim/autopilot/hsi_select_gps", "sim/autopilot/hsi_select_nav_1"]
        if any(path not in simulator.command_ids for path in commands):
            return "Required aircraft command is unavailable"
        if write_target:
            spec = self.datarefs.get(write_target)
            if spec is None or not spec.writable:
                return f"Simulator setting is not declared writable: {write_target}"
            if write_target not in simulator.values or not simulator.values[write_target].valid:
                return "Writable simulator setting is unavailable"
        return None


def normalize_aircraft_path(value: str) -> str:
    return value.replace("\\", "/").strip("\x00 /").casefold()


def aircraft_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True).decode("utf-8").rstrip("\x00")
    except (ValueError, UnicodeError):
        return None


def load_profiles(directory: Path | None = None) -> list[Profile]:
    directory = directory or RESOURCE_ROOT / "profiles"
    common = read_yaml(directory / "common.yaml")
    profiles = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "common.yaml":
            continue
        data = read_yaml(path)
        merged = {**common, **data}
        for key in ("datarefs", "actions", "disabled"):
            merged[key] = {**common.get(key, {}), **data.get(key, {})}
        profiles.append(Profile.model_validate(merged))
    if len({p.id for p in profiles}) != len(profiles):
        raise ValueError("Profile identifiers must be unique")
    return profiles


def number(values: dict[str, TelemetryValue], key: str) -> float | None:
    entry = values.get(key)
    value = entry.value if entry and entry.valid else None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def status_value(values: dict[str, TelemetryValue], key: str, allowed=(0, 1, 2)) -> int | None:
    value = number(values, key)
    return int(value) if value in allowed else None


def interpret(profile: Profile, values: dict[str, TelemetryValue]) -> AircraftState:
    state = AircraftState(profile.id)
    for key in ("heading", "course", "selected_altitude", "held_altitude", "vs", "baro"):
        state.values[key] = number(values, key)
    state.modes = {
        name: status_value(values, key)
        for name, key in {
            "HDG": "hdg_status",
            "APR": "apr_status",
            "ALT": "alt_status",
            "VS": "vs_status",
            "GS": "gs_status",
        }.items()
    }
    state.modes["AP"] = 2 if status_value(values, "ap", (0, 1)) == 1 else 0
    nav, gpss = status_value(values, "nav_status"), status_value(values, "gpss_status")
    state.modes["NAV"] = 2 if 2 in (nav, gpss) else (1 if 1 in (nav, gpss) else 0)
    arm = status_value(values, "alt_armed", (0, 1))
    state.modes["ALT_SEL"] = 1 if profile.altitude_preselection and arm == 1 else 0
    # A held altitude is active only when the simulator says hold/capture is active.
    state.modes["ALT"] = 2 if state.modes["ALT"] == 2 else 0
    fd = status_value(values, "fd_mode")
    selection = status_value(values, "fd_selected", (0, 1))
    state.modes["FD"] = 2 if "FD" in profile.actions and fd in (1, 2) and selection == 1 else 0
    selected = status_value(values, "yd_selected", (0, 1))
    state.yd_selection = bool(selected) if selected is not None else None
    state.modes["YD"] = (
        2
        if profile.yd_engaged_verified and number(values, profile.yd_engaged_key or "") == 1
        else 0
    )
    source = status_value(values, "source")
    state.source = {0: "NAV1", 1: "NAV2", 2: "GPS"}.get(source, "----")
    entry = values.get("gear")
    gear = entry.value if entry and entry.valid else None
    for label in ("L", "N", "R"):
        index = profile.gear_indices.get(label)
        ratio = (
            gear[index]
            if isinstance(gear, list) and index is not None and index < len(gear)
            else None
        )
        state.gear[label] = (
            ratio >= 0.99
            if profile.gear_verified
            and type(ratio) in (int, float)
            and math.isfinite(ratio)
            and 0 <= ratio <= 1
            else None
        )
    ratio = number(values, "flap_actual")
    if profile.flaps_verified and ratio is not None:
        state.flaps = next(
            (label for label, value in profile.flap_detents.items() if abs(value - ratio) <= 0.02),
            None,
        )
    return state
