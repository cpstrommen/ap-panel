"""Exercise action outcomes, ordering, and interruption using a fake aircraft.

Writes and commands affect simulator-side state; only resync exposes it to
the executor. This separates ACK acceptance from observed flight state.
"""

import asyncio
import time
from collections.abc import Callable

import pytest

from ap_panel.actions import (
    ALT_ARM,
    GPS_SELECT,
    NAV1_SELECT,
    VS_PRESELECT,
    ActionError,
    ActionExecutor,
)
from ap_panel.models import TelemetryValue
from ap_panel.profiles import load_profiles
from ap_panel.xplane import XPlaneAPIError, XPlaneTimeoutError


class Aircraft:
    def __init__(self, profile):
        self.profile = profile
        self.generation = 1
        self.healthy = True
        self.command_ids = {path: i + 1 for i, path in enumerate(profile.command_paths())}
        self.dataref_ids = {key: i + 1 for i, key in enumerate(profile.datarefs)}
        self.dataref_types = {
            key: "float" if spec.value_type == "numeric" else spec.value_type
            for key, spec in profile.datarefs.items()
        }
        self.backend = dict.fromkeys(profile.datarefs, 0)
        self.backend.update(
            vs=600,
            selected_altitude=8000,
            held_altitude=3000,
            pilot_altitude=3000,
            source=0,
            course=90,
            nav1_bearing=123.5,
            nav1_flag=1,
            nav1_type=4,
        )
        self.values = {}
        self.events = []
        self.effects: dict[str, Callable[[], None]] = {
            VS_PRESELECT: lambda: self.backend.update(vs_status=2),
            ALT_ARM: lambda: self.backend.update(alt_armed=1),
            GPS_SELECT: lambda: self.backend.update(source=2),
            NAV1_SELECT: lambda: self.backend.update(source=0),
            "sim/autopilot/altitude_sync": lambda: self.backend.update(
                selected_altitude=self.backend["pilot_altitude"]
            ),
            "sim/radios/obs_HSI_direct": lambda: self.backend.update(
                course=self.backend["nav1_bearing"]
            ),
        }
        self.failures = {}
        self.after_command: Callable[[str], None] | None = None
        self.after_refresh: Callable[[], None] | None = None
        self.after_write: Callable[[], None] | None = None
        self.stale_keys = set()
        self.publish()

    def publish(self):
        for key, value in self.backend.items():
            if key in self.stale_keys:
                continue
            previous = self.values.get(key)
            self.values[key] = TelemetryValue(
                value=value,
                revision=previous.revision + 1 if previous else 1,
                generation=self.generation,
                updated_at=time.monotonic(),
            )

    async def resync(self):
        self.events.append(("resync",))
        await asyncio.sleep(0)
        self.publish()
        if self.after_refresh:
            self.after_refresh()

    async def command(self, path):
        self.events.append(("command", path))
        await asyncio.sleep(0)
        if path in self.failures:
            raise self.failures[path]
        if path in self.effects:
            self.effects[path]()
        if self.after_command:
            self.after_command(path)

    async def write(self, key, value):
        self.events.append(("write", key, value))
        await asyncio.sleep(0)
        self.backend[key] = value
        if self.after_write:
            self.after_write()

    async def wait_value(self, key, predicate, timeout=2.0, min_revision=None):
        self.events.append(("wait", key))
        await asyncio.sleep(0)
        value = self.values[key].value
        if predicate(value):
            return value
        raise XPlaneTimeoutError(f"No confirming feedback for {key}")

    @property
    def mutations(self):
        return [event for event in self.events if event[0] in ("command", "write")]


@pytest.fixture
def c90():
    profile = next(p for p in load_profiles() if p.id == "c90b")
    return profile, Aircraft(profile)


@pytest.fixture
def c172():
    profile = next(p for p in load_profiles() if p.id == "c172-analog")
    return profile, Aircraft(profile)


async def test_simple_command_sent_once_without_inferred_mode(c90):
    profile, sim = c90
    await ActionExecutor(sim, profile).execute("HDG")
    assert sim.events == [("command", "sim/autopilot/heading")]
    assert sim.values["hdg_status"].value == 0


async def test_alt_sel_preserves_dials_and_orders_feedback_before_arm(c90):
    profile, sim = c90
    await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT), ("command", ALT_ARM)]
    assert sim.events.index(("wait", "vs_status")) < sim.events.index(("command", ALT_ARM))
    assert sim.values["vs"].value == 600
    assert sim.values["selected_altitude"].value == 8000
    assert sim.values["alt_armed"].value == 1


