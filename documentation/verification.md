# Verification and acceptance matrix

This matrix separates software evidence from physical and installed-aircraft qualification. Automated coverage uses fake serial and X-Plane endpoints. A passing fake test does not establish that the breadboard is wired correctly or that a catalog command behaves identically on the installed aircraft.

The current prototype defaults leave hardware and aircraft qualification false. Keep those flags false until the corresponding checks in [commissioning.md](commissioning.md) pass. Exact suite/build results should accompany the checked revision; physical measurements belong in the qualification record.

## Historical development checks — October 5–6, 2026

These are dated development observations, not a claim that the current working tree is a finished or hardware-verified release.

- Initial checks: **345 passed**, with Python 3.12.8 on macOS. The launch-recovery revision passes **346 tests** on Python 3.14.8, including an installed-package regression independent of pytest's source path.
- `ruff check src tests` and `ruff format --check src tests`: passed.
- `node --check web/app.js` and `uv lock --check`: passed.
- Source distribution and wheel built successfully; the wheel installed in a separate environment outside the checkout. Its default resources, configuration validation, dashboard assets, demo server, and replay were exercised.
- The official 3.1.4 ZIP and Pico 2 UF2 matched both pinned SHA-256 checksums. No controller was flashed.
- Actual demo HTTP/WebSocket status delivery and cross-origin rejection were checked. A transport integration test covers a serial encoder event, native command request, subsequent simulator feedback, LCD serialization, and connection loss.
- The local discovery check found **no eligible Pico 2**, and the default X-Plane API did not respond. Real commissioning and native browser visual review remain pending.

The operator subsequently confirmed the local dashboard opened and rendered correctly. The macOS editable-install failure was reproduced: Python skipped the hidden `.pth` file and could not import `ap_panel.cli`. Documented uv commands now use `--no-editable`. On Python 3.14.8, repeated imports, demo start/Ctrl-C/restart on port 8787, and configuration dry run passed. The installation regression also changes copied source, LCD calibration, and CSS and verifies that uv rebuilds the installed package from those changes. Full responsive/interaction review and physical commissioning remain separate checks.

## Software coverage and its limits

| Acceptance area | Software evidence | Real-system acceptance still required |
|---|---|---|
| Canonical wiring | Configuration validation checks GPIO uniqueness, all 16 inputs/all 24 outputs, reversed ULN pins, reservations, safe names, and generated records | **PENDING:** continuity against the real breadboard, chip markings, power domains |
| Stock serial framing | Protocol tests cover separators/escaping, partial frames, firmware command-size bounds, LCD dimensions, output-mask order | **PENDING:** real Pico 2/3.1.4 USB CDC exchange and sustained traffic |
| Identity and provisioning | Fake serial tests cover selected identity, firmware/config mismatch, durable backup before mutation, append acknowledgements, no retries, activation errors/readback | **PENDING:** actual flash, selected board, configuration upload/readback |
| Snapshot and input baseline | Tests cover all indices, missing completion/levels, event interleaving, held-button startup, gear baseline, reconnect generations | **PENDING:** all real switch states, wiring, bounce, and snapshot transitions |
| Encoders | Tests cover direction/fast-event variants, inversion, state resets, qualified event routing | **PENDING:** actual EC11 decoder type, detent counts, fast turns, electrical isolation |
| X-Plane API v2 | Fake HTTP/WebSocket tests cover capabilities, exact named lookup, initial/static/array values, sparse updates, acknowledgement, timeout, reconnection, ID replacement | **PENDING:** installed X-Plane endpoint and exact aircraft resources |
| Aircraft compatibility | Profile/runtime tests cover candidates, required versus optional resources, AP type, unsupported controls, qualification gates, aircraft changes | **PENDING:** exact tested aircraft path and actual available capabilities |
| Multi-step C90B ALT SEL | Tests cover VS feedback before arming, dial preservation, already-armed behavior, immediate capture, wrong-altitude hold, partial rejection, Stop/loss during the sequence | **PENDING:** actual native-command side effects, AP/FD preservation, VS sign, capture and failure scenarios |
| Five encoder pushes / other actions | Action tests cover qualified command/write routing, VS zero, source selection, sync prerequisites, unsupported functions, stale feedback and denied acknowledgements | **PENDING:** all five pushes and every available action on each installed aircraft |
| Closed-loop annunciation | Renderer tests cover armed/active distinction, GS capture, unknown source, YD selection versus engagement, reservations, invalid feedback | **PENDING:** compare every physical lamp with the cockpit through real mode transitions |
| Gear/flap calibration | Tests cover unmeasured mappings staying off, validated measured indices/ratios, partial/invalid gear feedback, flap motion/settling and C90B unused Flaps 2 | **PENDING:** measure L/N/R indices and actual flap detents; qualify genuine YD engagement feedback |
| 20 × 4 LCD | Tests enforce 80 printable ASCII characters, four exact rows, padding, placeholders, overflow, C172 held-altitude behavior | **PENDING:** backpack address, physical row order, contrast/backlight, character replacement |
| Runtime Stop/fault/reconnect | Fake runtime tests cover queue invalidation, held baselines, lease failure, aircraft change, bounded backlog, stale events, failed actions, bench cancellation, transport shutdown | **PENDING:** physical outputs on loss/Stop/reconnect and measured detection latency |
| Dashboard API/security | HTTP/WS tests cover loopback Host/Origin, bounded JSON, readiness errors, bench-only output actions, missing routes/assets, no flight-control mutation routes, unsubscribe | **PENDING:** browser visual/responsive/interaction review |
| Local demo | Actual local HTTP serving and diagnostic WebSocket updates exercised with offline demo data; dashboard rendering subsequently observed locally | **PENDING:** full responsive and interaction review |
| Recording / replay | Tests cover metadata/configuration integrity, input/feedback reconstruction, malformed recordings, and zero transport actions in replay | **PENDING:** a complete recorded real commissioning session |
| Installation / package | CLI help, validation, configuration dry run, fake tests, lint, and package checks are recorded separately for the delivered revision | **PENDING:** use on the operator's target macOS environment with real transports |

