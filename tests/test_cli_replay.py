import json
import zipfile

import pytest

from ap_panel import cli
from ap_panel.config import FirmwareManifest, firmware_manifest, load_hardware, load_settings
from ap_panel.profiles import load_profiles
from ap_panel.replay import replay
from ap_panel.runtime import Runtime


def test_cli_modes_and_configuration():
    parser = cli.parser()
    args = parser.parse_args(
        ["run", "--observe", "--port", "/dev/explicit", "--dashboard-port", "8799"]
    )
    hardware, profiles, settings = cli.configuration(args)
    assert len(profiles) == 2 and hardware.lcd.address == 39
    assert settings.port == "/dev/explicit" and settings.dashboard_port == 8799
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--observe", "--commission"])


async def test_devices_filters_eligibility_without_opening(monkeypatch, capsys):
    ports = [
        {"port": "eligible", "mobiflight_pico2": True},
        {"port": "other", "mobiflight_pico2": False},
    ]
    monkeypatch.setattr(cli, "list_devices", lambda: ports)
    await cli.execute(cli.parser().parse_args(["devices"]))
    assert json.loads(capsys.readouterr().out) == ports[:1]
    await cli.execute(cli.parser().parse_args(["devices", "--all"]))
    assert json.loads(capsys.readouterr().out) == ports


async def test_dry_run_never_opens_transport(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run opened a transport")

    monkeypatch.setattr(cli, "BoardClient", forbidden)
    args = cli.parser().parse_args(["configure", "--dry-run"])
    assert await cli.execute(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["records"][-2:] == ["10.19.18.17.3.OUT:", "7.39.20.4.LCD:"]


async def test_validate_regenerates_both_artifacts(tmp_path, capsys):
    hardware = load_hardware()
    import yaml

    pinmap = tmp_path / "electronics" / "pinmap.yaml"
    pinmap.parent.mkdir()
    pinmap.write_text(yaml.safe_dump(hardware.model_dump()))
    args = cli.parser().parse_args(["validate", "--pinmap", str(pinmap), "--generate"])
    assert await cli.execute(args) == 0
    assert pinmap.with_name("pinout.md").read_text() == hardware.pinout_markdown()
    records = tmp_path / "mobiflight" / "devices.txt"
    assert records.read_text().splitlines() == hardware.records()
    records.write_text("stale")
    args.generate = False
    with pytest.raises(ValueError, match="stale"):
        await cli.execute(args)
    records.write_text("\n".join(hardware.records()) + "\n")
    pinmap.with_name("pinout.md").unlink()
    with pytest.raises(ValueError, match="missing"):
        await cli.execute(args)


def test_firmware_verifies_both_hashes_and_exclusive_extract(tmp_path, monkeypatch):
    import hashlib

    archive = tmp_path / "firmware.zip"
    uf2 = b"a verified test fixture, not actual firmware"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("expected.uf2", uf2)
    manifest = {
        "version": "fixture",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "uf2_sha256": hashlib.sha256(uf2).hexdigest(),
        "uf2_member": "expected.uf2",
    }
    monkeypatch.setattr(cli, "firmware_manifest", lambda: manifest)
    target = tmp_path / "verified.uf2"
    assert cli.verify_firmware(archive, target)["uf2_verified"]
    assert target.read_bytes() == uf2
    with pytest.raises(FileExistsError):
        cli.verify_firmware(archive, target)
    manifest["uf2_sha256"] = "bad"
    with pytest.raises(ValueError, match="UF2"):
        cli.verify_firmware(archive, None)
    manifest["archive_sha256"] = "bad"
    with pytest.raises(ValueError, match="archive checksum"):
        cli.verify_firmware(archive, None)


async def test_record_and_replay_are_transport_free(tmp_path):
    hardware = load_hardware()
    path = tmp_path / "recording.jsonl"
    runtime = Runtime(hardware, load_profiles(), load_settings(), "demo", record_path=path)
    await runtime.launch()
    import asyncio

    await asyncio.sleep(0.12)
    await runtime.close()
    result = replay(path)
    assert result["profile"] == "c90b"
    assert result["transport_actions"] == 0
    assert result["feedback_panel"]["rows"][1] == "ALT 05500 VS +0700".ljust(20)
    assert result["recorded_panel"]["lamps"] == 0  # graceful shutdown
    assert result["kinds"]["output"] >= 1


@pytest.mark.parametrize("content", ["", "{}\n", '{"kind":"unknown"}\n'])
def test_replay_rejects_invalid_recordings(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(ValueError):
        replay(path)


def test_replay_rejects_hash_mismatch(tmp_path):
    hardware = load_hardware()
    data = {
        "time": 1,
        "monotonic": 1,
        "kind": "metadata",
        "data": {
            "schema_version": 1,
            "hardware": hardware.model_dump(),
            "configuration_hash": "bad",
        },
    }
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(data) + "\n")
    with pytest.raises(ValueError, match="hash"):
        replay(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "3.1.3"),
        ("schema_version", 2),
        ("archive_sha256", "bad"),
        ("release_url", "https://example.com"),
        ("unknown", 1),
    ],
)
def test_firmware_manifest_is_strict_and_pinned(field, value):
    data = firmware_manifest()
    data[field] = value
    with pytest.raises(ValueError):
        FirmwareManifest.model_validate(data)