async def test_alt_sel_already_armed_does_not_toggle_arm_off(c90):
    profile, sim = c90
    sim.backend.update(alt_armed=1, vs_status=2)
    await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT)]
    assert sim.values["alt_armed"].value == 1


async def test_alt_sel_accepts_immediate_capture_at_selected_altitude(c90):
    profile, sim = c90
    sim.effects[VS_PRESELECT] = lambda: sim.backend.update(
        vs_status=0, alt_status=2, held_altitude=8000, pilot_altitude=7975
    )
    await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT)]


async def test_alt_sel_rejects_existing_alt_hold_at_other_altitude(c90):
    profile, sim = c90
    sim.backend["alt_status"] = 2
    sim.effects[VS_PRESELECT] = lambda: None
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT)]


@pytest.mark.parametrize("key", ["vs", "selected_altitude"])
async def test_alt_sel_stops_if_native_command_changes_a_dial(c90, key):
    profile, sim = c90
    sim.effects[VS_PRESELECT] = lambda: sim.backend.update(
        vs_status=2, **{key: sim.backend[key] + 200}
    )
    with pytest.raises(ActionError, match="changed during ALT SEL"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT)]


async def test_alt_sel_partial_failure_never_retries_or_restores(c90):
    profile, sim = c90
    sim.failures[ALT_ARM] = XPlaneAPIError("rejected", "aircraft refused")
    with pytest.raises(ActionError, match="aircraft refused"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT), ("command", ALT_ARM)]
    assert sim.values["vs_status"].value == 2


async def test_arm_ack_alone_is_not_success(c90):
    profile, sim = c90
    sim.effects[ALT_ARM] = lambda: None
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT), ("command", ALT_ARM)]


async def test_guard_changed_while_command_awaited_stops_next_operation(c90):
    profile, sim = c90
    running = True

    def stop(_):
        nonlocal running
        running = False

    sim.after_command = stop
    with pytest.raises(ActionError, match="panel or aircraft profile changed"):
        await ActionExecutor(sim, profile, lambda: running).execute("ALT_SEL")
    assert sim.events == [("resync",), ("command", VS_PRESELECT)]


async def test_generation_changed_during_refresh_stops_all_effects(c90):
    profile, sim = c90
    sim.after_refresh = lambda: setattr(sim, "generation", sim.generation + 1)
    with pytest.raises(ActionError, match="connection changed"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert not sim.mutations


async def test_guard_prevents_initial_command(c90):
    profile, sim = c90
    with pytest.raises(ActionError, match="interrupted"):
        await ActionExecutor(sim, profile, lambda: False).execute("AP")
    assert not sim.events


async def test_profile_replaced_during_await_stops_next_step(c90):
    profile, sim = c90
    executor = ActionExecutor(sim, profile)
    sim.after_command = lambda _: setattr(executor, "profile", profile.model_copy(deep=True))
    with pytest.raises(ActionError, match="profile changed"):
        await executor.execute("ALT_SEL")
    assert sim.events == [("resync",), ("command", VS_PRESELECT)]


async def test_old_generation_feedback_prevents_even_simple_command(c90):
    profile, sim = c90
    sim.generation += 1
    with pytest.raises(ActionError, match="Unavailable simulator feedback"):
        await ActionExecutor(sim, profile).execute("HDG")
    assert not sim.events


async def test_missing_command_preflight_prevents_partial_alt_sel(c90):
    profile, sim = c90
    del sim.command_ids[ALT_ARM]
    with pytest.raises(ActionError, match="command is unavailable"):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert not sim.events


async def test_c172_alt_sel_is_unavailable(c172):
    profile, sim = c172
    with pytest.raises(ActionError):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert not sim.events


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "600"])
async def test_invalid_numeric_required_feedback_stops_command(c90, value):
    profile, sim = c90
    sim.backend["vs"] = value
    sim.publish()
    with pytest.raises(ActionError, match="Invalid numeric"):
        await ActionExecutor(sim, profile).execute("VS_CW")
    assert not sim.events


