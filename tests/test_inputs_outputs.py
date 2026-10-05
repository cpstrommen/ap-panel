from copy import deepcopy

import pytest

from ap_panel.board import BoardEvent
from ap_panel.config import Hardware, load_hardware
from ap_panel.inputs import InputProcessor, action_name
from ap_panel.models import PanelFrame, TelemetryValue
from ap_panel.outputs import OutputRenderer
from ap_panel.profiles import Profile, interpret, load_profiles


@pytest.fixture
def hardware():
    return load_hardware()


@pytest.fixture
def profiles():
    return {profile.id: profile for profile in load_profiles()}


def button(index, pressed, timestamp):
    return BoardEvent("button", "IN", index, 0 if pressed else 1, timestamp)


def telemetry(**values):
    return {key: TelemetryValue(value) for key, value in values.items()}


def lit(frame, hardware):
    return {pin.name for pin in hardware.outputs if frame.lamps & (1 << pin.index)}


def calibrated(profile):
    data = deepcopy(profile.model_dump())
    data.update(
        gear_verified=True,
        gear_indices={"L": 2, "N": 0, "R": 4},
        flaps_verified=True,
        flap_detents={"UP": 0.0, "1": 0.5, "FULL": 1.0},
    )
    if profile.id == "c172-analog":
        data["flap_detents"] = {"UP": 0.0, "1": 0.33, "2": 0.67, "FULL": 1.0}
    return Profile.model_validate(data)


def test_snapshot_requires_all_sixteen_levels(hardware):
    processor = InputProcessor(hardware)
    with pytest.raises(ValueError, match="All 16"):
        processor.reset({0: False}, 1)


def test_startup_held_momentary_must_release_and_press_again(hardware):
    processor = InputProcessor(hardware)
    processor.reset({index: index == 0 for index in range(16)}, 7)
    assert processor.flush(10) == []
    assert processor.feed(button(0, True, 10)) == []
    assert processor.flush(10.1) == []
    processor.feed(button(0, False, 11))
    assert processor.flush(11.04) == []
    processor.feed(button(0, True, 12))
    (event,) = processor.flush(12.04)
    assert (event.control, event.value, event.generation) == ("AP", True, 7)


def test_stable_debounce_cancels_bounced_press_and_release(hardware):
    processor = InputProcessor(hardware)
    processor.reset(dict.fromkeys(range(16), False), 1)
    processor.feed(button(2, True, 0.0))
    assert processor.flush(0.02) == []
    processor.feed(button(2, False, 0.021))
    assert processor.flush(0.1) == []
    processor.feed(button(2, True, 0.2))
    (event,) = processor.flush(0.231)
    assert event.control == "HDG"
    processor.feed(button(2, False, 0.3))
    processor.feed(button(2, True, 0.31))
    assert processor.flush(0.4) == []
    assert processor.states["HDG"] is True
    processor.feed(button(2, False, 0.5))
    assert processor.flush(0.531) == []


@pytest.mark.parametrize("index", range(16))
def test_every_serial_input_maps_to_its_control_once(hardware, index):
    processor = InputProcessor(hardware)
    processor.reset(dict.fromkeys(range(16), False), 9)
    processor.feed(button(index, True, 1))
    processor.feed(button(index, True, 1.01))  # Repetition must not restart the stable timer.
    (event,) = processor.flush(1.031)
    assert event.control == hardware.inputs[index].name
    assert event.value is True
    assert event.generation == 9
    processor.feed(button(index, True, 2))
    assert processor.flush(2.1) == []


@pytest.mark.parametrize("initial", [False, True])
def test_gear_has_no_startup_action_and_emits_subsequent_absolute_edges(hardware, initial):
    processor = InputProcessor(hardware)
    snapshot = dict.fromkeys(range(16), False)
    snapshot[15] = initial
    processor.reset(snapshot, 2)
    assert processor.flush(0.1) == []
    processor.feed(button(15, not initial, 1))
    (event,) = processor.flush(1.04)
    assert action_name(event) == ("GEAR_UP" if initial else "GEAR_DOWN")
    processor.feed(button(15, initial, 2))
    (event,) = processor.flush(2.04)
    assert action_name(event) == ("GEAR_DOWN" if initial else "GEAR_UP")


@pytest.mark.parametrize("code,direction", [(0, -1), (1, -1), (2, 1), (3, 1)])
@pytest.mark.parametrize("inverted", [False, True])
def test_encoder_fast_modes_are_one_detent_with_calibrated_direction(
    hardware, code, direction, inverted
):
    data = hardware.model_dump()
    data["encoders"][0]["inverted"] = inverted
    processor = InputProcessor(Hardware.model_validate(data))
    processor.reset(dict.fromkeys(range(16), False), 3)
    (event,) = processor.feed(BoardEvent("encoder", "HDG", None, code, 10))
    expected = direction * (-1 if inverted else 1)
    assert event.value == expected
    assert action_name(event) == ("HDG_CW" if expected == 1 else "HDG_CCW")
    assert processor.flush(10.1) == []


