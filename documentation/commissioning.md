# Panel commissioning

Qualify the real panel and the exact installed aircraft separately. Automated tests use fake endpoints; they do not mark either qualification complete. Keep the board backup, configuration hash, recordings, measured calibration, and test notes together.

See [verification.md](verification.md) for software evidence versus pending physical acceptance, timing targets, and the required actual soak longer than one hour.

## 1. Prepare and provision

```bash
uv run ap-panel validate --generate
uv run ap-panel devices
uv run ap-panel doctor
uv run ap-panel configure --serial SN-YOUR_BOARD_ID --dry-run
uv run ap-panel configure --serial SN-YOUR_BOARD_ID --backup backups/before-commission.json
uv run ap-panel bench --serial SN-YOUR_BOARD_ID
```

- [ ] Confirm the actual controller is a Pico 2 and GetInfo reports `MobiFlight RaspiPico2`, firmware `3.1.4`.
- [ ] Confirm physical chip markings are 74HC165, rather than relying on the raster's `74165N` label.
- [ ] Confirm shared GND, 3.3 V logic, 5 V lamp/LCD supply, level-shifted LCD SDA/SCL, and decoupling on all five HC chips.
- [ ] Check each 165 input shares one node with its 10 kΩ pull-up and the switch signal contact. Only the other contact is grounded.
- [ ] Check the schematic's apparent input/GND connection against actual breadboard continuity.
- [ ] Check all three output pairs use Q0→IN8, Q1→IN7, …, Q7→IN1.
- [ ] Review the generated device records before provisioning.
- [ ] Verify the backup exists and the complete board configuration reads back correctly.

The LCD defaults to `0x27` / decimal `39`. A measured `0x3F` backpack requires pin-map address `63`, reprovisioning, and repeated LCD tests. The bridge cannot scan the I²C bus through the stock protocol.

## 2. Check all 16 discrete inputs

Bench mode disables simulator flight commands. Inspect dashboard input states/events while physically operating each control.

| Indices | Controls |
|---|---|
| 0–7 | AP, FD, HDG, NAV, APR, ALT SEL, ALT, VS |
| 8–9 | YD, CDI |
| 10–14 | HDG, ALT, VS, CRS, BARO encoder pushes |
| 15 | Gear toggle |

- [ ] Startup provides all 16 unique input indices.
- [ ] Each momentary input is released/HIGH normally and pressed/LOW when operated.
- [ ] Press/release changes only the intended input and returns to the baseline afterward.
- [ ] Contact bounce does not produce duplicate qualified actions.
- [ ] Gear OPEN reports UP; CLOSED reports DOWN.
- [ ] Startup/reconnect snapshots report held controls without executing flight actions.
- [ ] Moving a switch during retrigger produces a complete result or a handled retry, rather than a fabricated position.

Retrigger performs separate release and press polls, so it is not an atomic snapshot. Firmware indices are IC1 A–H (0–7), then IC2 A–H (8–15).

## 3. Qualify encoder decoder type and direction

| Encoder | A / B | Push index |
|---|---|---:|
| HDG | GP0 / GP1 | 10 |
| ALT | GP2 / GP3 | 11 |
| VS | GP6 / GP7 | 12 |
| CRS | GP8 / GP9 | 13 |
| BARO | GP10 / GP11 | 14 |
| FLAPS | GP12 / GP13 | Unused |

For every encoder:

- [ ] Turn one detent clockwise and counterclockwise; record decoded direction.
- [ ] Turn ten slow detents in both directions; verify count and reversal behavior.
- [ ] Compare the dashboard's signed raw firmware event total before/after 20 detents in one direction; interpret counts as diagnostic events, not selected flight values.
- [ ] Turn quickly in both directions and inspect regular/fast event handling.
- [ ] Confirm adjacent encoders and push switches remain independent.
- [ ] Set the measured `decoder_type` and host `inverted` value in the pin map.
- [ ] Reprovision after a decoder-type change, then repeat the checks.

Decoder type belongs to the firmware's device configuration. Inversion changes host interpretation. Neither should be recorded as measured from the generic EC11 product name alone.

## 4. Qualify lamps and LCD physically

Use the dashboard's bounded lamp pulse and walking test. The walk covers 19 assigned lamps; test the five spare channels individually if connected. Browser previews alone cannot verify physical outputs.

