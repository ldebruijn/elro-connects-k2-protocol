"""Parser tests — golden fixture tests and edge-case unit tests.

Golden tests: every file in tests/fixtures/ that has a "parser" field is
loaded, its "input" is passed through the named parser function, and the
result is compared field-by-field against "expected". Adding a new fixture
file is all that is needed to add a new golden test case — no Python changes.

Edge-case tests: inline payloads that test specific failure modes and
boundary conditions not easily captured as real-device fixtures.
"""

from __future__ import annotations

import pytest
from conftest import all_parser_fixtures, assert_device_matches

from elro_connects_k2_protocol.device_profiles import get_profile
from elro_connects_k2_protocol.models import AlarmState, SubDevice
from elro_connects_k2_protocol.parser import (
    decode_co2_th_measurement,
    decode_device_name,
    parse_gateway_info,
    parse_push_update,
    parse_status_record,
    parse_sub_device_info_response,
    parse_sync_response,
)

_PARSERS = {
    "parse_sync_response": parse_sync_response,
    "parse_push_update": parse_push_update,
}


# ── golden fixture tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,fixture", all_parser_fixtures())
def test_fixture(filename: str, fixture: dict) -> None:  # type: ignore[type-arg]
    if fixture.get("skip"):
        pytest.skip(f"Fixture {filename} is a placeholder — fill in input/expected from live data")

    parser_name: str = fixture["parser"]
    parser = _PARSERS[parser_name]
    result = parser(fixture["input"])
    expected: dict = fixture["expected"]  # type: ignore[type-arg]

    if parser_name == "parse_sync_response":
        assert isinstance(result, dict)
        assert len(result) == len(expected), (
            f"{filename}: expected {len(expected)} devices, got {len(result)}"
        )
        for key, exp_device in expected.items():
            sub_id = int(key)
            assert sub_id in result, f"{filename}: sub_id {sub_id} missing from result"
            assert_device_matches(result[sub_id], exp_device)

    elif parser_name == "parse_push_update":
        if expected.get("sub_id") is None:
            assert result is None
        else:
            assert result is not None
            assert_device_matches(result, expected)


# ── edge cases: parse_sync_response ──────────────────────────────────────────

def test_sync_skips_deleted_type_ffff() -> None:
    result = parse_sync_response({
        "msg": {"CMD_CODE": 55, "data_str1": "01FFFF0057AA55", "data_str2": ""},
    })
    assert 1 not in result


def test_sync_skips_trailing_0000() -> None:
    result = parse_sync_response({
        "msg": {"CMD_CODE": 55, "data_str1": "0100134057" + "0000", "data_str2": ""},
    })
    assert 1 not in result


def test_sync_ignores_non_multiple_of_14() -> None:
    result = parse_sync_response({
        "msg": {"CMD_CODE": 55, "data_str1": "0100134057AA5", "data_str2": ""},
    })
    assert len(result) == 0


def test_sync_empty_fields_returns_empty() -> None:
    result = parse_sync_response({"msg": {"CMD_CODE": 55, "data_str1": "", "data_str2": ""}})
    assert result == {}


def test_sync_accepts_rev_str_field_names() -> None:
    # Some firmware versions may use rev_str* instead of data_str*
    result = parse_sync_response({
        "msg": {"CMD_CODE": 55, "rev_str1": "0100134057AA55", "rev_str2": ""},
    })
    assert 1 in result
    assert result[1].battery_pct == 87


# ── edge cases: parse_push_update ────────────────────────────────────────────

def test_push_update_8_char_status() -> None:
    device = parse_push_update({
        "msg": {"CMD_CODE": 19, "data_str1": "00010013", "data_str2": "0457AA55"},
    })
    assert device is not None
    assert device.sub_id == 1
    assert device.device_type == "013"
    assert device.signal_bars == 4
    assert device.battery_pct == 87
    assert device.alarm_state == AlarmState.CLEAR