def test_reset_discards_pending_edges_and_old_held_state(hardware):
    processor = InputProcessor(hardware)
    processor.reset(dict.fromkeys(range(16), False), 1)
    processor.feed(button(0, True, 0))
    processor.reset(dict.fromkeys(range(16), False), 2)
    assert processor.flush(1) == []
    processor.feed(button(0, True, 2))
    assert processor.flush(2.04)[0].generation == 2


def test_mode_feedback_can_arm_nav_while_heading_remains_active(hardware, profiles):
    renderer = OutputRenderer(hardware)
    values = telemetry(hdg_status=2, nav_status=1, source=2)
    bright = renderer.render(profiles["c90b"], values, 1.1)
    dim = renderer.render(profiles["c90b"], values, 1.6)
    assert {"HDG", "NAV", "GPS"} <= lit(bright, hardware)
    assert "HDG" in lit(dim, hardware)
    assert "NAV" not in lit(dim, hardware)
    assert bright.lamps & ~hardware.assigned_mask == 0


def test_gpss_arming_also_blinks_shared_nav_lamp(hardware, profiles):
    renderer = OutputRenderer(hardware)
    values = telemetry(hdg_status=2, nav_status=0, gpss_status=1, source=2)
    assert {"NAV", "HDG", "GPS"} <= lit(renderer.render(profiles["c90b"], values, 0.1), hardware)
    assert "NAV" not in lit(renderer.render(profiles["c90b"], values, 0.6), hardware)


def test_altitude_arm_to_capture_uses_feedback_not_button_latches(hardware, profiles):
    renderer = OutputRenderer(hardware)
    profile = profiles["c90b"]
    armed = renderer.render(
        profile, telemetry(alt_armed=1, alt_status=0, selected_altitude=7000), 2.1
    )
    assert "ALT_SEL" in lit(armed, hardware) and "ALT" not in lit(armed, hardware)
    assert "ALTSEL ARM" in armed.rows[3]
    captured = renderer.render(
        profile, telemetry(alt_armed=0, alt_status=2, selected_altitude=7000), 2.2
    )
    assert "ALT" in lit(captured, hardware) and "ALT_SEL" not in lit(captured, hardware)
    assert "ALT HOLD" in captured.rows[3]


def test_glideslope_armed_is_lcd_only_and_capture_lights_lamp(hardware, profiles):
    renderer = OutputRenderer(hardware)
    armed = renderer.render(profiles["c90b"], telemetry(gs_status=1), 0.1)
    assert "GS ARM" in armed.rows[3]
    assert "GS_CAP" not in lit(armed, hardware)
    captured = renderer.render(profiles["c90b"], telemetry(gs_status=2), 0.2)
    assert "GS CAP" in captured.rows[3]
    assert "GS_CAP" in lit(captured, hardware)


def test_generic_yaw_damper_selection_does_not_claim_engagement(hardware, profiles):
    profile = profiles["c90b"]
    values = telemetry(yd_selected=1)
    assert interpret(profile, values).yd_selection is True
    assert "YD" not in lit(OutputRenderer(hardware).render(profile, values, 0.1), hardware)


def test_unmeasured_gear_and_flap_feedback_keeps_annunciators_off(hardware, profiles):
    values = telemetry(gear=[1.0] * 10, flap_actual=1.0)
    renderer = OutputRenderer(hardware)
    renderer.render(profiles["c90b"], values, 0)
    names = lit(renderer.render(profiles["c90b"], values, 1), hardware)
    assert not any(name.startswith(("GEAR_", "FLAPS_")) for name in names)


def test_verified_gear_uses_measured_indices_and_partial_feedback(hardware, profiles):
    profile = calibrated(profiles["c90b"])
    renderer = OutputRenderer(hardware)
    # Measured order is N at0, L at2, R at4. Only deployed positions turn green.
    frame = renderer.render(profile, telemetry(gear=[1.0, 1.0, 0.995, 1.0, 0.4]), 0.1)
    assert {"GEAR_N", "GEAR_L"} <= lit(frame, hardware)
    assert "GEAR_R" not in lit(frame, hardware)
    short = renderer.render(profile, telemetry(gear=[1.0]), 0.2)
    assert lit(short, hardware) == {"GEAR_N"}


@pytest.mark.parametrize("bad_ratio", [float("nan"), float("inf"), -0.1, 1.1, True, "1"])
def test_invalid_gear_ratio_never_lights_green(hardware, profiles, bad_ratio):
    profile = calibrated(profiles["c90b"])
    frame = OutputRenderer(hardware).render(profile, telemetry(gear=[bad_ratio] * 10), 0.1)
    assert not any(name.startswith("GEAR_") for name in lit(frame, hardware))


