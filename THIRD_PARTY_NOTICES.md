# Third-party projects and references

The [MIT license](LICENSE) covers original ap-panel code and documentation. Upstream software, firmware, and simulator products retain their own licenses and terms.

## Firmware and simulator interfaces

| Project | Relationship to ap-panel |
|---|---|
| [MobiFlight firmware](https://github.com/MobiFlight/MobiFlight-FirmwareSource) | Stock firmware for the Pico 2. The project pins release 3.1.4 and its official artifact checksums in the [firmware manifest](mobiflight/firmware-manifest.json). Firmware downloads remain external to the repository. |
| [Arduino CmdMessenger](https://github.com/MobiFlight/Arduino-CmdMessenger/tree/4.2.2) | Reference for the serial framing used by the stock firmware. The Python bridge implements the host-side framing in `src/ap_panel/protocol.py`. |
| [MobiFlight XPlaneConnector](https://github.com/MobiFlight/XPlaneConnector) | A command-catalog reference for candidate aircraft bindings. The host bridge uses X-Plane's Web API directly. |
| [X-Plane](https://www.x-plane.com/) | External simulator. Integration uses the documented [Web API](https://developer.x-plane.com/article/x-plane-web-api/) and [DataRefs](https://developer.x-plane.com/datarefs/). No simulator or aircraft assets are included. |
| [Raspberry Pi Pico](https://www.raspberrypi.com/products/raspberry-pi-pico-2/) | External controller hardware; [official documentation](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html) supplies board and BOOTSEL references. |

## Python dependencies and development tools

Dependencies are installed separately. Exact resolved versions and source URLs are recorded in [`uv.lock`](uv.lock).

| Project | Use |
|---|---|
| [aiohttp](https://github.com/aio-libs/aiohttp) | Simulator HTTP/WebSocket transport and the local dashboard server |
| [pySerial](https://github.com/pyserial/pyserial) | USB serial transport |
| [Pydantic](https://github.com/pydantic/pydantic) | Configuration validation |
| [PyYAML](https://github.com/yaml/pyyaml) | YAML configuration parsing |
| [uv](https://github.com/astral-sh/uv) | Dependency locking and environment management |
| [Hatchling](https://github.com/pypa/hatch) | Python package builds |
| [pytest](https://github.com/pytest-dev/pytest) and [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Automated checks |
| [Ruff](https://github.com/astral-sh/ruff) | Python linting and formatting |
| [build](https://github.com/pypa/build) | Source distribution and wheel build frontend |

Refer to each upstream project's license for its material. This is an independent hobby project; upstream names identify the components and integration targets.
