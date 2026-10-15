# Troubleshooting

Begin with board identity/configuration, loaded aircraft, capability reasons, telemetry validity, and recent events in the dashboard. Record repeatable behavior with `run --record PATH`; `replay PATH` gives an offline diagnostic summary. Use [commissioning.md](commissioning.md) to qualify a correction.

Application logs default to `.ap-panel/bridge.log`, rotating at 2,000,000 bytes with three backups. `settings.local.yaml` controls `log_directory`, `log_level`, `log_max_bytes`, and `log_backup_count`. `doctor` reads identity/configuration and simulator capabilities; it does not drive physical outputs.

## Application or dashboard does not start

Run `uv sync --no-editable --group dev`, `uv run --no-editable ap-panel --help`, and `uv run --no-editable ap-panel doctor`. Python 3.12 or newer is required. `run --demo` separates installation/browser checks from board/simulator problems.

The dashboard uses `http://127.0.0.1:8787` by default. Stop another instance if the port is occupied, or choose `--dashboard-port`. Use its configured loopback host/port directly. Arbitrary Host headers and cross-origin POST/WebSocket requests are rejected; this service is not configured for public URLs or reverse proxies.

Closing the browser does not stop the application. Dashboard Stop clears outputs and disables dispatch while keeping the server open. **Ctrl-C** in the foreground terminal shuts down the application and releases its port.

## Import fails after a successful launch

If `uv run ap-panel ...` raises `ModuleNotFoundError: No module named 'ap_panel.cli'`, an editable install may have lost its source path. On macOS, Python skips a `.pth` file with the filesystem hidden flag. Dashboard/configuration assets can still exist under `site-packages/ap_panel`, so the package appears present even though its Python modules are missing.

Use regular installation consistently:

```bash
uv sync --no-editable
uv run --no-editable ap-panel run --demo
```

Keep `--no-editable` on subsequent uv commands; a bare `uv run` can switch the project back to editable installation. Alternatively, `export UV_NO_EDITABLE=1` in the current terminal makes that mode apply to subsequent uv commands. The project's cache keys track source/profile/calibration/asset changes so regular installations refresh after edits.

The environment recreation message when uv selects a different Python version is normal. Both Python 3.12 and 3.14 can run this project; choosing 3.14 was not the cause of the hidden path-file failure.

## Terminal says suspended or refuses to exit

**Ctrl-Z** suspends the process rather than closing it. A suspended server cannot answer requests, but still owns its listening port. In the terminal that launched it, run `jobs -l`, use `fg` (or `fg %N` for the matching job number), then press **Ctrl-C**. Do this before launching another instance. There is no need to restart the Mac or delete the project environment.

## No Pico appears

Check a USB data cable and the macOS `/dev/cu.usbmodem...` device. Close other serial applications so one host owns the port. Run `ap-panel devices` and choose the intended board serial when several are present.

Runtime firmware should expose VID `2E8A`, PID `F00A`. A visible `RP2350` storage volume means BOOTSEL mode. Copy the verified Pico 2 UF2 and allow it to reboot into serial firmware. The reported identity must be `MobiFlight RaspiPico2`, version `3.1.4`.

Pico serial identities can be longer than the AVR format. Use the MobiFlight GetInfo identity rather than assuming a stable macOS device filename.

## Firmware or configuration mismatch

Check `mobiflight/firmware-manifest.json` and verify the downloaded artifact with `firmware verify --archive PATH`. The application expects the pinned firmware protocol.

Run `validate --generate` and `configure --dry-run` to inspect canonical records. Provision explicitly with `configure --serial SN --backup PATH`, then verify complete readback. Review the saved backup after a failed upload. A timed-out append is not automatically retried because it might already have been applied.

Normal reconnect reads stored configuration; repeatedly sending ActivateConfig is not a recovery strategy. Firmware already activates stored configuration at boot.

## Inputs are absent or all pressed

GetConfig (`12`) enables input reporting; the bridge performs this automatically. GetInfo alone is insufficient.

If all 165 inputs are low, inspect the switch/pull-up wiring. The supplied raster appears to place input signals on grounded switch contacts. On the real board, one contact must join the input and 10 kΩ pull-up to 3.3 V; only the other contact is grounded.

Check IC1 QH pin 9→GP16, IC2 QH pin 9→IC1 SER pin 10, grounded IC2 SER, GP14 load, GP15 clock, inhibited-clock grounding, and chip power. Complementary QH pin 7 is not the chain output.