def test_flaps_require_settle_and_clear_immediately_in_transit(hardware, profiles):
    profile = calibrated(profiles["c90b"])
    renderer = OutputRenderer(hardware)
    deployed = telemetry(flap_actual=0.5)
    assert "FLAPS_1" not in lit(renderer.render(profile, deployed, 1.0), hardware)
    assert "FLAPS_1" not in lit(renderer.render(profile, deployed, 1.099), hardware)
    assert "FLAPS_1" in lit(renderer.render(profile, deployed, 1.101), hardware)
    transit = renderer.render(profile, telemetry(flap_actual=0.7), 1.11)
    assert not any(name.startswith("FLAPS_") for name in lit(transit, hardware))
    assert "FLAPS_FULL" not in lit(
        renderer.render(profile, telemetry(flap_actual=1.0), 1.2), hardware
    )
    full = renderer.render(profile, telemetry(flap_actual=1.0), 1.301)
    assert "FLAPS_FULL" in lit(full, hardware)
    assert "FLAPS_2" not in lit(full, hardware)


def test_c172_all_four_measured_flap_positions(hardware, profiles):
    profile = calibrated(profiles["c172-analog"])
    for label, ratio in profile.flap_detents.items():
        renderer = OutputRenderer(hardware)
        renderer.render(profile, telemetry(flap_actual=ratio), 0)
        frame = renderer.render(profile, telemetry(flap_actual=ratio), 0.101)
        assert lit(frame, hardware) == {"FLAPS_" + label}


def test_actual_feedback_not_commanded_handle_drives_flap_lamps(hardware, profiles):
    profile = calibrated(profiles["c90b"])
    renderer = OutputRenderer(hardware)
    values = telemetry(flap_actual=0.0, flap_requested=1.0)
    renderer.render(profile, values, 0)
    assert lit(renderer.render(profile, values, 0.101), hardware) == {"FLAPS_UP"}


def test_all_24_output_bits_respect_reservations(hardware, profiles):
    profile = calibrated(profiles["c90b"])
    values = telemetry(
        ap=1,
        fd_mode=2,
        fd_selected=1,
        hdg_status=2,
        nav_status=2,
        apr_status=2,
        alt_armed=1,
        alt_status=2,
        vs_status=2,
        gs_status=2,
        source=2,
        gear=[1.0] * 10,
        flap_actual=1.0,
        yd_selected=1,
    )
    renderer = OutputRenderer(hardware)
    renderer.render(profile, values, 1)
    frame = renderer.render(profile, values, 1.101)
    expected = {
        "AP",
        "FD",
        "HDG",
        "NAV",
        "APR",
        "ALT_SEL",
        "ALT",
        "VS",
        "GPS",
        "GS_CAP",
        "GEAR_L",
        "GEAR_N",
        "GEAR_R",
        "FLAPS_FULL",
    }
    assert lit(frame, hardware) == expected
    for pin in hardware.outputs:
        assert bool(frame.lamps & (1 << pin.index)) is (pin.name in expected)


def test_lcd_is_exact_20x4_and_marks_overflow(hardware, profiles):
    values = telemetry(
        heading=721, course=-1, selected_altitude=100000, vs=-100000, baro=100.0, source=0
    )
    frame = OutputRenderer(hardware).render(profiles["c90b"], values, 0)
    assert len(frame.text) == 80 and all(len(row) == 20 for row in frame.rows)
    assert "HDG 001" in frame.rows[0] and "CRS 359" in frame.rows[0]
    assert "ALT #####" in frame.rows[1] and "VS #####" in frame.rows[1]
    assert "BARO #####" in frame.rows[2]


def test_c172_display_uses_held_altitude_only_during_hold(hardware, profiles):
    renderer = OutputRenderer(hardware)
    values = telemetry(alt_status=2, selected_altitude=12000, held_altitude=3200)
    held = renderer.render(profiles["c172-analog"], values, 0)
    assert "ALT 03200" in held.rows[1] and "ALT HOLD" in held.rows[3]
    assert "ALT_SEL" not in lit(held, hardware)
    values["alt_status"] = TelemetryValue(0)
    idle = renderer.render(profiles["c172-analog"], values, 0.1)
    assert "ALT -----" in idle.rows[1]
    assert "ALT HOLD --" in idle.rows[3]


def test_invalid_or_missing_feedback_uses_placeholders_and_dark_lamps(hardware, profiles):
    values = {
        "ap": TelemetryValue(1, valid=False),
        "heading": TelemetryValue(float("nan")),
        "source": TelemetryValue(2, valid=False),
        "alt_status": TelemetryValue(99),
    }
    frame = OutputRenderer(hardware).render(profiles["c90b"], values, 0)
    assert frame.lamps == 0
    assert "HDG ---" in frame.rows[0] and "CDI ----" in frame.rows[2]


def test_panel_frame_rejects_unrenderable_dimensions_and_text():
    with pytest.raises(ValueError, match="exactly 20"):
        PanelFrame(0, ("short", " " * 20, " " * 20, " " * 20))
    with pytest.raises(ValueError, match="printable ASCII"):
        PanelFrame(0, ("é" * 20, " " * 20, " " * 20, " " * 20))
