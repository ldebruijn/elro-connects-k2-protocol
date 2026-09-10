"""Parse raw K2 UDP message dicts into domain objects.

All functions here are pure — they take a decoded JSON dict (or a raw
hex string) and return typed domain objects. No I/O, no side effects.
This makes them directly testable against fixture data without a gateway.

## Why there are two CMD_CODE 19 parse paths

Every K2 sub-device sends unsolicited push events via CMD_CODE 19, but the
payload shape depends on the device category:

  • All alarm/status devices (smoke, CO, gas, heat, water, PIR, door, socket,
    button, …) use an 8-char hex ``data_str2`` that encodes signal + battery +
    alarm state in a universal format.  ``parse_push_update`` handles these.

  • The CO2/Temperature/Humidity detector (device type "018") is the single
    exception.  It reports one measurement at a time in a 6-char ``data_str2``
    tagged with a 2-char type prefix ("00"=temp, "04"=humidity, "08"=CO2).
    ``decode_co2_th_measurement`` handles these.

This split is not an arbitrary design choice — it mirrors exactly what the
official Android app does in ``ReceiveHandler.uploadDeviceStatus``, which
dispatches on ``len(data_str2)`` without ever inspecting the device type:

    if len == 6  →  UPLOAD_CO2_TEMP_HUM  (one partial measurement)
    if len == 8  →  generic device refresh  (complete alarm state)

The CMD_CODE 55/56 sync path is simpler: all device types use the same
14-char record layout, so ``parse_sync_response`` / ``parse_status_record``
remain fully type-agnostic.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from elro_connects_k2_protocol.device_profiles import get_profile
from elro_connects_k2_protocol.models import (
    AlarmState,
    GatewayInfo,
    SubDevice,
    ThermostatMode,
)

_LOGGER = logging.getLogger(__name__)

_SIGNAL_TABLE: dict[str, int] = {"00": 1, "01": 1, "02": 2, "03": 3, "04": 4}

# Type codes that should never be deleted even when trailing status is 0000
_NO_DELETE_TYPES = {"0025"}


def normalize_type(raw_type: str) -> str:
    """Normalize a 4-char wire type to a 3-char profile key.

    The app's SmartDevice.getType drops the first character when the type
    string is exactly 4 characters long, so "0013" becomes "013".
    """
    s = raw_type.upper()
    return s[1:] if len(s) == 4 else s


def decode_device_status(device_status: str) -> tuple[int, int, AlarmState]:
    """Decode an 8-char deviceStatus hex string.

    Returns (signal_bars, battery_pct, alarm_state).

    Field layout (confirmed against live hardware, 2026-07-10):
      [0:2]  signal: "0" prefix + nibble A from sync record
      [2:4]  battery byte: int(hex) & 0x7F → percentage
      [4:6]  alarm state: "AA"=clear, "55"=alarm, "50"=silenced, "BB"=alert, "11"=fault
      [6:8]  sub-state: purpose unknown; logged as diagnostic only
    """
    if len(device_status) < 6:
        return 0, 0, AlarmState.UNKNOWN

    signal = _SIGNAL_TABLE.get(device_status[0:2], 0)

    try:
        battery = int(device_status[2:4], 16) & 0x7F
    except ValueError:
        battery = 0

    alarm_hex = device_status[4:6].upper()
    try:
        alarm = AlarmState(alarm_hex)
    except ValueError:
        alarm = AlarmState.UNKNOWN

    return signal, battery, alarm


def parse_status_record(record: str) -> SubDevice | None:
    """Parse one 14-char status record from a CMD_CODE 55/56 response.

    Record layout (from ReceiveHandler.uploadAllDeviceStatus):
      [0:2]   sub_id (1 byte)
      [2:6]   raw device type (2 bytes)
      [6:7]   signal nibble A
      [7:8]   room nibble B (stored as room id, not used for alarm state)
      [8:10]  battery byte
      [10:14] trailing word: alarm state [10:12] + sub-state [12:14]

    deviceStatus assembled as: "0" + record[6] + record[8:10] + record[10:14]
    """
    if len(record) != 14:
        return None

    try:
        sub_id = int(record[0:2], 16)
    except ValueError:
        return None

    if sub_id == 0:
        return None

    raw_type = record[2:6]

    # Skip deleted devices (type FFFF, or trailing 0000 unless type is 0025)
    if raw_type.upper() == "FFFF":
        return None
    trailing = record[10:14]
    if trailing.upper() == "0000" and raw_type.upper() not in _NO_DELETE_TYPES:
        return None

    device_type = normalize_type(raw_type)
    device_status = "0" + record[6] + record[8:10] + record[10:14]
    signal, battery, alarm = decode_device_status(device_status)
    profile = get_profile(device_type)

    return SubDevice(
        sub_id=sub_id,
        raw_type=raw_type,
        device_type=device_type,
        profile=profile,
        signal_bars=signal,
        battery_pct=battery,
        alarm_state=alarm,
        raw_status=device_status,
    )


def parse_sync_response(obj: dict[str, Any]) -> dict[int, SubDevice]:
    """Parse a CMD_CODE 55 or 56 response into a sub_id → SubDevice mapping.

    The K2 packs 14-char records into data_str1 and data_str2 back-to-back.
    Both fields are processed; any records that cannot be parsed are skipped
    with a debug log.
    """
    msg: Any = obj.get("msg", obj)
    devices: dict[int, SubDevice] = {}

    for field in ("data_str1", "data_str2", "rev_str1", "rev_str2"):
        value = msg.get(field) if isinstance(msg, dict) else None
        if not isinstance(value, str) or not value:
            continue
        if len(value) % 14 != 0:
            _LOGGER.debug("Skipping field %s: length %d not a multiple of 14", field, len(value))
            continue
        for offset in range(0, len(value), 14):
            record = value[offset: offset + 14]
            device = parse_status_record(record)
            if device is not None:
                devices[device.sub_id] = device

    return devices


def parse_push_update(obj: dict[str, Any]) -> SubDevice | None:
    """Parse a CMD_CODE 19 unsolicited push into a SubDevice.

    data_str1 layout (from ReceiveHandler.uploadDeviceStatus):
      [0:4]  sub_id (2 bytes)
      [4:8]  raw device type (2 bytes)
      [8:]   room id or extra status

    data_str2 variants handled here:
      "NULL"   → device offline/unknown  (returns SubDevice with UNKNOWN state)
      8 chars  → alarm/status update for all non-CO2/TH devices  (confirmed)
      10 chars → alternate status shape  (logged and skipped for now)

    The 6-char CO2/TH measurement push is intentionally NOT handled here.
    The gateway dispatches it to ``_on_co2_th_push`` before calling this
    function, using ``decode_co2_th_measurement`` instead.  See the module
    docstring for the full routing rationale.
    """
    msg: Any = obj.get("msg", obj)
    if not isinstance(msg, dict):
        return None

    data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
    data_str2: Any = msg.get("data_str2") or msg.get("rev_str2")

    if not isinstance(data_str1, str) or len(data_str1) < 8:
        return None
    if not isinstance(data_str2, str):
        return None

    try:
        sub_id = int(data_str1[0:4], 16)
    except ValueError:
        return None

    if sub_id == 0:
        return None

    raw_type = data_str1[4:8]
    device_type = normalize_type(raw_type)
    profile = get_profile(device_type)

    if data_str2.upper() == "NULL":
        return SubDevice(
            sub_id=sub_id,
            raw_type=raw_type,
            device_type=device_type,
            profile=profile,
            signal_bars=0,
            battery_pct=0,
            alarm_state=AlarmState.UNKNOWN,
            raw_status="NULL",
        )

    if len(data_str2) == 8:
        signal, battery, alarm = decode_device_status(data_str2)
        return SubDevice(
            sub_id=sub_id,
            raw_type=raw_type,
            device_type=device_type,
            profile=profile,
            signal_bars=signal,
            battery_pct=battery,
            alarm_state=alarm,
            raw_status=data_str2,
        )

    _LOGGER.debug(
        "CMD_CODE 19 sub_id=%d: unrecognised data_str2 length %d, value=%r",
        sub_id, len(data_str2), data_str2,
    )
    return None


# A CMD_CODE 62 join notification carries no status bytes — the hub only says
# which slot the device landed in and what type it is.  The vendor app
# substitutes this placeholder so the device renders immediately: "04" = 4
# signal bars, "64" = 0x64 = 100 % battery, "AA" = clear, "00" = sub-state.
# None of it is a reading; real values arrive on the next CMD_CODE 55 sync or
# status push.  See ReceiveHandler.uploadAddSubDevice.
ADD_SUB_DEVICE_PLACEHOLDER_STATUS = "0464AA00"


def parse_add_sub_device(obj: dict[str, Any]) -> SubDevice | None:
    """Parse a CMD_CODE 62 "sub-device joined" frame into a SubDevice.

    The hub emits this unsolicited after a detector completes pairing during a
    join window opened by CMD_CODE 2.

    data_str1 layout (from ReceiveHandler.uploadAddSubDevice):
      [0:4]  sub_id (2 bytes)
      [4:8]  raw device type (2 bytes)
      [8:]   room id

    The app discards payloads of 8 chars or shorter, so a frame without the
    trailing room id is not a valid join notification and is rejected here too.
    """
    msg: Any = obj.get("msg", obj)
    if not isinstance(msg, dict):
        return None

    data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
    if not isinstance(data_str1, str) or len(data_str1) <= 8:
        return None

    try:
        sub_id = int(data_str1[0:4], 16)
    except ValueError:
        return None

    if sub_id == 0:
        return None

    raw_type = data_str1[4:8]
    device_type = normalize_type(raw_type)
    signal, battery, alarm = decode_device_status(ADD_SUB_DEVICE_PLACEHOLDER_STATUS)

    return SubDevice(
        sub_id=sub_id,
        raw_type=raw_type,
        device_type=device_type,
        profile=get_profile(device_type),
        signal_bars=signal,
        battery_pct=battery,
        alarm_state=alarm,
        raw_status=ADD_SUB_DEVICE_PLACEHOLDER_STATUS,
    )


def decode_co2_th_measurement(data_str2: str) -> tuple[str, float | int] | None:
    """Decode one CO2/temp/humidity measurement from a 6-char CMD_CODE 19 push.

    The CO2/TH detector (type "018") sends one measurement at a time rather
    than a full status snapshot.  Each push carries a 2-char type tag followed
    by a 4-char hex value:

        "00" + TTTT  →  temperature in °C:  (int(TTTT, 16) − 300) / 10.0
        "04" + HHHH  →  humidity in %:       int(HHHH, 16) / 10.0
        "08" + CCCC  →  CO₂ in ppm:          max(int(CCCC, 16), 400)

    The temperature encoding uses 300 as the zero-point offset so that −10°C
    maps to 200 (0x00C8) and 50°C maps to 800 (0x0320).  The CO₂ floor of 400
    ppm matches the app's ``Math.max(value, AUTHENTICATION_INVALID)`` where
    ``AUTHENTICATION_INVALID = 400`` (Huawei AGConnect SDK constant that
    coincidentally equals outdoor ambient CO₂ level).

    Returns a ``(field_name, value)`` pair where ``field_name`` matches the
    corresponding ``SubDevice`` attribute (``"temperature_c"``,
    ``"humidity_pct"``, or ``"co2_ppm"``), or ``None`` for an unknown tag or
    parse failure.

    Decoding source: ``Detector241Activity.onRefresh241Status`` in the vendor
    Android app (confirmed against the full ``ReceiveHandler`` dispatch).
    """
    if len(data_str2) != 6:
        return None
    tag = data_str2[0:2]
    raw = data_str2[2:6]
    try:
        int_val = int(raw, 16)
    except ValueError:
        return None
    if tag == "00":
        return "temperature_c", (int_val - 300) / 10.0
    if tag == "04":
        return "humidity_pct", int_val / 10.0
    if tag == "08":
        return "co2_ppm", max(int_val, 400)
    return None


def parse_sub_device_info_response(
    obj: dict[str, Any],
    existing: SubDevice | None = None,
) -> SubDevice | None:
    """Parse a CMD_CODE 66 sub-device info response for a CO2/TH device.

    CMD_CODE 66 is the response to a CMD_CODE 16 (sub-device info) request, and
    it has two payload shapes — ``ReceiveHandler.uploadSubDeviceInfo`` branches
    on ``data_str2.length() == 30`` and treats anything else as a plain
    deviceStatus string.

    For CO2/TH devices (type "018") it carries a 30-char ``data_str2`` that
    packs signal/battery/alarm followed by all three current measurements:

        [0:6]   6-char signal/battery/alarm   (same encoding as normal 8-char
                                               deviceStatus, just without the
                                               trailing sub-state byte)
        [6:8]   "00"  temperature tag
        [8:12]  4-char temperature value
        [12:14] "04"  humidity tag
        [14:18] 4-char humidity value
        [18:20] "08"  CO₂ tag
        [20:24] 4-char CO₂ value
        [24:30] 6 unknown chars  (not decoded by the app either)

    The existing ``decode_device_status`` function handles ≥6-char strings, so
    the 6-char status prefix feeds into it directly (the app appends "FF" to
    make 8 chars for its local DB; we skip that step).

    ``existing`` is the previously known ``SubDevice`` for this sub_id.  When
    provided its ``raw_type`` and ``profile`` are reused, which avoids a
    second look-up and preserves any fields not present in this response.  If
    ``existing`` is ``None`` the type is inferred from ``data_str1[4:8]``.

    Any other length is the short form: ``data_str2`` is a deviceStatus on its
    own, carrying signal/battery/alarm and no measurements.  Every non-CO2/TH
    device answers this way, and so does a CO2/TH slot with nothing paired into
    it — the reference hub returns ``"FFFFFFFF"`` for one of those.

    Returns ``None`` if the response cannot be parsed (too short for a status,
    bad sub_id, missing fields).
    """
    msg: Any = obj.get("msg", obj)
    if not isinstance(msg, dict):
        return None

    data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
    data_str2: Any = msg.get("data_str2") or msg.get("rev_str2")

    if not isinstance(data_str1, str) or len(data_str1) < 4:
        return None
    if not isinstance(data_str2, str) or len(data_str2) < 6:
        return None

    try:
        sub_id = int(data_str1[0:4], 16)
    except ValueError:
        return None
    if sub_id == 0:
        return None

    raw_type = existing.raw_type if existing else data_str1[4:8] if len(data_str1) >= 8 else "0018"
    device_type = existing.device_type if existing else normalize_type(raw_type)
    profile = existing.profile if existing else get_profile(device_type)

    # Two payload shapes, exactly as ReceiveHandler.uploadSubDeviceInfo branches:
    # a 30-char CO2/TH payload with tagged measurements, and anything else,
    # which the app stores as a plain deviceStatus with no measurements.
    #
    # Only the 30-char form was implemented here, and the other branch returned
    # None -- which left get_sub_device_info's future unresolved and every query
    # to a non-CO2/TH device burning the full timeout despite the hub having
    # answered instantly.  The reference hub returns the short form for its
    # smoke alarms and for an empty CO2/TH slot alike.
    is_measurement_payload = len(data_str2) == 30

    # The first 6 chars are signal/battery/alarm — decode_device_status
    # accepts any string of length ≥ 6, reading only the first 6 chars.
    signal, battery, alarm = decode_device_status(data_str2[0:6])

    co2_ppm: int | None = existing.co2_ppm if existing else None
    temperature_c: float | None = existing.temperature_c if existing else None
    humidity_pct: float | None = existing.humidity_pct if existing else None

    # Walk the three tagged measurement pairs at fixed positions.
    if is_measurement_payload:
        for offset in (6, 12, 18):
            result = decode_co2_th_measurement(data_str2[offset: offset + 6])
            if result is None:
                continue
            field, value = result
            if field == "temperature_c":
                temperature_c = float(value)
            elif field == "humidity_pct":
                humidity_pct = float(value)
            elif field == "co2_ppm":
                co2_ppm = int(value)

    # The app keeps the 6-char prefix plus a synthetic "FF" for the measurement
    # form, and stores the short form verbatim.  Mirrored here so raw_status
    # stays comparable with what the vendor would have recorded.
    raw_status = data_str2[0:6] + "FF" if is_measurement_payload else data_str2

    return SubDevice(
        sub_id=sub_id,
        raw_type=raw_type,
        device_type=device_type,
        profile=profile,
        signal_bars=signal,
        battery_pct=battery,
        alarm_state=alarm,
        raw_status=raw_status,
        co2_ppm=co2_ppm,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
    )


class ThermostatStatus(NamedTuple):
    """Decoded GS361 thermostat status.  ``mode``/``current_temperature_c`` are
    ``None`` when the status string is too short to include the second byte."""

    setpoint_c: float
    valve_open: bool
    window_open: bool
    mode: ThermostatMode | None
    current_temperature_c: float | None


def decode_thermostat_status(raw_status: str) -> ThermostatStatus | None:
    """Decode status bytes from a GS361 thermostat/radiator valve (type 215).

    The GS361 repurposes the standard alarm byte with a completely different bit
    layout (source: ``ThermostatVModel.onAnalysisStatus`` in the vendor Android app,
    constants from ``com.alibaba.ailabs.iot.aisbase.Constants.CMD_TYPE``):

        raw_status[4:6]  (the "alarm state" byte in standard devices):
            bit 7 (0x80):  window-open detection active  (TRV sensed open window)
            bit 6 (0x40):  valve currently open          (radiator actively heating)
            bit 5 (0x20):  setpoint has a 0.5 °C increment
            bits 0–4 (0x1F): setpoint floor in integer °C  (range 0–31 °C)

        raw_status[6:8]  (the sub-state byte in standard devices):
            bits 0–1 (0x03): operating mode, see ``ThermostatMode``
            bits 2–7 (0xFC): measured room temperature in whole °C (0–63)

    The bytes at [0:4] are NOT decoded here; the app reads valve/window/lock
    control state from those positions when it writes settings but they are not
    relevant for reading push status.

    Confidence note — mode is confirmed in both directions (read as ``b3 & 3``,
    written back as ``modelState.get() & 3``).  The temperature decoding is
    read-path only: the app renders ``((b3 >> 2) & 63) + "°C"`` beneath a
    ``current_temperature`` label on the thermostat screen.  It cannot be
    cross-checked against ``getSettingStateCode``, which builds a *command*
    payload and reads from a different offset (``[0:2]``, not ``[4:8]``), so it
    describes the control encoding rather than the status encoding.  Worth
    re-verifying against a real GS361 alongside the app's own display.
    """
    if len(raw_status) < 6:
        return None
    try:
        b2 = int(raw_status[4:6], 16)
    except ValueError:
        return None
    valve_open = bool(b2 & 0x40)
    window_open = bool(b2 & 0x80)
    setpoint = float(b2 & 0x1F) + (0.5 if (b2 & 0x20) else 0.0)

    mode: ThermostatMode | None = None
    current_temperature: float | None = None
    if len(raw_status) >= 8:
        try:
            b3 = int(raw_status[6:8], 16)
        except ValueError:
            pass
        else:
            # Every 2-bit value maps to a defined mode, so this cannot raise.
            mode = ThermostatMode(b3 & 0x03)
            current_temperature = float((b3 >> 2) & 0x3F)

    return ThermostatStatus(setpoint, valve_open, window_open, mode, current_temperature)


def decode_device_name(data_str2: str) -> tuple[int, str] | None:
    """Decode one sub-device name record from a CMD_CODE 17 response.

    ``data_str2`` is a 36-char hex string:
      [0:4]   sub_id (2 bytes)
      [4:36]  16-byte GBK-encoded name field (32 hex chars)

    The name field uses the app's GBK padding scheme: '@' characters fill
    unused leading bytes and '$' marks the end of the name
    (source: CoderUtils.getStringFromAscii / getAscii in the vendor Android app).
    Returns ``(sub_id, name)`` or ``None`` if the record cannot be decoded
    or the name is empty.
    """
    if len(data_str2) != 36:
        return None
    try:
        sub_id = int(data_str2[0:4], 16)
    except ValueError:
        return None
    if sub_id == 0:
        return None
    try:
        raw = bytes.fromhex(data_str2[4:36]).decode("gbk", errors="replace")
    except ValueError:
        return None
    if "$" not in raw:
        return None
    # rfind("@") + 1 gives 0 when no padding is present, matching the app's
    # lastIndexOf('@') + 1 logic in CoderUtils.getStringFromAscii.
    name = raw[raw.rfind("@") + 1 : raw.index("$")]
    return (sub_id, name) if name else None


def parse_gateway_info(obj: dict[str, Any]) -> GatewayInfo | None:
    """Parse a CMD_CODE 13 gateway info response.

    ``data_str1`` is the Wi-Fi SSID as plain text — not hex, unlike almost every
    other payload in this protocol.  ``data_str2`` packs two fields: ``[0:2]``
    is the gateway room id and ``[2:]`` is the sub-device push flag, where
    ``"00"`` means enabled.

    Both fields are decoded leniently.  A hub that returns a shorter
    ``data_str2`` than expected leaves the affected field ``None`` rather than
    failing the whole parse, because the caller wants the SSID even from a hub
    whose other fields it cannot read.
    """
    msg: Any = obj.get("msg", obj)
    if not isinstance(msg, dict):
        return None
    device_name = obj.get("devID")
    if not isinstance(device_name, str):
        return None

    data_str1 = str(msg.get("data_str1") or msg.get("rev_str1") or "")
    data_str2 = str(msg.get("data_str2") or msg.get("rev_str2") or "")

    return GatewayInfo(
        device_name=device_name,
        ip="",  # caller fills this in from the packet source address
        raw_data_str1=data_str1,
        raw_data_str2=data_str2,
        ssid=data_str1 or None,
        room_id=data_str2[0:2] if len(data_str2) >= 2 else None,
        sub_device_push=(data_str2[2:] == "00") if len(data_str2) > 2 else None,
    )
