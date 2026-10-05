from copy import deepcopy

import pytest
from pydantic import ValidationError

from ap_panel.config import Hardware, Settings, load_hardware, read_yaml
from ap_panel.profiles import Profile, load_profiles


@pytest.fixture
def hardware_data():
    return load_hardware().model_dump()


@pytest.fixture
def profile_data():
    return {profile.id: profile.model_dump() for profile in load_profiles()}


def test_actual_hardware_preserves_complete_inventory_and_reversed_uln():
    hardware = load_hardware()
    assert [pin.index for pin in hardware.inputs] == list(range(16))
    assert [pin.name for pin in hardware.inputs] == [
        "AP",
        "FD",
        "HDG",
        "NAV",
        "APR",
        "ALT_SEL",
        "ALT",
        "VS",
        "YD",
        "CDI",
        "HDG_PUSH",
        "ALT_PUSH",
        "VS_PUSH",
        "CRS_PUSH",
        "BARO_PUSH",
        "GEAR",
    ]
    assert [pin.index for pin in hardware.outputs] == list(range(24))
    assert hardware.assigned_mask == (1 << 19) - 1
    for pin in hardware.outputs:
        assert pin.uln_input == 8 - pin.q
        assert pin.uln_output == 11 + pin.q
        assert pin.reserved is (pin.index >= 19)
    assert len(hardware.records()) == 9
    assert hardware.records()[-3:] == [
        "12.14.15.16.2.IN:",
        "10.19.18.17.3.OUT:",
        "7.39.20.4.LCD:",
    ]
    assert "U2 Q0 / 15 | IC3 8 / 11 | JP31" in hardware.pinout_markdown()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data.update(unknown_option=True),
        lambda data: data["encoders"][0].update(gpio_a="0"),
        lambda data: data["encoders"][0].update(gpio_a=data["encoders"][1]["gpio_a"]),
        lambda data: data["encoders"][0].update(decoder_type=5),
        lambda data: data["encoders"][1].update(connector=data["encoders"][0]["connector"]),
        lambda data: data["encoders"][0].update(name="bad/name"),
        lambda data: data["input_chain"].update(chips=3),
        lambda data: data["output_chain"].update(chips=2),
        lambda data: data["lcd"].update(address=40),
        lambda data: data["inputs"][0].update(active_low=False),
        lambda data: data["inputs"][0].update(name="UNUSED_BUTTON"),
        lambda data: data["inputs"][0].update(channel="B"),
        lambda data: data["inputs"][0].update(physical_pin=12),
        lambda data: data["inputs"][0].update(chip="IC2"),
        lambda data: data["inputs"][1].update(index=0),
        lambda data: data["inputs"][1].update(connector=data["inputs"][0]["connector"]),
        lambda data: data["outputs"][0].update(uln_input=1),
        lambda data: data["outputs"][0].update(name="UNUSED_LAMP"),
        lambda data: data["outputs"][0].update(uln_output=18),
        lambda data: data["outputs"][0].update(physical_pin=1),
        lambda data: data["outputs"][0].update(q=1),
        lambda data: data["outputs"][0].update(uln_chip="IC4"),
        lambda data: data["outputs"][19].update(reserved=False),
        lambda data: data["outputs"].pop(),
    ],
)
def test_hardware_rejects_invalid_schema_inventory_and_chip_mapping(hardware_data, mutation):
    mutation(hardware_data)
    with pytest.raises(ValidationError):
        Hardware.model_validate(hardware_data)


@pytest.mark.parametrize(
    "section,key",
    [
        ("encoder", "physical_a"),
        ("encoder", "physical_b"),
        ("input_chain", "physical_load"),
        ("input_chain", "physical_clock"),
        ("input_chain", "physical_data"),
        ("output_chain", "physical_data"),
        ("output_chain", "physical_clock"),
        ("output_chain", "physical_latch"),
    ],
)
def test_pico_physical_pin_must_match_gpio(hardware_data, section, key):
    entry = hardware_data["encoders"][0] if section == "encoder" else hardware_data[section]
    entry[key] = 3  # A ground pin, never the corresponding GPIO.
    with pytest.raises(ValidationError, match="Pico 2 header"):
        Hardware.model_validate(hardware_data)


