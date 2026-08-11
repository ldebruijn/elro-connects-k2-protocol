"""Out-of-band SubDevice state must survive frames that rebuild the device.

CO2/TH measurements arrive one field at a time on 6-char CMD_CODE 19 pushes and
nicknames come from CMD_CODE 24 -> 17.  Neither a 14-char CMD_CODE 55 sync
record nor an 8-char status push carries any of them, so a naive rebuild drops
the accumulated values and HA entities flip to "unknown" -- visible as gaps in
the history graph every time the hub re-syncs.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.models import AlarmState, SubDevice

# sub 3, type 0018 (CO2/TH), signal 4, battery 0x57, alarm "AA" (clear)
SYNC_RECORD_CO2_TH = "03" + "0018" + "4" + "5" + "57" + "AA" + "55"
PUSH_DATA_STR1 = "00030018"

# 6-char measurement pushes: "00"+temp, "04"+humidity, "08"+co2
MEASUREMENT_PUSHES = ("000225", "0401E0", "08028A")
EXPECTED_TEMP_C = 24.9
EXPECTED_HUMIDITY_PCT = 48.0
EXPECTED_CO2_PPM = 650


def _frame(data_str1: str, data_str2: str) -> dict[str, Any]:
    return {"msg": {"CMD_CODE": 19, "data_str1": data_str1, "data_str2": data_str2}}


def _gateway_with_measurements() -> K2Gateway:
    """A gateway holding a CO2/TH device with all measurements and a nickname."""
    gw = K2Gateway("127.0.0.1", "TEST_DEVICE")
    gw._on_sync_response(_frame(SYNC_RECORD_CO2_TH, ""))
    gw._devices[3] = dataclasses.replace(gw._devices[3], nickname="Living room")
    for data_str2 in MEASUREMENT_PUSHES:
        gw._on_co2_th_push(_frame(PUSH_DATA_STR1, data_str2))
    return gw


def _assert_measurements_intact(device: SubDevice) -> None:
    assert device.temperature_c == EXPECTED_TEMP_C
    assert device.humidity_pct == EXPECTED_HUMIDITY_PCT
    assert device.co2_ppm == EXPECTED_CO2_PPM
    assert device.nickname == "Living room"


def test_measurements_accumulate_across_pushes() -> None:
    _assert_measurements_intact(_gateway_with_measurements()._devices[3])


def test_unsolicited_resync_preserves_measurements() -> None:
    """The hub's periodic CMD_CODE 55 must not wipe accumulated state."""
    gw = _gateway_with_measurements()
    gw._on_sync_response(_frame(SYNC_RECORD_CO2_TH, ""))
    _assert_measurements_intact(gw._devices[3])


def test_status_push_preserves_measurements() -> None:
    """An 8-char alarm/status push must not wipe accumulated state."""
    gw = _gateway_with_measurements()
    gw._on_push_update(_frame(PUSH_DATA_STR1, "04557AA5"))
    _assert_measurements_intact(gw._devices[3])


def test_status_push_still_updates_alarm_state() -> None:
    """Retention must not mask genuinely new values from the incoming frame."""
    gw = _gateway_with_measurements()
    before = gw._devices[3].alarm_state
    gw._on_push_update(_frame(PUSH_DATA_STR1, "0455" + "5500"))
    after = gw._devices[3]
    assert after.alarm_state != before
    assert after.alarm_state == AlarmState.ALARM
    _assert_measurements_intact(after)


def test_newer_measurement_overrides_retained_value() -> None:
    """A fresh measurement wins over the retained one."""
    gw = _gateway_with_measurements()
    gw._on_co2_th_push(_frame(PUSH_DATA_STR1, "08" + format(1200, "04X")))
    assert gw._devices[3].co2_ppm == 1200
    assert gw._devices[3].temperature_c == EXPECTED_TEMP_C
