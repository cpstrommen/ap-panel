"""Versioned configuration, canonical pin maps and deterministic device records."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = (
    ROOT if (ROOT / "electronics/pinmap.yaml").is_file() else Path(__file__).parent / "resources"
)
SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
Gpio = Annotated[int, Field(ge=0, le=28)]
PICO_HEADER_PINS = {
    0: 1,
    1: 2,
    2: 4,
    3: 5,
    4: 6,
    5: 7,
    6: 9,
    7: 10,
    8: 11,
    9: 12,
    10: 14,
    11: 15,
    12: 16,
    13: 17,
    14: 19,
    15: 20,
    16: 21,
    17: 22,
    18: 24,
    19: 25,
    20: 26,
    21: 27,
    22: 29,
    26: 31,
    27: 32,
    28: 34,
}
INPUT_CONTROL_NAMES = {
    "AP",
    "FD",
    "HDG",
    "NAV",
    "APR",
    "ALT_SEL",
    "ALT",
    "VS",
    "YD",
    "CDI",
    "HDG_PUSH",
    "ALT_PUSH",
    "VS_PUSH",
    "CRS_PUSH",
    "BARO_PUSH",
    "GEAR",
}
OUTPUT_CONTROL_NAMES = {
    "AP",
    "FD",
    "HDG",
    "NAV",
    "APR",
    "ALT_SEL",
    "ALT",
    "VS",
    "YD",
    "GPS",
    "VLOC",
    "GS_CAP",
    "GEAR_L",
    "GEAR_N",
    "GEAR_R",
    "FLAPS_UP",
    "FLAPS_1",
    "FLAPS_2",
    "FLAPS_FULL",
    "SPARE_1",
    "SPARE_2",
    "SPARE_3",
    "SPARE_4",
    "SPARE_5",
}


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently discarding assignments."""


