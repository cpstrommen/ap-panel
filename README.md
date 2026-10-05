# X-Plane Autopilot / IFR Panel

A native macOS bridge for a Raspberry Pi Pico 2 running stock MobiFlight firmware. It connects ten buttons, six encoders, gear control, 19 annunciators, and a 20 × 4 LCD to X-Plane 12 through USB serial and X-Plane's built-in Web API v2.

The host runs directly on macOS. MobiFlight provides the Pico firmware; this project's Python application supplies the host connection, aircraft mappings, feedback rendering, and local diagnostic dashboard. No custom Pico firmware or custom X-Plane plugin is required.

**Qualification status:** the software is implemented and tested with fake serial/simulator endpoints. The actual breadboard and installed aircraft remain uncommissioned. The shipped pin map has `bench_verified: false`; profiles have `simulator_tested: false`. Normal Start is gated on those recorded checks. Observe mode and explicit commissioning mode support completing them without presenting an untested candidate as qualified.

## Architecture and authoritative state

```text
Physical controls → Pico 2 / MobiFlight 3.1.4 → USB serial
                                                  ↓
                                         Native Python bridge
                                                  ↓
                                  X-Plane 12 Web API v2 commands
                                                  ↓
                                       Actual simulator feedback
                                                  ↓
                                      Lamps / LCD / local dashboard
```

Physical inputs request commands or supported DataRef writes. Every flight-state lamp and display value comes back from X-Plane. A button press does not turn its lamp on locally; encoder counts do not become authoritative selected values.

The application verifies the selected board and stored configuration, snapshots input positions, identifies the aircraft, resolves named commands/DataRefs to current API IDs, subscribes to feedback, and then qualifies input routing. Connection generations prevent old IDs, callbacks, or queued actions from leaking into a new session. Flight actions are never replayed after an uncertain acknowledgement or reconnection.

The dashboard controls the host session and diagnostics. Flight controls remain physical.

## Requirements and installation

| Component | Requirement |
|---|---|
| Host | macOS, Python 3.12 or newer |
| Python environment | [uv](https://docs.astral.sh/uv/) recommended |
| Simulator | X-Plane 12.1.4 or newer, Web API **v2** available |
| Simulator endpoint | Default `http://127.0.0.1:8086` |
| Controller | Raspberry Pi Pico 2, stock MobiFlight **3.1.4** |
| Board identity | `MobiFlight RaspiPico2`, USB VID `2E8A` / PID `F00A` |
| Inputs / outputs | Two 74HC165s, three 74HC595s, three ULN2803s |
| Display | 20 × 4 HD44780-compatible I²C LCD with level-shifted SDA/SCL |
| Dashboard | Default `http://127.0.0.1:8787`, loopback only |

From the repository directory:

```bash
uv sync --group dev
uv run ap-panel --help
uv run ap-panel validate --generate
uv run ap-panel run --demo
```

Install `uv` first if it is absent (`brew install uv` on a Homebrew Mac), or follow the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).