- [ ] Channels 0–7: AP, FD, HDG, NAV, APR, ALT SEL, ALT, VS.
- [ ] Channels 8–15: YD, GPS, VLOC, GS CAP, gear L/N/R, FLAPS UP.
- [ ] Channels 16–18: FLAPS 1, FLAPS 2, FLAPS FULL.
- [ ] Channels 19–23: all five spare outputs; record whether lamps are attached.
- [ ] Exactly one intended lamp lights for each single-channel test.
- [ ] HIGH means lamp ON; reversed ULN pin numbering does not invert logic.
- [ ] All-off clears every connected lamp.
- [ ] An 80-character LCD pattern appears in the correct four rows/20 columns.
- [ ] A shorter replacement text clears trailing characters with spaces.
- [ ] Physical contrast/backlight are satisfactory.
- [ ] Stop clears physical outputs.
- [ ] Power-cycle with persisted configuration and record startup behavior.

Because 595 `/OE` is hard-wired to GND, outputs before firmware initialization or during BOOTSEL are undefined. Firmware and host clearing do not constitute a hardware startup inhibit.

After every hardware check passes, set `bench_verified: true` in `electronics/pinmap.yaml` and record the tested map/hash. This flag records a result; it does not replace the checklist.

## 5. Observe the actual aircraft

```bash
uv run ap-panel run --serial SN-YOUR_BOARD_ID --observe --record recordings/observe.jsonl
```

Load the intended analog C172 or C90B in X-Plane with API v2 available.

- [ ] Record the exact aircraft path seen by the bridge.
- [ ] Confirm a compatible candidate profile and its expected autopilot type.
- [ ] Compare heading, course, altitude, VS, and barometer feedback to the cockpit.
- [ ] Verify unknown/unavailable feedback displays placeholders and capability reasons.
- [ ] Confirm C172 restrictions: ALT SEL, selected-altitude rotation/sync, independent FD, YD, and retractable gear are unavailable.
- [ ] Use cockpit controls to observe each mode's inactive, armed, and active feedback.
- [ ] Armed lamps blink at 1 Hz; active/captured lamps stay steady.
- [ ] GS CAP responds to actual capture, rather than APR selection or GS arming.
- [ ] GPS/VLOC follow actual pilot CDI source.
- [ ] Distinguish yaw-damper selection from genuine engagement feedback.
- [ ] Record each supported DataRef/action binding's reference provenance and individual installed-aircraft test result; a catalog match alone does not set `simulator_tested`.

### Gear, flap, and YD calibration

Profiles initially contain `gear_verified: false`, `gear_indices: {}`, `flaps_verified: false`, `flap_detents: {}`, and unverified YD engagement feedback. Those indicators remain disabled/off until qualified.

For retractable gear, inspect a complete deployment cycle. Identify the actual array elements for left, nose, and right, then record `gear_indices` with keys `L`, `N`, `R`. Verify each lamp independently and during transit before setting `gear_verified: true`. Fixed gear does not need a fabricated retractable mapping.

Record actual settled flap deployment at every installed detent. `flap_detents` uses normalized ratios keyed `UP`, `1`, optional `2`, and `FULL`. C90B supports UP / 1 / FULL and keeps Flaps 2 unused. Confirm intermediate motion is not mistaken for a stable detent before setting `flaps_verified: true`.

For YD, add a reference-verified DataRef representing engagement to the profile and set `yd_engaged_key` to its logical key. Test selected-versus-engaged cases before setting `yd_engaged_verified: true`. A switch-selection DataRef alone does not qualify the engagement lamp.

- [ ] Gear indices measured and verified, or explicit unavailability recorded.
- [ ] Actual flap ratios measured and labels verified.
- [ ] YD engagement feedback qualified, or lamp remains unavailable.
- [ ] Calibration changes pass configuration validation and repeated observation.

## 6. Test physical commands in explicit commissioning mode

```bash
uv run ap-panel run --serial SN-YOUR_BOARD_ID --commission --record recordings/aircraft-commission.jsonl
```

The dashboard must clearly show commissioning mode and the unverified candidate. Start the commissioning session to enable physical command testing.