def _unique_mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def read_yaml(path: Path) -> dict:
    data = yaml.load(path.read_text(), Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Encoder(StrictModel):
    name: str
    gpio_a: Gpio
    gpio_b: Gpio
    physical_a: int
    physical_b: int
    connector: str
    decoder_type: Annotated[int, Field(ge=0, le=4)] = 0
    inverted: bool = False


class InputChain(StrictModel):
    name: Literal["IN"] = "IN"
    load: Gpio
    clock: Gpio
    data: Gpio
    physical_load: int
    physical_clock: int
    physical_data: int
    chips: Literal[2] = 2


class OutputChain(StrictModel):
    name: Literal["OUT"] = "OUT"
    data: Gpio
    clock: Gpio
    latch: Gpio
    physical_data: int
    physical_clock: int
    physical_latch: int
    chips: Literal[3] = 3


class LCD(StrictModel):
    name: Literal["LCD"] = "LCD"
    address: Literal[39, 63] = 39
    columns: Literal[20] = 20
    rows: Literal[4] = 4
    sda: Literal[4] = 4
    scl: Literal[5] = 5
    physical_sda: Literal[6] = 6
    physical_scl: Literal[7] = 7


class InputPin(StrictModel):
    index: Annotated[int, Field(ge=0, le=15)]
    name: str
    chip: Literal["IC1", "IC2"]
    channel: Literal["A", "B", "C", "D", "E", "F", "G", "H"]
    physical_pin: int
    connector: str
    active_low: Literal[True] = True


class OutputPin(StrictModel):
    index: Annotated[int, Field(ge=0, le=23)]
    name: str
    chip: Literal["U2", "U3", "U4"]
    q: Annotated[int, Field(ge=0, le=7)]
    physical_pin: int
    uln_chip: Literal["IC3", "IC4", "IC5"]
    uln_input: Annotated[int, Field(ge=1, le=8)]
    uln_output: Annotated[int, Field(ge=11, le=18)]
    connector: str
    reserved: bool = False


class Hardware(StrictModel):
    schema_version: Literal[1]
    board_type: Literal["MobiFlight RaspiPico2"]
    firmware: Literal["3.1.4"]
    bench_verified: bool = False
    encoders: list[Encoder]
    input_chain: InputChain
    output_chain: OutputChain
    lcd: LCD
    inputs: list[InputPin]
    outputs: list[OutputPin]

    @model_validator(mode="after")
    def validate_inventory(self) -> "Hardware":
        expected = {"HDG", "ALT", "VS", "CRS", "BARO", "FLAPS"}
        if len(self.encoders) != 6 or {x.name for x in self.encoders} != expected:
            raise ValueError("Exactly the six panel encoders are required")
        if len({encoder.connector for encoder in self.encoders}) != 6:
            raise ValueError("Encoder connector assignments must be unique")
        pins = [pin for e in self.encoders for pin in (e.gpio_a, e.gpio_b)]
        pins += [
            self.input_chain.load,
            self.input_chain.clock,
            self.input_chain.data,
            self.output_chain.data,
            self.output_chain.clock,
            self.output_chain.latch,
            self.lcd.sda,
            self.lcd.scl,
        ]
        if len(set(pins)) != len(pins):
            raise ValueError("GPIO assignments overlap")
        gpio_packages = [(e.gpio_a, e.physical_a) for e in self.encoders]
        gpio_packages += [(e.gpio_b, e.physical_b) for e in self.encoders]
        gpio_packages += [
            (getattr(chain, field), getattr(chain, "physical_" + field))
            for chain, fields in (
                (self.input_chain, ("load", "clock", "data")),
                (self.output_chain, ("data", "clock", "latch")),
            )
            for field in fields
        ]
        if any(
            gpio not in PICO_HEADER_PINS or PICO_HEADER_PINS[gpio] != physical
            for gpio, physical in gpio_packages
        ):
            raise ValueError("GPIO and physical pin must match the Pico 2 header")
        for entries, count in ((self.inputs, 16), (self.outputs, 24)):
            if len(entries) != count or {x.index for x in entries} != set(range(count)):
                raise ValueError(f"Expected every index 0..{count - 1} exactly once")
            if len({x.name for x in entries}) != count:
                raise ValueError("Control names must be unique")
            if len({x.connector for x in entries}) != count:
                raise ValueError("Connector assignments must be unique")
        if {pin.name for pin in self.inputs} != INPUT_CONTROL_NAMES:
            raise ValueError("Input names must match the sixteen panel controls")
        if {pin.name for pin in self.outputs} != OUTPUT_CONTROL_NAMES:
            raise ValueError("Output names must match the panel lamps and five spares")
        # Numeric package relations are validated, including the reversed ULN wiring.
        input_pins = [11, 12, 13, 14, 3, 4, 5, 6]
        output_pins = [15, 1, 2, 3, 4, 5, 6, 7]
        for entry in self.inputs:
            q = entry.index % 8
            if (
                entry.chip != ("IC1", "IC2")[entry.index // 8]
                or entry.channel != "ABCDEFGH"[q]
                or entry.physical_pin != input_pins[q]
            ):
                raise ValueError("Input chip/channel/pin does not match its serial index")
        for entry in self.outputs:
            q = entry.index % 8
            if (
                entry.q != q
                or entry.physical_pin != output_pins[q]
                or entry.chip != ("U2", "U3", "U4")[entry.index // 8]
                or entry.uln_chip != ("IC3", "IC4", "IC5")[entry.index // 8]
                or entry.uln_input != 8 - q
                or entry.uln_output != 11 + q
                or entry.reserved != (entry.index >= 19)
                or entry.reserved != entry.name.startswith("SPARE_")
            ):
                raise ValueError("Output mapping must preserve reversed ULN channels and spares")
        if any(
            not SAFE_NAME.fullmatch(x.name) for x in [*self.encoders, *self.inputs, *self.outputs]
        ):
            raise ValueError("Names must be safe ASCII identifiers of at most 16 characters")
        return self

    def records(self) -> list[str]:
        records = [f"8.{e.gpio_a}.{e.gpio_b}.{e.decoder_type}.{e.name}:" for e in self.encoders]
        i, o, lcd = self.input_chain, self.output_chain, self.lcd
        return records + [
            f"12.{i.load}.{i.clock}.{i.data}.{i.chips}.{i.name}:",
            f"10.{o.latch}.{o.clock}.{o.data}.{o.chips}.{o.name}:",
            f"7.{lcd.address}.{lcd.columns}.{lcd.rows}.{lcd.name}:",
        ]

    @property
    def raw_config(self) -> str:
        return "".join(self.records())

    @property
    def configuration_hash(self) -> str:
        return hashlib.sha256(self.raw_config.encode("ascii")).hexdigest()

    @property
    def assigned_mask(self) -> int:
        return sum(1 << x.index for x in self.outputs if not x.reserved)

    def pinout_markdown(self) -> str:
        lines = [
            "# Generated hardware pinout",
            "",
            "Generated from `electronics/pinmap.yaml`.",
            "Do not edit these tables manually.",
            "",
            "## GPIO",
            "",
            "| Device | GPIO | Pico physical pin |",
            "|---|---|---|",
        ]
        for e in self.encoders:
            lines.append(
                f"| {e.name} A / B ({e.connector}) | GP{e.gpio_a} / GP{e.gpio_b} | {e.physical_a} / {e.physical_b} |"
            )
        for title, chain, fields in (
            ("Input chain", self.input_chain, ("load", "clock", "data")),
            ("Output chain", self.output_chain, ("data", "clock", "latch")),
        ):
            for key in fields:
                lines.append(
                    f"| {title} {key} | GP{getattr(chain, key)} | {getattr(chain, 'physical_' + key)} |"
                )
        lines += [
            f"| LCD SDA / SCL | GP{self.lcd.sda} / GP{self.lcd.scl} | {self.lcd.physical_sda} / {self.lcd.physical_scl} |",
            "",
            "## Active-low inputs",
            "",
            "| Index | Control | Chip/input | Pin | Connector |",
            "|---:|---|---|---:|---|",
        ]
        lines += [
            f"| {x.index} | {x.name} | {x.chip} {x.channel} | {x.physical_pin} | {x.connector} |"
            for x in sorted(self.inputs, key=lambda x: x.index)
        ]
        lines += [
            "",
            "## Outputs (HIGH = lamp on)",
            "",
            "| Index | Lamp | 595 output/pin | ULN input/output | Connector |",
            "|---:|---|---|---|---|",
        ]
        lines += [
            f"| {x.index} | {x.name} | {x.chip} Q{x.q} / {x.physical_pin} | {x.uln_chip} {x.uln_input} / {x.uln_output} | {x.connector} |"
            for x in sorted(self.outputs, key=lambda x: x.index)
        ]
        lines += [
            "",
            "## Stock firmware device records",
            "",
            "```text",
            *self.records(),
            "```",
            "",
            f"Configuration SHA-256: `{self.configuration_hash}`",
            "",
        ]
        return "\n".join(lines)


class Settings(StrictModel):
    schema_version: Literal[1] = 1
    serial_number: str | None = None
    port: str | None = None
    simulator_url: str = "http://127.0.0.1:8086"
    dashboard_port: Annotated[int, Field(ge=1024, le=65535)] = 8787
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_directory: str = ".ap-panel"
    log_max_bytes: Annotated[int, Field(ge=1024)] = 2_000_000
    log_backup_count: Annotated[int, Field(ge=0, le=10)] = 3

    @model_validator(mode="after")
    def local_simulator(self) -> "Settings":
        from urllib.parse import urlparse

        url = urlparse(self.simulator_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Simulator URL must be a local HTTP origin")
        # Accessing .port also rejects nonnumeric/out-of-range URL ports.
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("Simulator HTTP port must be between 1 and 65535")
        return self


def load_hardware(path: Path | None = None) -> Hardware:
    path = path or (
        RESOURCE_ROOT / ("electronics/pinmap.yaml" if RESOURCE_ROOT == ROOT else "pinmap.yaml")
    )
    return Hardware.model_validate(read_yaml(path))


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path("settings.local.yaml")
    return Settings.model_validate(read_yaml(path)) if path.is_file() else Settings()


class FirmwareManifest(StrictModel):
    schema_version: Literal[1]
    version: Literal["3.1.4"]
    board_type: Literal["MobiFlight RaspiPico2"]
    usb_vid: Literal["2E8A"]
    usb_pid: Literal["F00A"]
    baudrate: Literal[115200]
    dtr: Literal[True]
    connection_delay_seconds: Literal[1.25]
    release_url: str
    archive_url: str
    archive_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    uf2_member: str
    uf2_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    protocol_source: str

    @model_validator(mode="after")
    def official_provenance(self) -> "FirmwareManifest":
        base = "https://github.com/MobiFlight/MobiFlight-FirmwareSource/"
        if (
            self.release_url != base + "releases/tag/3.1.4"
            or self.archive_url != base + "releases/download/3.1.4/Mobiflight-Firmware_3.1.4.zip"
            or self.protocol_source != base + "tree/3.1.4/src"
            or self.uf2_member != "Mobiflight-Firmware/firmware/mobiflight_raspberrypico2_3_1_4.uf2"
        ):
            raise ValueError("Firmware provenance must identify the pinned official Pico 2 release")
        return self


def firmware_manifest() -> dict:
    path = RESOURCE_ROOT / (
        "mobiflight/firmware-manifest.json" if RESOURCE_ROOT == ROOT else "firmware-manifest.json"
    )

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return FirmwareManifest.model_validate(
        json.loads(path.read_text(), object_pairs_hook=unique_pairs)
    ).model_dump()