async def test_unknown_status_and_wrong_type_are_unavailable(c90):
    profile, sim = c90
    sim.backend["hdg_status"] = 3
    sim.publish()
    with pytest.raises(ActionError, match="Unknown simulator status"):
        await ActionExecutor(sim, profile).execute("HDG")
    sim.backend["hdg_status"] = 0
    sim.publish()
    sim.dataref_types["hdg_status"] = "float_array"
    with pytest.raises(ActionError, match="Unexpected simulator type"):
        await ActionExecutor(sim, profile).execute("HDG")
    assert not sim.events


async def test_write_ack_requires_observed_feedback(c90):
    profile, sim = c90
    await ActionExecutor(sim, profile).execute("VS_PUSH")
    assert sim.events == [("write", "vs", 0.0), ("resync",), ("wait", "vs")]
    assert sim.values["vs"].value == 0
    sim.after_write = lambda: sim.backend.update(vs=600)
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("VS_PUSH")
    assert sim.mutations.count(("write", "vs", 0.0)) == 2


async def test_write_rechecks_writable_declaration(c90):
    profile, sim = c90
    profile.datarefs["vs"].writable = False
    with pytest.raises(ActionError, match="not declared writable"):
        await ActionExecutor(sim, profile).execute("VS_PUSH")
    assert not sim.mutations


async def test_guard_stopped_after_write_does_not_refresh_or_repeat(c90):
    profile, sim = c90
    running = True

    def stop():
        nonlocal running
        running = False

    sim.after_write = stop
    with pytest.raises(ActionError, match="interrupted"):
        await ActionExecutor(sim, profile, lambda: running).execute("VS_PUSH")
    assert sim.events == [("write", "vs", 0.0)]
    assert sim.values["vs"].value == 600


async def test_native_alt_sync_and_explicit_write_fallback(c90):
    profile, sim = c90
    sim.backend["pilot_altitude"] = 3217.5
    await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert sim.mutations == [("command", "sim/autopilot/altitude_sync")]
    assert sim.values["selected_altitude"].value == 3217.5
    profile.alt_sync_use_write = True
    sim.backend["pilot_altitude"] = 4175.25
    await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert sim.mutations[-1] == ("write", "selected_altitude", 4175.25)
    assert sim.values["selected_altitude"].value == 4175.25


async def test_native_alt_sync_does_not_pretend_ack_changed_selector(c90):
    profile, sim = c90
    sim.effects["sim/autopilot/altitude_sync"] = lambda: None
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert sim.values["selected_altitude"].value == 8000


async def test_alt_sync_write_fallback_requires_declared_writable_selector(c90):
    profile, sim = c90
    profile.alt_sync_use_write = True
    profile.datarefs["selected_altitude"].writable = False
    with pytest.raises(ActionError, match="not declared writable"):
        await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert not sim.mutations


async def test_fractional_write_to_integer_dataref_is_rejected(c90):
    profile, sim = c90
    profile.alt_sync_use_write = True
    profile.datarefs["selected_altitude"].value_type = "int"
    sim.dataref_types["selected_altitude"] = "int"
    sim.backend["pilot_altitude"] = 3123.5
    with pytest.raises(ActionError, match="requires an integer"):
        await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert not sim.mutations


async def test_special_actions_refuse_feedback_not_refreshed(c90):
    profile, sim = c90
    sim.stale_keys.add("pilot_altitude")
    with pytest.raises(ActionError, match="Fresh simulator feedback"):
        await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert not sim.mutations


@pytest.mark.parametrize("source,expected", [(0, GPS_SELECT), (1, GPS_SELECT), (2, NAV1_SELECT)])
async def test_cdi_uses_explicit_nav_gps_selection(c90, source, expected):
    profile, sim = c90
    sim.backend["source"] = source
    await ActionExecutor(sim, profile).execute("CDI")
    assert sim.mutations == [("command", expected)]
    assert sim.values["source"].value == (0 if source == 2 else 2)


async def test_cdi_unknown_source_refused_and_ack_requires_feedback(c90):
    profile, sim = c90
    sim.backend["source"] = 7
    with pytest.raises(ActionError, match="Unknown simulator status"):
        await ActionExecutor(sim, profile).execute("CDI")
    assert not sim.mutations
    sim.backend["source"] = 0
    sim.publish()
    sim.effects[GPS_SELECT] = lambda: None
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("CDI")
    assert sim.mutations == [("command", GPS_SELECT)]


@pytest.mark.parametrize("source", [1, 2])
async def test_c172_course_push_never_mutates_nav2_or_gps(c172, source):
    profile, sim = c172
    sim.backend["source"] = source
    with pytest.raises(ActionError, match="NAV1 selected"):
        await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert not sim.mutations