def test_push_update_alarm_state_55() -> None:
    device = parse_push_update({
        "msg": {"CMD_CODE": 19, "data_str1": "00010013", "data_str2": "040055FF"},
    })
    assert device is not None
    assert device.alarm_state == AlarmState.ALARM


def test_push_update_null_status_gives_unknown() -> None:
    device = parse_push_update({
        "msg": {"CMD_CODE": 19, "data_str1": "00010013", "data_str2": "NULL"},
    })
    assert device is not None
    assert device.alarm_state == AlarmState.UNKNOWN
    assert device.raw_status == "NULL"


def test_push_update_missing_data_str1_returns_none() -> None:
    assert parse_push_update({"msg": {"CMD_CODE": 19, "data_str2": "0457AA55"}}) is None


def test_push_update_sub_id_zero_returns_none() -> None:
    # sub_id 0 is a gateway-level event, not a sub-device
    assert parse_push_update({
        "msg": {"CMD_CODE": 19, "data_str1": "00000013", "data_str2": "0457AA55"},
    }) is None


# ── edge cases: parse_status_record ──────────────────────────────────────────

def test_parse_status_record_live_sub1() -> None:
    device = parse_status_record("0100134057AA55")
    assert device is not None
    assert device.sub_id == 1
    assert device.battery_pct == 87
    assert device.signal_bars == 4
    assert device.alarm_state == AlarmState.CLEAR
    assert device.raw_status == "0457AA55"


def test_parse_status_record_wrong_length() -> None:
    assert parse_status_record("0100134057AA5") is None
    assert parse_status_record("0100134057AA555") is None
    assert parse_status_record("") is None


def test_parse_status_record_sub_id_zero_skipped() -> None:
    assert parse_status_record("0000134057AA55") is None


# ── decode_co2_th_measurement ────────────────────────────────────────────────

@pytest.mark.parametrize("payload,expected_field,expected_value", [
    # Temperature: tag "00", encoded as (raw - 300) / 10
    ("000140", "temperature_c", 2.0),    # 0x140=320 → 2.0 °C
    ("00012C", "temperature_c", 0.0),    # 0x12C=300 → 0.0 °C (zero-point)
    ("0000C8", "temperature_c", -10.0),  # 0x0C8=200 → -10.0 °C
    # Humidity: tag "04", raw / 10
    ("0401F4", "humidity_pct", 50.0),    # 0x1F4=500 → 50.0 %
    ("040000", "humidity_pct", 0.0),     # 0 → 0.0 %
    # CO2: tag "08", max(raw, 400)
    ("0807D0", "co2_ppm", 2000),         # 0x7D0=2000, above floor
    ("080064", "co2_ppm", 400),          # 0x064=100, clamped to floor
    ("080190", "co2_ppm", 400),          # 0x190=400, exactly at floor
])
def test_decode_co2_th_measurement_valid(
    payload: str, expected_field: str, expected_value: float
) -> None:
    result = decode_co2_th_measurement(payload)
    assert result is not None
    field, value = result
    assert field == expected_field
    assert value == expected_value


def test_decode_co2_th_measurement_unknown_tag() -> None:
    assert decode_co2_th_measurement("0C0000") is None


def test_decode_co2_th_measurement_wrong_length() -> None:
    assert decode_co2_th_measurement("0401F") is None    # 5 chars
    assert decode_co2_th_measurement("0401F400") is None  # 8 chars
    assert decode_co2_th_measurement("") is None


def test_decode_co2_th_measurement_invalid_hex() -> None:
    assert decode_co2_th_measurement("04GGGG") is None


# ── parse_sub_device_info_response ────────────────────────────────────────────