def test_known_logical_names_can_be_reassigned_to_measured_serial_positions(hardware_data):
    hardware_data["inputs"][0]["name"], hardware_data["inputs"][1]["name"] = "FD", "AP"
    hardware_data["outputs"][0]["name"], hardware_data["outputs"][1]["name"] = "FD", "AP"
    hardware = Hardware.model_validate(hardware_data)
    assert hardware.inputs[0].name == "FD" and hardware.outputs[1].name == "AP"


def test_active_lamp_cannot_be_hidden_in_reserved_output_position(hardware_data):
    hardware_data["outputs"][0]["name"], hardware_data["outputs"][19]["name"] = "SPARE_1", "AP"
    with pytest.raises(ValidationError, match="spares"):
        Hardware.model_validate(hardware_data)


@pytest.mark.parametrize("gpio", [23, 24, 25])
def test_internal_pico_gpio_cannot_be_assigned_to_header(hardware_data, gpio):
    hardware_data["encoders"][0].update(gpio_a=gpio, physical_a=30)
    with pytest.raises(ValidationError, match="Pico 2 header"):
        Hardware.model_validate(hardware_data)


@pytest.mark.parametrize(
    "source", ["schema_version: 1\nschema_version: 2\n", "outer:\n  pin: 1\n  pin: 2\n"]
)
def test_yaml_duplicate_keys_are_errors(tmp_path, source):
    path = tmp_path / "duplicate.yaml"
    path.write_text(source)
    with pytest.raises(ValueError, match="Duplicate YAML key"):
        read_yaml(path)


@pytest.mark.parametrize("source", ["[]", "- first", "null"])
def test_yaml_root_requires_mapping(tmp_path, source):
    path = tmp_path / "invalid.yaml"
    path.write_text(source)
    with pytest.raises(ValueError, match="YAML mapping"):
        read_yaml(path)


@pytest.mark.parametrize(
    "origin",
    [
        "https://localhost:8086",
        "http://example.com:8086",
        "http://192.168.1.10:8086",
        "http://user:password@localhost:8086",
        "http://localhost:8086/api/v2",
        "http://localhost/?secret=x",
        "http://localhost/#fragment",
        "http://localhost:abc",
        "http://localhost:99999",
        "http://localhost:0",
    ],
)
def test_simulator_settings_require_valid_local_origin(origin):
    with pytest.raises(ValidationError):
        Settings(simulator_url=origin)


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8086", "http://localhost:8088/", "http://[::1]:8086"]
)
def test_loopback_custom_port_is_supported(origin):
    assert Settings(simulator_url=origin).simulator_url == origin


def test_logging_preferences_defaults_and_valid_custom_values():
    settings = Settings()
    assert (
        settings.log_level,
        settings.log_directory,
        settings.log_max_bytes,
        settings.log_backup_count,
    ) == (
        "INFO",
        ".ap-panel",
        2_000_000,
        3,
    )
    custom = Settings(
        log_level="DEBUG", log_directory="logs/custom", log_max_bytes=4096, log_backup_count=0
    )
    assert custom.log_directory == "logs/custom" and custom.log_backup_count == 0


