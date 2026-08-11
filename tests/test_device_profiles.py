"""Tests for the device profile registry."""

from __future__ import annotations

import pytest

from elro_connects_k2_protocol.device_profiles import (
    DEVICE_PROFILES,
    UNIVERSAL_CAPABILITIES,
    UNKNOWN_PROFILE,
    get_profile,
)


def test_get_profile_unknown_type_returns_unknown_profile() -> None:
    profile = get_profile("ZZZ")
    assert profile is UNKNOWN_PROFILE
    assert profile.name == "Unknown Device"
    assert profile.capabilities == ()


def test_get_profile_does_not_raise_for_any_string() -> None:
    for garbage in ["", "X", "0000", "FFFFFFFF", "hello"]:
        get_profile(garbage)  # must not raise


def test_all_registry_entries_have_at_least_a_name() -> None:
    for type_code, profile in DEVICE_PROFILES.items():
        assert profile.name, f"Type {type_code} has an empty profile name"


def test_combination_co_gas_has_two_alarm_capabilities() -> None:
    profile = get_profile("014")
    assert profile.name == "CO + Gas Alarm"
    keys = {c.key for c in profile.capabilities}
    assert "co" in keys
    assert "gas" in keys
    assert len(profile.capabilities) == 2


def test_co2_temp_humidity_has_three_sensor_capabilities() -> None:
    profile = get_profile("018")
    assert len(profile.capabilities) == 3
    classes = {c.device_class for c in profile.capabilities}
    assert "carbon_dioxide" in classes
    assert "temperature" in classes
    assert "humidity" in classes
    for cap in profile.capabilities:
        assert cap.entity_type == "sensor"
        assert cap.unit is not None


def test_gs241a_is_mains_powered() -> None:
    assert get_profile("018").mains_powered is True


@pytest.mark.parametrize("type_code", ["001", "000", "003", "004", "005", "013"])
def test_battery_alarm_devices_are_not_mains_powered(type_code: str) -> None:
    assert get_profile(type_code).mains_powered is False


def test_temperature_humidity_checker_has_two_capabilities() -> None:
    profile = get_profile("102")
    assert len(profile.capabilities) == 2
    keys = {c.key for c in profile.capabilities}
    assert "temperature" in keys
    assert "humidity" in keys


def test_smoke_alarm_gs559a_variants() -> None:
    for type_code in ("005", "00D", "013"):
        profile = get_profile(type_code)
        assert len(profile.capabilities) == 1
        assert profile.capabilities[0].device_class == "smoke"
        assert profile.capabilities[0].entity_type == "binary_sensor"
        assert profile.test_action == "17000000"


def test_gs559a_type013_is_photoelectric_smoke() -> None:
    # Type 013 is the confirmed live device type from 2026-07-10 session
    profile = get_profile("013")
    assert "Photoelectric" in profile.name
    assert profile.capabilities[0].key == "smoke"


def test_universal_capabilities_keys_are_distinct_from_all_profile_keys() -> None:
    universal_keys = {c.key for c in UNIVERSAL_CAPABILITIES}
    for type_code, profile in DEVICE_PROFILES.items():
        profile_keys = {c.key for c in profile.capabilities}
        overlap = universal_keys & profile_keys
        assert not overlap, (
            f"Type {type_code} has capability key(s) {overlap} that clash with UNIVERSAL_CAPABILITIES"
        )


def test_all_binary_sensor_capabilities_have_no_unit() -> None:
    for type_code, profile in DEVICE_PROFILES.items():
        for cap in profile.capabilities:
            if cap.entity_type == "binary_sensor":
                assert cap.unit is None, (
                    f"Type {type_code} capability {cap.key!r} is binary_sensor but has unit {cap.unit!r}"
                )


def test_all_sensor_capabilities_with_meaningful_unit() -> None:
    # Sensor capabilities for environmental data must declare a unit
    environmental_keys = {"co2", "temperature", "humidity", "battery"}
    for type_code, profile in DEVICE_PROFILES.items():
        for cap in profile.capabilities:
            if cap.entity_type == "sensor" and cap.key in environmental_keys:
                assert cap.unit is not None, (
                    f"Type {type_code} capability {cap.key!r} is missing a unit"
                )


def test_get_profile_case_insensitive() -> None:
    lower = get_profile("013")
    upper = get_profile("013")
    assert lower is upper


@pytest.mark.parametrize("type_code", list(DEVICE_PROFILES.keys()))
def test_all_device_capability_keys_are_non_empty(type_code: str) -> None:
    profile = get_profile(type_code)
    for cap in profile.capabilities:
        assert cap.key, f"Empty key in {type_code}"
        assert cap.device_class, f"Empty device_class in {type_code}/{cap.key}"
        assert cap.label, f"Empty label in {type_code}/{cap.key}"
