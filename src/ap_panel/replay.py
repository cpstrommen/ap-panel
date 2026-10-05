"""Reconstruct diagnostic input and panel state without opening transports."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from .config import Hardware
from .inputs import InputProcessor
from .models import PanelFrame, TelemetryValue
from .outputs import OutputRenderer
from .profiles import Profile


def replay(path: Path) -> dict:
    counts: Counter = Counter()
    hardware = profile = processor = renderer = None
    values = {}
    last_frame = None
    generated_frame = None
    inputs = []
    first = last = None
    previous_monotonic = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or set(record) != {
                    "time",
                    "monotonic",
                    "kind",
                    "data",
                }:
                    raise ValueError("Invalid recording envelope")
                stamp = float(record["monotonic"])
                if (
                    not math.isfinite(stamp)
                    or not isinstance(record["time"], (int, float))
                    or not math.isfinite(record["time"])
                ):
                    raise ValueError("Recording timestamps must be finite numbers")
                if previous_monotonic is not None and stamp < previous_monotonic:
                    raise ValueError("Recording timestamps are out of order")
                previous_monotonic = stamp
                first = record["time"] if first is None else first
                last = record["time"]
                kind, data = record["kind"], record["data"]
                counts[kind] += 1
                if line_number == 1 and kind != "metadata":
                    raise ValueError("Recording must start with metadata")
                if kind == "metadata":
                    if line_number != 1 or data["schema_version"] != 1:
                        raise ValueError("Unsupported or repeated recording metadata")
                    hardware = Hardware.model_validate(data["hardware"])
                    if hardware.configuration_hash != data["configuration_hash"]:
                        raise ValueError("Recorded hardware configuration hash does not match")
                    renderer, processor = OutputRenderer(hardware), InputProcessor(hardware)
                elif kind == "profile":
                    profile = Profile.model_validate(data)
                    values.clear()
                    renderer.reset()
                elif kind == "telemetry":
                    values.update({k: TelemetryValue(**v) for k, v in data.items()})
                elif kind == "output":
                    last_frame = PanelFrame(data["lamps"], tuple(data["rows"]))
                elif kind == "event" and data.get("kind") == "snapshot":
                    snapshot = {int(k): bool(v) for k, v in data["data"].items()}
                    processor.reset(snapshot, processor.generation + 1)
                elif kind == "event" and data.get("kind") == "fault":
                    values = {k: TelemetryValue(v.value, valid=False) for k, v in values.items()}
                elif kind == "physical_input" and processor.states:
                    inputs.extend(processor.feed(SimpleNamespace(**data)))
                if processor and processor.states:
                    inputs.extend(processor.flush(stamp))
                if profile and renderer and values:
                    generated_frame = renderer.render(profile, values, stamp)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Recording line {line_number}: {exc}") from exc
    if not counts:
        raise ValueError("Recording is empty")
    return {
        "records": sum(counts.values()),
        "kinds": dict(counts),
        "duration_seconds": last - first,
        "profile": profile.id if profile else None,
        "logical_inputs": len(inputs),
        "input_states": processor.states if processor else {},
        "recorded_panel": {"lamps": last_frame.lamps, "rows": last_frame.rows}
        if last_frame
        else None,
        "feedback_panel": {"lamps": generated_frame.lamps, "rows": generated_frame.rows}
        if generated_frame
        else None,
        "transport_actions": 0,
    }