Open [the local dashboard](http://127.0.0.1:8787). Demo mode shows offline sample feedback and connects to neither board nor simulator. HTML, CSS, and JavaScript are served directly; there is no frontend build requirement.

A conventional virtual environment with `pip install -e .` also provides the `ap-panel` entry point. Dependencies are aiohttp, pyserial, Pydantic, and PyYAML. Packaged installations include dashboard assets and default configuration resources.

## Firmware installation on macOS

[`mobiflight/firmware-manifest.json`](mobiflight/firmware-manifest.json) pins the official release, archive/member hashes, board type, and serial settings. Download the [official firmware 3.1.4 archive](https://github.com/MobiFlight/MobiFlight-FirmwareSource/releases/tag/3.1.4), then verify it:

```bash
uv run ap-panel firmware verify --archive /path/to/Mobiflight-Firmware_3.1.4.zip --extract /path/to/mobiflight_raspberrypico2_3_1_4.uf2
```

The Pico 2 image is `Mobiflight-Firmware/firmware/mobiflight_raspberrypico2_3_1_4.uf2`.

`--extract` names a new output file in an existing directory; it refuses to overwrite an existing file.

| Artifact | SHA-256 |
|---|---|
| Official ZIP | `188bd74f04727f21ad42f656743accdab34c7865e4111c4d894ca49729249e2c` |
| Pico 2 UF2 | `35174962d275404626b1cbb4afd5fc5e138e3c355dd71799b3e8d6c4b77d056e` |

Hold BOOTSEL while connecting the Pico 2's USB cable. Its `RP2350` volume contains `INFO_UF2.TXT`. Copy the verified Pico 2 UF2 to that volume; it disconnects as the board starts firmware. Discover the resulting serial device and check its reported type/version. [Raspberry Pi documents BOOTSEL and Pico programming](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html).

Flashing installs firmware. Provisioning separately defines the six encoders, input chain, output chain, and LCD in the board's persistent configuration.

## Configuration and CLI workflow

[`electronics/pinmap.yaml`](electronics/pinmap.yaml) is the canonical hardware mapping. Profiles in [`profiles/`](profiles/) define aircraft commands, capabilities, feedback interpretation, calibration, and qualification. `validate --generate` validates them and generates `electronics/pinout.md` and `mobiflight/devices.txt`.

An optional `settings.local.yaml` holds host preferences:

```yaml
schema_version: 1
serial_number: "SN-YOUR_BOARD_ID"
port: null
simulator_url: "http://127.0.0.1:8086"
dashboard_port: 8787
log_directory: ".ap-panel"
log_level: "INFO"
log_max_bytes: 2000000
log_backup_count: 3
```

Select the MobiFlight-reported board serial rather than depending on a changing `/dev/cu.usbmodem...` filename. Pico identities contain the unique board ID and can be longer than legacy AVR serial strings. An explicit port is useful for discovery/diagnosis.

Logs default to `.ap-panel/bridge.log` at INFO, rotating at 2,000,000 bytes with three backups. These settings control rotation and verbosity; session recordings are separate explicitly selected files.

```bash
uv run ap-panel devices
uv run ap-panel doctor
uv run ap-panel configure --serial SN-YOUR_BOARD_ID --dry-run
uv run ap-panel configure --serial SN-YOUR_BOARD_ID --backup backups/pico-before-config.json
uv run ap-panel bench --serial SN-YOUR_BOARD_ID
uv run ap-panel run --serial SN-YOUR_BOARD_ID --observe
uv run ap-panel run --serial SN-YOUR_BOARD_ID --commission --record recordings/commission.jsonl
uv run ap-panel run --serial SN-YOUR_BOARD_ID
uv run ap-panel replay recordings/commission.jsonl
```

`devices` lists eligible Pico 2 ports without opening them; `devices --all` includes other serial ports for diagnosis.

`configure` saves the existing configuration before resetting/uploading. It checks cumulative append acknowledgements, saves, activates once, and reads back the complete configuration. An append that times out is not blindly retried. Normal connections verify persisted configuration and leave a matching configuration intact.

`bench` and `run` support `--port`, `--serial`, `--dashboard-port`, `--simulator-url`, `--pinmap`, `--profiles-dir`, `--settings`, and `--no-browser`. The `run` alternatives `--observe`, `--commission`, and `--demo` are mutually exclusive. `--record PATH` writes a session recording; `replay` produces an offline summary and sends no physical or simulator commands. `--no-browser` leaves browser opening to you.

The LCD address defaults to `0x27` / decimal `39`. A backpack at `0x3F` needs pin-map `address: 63` and reprovisioning. The stock protocol has no I²C scan command; verify the address on the actual display.

## Modes and readiness

| Mode | Physical flight commands | Outputs |
|---|---|---|
| Normal run | Enabled after compatibility, freshness, and qualification checks | Actual simulator feedback |
| Observe | Disabled | Actual simulator feedback |
| Commission | Explicitly permits candidate testing | Actual simulator feedback; prominent unverified banner |
| Bench | Disabled | Bounded pulses on any of 24 channels, walk of 19 assigned lamps, LCD text |
| Demo | Disabled | Offline sample feedback |

Normal Start requires `bench_verified: true`, a profile with `simulator_tested: true`, its exact `tested_aircraft_path`, matching live aircraft identity, a verified board configuration, and current required feedback. Completing a unit test or finding a mapping in a reference catalog does not satisfy these physical checks.

The dashboard shows board identity/configuration, simulator health/aircraft, compatible profiles, readiness, capability reasons, all inputs, signed raw encoder event totals, all 24 outputs, four LCD rows, telemetry validity, and events. Raw event totals support decoder calibration and never substitute for flight values. Profile selection and Start/Stop manage the bridge session; bench controls appear only in bench mode. Closing the browser leaves the application running.

See [commissioning](documentation/commissioning.md) for the exact progression, [verification](documentation/verification.md) for software evidence versus pending physical acceptance, and [troubleshooting](documentation/troubleshooting.md) for symptom-based checks.

## Aircraft behavior

Initial targets are the default **Laminar C172 analog** (`c172-analog`) and **King Air C90B** (`c90b`). A G1000 C172, another King Air, or an add-on aircraft needs its own qualified profile. Commands/DataRefs are named in YAML and resolved for the current simulator connection; numeric API IDs are not persisted as mappings. [Laminar documents the Web API](https://developer.x-plane.com/article/x-plane-web-api/).

### Buttons

| Control | Intended action |
|---|---|
| AP | Autopilot engagement/disengagement |
| FD | Independent flight director control where supported |
| HDG / NAV / APR | Heading, navigation, approach mode selection |
| ALT SEL | Selected-VS/altitude-capture sequence where supported |
| ALT | Hold current altitude |
| VS | Vertical-speed mode |
| YD | Yaw-damper selection where supported |
| CDI | Pilot GPS/VLOC source change |

**ALT SEL and ALT remain separate.** The C90B ALT SEL sequence uses selected vertical speed and arms altitude capture; ALT requests current-altitude hold. Lamps reflect the resulting simulator modes.

The analog C172's installed S-Tec profile disables ALT SEL, selected-altitude rotation/sync, independent FD, YD, and retractable-gear control. Disabled functions remain visible with a reason and do not fall through to guessed generic commands.

### Six encoders and all five pushes

| Encoder | Clockwise / counterclockwise | Push |
|---|---|---|
| HDG | Heading bug increase / decrease | Sync to actual heading |
| ALT | Selected altitude increase / decrease where supported | Sync to actual altitude where supported |
| VS | Selected vertical speed increase / decrease | Set selected VS to zero |
| CRS | Pilot OBS/course increase / decrease | Course sync only with valid supported source/course |
| BARO | Pilot barometer increase / decrease | 29.92 inHg |
| FLAPS | Down / up one aircraft detent | Unused |

All five connected pushes have defined behavior subject to aircraft capability and valid feedback. Decoder type and inversion are qualified on the purchased EC11s. Regular and fast events each request one native increment in v1. Expected VS increments are 100 fpm for the C172 and 200 fpm for the C90B; confirm them during commissioning. Turn-rate handling does not substitute local values for simulator feedback or replay a stale backlog.

Gear **open = UP**, **closed = DOWN**. Startup and reconnect snapshots record switch position without commanding the simulator to match it. After checking actual gear state, operate the toggle deliberately. L/N/R lamps use individually calibrated gear deployment feedback, not the handle command. With deployment feedback, a lamp means ratio ≥0.99; it does not establish a mechanical down-and-locked sensor.

Flap lamps use actual deployment ratios at calibrated aircraft detents. The C90B has three positions: UP / 1 / FULL; physical Flaps 2 remains unused. The shipped profiles leave gear indices and flap ratios unmeasured and those indicators disabled. YD engagement annunciation also stays off until a genuine engagement DataRef is verified; switch selection alone is insufficient.

### Annunciators and LCD

AP/FD/mode lamps follow actual feedback. Armed modes blink at 1 Hz; active modes are steady. GPS/VLOC follow the pilot source, and an unknown source asserts neither. GS CAP follows actual glideslope capture, not simply APR or GS arming. Five spare outputs remain off during normal operation.

The CDI button's internal lamp has no flight-status assignment; GPS and VLOC use their separate indicators.

LCD frames contain four rows of exactly 20 printable ASCII characters. Heading/course use degrees, altitude feet, VS feet per minute, and barometer inches of mercury. Invalid/unavailable values show placeholders; overflow is marked rather than corrupting adjacent fields. The C172 has no altitude preselector, so the altitude field shows held altitude while ALT hold is active and is unavailable otherwise.

```text
HDG 274  CRS 143
ALT 05500 VS +0700
BARO 29.92 CDI GPS
ALTSEL ARM GS ARM
```

## Breadboard pin map

The supplied [breadboard schematic](ap-panel-schematic-breadboard.png) is the physical reference. Its key change is reversed 595→ULN wiring. Reversing the ULN channel arrangement and matching lamp connections preserves the logical Q0–Q7 lamp order.

### Pico GPIOs

| Function | GPIO | Pico physical pin |
|---|---|---|
| HDG A / B | GP0 / GP1 | 1 / 2 |
| ALT A / B | GP2 / GP3 | 4 / 5 |
| LCD SDA / SCL | GP4 / GP5 | 6 / 7 |
| VS A / B | GP6 / GP7 | 9 / 10 |
| CRS A / B | GP8 / GP9 | 11 / 12 |
| BARO A / B | GP10 / GP11 | 14 / 15 |
| FLAPS A / B | GP12 / GP13 | 16 / 17 |
| 165 load / clock / data | GP14 / GP15 / GP16 | 19 / 20 / 21 |
| 595 data / clock / latch | GP17 / GP18 / GP19 | 22 / 24 / 25 |

Encoders use grounded commons and direct Pico inputs. Switch inputs use 10 kΩ pull-ups to 3.3 V and close to GND: released/open is HIGH, pressed/closed LOW.

### Input-chain indices

| Index | Control | Chip / input / physical pin | Connector |
|---:|---|---|---|
| 0 | AP | IC1 A / 11 | JP8 |
| 1 | FD | IC1 B / 12 | JP9 |
| 2 | HDG | IC1 C / 13 | JP10 |
| 3 | NAV | IC1 D / 14 | JP11 |
| 4 | APR | IC1 E / 3 | JP12 |
| 5 | ALT SEL | IC1 F / 4 | JP13 |
| 6 | ALT | IC1 G / 5 | JP14 |
| 7 | VS | IC1 H / 6 | JP15 |
| 8 | YD | IC2 A / 11 | JP16 |
| 9 | CDI | IC2 B / 12 | JP17 |
| 10 | HDG push | IC2 C / 13 | JP18 |
| 11 | ALT push | IC2 D / 14 | JP19 |
| 12 | VS push | IC2 E / 3 | JP20 |
| 13 | CRS push | IC2 F / 4 | JP21 |
| 14 | BARO push | IC2 G / 5 | JP22 |
| 15 | Gear | IC2 H / 6 | JP23 |

IC1 is nearest GP16. IC2 QH pin 9 feeds IC1 SER pin 10; IC1 QH pin 9 feeds GP16; IC2 SER pin 10 is grounded. Firmware indices are IC1 A–H followed by IC2 A–H.

The raster appears to connect each switch's pin 2 to both GND and its 165 input, while pin 1 reaches its pull-up. As drawn that would hold inputs low. Check actual breadboard continuity: the input and pull-up must share one signal contact; only the other contact is grounded. The image alone does not establish whether the physical breadboard has this error.

### Exact reversed 595→ULN wiring

This applies to U2→IC3, U3→IC4, and U4→IC5:

| 595 output | 595 package pin | ULN input / package pin | ULN output package pin |
|---|---:|---|---:|
| Q0 / QA | 15 | IN8 / 8 | 11 |
| Q1 / QB | 1 | IN7 / 7 | 12 |
| Q2 / QC | 2 | IN6 / 6 | 13 |
| Q3 / QD | 3 | IN5 / 5 | 14 |
| Q4 / QE | 4 | IN4 / 4 | 15 |
| Q5 / QF | 5 | IN3 / 3 | 16 |
| Q6 / QG | 6 | IN2 / 2 | 17 |
| Q7 / QH | 7 | IN1 / 1 | 18 |

There is **no polarity inversion**: HIGH activates the ULN sink and lights its lamp. Every lamp connector uses pin 1 for +5 V and pin 2 for the sink. ULN pin 9 is GND; pin 10 is the flyback common, not VCC.

| Index | Output | Lamp | Connector |
|---:|---|---|---|
| 0 | U2 Q0 | AP | JP31 |
| 1 | U2 Q1 | FD | JP30 |
| 2 | U2 Q2 | HDG | JP29 |
| 3 | U2 Q3 | NAV | JP28 |
| 4 | U2 Q4 | APR | JP27 |
| 5 | U2 Q5 | ALT SEL | JP26 |
| 6 | U2 Q6 | ALT | JP25 |
| 7 | U2 Q7 | VS | JP24 |
| 8 | U3 Q0 | YD | JP39 |
| 9 | U3 Q1 | GPS | JP38 |
| 10 | U3 Q2 | VLOC | JP37 |
| 11 | U3 Q3 | GS CAP | JP36 |
| 12 | U3 Q4 | Gear L | JP35 |
| 13 | U3 Q5 | Gear N | JP34 |
| 14 | U3 Q6 | Gear R | JP33 |
| 15 | U3 Q7 | Flaps UP | JP32 |
| 16 | U4 Q0 | Flaps 1 | JP47 |
| 17 | U4 Q1 | Flaps 2 | JP46 |
| 18 | U4 Q2 | Flaps FULL | JP45 |
| 19 | U4 Q3 | Spare 1 | JP44 |
| 20 | U4 Q4 | Spare 2 | JP43 |
| 21 | U4 Q5 | Spare 3 | JP42 |
| 22 | U4 Q6 | Spare 4 | JP41 |
| 23 | U4 Q7 | Spare 5 | JP40 |

GP17 feeds U2 SER; U2 Q7S pin 9 feeds U3 SER, then U3 Q7S feeds U4 SER. The logical 24-bit word keeps AP at bit 0. Verify physical chip markings: the drawing uses `74165N` and `RPI_PICO` symbols, while the actual parts must be 74HC165s at 3.3 V and a Pico 2. Hidden power pins/omitted decoupling are not verified by the raster.

## Native serial protocol

The implementation pins [MobiFlight firmware 3.1.4](https://github.com/MobiFlight/MobiFlight-FirmwareSource/tree/3.1.4/src) and [CmdMessenger 4.2.2](https://github.com/MobiFlight/Arduino-CmdMessenger/tree/4.2.2). Serial uses 115200 baud, DTR, and the board's startup delay.

| Command | Role |
|---:|---|
| 9 → 10 | Get board type/name/serial/firmware/core version |
| 12 → 10 | Read complete configuration; also enables input reporting |
| 13 → 5 | Reset before explicit provisioning |
| 11 → 5 | Append a record; acknowledgement is cumulative length |
| 14 → 15 | Commit configuration |
| 16 → 17 | Activate newly provisioned configuration once |
| 23 → 29 / 34 | Retrigger switch states and snapshot completion |
| 6 / 29 | Encoder / discrete-input events |
| 27 | Set/clear output-chain masks |
| 25 | Write all 80 characters to registered LCD index 0 |
| 18,0 | Periodic stock power-saving keepalive |

Complete host commands fit within **95 bytes including `;`**, accounting for escaping. The firmware's receive buffer is 96 bytes; host configuration replies need a separate, larger receive bound. An unescaped 80-character LCD command is 86 bytes. Command 27 mask order is far / middle / near; the bridge translates the logical output word.

Command 23 separately polls released and pressed switches; it is not atomic. The bridge checks for all 16 unique indices and does not route snapshots as flight actions. Firmware already loads stored configuration at boot; reconnect does not repeatedly issue ActivateConfig.

## Faults, startup, and accepted limits

Simulator loss, board loss, incompatible aircraft, stale/invalid feedback, and failed acknowledgements block qualified input routing. Reconnect rebuilds identity, configuration, snapshot, aircraft mapping, IDs, and subscriptions. Outputs clear when the board connection still permits it; unavailable values do not continue to assert fresh flight state.

Stock power saving is a **15-minute fallback**, not a short watchdog. Ordinary LCD/output traffic does not refresh its timer, so the application sends `18,0;` periodically. A hard process crash or lost cable can leave the last latched lamps visible until that fallback. This accepted limitation is included in commissioning.

With 595 `/OE` tied to GND, outputs before firmware initialization are undefined, including BOOTSEL time. Firmware clears configured outputs on boot, and the host clears them during initialization/stop. Those software steps cannot guarantee off lamps before MCU initialization.

## Commissioning and development

Complete [the hardware/aircraft checklist](documentation/commissioning.md), record measurements, set `bench_verified`, calibrate gear/flap/YD feedback, test with `run --commission`, and then record `simulator_tested` plus the exact `tested_aircraft_path`. Shipped defaults do not claim measured encoder characteristics, gear indices, flap ratios, or successful installed-simulator integration.

Binding metadata separates authoritative-reference verification from individual installed-aircraft testing; the top-level profile records aggregate qualification. Real acceptance includes a continuous recorded session longer than one hour, measured local feedback against a 250 ms target, measured simulator-loss response against a 2.5-second target, and reproduction of the accepted hard-crash fallback. These physical timing/endurance measurements remain pending, as does browser visual QA; actual local demo HTTP/WebSocket service checks are separate evidence.

```bash
uv run pytest
uv run ruff check .
uv run python -m build
```

Automated checks cover protocol boundaries, fake serial configuration/identity/snapshots, fake simulator resolution/acknowledgements/reconnection, input and capability rules, feedback rendering, and dashboard HTTP/WebSocket validation. They do not test the physical wiring or actual aircraft. Session recordings and offline replay summaries help diagnose behavior; they are not substitutes for physical qualification.

The dashboard validates loopback Host/port, rejects cross-origin POST/WebSocket requests, validates bounded JSON actions, and exposes no CORS policy. Lamp tests are bounded and available only in bench mode.

| Repository area | Purpose |
|---|---|
| `src/ap_panel/` | Configuration, transports, profiles, inputs/actions, outputs, runtime, CLI, dashboard |
| `electronics/pinmap.yaml` | Canonical physical mapping and hardware qualification |
| `profiles/` | Aircraft candidates, calibration, qualification |
| `mobiflight/firmware-manifest.json` | Official firmware artifact pinning |
| `web/` | Local diagnostic UI |
| `documentation/` | Commissioning and operational diagnosis |
| `tests/` | Hardware-independent verification |

CAD/faceplate manufacture, enclosure construction, and additional aircraft/control expansion remain outside this software implementation.

Primary references: [Laminar Web API](https://developer.x-plane.com/article/x-plane-web-api/), [DataRefs](https://developer.x-plane.com/datarefs/), [autopilot interfaces](https://developer.x-plane.com/article/accessing-the-x-plane-autopilot-from-datarefs/), [MobiFlight configuration](https://github.com/MobiFlight/MobiFlight-FirmwareSource/blob/3.1.4/src/Config.cpp), [output shifter](https://github.com/MobiFlight/MobiFlight-FirmwareSource/blob/3.1.4/src/MF_OutputShifter/OutputShifter.cpp), and [Pico documentation](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html).