If a snapshot is incomplete, keep controls still and reconnect after inspecting the error. Separate release/press polls can miss a transition made during retrigger. Missing snapshot state must not become a gear command.

## Encoder direction/count is wrong

Use bench mode to inspect single slow detents. Check A/B and grounded common, then set host `inverted` if direction is reversed. Determine `decoder_type` experimentally; changing it requires reprovisioning.

Test ten detents in both directions, reversal, and fast turns. Decoder type and aircraft command step size are separate issues. Push switches are separate 165 inputs; cross-coupled events warrant connector/wiring checks.

## A physical control reports events but does not operate X-Plane

Check mode and Start status. Bench/observe/demo block flight commands. Normal Start requires hardware and exact-aircraft qualification; `run --commission` deliberately permits candidate testing with a prominent banner.

Read the specific capability reason. The analog C172 disables selected-altitude control/ALT SEL, independent FD, YD, and retractable gear. A G1000 C172 or add-on King Air is not the shipped aircraft candidate. Check exact aircraft path and profile compatibility.

Missing/denied commands and writable DataRefs remain unavailable; they are not replaced with guessed actions. A failed acknowledgement does not establish that a requested state changed.

## Simulator disconnected or API commands unavailable

X-Plane must offer Web API v2. Check its version/capabilities and local endpoint, normally `http://127.0.0.1:8086`. `--simulator-url` and local settings must describe the running local simulator origin.

After correcting an endpoint/version issue, reconnect so resource IDs and subscriptions rebuild. A v1-only connection cannot satisfy the command interface. Aircraft changes also require fresh compatibility/resolution.

## Wrong lamp lights

Bench-test one channel at a time. The wiring is Q0→ULN IN8, Q1→IN7, …, Q7→IN1; sink package pins are 11–18 respectively. Logical order remains AP at U2 Q0 through VS at U2 Q7; YD through FLAPS UP on U3; FLAPS 1/2/FULL on U4 Q0–Q2.

Connector pin 1 is +5 V and pin 2 the sink. HIGH means ON. Command 27 masks travel far/middle/near; reversed physical ULN channels do not reverse the logical lamp word. Compare the walking test with `electronics/pinmap.yaml`.

## Startup lamps or stale lamps after a crash

With 595 `/OE` grounded, output state before firmware initialization and during BOOTSEL is undefined. Firmware clears a configured chain on boot, and host initialization/Stop clears it. Software cannot inhibit outputs before firmware starts.

Stock power saving is a 15-minute fallback. A hard process crash or lost cable can leave last latched lamps visible until that timeout. This accepted limitation is separate from normal orderly Stop.

If a healthy session blanks at about 15 minutes, inspect the periodic `18,0;` keepalive. Ordinary LCD/output traffic does not refresh that firmware timer.

## LCD blank, garbled, or old text remains

Check 5 V power, GND, level-shifter LV/HV, SDA GP4, SCL GP5, physical contrast, and backpack address. `0x27` is decimal `39`; `0x3F` is `63`. Change the pin map and reprovision for a different address. There is no stock serial I²C scan API.

Command 25 targets registered LCD index `0`, not the backpack address. Frames replace all 80 printable ASCII characters; shorter bench text is padded. Escaping counts toward the 94-byte complete command bound.

Stock LCD initialization uses 400 kHz I²C; the native host cannot adjust that through this protocol. If wiring/address checks pass but a specific backpack fails, investigate hardware compatibility. USB baud is unrelated to the I²C clock.

A correct browser preview with incorrect physical text points toward the board/display path. Incorrect previews and physical text together point toward feedback/profile/rendering.

## Gear, flap, or YD lamps stay off

Shipped profiles intentionally leave calibration unverified. Gear needs measured, distinct L/N/R array indices; flaps need measured actual deployment ratios at installed detents; YD needs genuine engagement feedback. Review `gear_verified`, `flaps_verified`, and `yd_engaged_verified` plus their associated mappings.

Handle selection is not gear-down or YD-engaged confirmation. C90B supports three flap positions and leaves Flaps 2 unused. Fixed gear does not require a fabricated retractable-gear mapping. Follow the calibration procedure before enabling these indicators.

## Armed/active/GS lamps disagree with the cockpit

Observe actual mode feedback through arming, interception, capture, and cancellation on the exact aircraft. Armed should blink at 1 Hz, active should stay steady, and GS CAP should follow capture. YD selection and engagement must be distinguished.

Record aircraft path, simulator version, values, and sequence when changing profile interpretation. Repeat the affected commissioning scenarios afterward.
