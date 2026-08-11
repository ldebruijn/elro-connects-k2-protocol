"""Tests for status field decoding — confirmed against live hardware 2026-07-10."""

from __future__ import annotations

import pytest

from elro_connects_k2_protocol.models import AlarmState, ThermostatMode
from elro_connects_k2_protocol.parser import (
    decode_device_status,
    decode_thermostat_status,
    normalize_type,
)

# ── deviceStatus decoding ─────────────────────────────────────────────────────

@pytest.mark.parametrize("device_status,expected_signal,expected_battery,expected_alarm", [
    # Confirmed live values (2026-07-10), three GS559A detectors:
    ("0457AA55", 4, 87, AlarmState.CLEAR),
    ("035FAA34", 3, 95, AlarmState.CLEAR),
    ("035FAA9E", 3, 95, AlarmState.CLEAR),
    # Boundary and edge cases:
    ("0455AA00", 4, 85, AlarmState.CLEAR),   # full battery, clear
    ("010055FF", 1, 0,  AlarmState.ALARM),   # lowest signal, no battery, alarming
    ("0200500F", 2, 0,  AlarmState.SILENCED),
    ("0411BB00", 4, 17, AlarmState.ALERT),   # test/alert state
    ("041111FF", 4, 17, AlarmState.FAULT),
    ("0400ZZZZ", 4, 0,  AlarmState.UNKNOWN), # garbage alarm hex
    # Door/contact sensor open states (GS320 series):
    ("0457A055", 4, 87, AlarmState.OPEN),          # A0 = primary open encoding
    ("045766FF", 4, 87, AlarmState.OPEN_VARIANT),  # 66 = alternate open encoding
])
def test_decode_device_status(
    device_status: str,
    expected_signal: int,
    expected_battery: int,
    expected_alarm: AlarmState,
) -> None:
    signal, battery, alarm = decode_device_status(device_status)
    assert signal == expected_signal, f"signal mismatch for {device_status!r}"
    assert battery == expected_battery, f"battery mismatch for {device_status!r}"
    assert alarm == expected_alarm, f"alarm mismatch for {device_status!r}"


def test_battery_msb_strip() -> None:
    # If the high bit of the battery byte is set, strip it (getQuantityStatus behaviour)
    # 0xFF = 0b11111111 → 0x7F = 127
    _signal, battery, _ = decode_device_status("04FF" + "AA00")
    assert battery == 127

    # 0x87 = 0b10000111 → 0x07 = 7
    _signal2, battery2, _ = decode_device_status("0487" + "AA00")
    assert battery2 == 7


def test_signal_table_boundaries() -> None:
    # Values outside the known table map to 0
    signal, _, _ = decode_device_status("0557AA00")
    assert signal == 0

    _, _, _ = decode_device_status("FF57AA00")
    # No crash; signal is 0 (unknown)


def test_short_status_returns_unknown() -> None:
    signal, battery, alarm = decode_device_status("04")
    assert alarm == AlarmState.UNKNOWN
    assert signal == 0
    assert battery == 0


# ── decode_thermostat_status ─────────────────────────────────────────────────
#
# raw_status[4:6] is the status byte for GS361 thermostat (type 215).
# We use a minimal 6-char string "0000XX" where XX is the byte under test.

@pytest.mark.parametrize("raw_status,expected_setpoint,expected_valve,expected_window", [
    ("000014", 20.0, False, False),  # 0x14=20, no flags
    ("000040", 0.0,  True,  False),  # 0x40: valve open
    ("000080", 0.0,  False, True),   # 0x80: window open
    ("000020", 0.5,  False, False),  # 0x20: half-degree only
    ("000035", 21.5, False, False),  # 0x35: bits0-4=21, bit5=1 → 21.5
    ("0000E4", 4.5,  True,  True),   # 0xE4: bit7+bit6+bit5, bits0-4=4 → 4.5
    ("00001F", 31.0, False, False),  # 0x1F: max setpoint floor (31 °C)
    ("00003F", 31.5, False, False),  # 0x3F: max setpoint + half-degree
])
def test_decode_thermostat_status(
    raw_status: str,
    expected_setpoint: float,
    expected_valve: bool,
    expected_window: bool,
) -> None:
    result = decode_thermostat_status(raw_status)
    assert result is not None
    assert result.setpoint_c == expected_setpoint
    assert result.valve_open is expected_valve
    assert result.window_open is expected_window
    # A 6-char status has no second byte, so mode/temperature stay unset.
    assert result.mode is None
    assert result.current_temperature_c is None


def test_decode_thermostat_status_too_short() -> None:
    assert decode_thermostat_status("0000") is None
    assert decode_thermostat_status("") is None


def test_decode_thermostat_status_invalid_hex() -> None:
    assert decode_thermostat_status("0000GG") is None


def test_decode_thermostat_status_ignores_non_status_bytes() -> None:
    # Only [4:6] matters; the surrounding bytes are not decoded.
    r1 = decode_thermostat_status("FFFF14")
    r2 = decode_thermostat_status("000014")
    assert r1 == r2


# ── type normalization ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("0013", "013"),   # 4-char: drop first
    ("013",  "013"),   # 3-char: unchanged (after upper())
    ("0001", "001"),
    ("001",  "001"),
    ("01A",  "01A"),
    ("001A", "01A"),
    ("ff",   "FF"),    # 2-char: uppercased, not trimmed
])
def test_normalize_type(raw: str, expected: str) -> None:
    assert normalize_type(raw) == expected


# ── decode_thermostat_status: second status byte ─────────────────────────────
#
# raw_status[6:8] carries the operating mode in bits 0-1 and the measured room
# temperature in bits 2-7.  Test strings are "0000" + setpoint byte + this byte.

@pytest.mark.parametrize("byte3,expected_mode,expected_temp", [
    ("00", ThermostatMode.ANTI_FROST,  0.0),
    ("01", ThermostatMode.TIMER,       0.0),
    ("02", ThermostatMode.MANUAL,      0.0),
    ("03", ThermostatMode.MANUAL_TEST, 0.0),
    ("58", ThermostatMode.ANTI_FROST, 22.0),  # 0b010110_00 → temp 22, mode 0
    ("59", ThermostatMode.TIMER,      22.0),  # same temp, mode differs
    ("5A", ThermostatMode.MANUAL,     22.0),
    ("FF", ThermostatMode.MANUAL_TEST, 63.0),  # max temperature
])
def test_decode_thermostat_second_byte(
    byte3: str, expected_mode: ThermostatMode, expected_temp: float
) -> None:
    result = decode_thermostat_status("0000" + "14" + byte3)
    assert result is not None
    assert result.mode is expected_mode
    assert result.current_temperature_c == expected_temp
    # The first status byte must still decode independently.
    assert result.setpoint_c == 20.0


def test_decode_thermostat_second_byte_invalid_hex() -> None:
    """A bad second byte must not discard the valid first byte."""
    result = decode_thermostat_status("00001 4GG".replace(" ", ""))
    assert result is not None
    assert result.setpoint_c == 20.0
    assert result.mode is None
    assert result.current_temperature_c is None
