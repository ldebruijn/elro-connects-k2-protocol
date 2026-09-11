"""Data models for the ELRO Connects K2 protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class UpdateSource(Enum):
    PUSH = auto()  # CMD_CODE 19 arrived unsolicited from the gateway
    POLL = auto()  # CMD_CODE 55/56 arrived in response to our CMD_CODE 54
    PAIRED = auto()  # CMD_CODE 62 — a sub-device just joined during pairing


class ThermostatMode(Enum):
    """Operating mode of a GS361 radiator thermostat (type 215).

    Decoded from bits 0-1 of the second thermostat status byte.  Confirmed in
    both directions in the vendor app: read in ``ThermostatVModel
    .onAnalysisStatus`` (``mCurrentModel = b3 & 3``) and written back in
    ``getSettingStateCode`` (``modelState.get() & 3``).  The label mapping
    comes from ``showModelState``, which drives one UI toggle per value.
    """

    ANTI_FROST = 0  # freezeState — frost protection setpoint (defaults to 5 °C)
    TIMER = 1       # timerState — follows the programmed schedule
    MANUAL = 2      # manualState — fixed setpoint
    MANUAL_TEST = 3 # manualState + testVisible — manual with the test option shown


class AlarmState(Enum):
    CLEAR = "AA"
    ALARM = "55"
    SILENCED = "50"
    ALERT = "BB"  # test/alert state — same hex prefix as the test action code
    FAULT = "11"
    # Door/contact sensor open states: GS320 series sends A0 or 66 for "open"
    # in addition to the standard 55; all three map to is_on=True in HA.
    OPEN = "A0"
    OPEN_VARIANT = "66"
    UNKNOWN = ""


@dataclass(frozen=True)
class DeviceCapability:
    key: str          # unique slug within a device, e.g. "smoke", "co", "temperature"
    entity_type: str  # "binary_sensor" or "sensor"
    device_class: str # HA device class string, e.g. "smoke", "carbon_monoxide"
    label: str        # human-readable suffix appended to entity name
    unit: str | None = None  # measurement unit; None for binary sensors


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    model_hints: tuple[str, ...]
    capabilities: tuple[DeviceCapability, ...]
    test_action: str | None   # CMD_CODE 1 rev_str2 to trigger test/alarm
    mute_action: str | None   # CMD_CODE 1 rev_str2 to silence active alarm
    mains_powered: bool = False  # True → suppress battery entity in HA


@dataclass
class SubDevice:
    sub_id: int
    raw_type: str        # 4-char hex from the wire, e.g. "0013"
    device_type: str     # normalized 3-char, e.g. "013"
    profile: DeviceProfile
    signal_bars: int     # 1–4 decoded from deviceStatus[0:2]
    battery_pct: int     # 0–100 decoded from int(deviceStatus[2:4], 16) & 0x7F
    alarm_state: AlarmState
    raw_status: str      # full 8-char deviceStatus hex, kept for diagnostics
    # Populated only for CO2/temp/humidity devices (type 018) via CMD_CODE 66:
    co2_ppm: int | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    # Populated only for thermostat/radiator valve devices (type 215 / GS361).
    # Note that the thermostat's measured room temperature is reported in the
    # shared ``temperature_c`` field above rather than a dedicated one.
    valve_open: bool | None = None
    temperature_setpoint: float | None = None
    window_open: bool | None = None
    thermostat_mode: ThermostatMode | None = None
    # Custom name set by the user in the ELRO app, fetched from the hub at
    # startup via CMD_CODE 24 → 17.  None when no name has been set.
    nickname: str | None = None


@dataclass(frozen=True)
class PairingResult:
    """Outcome of one sub-device join (CMD_CODE 62).

    ``already_known`` mirrors the distinction the vendor app draws when a join
    lands on a slot it has already seen: it shows "device has been exist"
    instead of the add-success flow.  A detector that was re-paired into its
    old slot is the usual cause.
    """

    device: SubDevice
    already_known: bool


@dataclass
class GatewayInfo:
    """What the hub reports about itself, from a CMD_CODE 12 → 13 exchange.

    ``ssid`` is the Wi-Fi network the hub is currently joined to, in plain text.
    It is worth more than it looks: a hub on a different SSID or VLAN from Home
    Assistant is a common cause of "the hub never answers", and until this field
    was decoded the only source for it was asking the owner — who reports what
    they configured, not what the hub actually did.  The vendor app shows this
    same value on its gateway-detail screen and writes it back with CMD_CODE 77
    (``DeviceInfoActivity``).

    ``room_id`` and ``sub_device_push`` are the two halves of ``data_str2``.
    The app slices ``[0:2]`` off as the gateway's room before it will send a
    status sync, and reads ``[2:]`` as the sub-device push flag — ``"00"`` means
    enabled, which it calls ``isEdgeGateway`` (``GatewayDetailActivity``).

    The raw fields are kept alongside the decoded ones because the decoding is
    only as good as the two hubs it was derived from, and a diagnostics dump
    that carries the original strings can be re-read later without a new capture.
    """

    device_name: str
    ip: str
    product_key: str = "a2AdG2E0EHL"
    raw_data_str1: str = ""
    raw_data_str2: str = ""
    ssid: str | None = None
    room_id: str | None = None
    sub_device_push: bool | None = None
