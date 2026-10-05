"""Offline sample telemetry for dashboard inspection; no transport methods."""

import base64
import time

from .models import TelemetryValue
from .profiles import Profile


class DemoSimulator:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.healthy = True
        self.capabilities = {"simulator": {"version": "Offline demo"}}
        self.command_ids = {name: index for index, name in enumerate(profile.command_paths())}
        self.values = {}
        self.tick()

    def tick(self) -> None:
        sample = {
            "aircraft": base64.b64encode(self.profile.aircraft_paths[0].encode()).decode(),
            "paused": 0,
            "ap_type": 0,
            "ap": 1,
            "hdg_status": 2,
            "nav_status": 1,
            "gpss_status": 0,
            "apr_status": 0,
            "alt_armed": 1,
            "alt_status": 0,
            "vs_status": 2,
            "gs_status": 0,
            "heading": 274.0,
            "selected_altitude": 5500.0,
            "held_altitude": 4200.0,
            "pilot_altitude": 4300.0,
            "vs": 700.0,
            "baro": 29.92,
            "course": 143.0,
            "source": 2,
            "fd_mode": 2,
            "fd_selected": 1,
            "yd_selected": 1,
            "gear": [0.0] * 10,
            "flap_actual": 0.0,
        }
        self.values = {k: TelemetryValue(v, updated_at=time.monotonic()) for k, v in sample.items()}