@pytest.mark.parametrize(
    "settings",
    [
        {"log_level": "TRACE"},
        {"log_max_bytes": 1023},
        {"log_max_bytes": "2000000"},
        {"log_backup_count": -1},
        {"log_backup_count": 11},
        {"log_directory": 1},
    ],
)
def test_invalid_logging_preferences_are_rejected(settings):
    with pytest.raises(ValidationError):
        Settings(**settings)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data.update(unsupported_setting=True),
        lambda data: data.update(simulator_tested=True, tested_aircraft_path=None),
        lambda data: data.update(gear_verified=True, gear_indices={}),
        lambda data: data.update(gear_indices={"L": 0, "N": 0, "R": 1}),
        lambda data: data.update(gear_indices={"L": 0, "N": 1, "R": 10}),
        lambda data: data.update(
            gear_verified=True, gear_indices={"L": 0, "N": 1, "R": 2}, datarefs={}
        ),
        lambda data: data["datarefs"]["gear"].update(value_type="numeric"),
        lambda data: data.update(flap_detents={"UP": 0.0, "1": 0.03, "FULL": 1.0}),
        lambda data: data.update(flap_detents={"UP": 1.0, "1": 0.5, "FULL": 0.0}),
        lambda data: data.update(flap_detents={"UP": 0.0, "1": 0.5, "FULL": float("nan")}),
        lambda data: data.update(flap_detents={"UP": 0.0, "1": 0.5, "2": 0.7, "FULL": 1.0}),
        lambda data: data.update(flaps_verified=True, flap_detents={"UP": 0.0, "1": 0.5}),
        lambda data: data.update(yd_engaged_verified=True, yd_engaged_key="missing"),
        lambda data: data.update(altitude_preselection=False),
        lambda data: data["datarefs"]["heading"].update(reference_verified=False),
        lambda data: data["datarefs"]["heading"].update(path=""),
        lambda data: data["actions"]["HDG"].update(requires=["unknown"]),
        lambda data: data["actions"]["VS_PUSH"].update(value=float("inf")),
        lambda data: data["actions"]["VS_PUSH"].update(dataref="heading"),
        lambda data: data["disabled"].update(AP="Must not override an active action"),
    ],
)
def test_profiles_reject_contradictory_capabilities_and_bad_calibration(profile_data, mutation):
    data = deepcopy(profile_data["c90b"])
    data.update(gear_verified=True, gear_indices={"L": 0, "N": 1, "R": 2})
    mutation(data)
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_c172_flap_calibration_requires_all_four_positions(profile_data):
    data = profile_data["c172-analog"]
    data.update(flaps_verified=True, flap_detents={"UP": 0.0, "1": 0.5, "FULL": 1.0})
    with pytest.raises(ValidationError, match="all four"):
        Profile.model_validate(data)


def test_profile_matches_exact_aircraft_and_corrected_tested_path(profile_data):
    c172 = Profile.model_validate(profile_data["c172-analog"])
    path = c172.aircraft_paths[0]
    assert c172.compatible(path.replace("/", "\\"), 3)
    assert not c172.compatible(path, 2)
    assert not c172.matches(path.replace("Cessna_172SP", "Cessna_172SP_G1000"))
    data = profile_data["c172-analog"]
    data.update(simulator_tested=True, tested_aircraft_path="Aircraft/My installed C172/analog.acf")
    corrected = Profile.model_validate(data)
    assert corrected.matches("Aircraft/My installed C172/analog.acf")
    assert not corrected.matches(path)


def test_profile_source_manifest_matches_commands_and_capabilities(profile_data):
    c172, c90 = (Profile.model_validate(profile_data[key]) for key in ("c172-analog", "c90b"))
    assert c172.datarefs["course"].path == "sim/cockpit2/radios/actuators/nav1_obs_deg_mag_pilot"
    assert c172.actions["CRS_CW"].command == "sim/radios/obs1_up"
    assert c90.datarefs["course"].path == "sim/cockpit2/radios/actuators/hsi_obs_deg_mag_pilot"
    assert c90.actions["CRS_CW"].command == "sim/radios/obs_HSI_up"
    assert not c172.altitude_preselection
    assert {
        "ALT_SEL",
        "ALT_CW",
        "ALT_CCW",
        "ALT_PUSH",
        "FD",
        "YD",
        "GEAR_UP",
        "GEAR_DOWN",
    } <= c172.disabled.keys()
    assert c90.altitude_preselection and c90.actions["ALT_SEL"].kind == "alt_sel"
    assert "sim/autopilot/vertical_speed_pre_sel" in c90.command_paths()
    assert "sim/autopilot/altitude_arm" in c90.command_paths()
    assert c90.datarefs["flap_actual"].path == "sim/cockpit2/controls/flap_system_deploy_ratio"
    assert c90.datarefs["flap_actual"].value_type == "float"
    assert c90.datarefs["nav_status"].value_type == "int"


def test_per_binding_measurement_metadata_is_preserved_without_claiming_whole_profile(profile_data):
    data = profile_data["c90b"]
    data["datarefs"]["heading"]["simulator_tested"] = True
    data["actions"]["HDG"]["simulator_tested"] = True
    profile = Profile.model_validate(data)
    assert not profile.simulator_tested
    assert profile.datarefs["heading"].simulator_tested
    assert profile.actions["HDG"].simulator_tested
    assert not profile.datarefs["vs"].simulator_tested
    assert not profile.actions["VS"].simulator_tested
    restored = Profile.model_validate(profile.model_dump())
    assert restored.datarefs["heading"].simulator_tested
    assert restored.actions["HDG"].simulator_tested
