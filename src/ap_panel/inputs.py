"""Physical state baselines and host-side stable debounce."""

from __future__ import annotations

from typing import Any

from .config import Hardware
from .models import InputEvent


class InputProcessor:
    def __init__(self, hardware: Hardware, debounce: float = 0.03):
        self.hardware = hardware
        self.debounce = debounce
        self.generation = 0
        self.states: dict[str, bool] = {}
        self.pending: dict[str, tuple[bool, float]] = {}
        self.held: set[str] = set()
        self._inputs = {x.index: x.name for x in hardware.inputs}
        self._encoders = {x.name: x for x in hardware.encoders}

    def reset(self, snapshot: dict[int, bool], generation: int) -> None:
        if set(snapshot) != set(self._inputs):
            raise ValueError("All 16 input states are required for a baseline")
        self.states = {name: snapshot[index] for index, name in self._inputs.items()}
        self.held = {name for name, pressed in self.states.items() if pressed and name != "GEAR"}
        self.pending.clear()
        self.generation = generation

    def feed(self, event: Any) -> list[InputEvent]:
        if event.kind == "button":
            if event.name != self.hardware.input_chain.name or event.index not in self._inputs:
                return []
            if event.value not in (0, 1):
                return []
            name, pressed = self._inputs[event.index], event.value == 0
            candidate = self.pending.get(name)
            if pressed == self.states.get(name):
                self.pending.pop(name, None)
            elif candidate is None or candidate[0] != pressed:
                self.pending[name] = pressed, event.timestamp
            return []
        if (
            event.kind != "encoder"
            or event.name not in self._encoders
            or event.value not in range(4)
        ):
            return []
        direction = -1 if event.value in (0, 1) else 1
        if self._encoders[event.name].inverted:
            direction *= -1
        return [InputEvent(event.name, "encoder", direction, event.timestamp, self.generation)]

    def flush(self, now: float) -> list[InputEvent]:
        result = []
        for name, (pressed, since) in list(self.pending.items()):
            if now - since < self.debounce:
                continue
            del self.pending[name]
            self.states[name] = pressed
            if not pressed:
                self.held.discard(name)
            if name == "GEAR" or (pressed and name not in self.held):
                result.append(InputEvent(name, "button", pressed, now, self.generation))
        return result


def action_name(event: InputEvent) -> str:
    if event.control == "GEAR":
        return "GEAR_DOWN" if event.value else "GEAR_UP"
    if event.kind == "encoder":
        return event.control + ("_CW" if event.value == 1 else "_CCW")
    return event.control