- [ ] AP engage/disengage agrees with actual cockpit engagement.
- [ ] HDG, NAV, APR select and cancel the intended modes.
- [ ] C90B ALT SEL uses selected VS and arms altitude capture; ALT independently holds current altitude.
- [ ] Repeated ALT SEL while already armed leaves capture armed; it does not toggle arming off.
- [ ] ALT SEL preserves the selected altitude and the selected VS value/sign in both climb and descent cases; it does not choose a VS sign automatically from the target altitude.
- [ ] ALT SEL and ALT preserve the AP engagement state. Test with servos initially off and on; mode selection must not silently request AP engagement.
- [ ] Immediate capture at the selected altitude is accepted; an unrelated altitude-hold state is not treated as selected-altitude capture.
- [ ] Altitude capture changes annunciation from armed to active appropriately.
- [ ] VS mode, rotation, and push agree with selected VS feedback.
- [ ] Native VS increments match the installed aircraft: expected 100 fpm for C172 and 200 fpm for C90B. Regular and fast events each produce one increment.
- [ ] Heading/course direction, increments, wrap, and sync agree with cockpit feedback.
- [ ] HDG push syncs to actual heading.
- [ ] ALT push syncs to actual altitude only where selected-altitude capability is supported.
- [ ] VS push selects zero VS.
- [ ] CRS push operates only for a valid supported source/course.
- [ ] BARO push selects 29.92 inHg, confirmed by feedback.
- [ ] CDI changes update actual source lamps and LCD.
- [ ] Gear transitions issue the intended command; startup/reconnect position issues none.
- [ ] Flap rotation moves one detent; indicators follow actual deployment.
- [ ] Unsupported controls produce a capability reason and no fallback command.
- [ ] Rapid rotations settle without replaying a stale backlog.
- [ ] Test relevant aircraft electrical-power and AP failure conditions; unavailable/denied actions do not claim successful engagement.
- [ ] Exercise partial ALT SEL failure after the VS request but before altitude arming. Later steps stop, uncertain commands are not replayed, dials are not restored automatically, and displays show actual remaining state.
- [ ] Exercise Stop, connection loss, or aircraft change during the multi-step sequence; no subsequent step enters a different session.

Exercise a full IFR scenario for each aircraft: heading vectors, navigation interception, altitude/VS changes, approach arming and GS capture, missed approach, and applicable gear/flap configuration changes.

## 7. Faults and qualification record

- [ ] Stop blocks input routing and clears outputs.
- [ ] Simulator/API loss marks feedback unavailable and blocks commands.
- [ ] Simulator reconnect rebuilds IDs/subscriptions without replaying commands.
- [ ] Aircraft change rechecks compatibility and blocks old actions.
- [ ] Pico reconnect repeats identity/configuration/snapshot checks.
- [ ] A held button or gear position during reconnect is reported without a flight command.
- [ ] Denied/timed-out requests do not fabricate successful feedback.
- [ ] Browser reload restores diagnostics without restarting the bridge session.
- [ ] Perform browser visual/responsive review; local HTTP/WebSocket checks alone do not verify the rendered UI.
- [ ] Measure valid received feedback to physical output change against the ≤250 ms local-feedback target; record normal/worst latency under rapid input.
- [ ] Measure simulator/API loss to blocked dispatch and cleared reachable outputs against the 2.5-second loss-detection target.
- [ ] Complete a recorded actual hardware/simulator session longer than one hour, with inputs/mode changes/reconnects and no unexpected power saving or growing backlog.
- [ ] Reproduce the accepted hard-crash limit: kill the host while lamps are lit, observe last-state persistence, and record the stock timeout relative to its last keepalive (approximately 15 minutes).

Mark individually tested supported DataRefs/actions with their binding-level `simulator_tested` result. After the whole aircraft procedure passes, set the aggregate profile's `simulator_tested: true` and `tested_aircraft_path` to the exact observed path. Record calibration/version/revision. Normal Start should then show both qualified hardware and qualified aircraft.

```text
Date / tester:
Pico serial / firmware:
Pin-map hash / revision:
Encoder decoder types / inversion:
LCD address:
macOS / Python / X-Plane versions:
Profile ID / revision:
Exact tested aircraft path:
Gear indices / flap ratios / YD feedback:
Hardware, aircraft, fault checks passed:
Supported binding test results / remaining unsupported bindings:
Local feedback / loss detection measurements:
Over-one-hour soak duration / observations:
Hard-crash timeout reproduction:
Recording / board backup paths:
Remaining unavailable capabilities / observations:
```