@pytest.mark.parametrize("nav_type,flag", [(0, 1), (8, 1), (16, 1), (12, 1), (4, 0), (4, 3)])
async def test_c172_course_push_requires_received_vor(c172, nav_type, flag):
    profile, sim = c172
    sim.backend.update(nav1_type=nav_type, nav1_flag=flag)
    with pytest.raises(ActionError):
        await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert not sim.mutations


@pytest.mark.parametrize("nav_type", [4, 1028, 4100])
async def test_c172_native_course_push_accepts_vor_vordme_vortac(c172, nav_type):
    profile, sim = c172
    sim.backend["nav1_type"] = nav_type
    await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.mutations == [("command", "sim/radios/obs_HSI_direct")]
    assert sim.values["course"].value == 123.5


async def test_c172_course_explicit_write_fallback_wraps_bearing(c172):
    profile, sim = c172
    profile.course_sync_use_write = True
    sim.backend["nav1_bearing"] = 361.5
    await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.mutations == [("write", "course", 1.5)]
    assert sim.values["course"].value == 1.5


async def test_explicit_alt_write_fallback_does_not_require_native_command(c90):
    profile, sim = c90
    profile.alt_sync_use_write = True
    native = profile.actions["ALT_PUSH"].command
    del sim.command_ids[native]
    assert native not in profile.command_paths()
    assert profile.missing_for("ALT_PUSH", sim) is None
    await ActionExecutor(sim, profile).execute("ALT_PUSH")
    assert sim.mutations == [("write", "selected_altitude", 3000.0)]


async def test_explicit_course_write_fallback_does_not_require_native_command(c172):
    profile, sim = c172
    profile.course_sync_use_write = True
    native = profile.actions["CRS_PUSH"].command
    del sim.command_ids[native]
    assert native not in profile.command_paths()
    assert profile.missing_for("CRS_PUSH", sim) is None
    await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.mutations == [("write", "course", 123.5)]


async def test_effect_attempt_flag_distinguishes_preflight_from_partial_failure(c90):
    profile, sim = c90
    executor = ActionExecutor(sim, profile)
    sim.backend["source"] = 2
    with pytest.raises(ActionError):
        await executor.execute("CRS_PUSH")
    assert executor.effects_attempted is False
    sim.failures[ALT_ARM] = XPlaneAPIError("rejected")
    with pytest.raises(ActionError):
        await executor.execute("ALT_SEL")
    assert executor.effects_attempted is True


async def test_failed_first_request_is_an_attempt_and_flag_resets_per_action(c90):
    profile, sim = c90
    executor = ActionExecutor(sim, profile)
    sim.failures["sim/autopilot/heading"] = XPlaneAPIError("rejected")
    with pytest.raises(ActionError):
        await executor.execute("HDG")
    assert executor.effects_attempted is True
    with pytest.raises(ActionError):
        await executor.execute("UNKNOWN")
    assert executor.effects_attempted is False


async def test_c172_native_course_ack_without_direct_course_rejected(c172):
    profile, sim = c172
    sim.effects["sim/radios/obs_HSI_direct"] = lambda: None
    with pytest.raises(ActionError, match="No confirming feedback"):
        await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.values["course"].value == 90


@pytest.mark.parametrize("source", [0, 1])
async def test_c90_hsi_push_on_vloc_receivers(c90, source):
    profile, sim = c90
    sim.backend["source"] = source
    await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.mutations == [("command", "sim/radios/obs_HSI_direct")]


async def test_c90_hsi_push_on_gps_does_not_change_flight_plan(c90):
    profile, sim = c90
    sim.backend["source"] = 2
    with pytest.raises(ActionError, match="NAV receiver selected"):
        await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert not sim.mutations


async def test_course_push_detects_source_change_during_native_command(c90):
    profile, sim = c90
    sim.effects["sim/radios/obs_HSI_direct"] = lambda: sim.backend.update(source=2)
    with pytest.raises(ActionError, match="source changed"):
        await ActionExecutor(sim, profile).execute("CRS_PUSH")
    assert sim.mutations == [("command", "sim/radios/obs_HSI_direct")]


async def test_cancelled_action_is_not_retried_or_wrapped(c90):
    profile, sim = c90
    sim.failures[VS_PRESELECT] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await ActionExecutor(sim, profile).execute("ALT_SEL")
    assert sim.mutations == [("command", VS_PRESELECT)]