Evidence lives in `tests/test_protocol.py`, `test_config.py`, `test_board.py`, `test_xplane.py`, `test_inputs_outputs.py`, `test_actions.py`, `test_runtime.py`, `test_dashboard.py`, `test_cli_replay.py`, `test_integration.py`, and `test_installation.py`. The installation check requires uv and already cached build/runtime dependencies; it uses offline mode. These are software checks, not hardware-operation tests.

## Binding provenance and qualification

DataRef and action metadata distinguish `reference_verified` from `simulator_tested`. Reference verification records that a name and interface were found in an authoritative catalog/source. It leaves installed-aircraft testing false.

During commissioning, record and mark each successfully tested DataRef/action binding individually. A binding is tested only after its actual type/value, command behavior or write acknowledgement, and resulting feedback have been checked on the recorded aircraft. Keep absent or unsupported bindings unqualified and document their capability reasons.

The profile-level `simulator_tested` is the aggregate qualification result for the supported panel behavior and installed checks; `tested_aircraft_path` records the exact aircraft. It is not a substitute for per-binding evidence. Gear, flap, and YD engagement additionally retain their dedicated calibration/verification metadata. Hardware `bench_verified` remains a separate result.

## Timing and endurance targets — unmeasured on real equipment

| Target | Acceptance measurement | Current evidence |
|---|---|---|
| Healthy local feedback ≤250 ms | Timestamp receipt of valid simulator feedback and the corresponding physical LCD/lamp change; measure under normal and rapid-input load | **PENDING / UNMEASURED:** fake behavior and scheduling checks do not establish physical latency |
| Simulator loss detected within 2.5 seconds | Interrupt the API/feedback path at a known time while the host and Pico remain reachable; measure dispatch inhibition and physical output clearing | **PENDING / UNMEASURED:** fault/lease behavior is covered with fake services |
| More than one hour of actual continuous operation | Run a recorded physical/simulator session for over 60 minutes with mode changes, encoder use, approach/capture, and reconnect checks; verify no unexpected power saving, backlog, or drift | **PENDING:** no real hardware/simulator soak completed |
| Accepted hard-crash fallback | Kill the host during a stable illuminated state; measure last-frame persistence and the stock timeout relative to its last keepalive | **PENDING:** stock behavior inspected; physical reproduction not completed |

The 250 ms target concerns local feedback delivery, not the aircraft's mechanical flap/gear travel time or an autopilot capture waiting for flight conditions. Record both normal and worst observed delays, load conditions, and any missed target.

The simulator-loss target applies while the application is still running and the board can receive clear commands. A host process killed outright cannot send a clear; the accepted fallback is stock MobiFlight power saving at approximately 15 minutes after the last keepalive. That is not a short watchdog.

## Required real-system completion

1. Complete every physical input, encoder, output, LCD, power-cycle, and snapshot check.
2. Observe the exact C172/C90B, resolve and individually qualify supported bindings, then measure gear/flap/YD feedback where applicable.
3. Exercise ALT SEL repeatedly while already armed, preserve selected altitude and VS sign/value, preserve AP state, and distinguish ALT hold from ALT SEL.
4. Exercise aircraft power/failure cases and partial multi-step failure. Confirm the host does not invent successful state, retry uncertain mutations, restore dials automatically, or continue later steps after a fault.
5. Measure healthy local feedback and loss-response targets, reproduce the hard-crash limitation, and complete a recorded actual session longer than one hour.
6. Perform browser visual/responsive review. Actual HTTP/WS demo checks alone do not prove layout or interaction quality.
7. Record results and exact versions/revisions before setting qualification flags and enabling normal Start.

Store the measured results with the qualification record rather than treating the checklist itself as completed evidence.
