"""Command-line installation, commissioning and bridge entry points."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import signal
import sys
import time
import webbrowser
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web
from yaml import YAMLError

from .board import BoardClient, list_devices
from .config import ROOT, Settings, firmware_manifest, load_hardware, load_settings
from .dashboard import create_app
from .profiles import load_profiles
from .replay import replay
from .runtime import Runtime
from .xplane import XPlaneClient


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ap-panel", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    devices = commands.add_parser("devices", help="List eligible USB boards without opening them")
    devices.add_argument("--all", action="store_true", help="Include ineligible serial ports")
    for name in ("doctor", "validate", "configure", "bench", "run"):
        command = commands.add_parser(name)
        command.add_argument("--pinmap", type=Path)
        command.add_argument("--profiles-dir", type=Path)
        command.add_argument("--settings", type=Path)
        command.add_argument("--port", help="Explicit serial device path")
        command.add_argument("--serial", help="Selected board serial number")
        command.add_argument("--simulator-url")
        command.add_argument("--dashboard-port", type=int)
        if name == "validate":
            command.add_argument(
                "--generate", action="store_true", help="Regenerate electronics/pinout.md"
            )
        elif name == "configure":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--backup", type=Path)
        elif name in {"run", "bench"}:
            command.add_argument("--no-browser", action="store_true")
            command.add_argument("--record", type=Path)
            if name == "run":
                mode = command.add_mutually_exclusive_group()
                mode.add_argument("--observe", action="store_true")
                mode.add_argument("--commission", action="store_true")
                mode.add_argument("--demo", action="store_true")
    offline = commands.add_parser(
        "replay", help="Reconstruct a recording without USB or simulator access"
    )
    offline.add_argument("recording", type=Path)
    firmware = commands.add_parser("firmware", help="Verify the pinned official firmware archive")
    operations = firmware.add_subparsers(dest="firmware_command", required=True)
    verify = operations.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--extract", type=Path, help="Write verified Pico 2 UF2 to this new file")
    return result


def configuration(args: argparse.Namespace):
    hardware, profiles = load_hardware(args.pinmap), load_profiles(args.profiles_dir)
    settings = load_settings(args.settings)
    data = settings.model_dump()
    for field, option in (
        ("port", "port"),
        ("serial_number", "serial"),
        ("simulator_url", "simulator_url"),
        ("dashboard_port", "dashboard_port"),
    ):
        if getattr(args, option) is not None:
            data[field] = getattr(args, option)
    return hardware, profiles, Settings.model_validate(data)


def verify_firmware(archive: Path, extract: Path | None) -> dict:
    manifest = firmware_manifest()
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_hash != manifest["archive_sha256"]:
        raise ValueError("Firmware archive checksum does not match the pinned official release")
    with zipfile.ZipFile(archive) as packed:
        try:
            data = packed.read(manifest["uf2_member"])
        except KeyError as exc:
            raise ValueError("Verified archive does not contain the expected Pico 2 image") from exc
    if hashlib.sha256(data).hexdigest() != manifest["uf2_sha256"]:
        raise ValueError("Pico 2 UF2 checksum does not match the manifest")
    if extract:
        with extract.open("xb") as output:
            output.write(data)
    return {
        "version": manifest["version"],
        "archive_verified": True,
        "uf2_verified": True,
        "extracted": str(extract) if extract else None,
    }


async def doctor(hardware, profiles, settings) -> tuple[dict, bool]:
    report = {
        "devices": list_devices(),
        "configuration_hash": hardware.configuration_hash,
        "board": {"checked": False},
        "simulator": {"checked": True},
    }
    okay = True
    if settings.port or settings.serial_number:
        board = BoardClient(
            hardware.raw_config, port=settings.port, serial_number=settings.serial_number
        )
        try:
            from dataclasses import asdict

            report["board"] = {
                "checked": True,
                "identity": asdict(await board.connect()),
                "configuration_ok": True,
            }
        except Exception as exc:
            report["board"] = {"checked": True, "error": str(exc)}
            okay = False
        finally:
            await board.close()
    else:
        report["board"]["reason"] = "Select --port or --serial to inspect a board"
    simulator = XPlaneClient(settings.simulator_url)
    try:
        report["simulator"]["capabilities"] = await simulator.connect()
        from .profiles import aircraft_path

        base = profiles[0]
        keys = ("aircraft", "ap_type", "paused")
        found = await simulator.resolve_datarefs({k: base.datarefs[k].path for k in keys})
        await simulator.subscribe(list(found))
        aircraft = aircraft_path(simulator.get("aircraft"))
        profile = next(
            (p for p in profiles if aircraft and p.compatible(aircraft, simulator.get("ap_type"))),
            None,
        )
        report["simulator"].update(
            {"aircraft": aircraft, "profile": profile.id if profile else None}
        )
        if profile:
            refs = await simulator.resolve_datarefs(
                {k: r.path for k, r in profile.datarefs.items()}
            )
            cmds = await simulator.resolve_commands(profile.command_paths())
            missing = [k for k, r in profile.datarefs.items() if r.required and k not in refs]
            wrong = [
                k
                for k in refs
                if not (
                    simulator.dataref_types[k] == profile.datarefs[k].value_type
                    or profile.datarefs[k].value_type == "numeric"
                    and simulator.dataref_types[k] in {"int", "float", "double"}
                )
            ]
            report["simulator"].update(
                {
                    "missing_required_datarefs": missing,
                    "wrong_types": wrong,
                    "missing_commands": sorted(set(profile.command_paths()) - set(cmds)),
                }
            )
            okay = okay and not missing and not wrong
        else:
            okay = False
    except Exception as exc:
        report["simulator"]["error"] = str(exc)
        okay = False
    finally:
        await simulator.close()
    return report, okay


async def serve(args, hardware, profiles, settings) -> None:
    mode = (
        "bench"
        if args.command == "bench"
        else next((m for m in ("observe", "commission", "demo") if getattr(args, m, False)), "run")
    )
    log_dir = Path(settings.log_directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "bridge.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("ap_panel").setLevel(settings.log_level)
    logging.getLogger("ap_panel").addHandler(handler)
    runtime = Runtime(hardware, profiles, settings, mode=mode, record_path=args.record)
    runner = web.AppRunner(create_app(runtime, port=settings.dashboard_port), access_log=None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", settings.dashboard_port).start()
        await runtime.launch()
        for signum in signals:
            loop.add_signal_handler(signum, stop.set)
        url = f"http://127.0.0.1:{settings.dashboard_port}"
        print(
            f"ap-panel {mode}: {url}\nPress Ctrl-C to clear reachable outputs and exit.", flush=True
        )
        if not args.no_browser:
            webbrowser.open(url)
        await stop.wait()
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)
        await runtime.close()
        await runner.cleanup()
        logging.getLogger("ap_panel").removeHandler(handler)
        handler.close()


async def execute(args) -> int:
    if args.command == "devices":
        devices = list_devices()
        print(
            json.dumps(
                devices if args.all else [d for d in devices if d["mobiflight_pico2"]], indent=2
            )
        )
        return 0
    if args.command == "firmware":
        print(json.dumps(verify_firmware(args.archive, args.extract), indent=2))
        return 0
    if args.command == "replay":
        print(json.dumps(replay(args.recording), indent=2))
        return 0
    hardware, profiles, settings = configuration(args)
    if args.command == "validate":
        firmware_manifest()
        project = ROOT if (ROOT / "electronics/pinmap.yaml").is_file() else Path.cwd()
        target = (
            args.pinmap.with_name("pinout.md") if args.pinmap else project / "electronics/pinout.md"
        )
        records_path = target.parent.parent / "mobiflight/devices.txt"
        records_text = "\n".join(hardware.records()) + "\n"
        if not args.generate and (args.pinmap or project == ROOT):
            for artifact in (target, records_path):
                if not artifact.is_file():
                    raise ValueError(
                        f"Generated artifact is missing: {artifact}; run validate --generate"
                    )
        if args.generate:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(hardware.pinout_markdown())
            records_path.parent.mkdir(parents=True, exist_ok=True)
            records_path.write_text(records_text)
        elif target.is_file() and target.read_text() != hardware.pinout_markdown():
            raise ValueError("Generated pinout is stale; run validate --generate")
        if records_path.is_file() and records_path.read_text() != records_text:
            raise ValueError("Generated firmware device records are stale; run validate --generate")
        print(
            json.dumps(
                {
                    "valid": True,
                    "configuration_hash": hardware.configuration_hash,
                    "profiles": [p.id for p in profiles],
                    "hardware_verified": hardware.bench_verified,
                    "simulator_tested_profiles": [p.id for p in profiles if p.simulator_tested],
                },
                indent=2,
            )
        )
    elif args.command == "configure":
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "records": hardware.records(),
                        "configuration_hash": hardware.configuration_hash,
                    },
                    indent=2,
                )
            )
        else:
            backup = args.backup or Path(f".ap-panel/config-backup-{time.time_ns()}.json")
            backup.parent.mkdir(parents=True, exist_ok=True)
            board = BoardClient(
                hardware.raw_config, port=settings.port, serial_number=settings.serial_number
            )
            try:
                print(json.dumps(await board.provision(backup), indent=2))
            finally:
                await board.close()
    elif args.command == "doctor":
        report, okay = await doctor(hardware, profiles, settings)
        print(json.dumps(report, indent=2))
        return 0 if okay else 1
    else:
        await serve(args, hardware, profiles, settings)
    return 0


def main() -> None:
    try:
        code = asyncio.run(execute(parser().parse_args()))
    except (ValueError, OSError, RuntimeError, YAMLError, zipfile.BadZipFile) as exc:
        print(f"ap-panel: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
