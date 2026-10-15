# Contributing

ap-panel is a work-in-progress breadboard and software project. Hardware bring-up and aircraft verification are ongoing. Focused fixes, measurements, and documentation improvements are welcome.

## Reporting a finding

Open an [issue](https://github.com/cpstrommen/ap-panel/issues) with what you expected, what happened, and the steps needed to reproduce it. Include the relevant OS, Python, X-Plane, MobiFlight firmware, and aircraft versions.

For hardware findings, include the pin-map revision, component type, affected connector/channel, and whether the behavior was observed on the actual panel or in a simulation. Screenshots and small, redacted diagnostic examples are helpful. Leave board serials, personal paths, credentials, and full session recordings out of public reports.

## Working on the code

From the repository root:

```bash
uv sync --no-editable --group dev
uv run --no-editable pytest
uv run --no-editable ruff check src tests
uv run --no-editable ruff format --check src tests
```

Keep `--no-editable` on uv commands; see the [installation troubleshooting](documentation/troubleshooting.md#import-fails-after-a-successful-launch). The installed-package regression runs offline and requires cached build/runtime dependencies.

Keep changes small and explain the observed problem, the resulting behavior, and how you checked it. Distinguish simulator or hardware measurements from mock tests. Use synthetic inputs for committed fixtures.

## Hardware and aircraft changes

The canonical hardware map is [`electronics/pinmap.yaml`](electronics/pinmap.yaml). After changing it, regenerate the pinout and firmware records:

```bash
uv run --no-editable ap-panel validate --generate
```

Aircraft mappings live in [`profiles/`](profiles/). A catalog entry is reference evidence, while an installed-aircraft test is separate evidence. Record both, and keep verification flags unset until the corresponding checks in the [commissioning guide](documentation/commissioning.md) have passed.

Preserve existing local work. Local settings, logs, recordings, board backups, firmware binaries, and virtual environments are excluded from version control.

## License

Contributions to original project code and documentation use the repository's [MIT license](LICENSE). Keep provenance and applicable notices for any third-party material; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