# 30-char data_str2 for CMD_CODE 66: 6 status + 3 × 6 tagged measurements + 6 tail
_SUB_INFO_STATUS = "0457AA"   # signal=4, battery=87, alarm=CLEAR
_SUB_INFO_TEMP   = "000140"   # tag "00" + 0x140=320 → 2.0 °C
_SUB_INFO_HUM    = "0401F4"   # tag "04" + 0x1F4=500 → 50.0 %
_SUB_INFO_CO2    = "080190"   # tag "08" + 0x190=400 → 400 ppm (at floor)
_SUB_INFO_TAIL   = "000000"
_DATA30 = _SUB_INFO_STATUS + _SUB_INFO_TEMP + _SUB_INFO_HUM + _SUB_INFO_CO2 + _SUB_INFO_TAIL


def _make_existing_sub(sub_id: int = 1, raw_type: str = "0018") -> SubDevice:
    device_type = raw_type[1:] if len(raw_type) == 4 else raw_type
    return SubDevice(
        sub_id=sub_id,
        raw_type=raw_type,
        device_type=device_type,
        profile=get_profile(device_type),
        signal_bars=3,
        battery_pct=50,
        alarm_state=AlarmState.CLEAR,
        raw_status="035032AA",
    )


def test_parse_sub_device_info_response_full_decode() -> None:
    obj = {"msg": {"CMD_CODE": 66, "data_str1": "00010018", "data_str2": _DATA30}}
    result = parse_sub_device_info_response(obj)
    assert result is not None
    assert result.sub_id == 1
    assert result.signal_bars == 4
    assert result.battery_pct == 87
    assert result.alarm_state == AlarmState.CLEAR
    assert result.temperature_c == 2.0
    assert result.humidity_pct == 50.0
    assert result.co2_ppm == 400


def test_parse_sub_device_info_response_reuses_existing_profile() -> None:
    existing = _make_existing_sub(sub_id=3, raw_type="0018")
    obj = {"msg": {"CMD_CODE": 66, "data_str1": "00030018", "data_str2": _DATA30}}
    result = parse_sub_device_info_response(obj, existing=existing)
    assert result is not None
    assert result.sub_id == 3
    assert result.profile is existing.profile


def test_parse_sub_device_info_response_preserves_existing_measurements() -> None:
    # When existing already has co2_ppm, a response without a CO2 tag preserves it.
    existing = _make_existing_sub()
    existing.co2_ppm = 1200
    # Build a 30-char string with junk tags (not 00/04/08) so no measurement decodes
    data_str2 = "0457AA" + "FF0000" + "FF0000" + "FF0000" + "000000"
    obj = {"msg": {"CMD_CODE": 66, "data_str1": "00010018", "data_str2": data_str2}}
    result = parse_sub_device_info_response(obj, existing=existing)
    assert result is not None
    assert result.co2_ppm == 1200


def test_parse_sub_device_info_response_wrong_data_str2_length() -> None:
    obj = {"msg": {"CMD_CODE": 66, "data_str1": "00010018", "data_str2": "0457AA"}}
    assert parse_sub_device_info_response(obj) is None


def test_parse_sub_device_info_response_sub_id_zero() -> None:
    obj = {"msg": {"CMD_CODE": 66, "data_str1": "00000018", "data_str2": _DATA30}}
    assert parse_sub_device_info_response(obj) is None


def test_parse_sub_device_info_response_missing_data_str1() -> None:
    obj = {"msg": {"CMD_CODE": 66, "data_str2": _DATA30}}
    assert parse_sub_device_info_response(obj) is None


def test_parse_sub_device_info_response_accepts_rev_str_fields() -> None:
    obj = {"msg": {"CMD_CODE": 66, "rev_str1": "00010018", "rev_str2": _DATA30}}
    result = parse_sub_device_info_response(obj)
    assert result is not None
    assert result.sub_id == 1


# ── parse_gateway_info ────────────────────────────────────────────────────────

def test_parse_gateway_info_normal() -> None:
    obj = {
        "devID": "K2_AB1234",
        "msg": {"CMD_CODE": 13, "data_str1": "payload1", "data_str2": "payload2"},
    }
    info = parse_gateway_info(obj)
    assert info is not None
    assert info.device_name == "K2_AB1234"
    assert info.raw_data_str1 == "payload1"
    assert info.raw_data_str2 == "payload2"
    assert info.ip == ""  # caller fills this in from the packet source address


def test_parse_gateway_info_missing_dev_id() -> None:
    obj = {"msg": {"CMD_CODE": 13, "data_str1": "x", "data_str2": "y"}}
    assert parse_gateway_info(obj) is None


def test_parse_gateway_info_non_string_dev_id() -> None:
    obj = {"devID": 42, "msg": {}}
    assert parse_gateway_info(obj) is None


def test_parse_gateway_info_no_msg_key_falls_back_to_obj() -> None:
    # When "msg" is absent the function uses obj itself as the message dict
    # (same obj.get("msg", obj) fallback pattern used throughout the parser).
    info = parse_gateway_info({"devID": "K2_X"})
    assert info is not None
    assert info.device_name == "K2_X"
    assert info.raw_data_str1 == ""
    assert info.raw_data_str2 == ""


# ── decode_device_name ───────────────────────────────────────────────────────

def _make_name_payload(sub_id: int, name: str) -> str:
    """Build the 36-char data_str2 the hub sends in a CMD_CODE 17 name frame.

    Mirrors CoderUtils.getAscii() from the vendor Android app:
      - encode name as GBK
      - left-pad with '@' to 15 GBK bytes
      - append '$' terminator → 16 bytes total
    """
    name_gbk = name.encode("gbk")
    padding = b"@" * (15 - len(name_gbk))
    field = padding + name_gbk + b"$"  # always 16 bytes
    return f"{sub_id:04X}" + field.hex()


def test_decode_device_name_ascii() -> None:
    result = decode_device_name(_make_name_payload(1, "Hallway"))
    assert result == (1, "Hallway")


def test_decode_device_name_full_15_byte_name() -> None:
    # A 15-char ASCII name leaves no room for '@' padding.
    name = "SmokeDetector!!"
    result = decode_device_name(_make_name_payload(3, name))
    assert result == (3, name)


def test_decode_device_name_gbk_multibyte() -> None:
    # Chinese characters are 2 GBK bytes each; verify the round-trip.
    name = "走廊"  # corridor / hallway
    result = decode_device_name(_make_name_payload(2, name))
    assert result == (2, name)


def test_decode_device_name_sub_id_encoded_correctly() -> None:
    # sub_id is the first 4 hex chars (big-endian 2-byte integer).
    result = decode_device_name(_make_name_payload(20, "Kitchen"))
    assert result is not None
    assert result[0] == 20


def test_decode_device_name_empty_name_returns_none() -> None:
    # All 15 bytes used for '@' padding → name is empty after stripping → None.
    field = b"@" * 15 + b"$"
    payload = "0001" + field.hex()
    assert decode_device_name(payload) is None


def test_decode_device_name_wrong_length_returns_none() -> None:
    assert decode_device_name(_make_name_payload(1, "X")[:-1]) is None  # 35 chars
    assert decode_device_name(_make_name_payload(1, "X") + "0") is None  # 37 chars
    assert decode_device_name("") is None


def test_decode_device_name_invalid_hex_returns_none() -> None:
    payload = "0001" + "ZZ" * 16  # non-hex characters in name field
    assert decode_device_name(payload) is None


def test_decode_device_name_no_dollar_terminator_returns_none() -> None:
    # A name field with no '$' is treated as malformed.
    field = b"@" * 15 + b"X"  # 'X' instead of '$'
    payload = "0001" + field.hex()
    assert decode_device_name(payload) is None


def test_decode_device_name_sub_id_zero_returns_none() -> None:
    assert decode_device_name(_make_name_payload(0, "Hallway")) is None


def test_parse_gateway_info_accepts_rev_str_fields() -> None:
    obj = {
        "devID": "K2_X",
        "msg": {"CMD_CODE": 13, "rev_str1": "r1", "rev_str2": "r2"},
    }
    info = parse_gateway_info(obj)
    assert info is not None
    assert info.raw_data_str1 == "r1"
    assert info.raw_data_str2 == "r2"
