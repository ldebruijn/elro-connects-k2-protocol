"""Device profile registry for the ELRO Connects K2.

Maps normalized 3-char device type codes to DeviceProfile instances that
describe exactly which Home Assistant entities a physical device exposes.
Entity platforms iterate profile.capabilities — no type-code branching in HA code.
"""

from __future__ import annotations

from elro_connects_k2_protocol.models import DeviceCapability, DeviceProfile

# ── helpers ──────────────────────────────────────────────────────────────────

def _bs(key: str, device_class: str, label: str) -> DeviceCapability:
    return DeviceCapability(key=key, entity_type="binary_sensor", device_class=device_class, label=label)

def _sensor(key: str, device_class: str, label: str, unit: str | None = None) -> DeviceCapability:
    return DeviceCapability(key=key, entity_type="sensor", device_class=device_class, label=label, unit=unit)

def _profile(
    name: str,
    models: tuple[str, ...],
    caps: tuple[DeviceCapability, ...],
    test: str | None,
    mute: str | None = None,
    mains: bool = False,
) -> DeviceProfile:
    # All devices that support test also support silence via CMD_CODE 1.
    # Standard alarm devices always use "50000000" to silence.
    # Pass mute explicitly only when the device needs a different payload.
    resolved_mute = mute if (mute is not None or test is None) else "50000000"
    return DeviceProfile(name=name, model_hints=models, capabilities=caps, test_action=test, mute_action=resolved_mute, mains_powered=mains)


# ── universal capabilities ────────────────────────────────────────────────────

# Added to every sub-device regardless of type; not included in DeviceProfile.capabilities
# so that profile.capabilities only describes hazard-specific traits.
UNIVERSAL_CAPABILITIES: tuple[DeviceCapability, ...] = (
    _sensor("battery", "battery", "Battery", "%"),
    _sensor("signal", "signal_strength", "Signal"),
)

# ── registry ─────────────────────────────────────────────────────────────────

_SMOKE = _bs("smoke", "smoke", "Smoke")
_CO = _bs("co", "carbon_monoxide", "CO")
_GAS = _bs("gas", "gas", "Gas")
_HEAT = _bs("heat", "heat", "Heat")
_MOISTURE = _bs("moisture", "moisture", "Water")
_MOTION = _bs("motion", "motion", "Motion")
_DOOR = _bs("door", "door", "Door")
_VIBRATION = _bs("vibration", "vibration", "Vibration")
# Thermostat / radiator valve (GS361, type 215)
_VALVE_OPEN = _bs("valve", "opening", "Valve")
_SETPOINT = _sensor("setpoint", "temperature", "Setpoint", "°C")
# Not a window contact sensor: the TRV infers an open window from a sudden
# temperature drop and reports it in status bit 0x80 (ThermostatVModel
# .onAnalysisStatus).  Named accordingly so it doesn't read as a door/window
# contact the K2 does not have.
_WINDOW_OPEN_DETECTED = _bs("window", "window", "Open window")
# Measured room temperature, reported in the second status byte alongside the
# operating mode.  Shares the "temperature" key (and SubDevice.temperature_c)
# with the CO2/TH detector so the sensor platform needs no special case.
_TRV_TEMPERATURE = _sensor("temperature", "temperature", "Temperature", "°C")
_TRV_MODE = _sensor("mode", "enum", "Mode")

DEVICE_PROFILES: dict[str, DeviceProfile] = {
    # ── smoke ────────────────────────────────────────────────────────────────
    "001": _profile("Smoke Alarm", ("GS530D",), (_SMOKE,), "BB000000"),
    "009": _profile("Smoke Alarm", ("GS530D variant",), (_SMOKE,), "BB000000"),
    "00F": _profile("Smoke Alarm", ("GS530D variant",), (_SMOKE,), "BB000000"),
    "005": _profile("Photoelectric Smoke Alarm", ("GS559A",), (_SMOKE,), "17000000"),
    "00D": _profile("Photoelectric Smoke Alarm", ("GS559A variant",), (_SMOKE,), "17000000"),
    "013": _profile("Photoelectric Smoke Alarm", ("GS559A variant",), (_SMOKE,), "17000000"),
    "01A": _profile("Photoelectric Smoke Alarm", ("GS592A",), (_SMOKE,), "02FFFFFF"),
    "025": _profile("Smoke Alarm", ("GS556 family",), (_SMOKE,), "BB000000"),
    # ── CO ───────────────────────────────────────────────────────────────────
    "000": _profile("CO Alarm", ("GS816A",), (_CO,), "BB000000"),
    "008": _profile("CO Alarm", ("GS816A variant",), (_CO,), "BB000000"),
    "00E": _profile("CO Alarm", ("GS816A variant",), (_CO,), "BB000000"),
    "019": _profile("CO Alarm", ("GS818A",), (_CO,), "02FF0000"),
    "030": _profile("CO Alarm", ("GS827W",), (_CO,), "BB000000"),
    # ── gas ──────────────────────────────────────────────────────────────────
    "002": _profile("Gas Alarm", ("GS870W",), (_GAS,), "BB000000", mains=True),
    "006": _profile("Gas Alarm", ("GS870W variant",), (_GAS,), "BB000000", mains=True),
    "00A": _profile("Gas Alarm", ("GS870W variant",), (_GAS,), "BB000000", mains=True),
    "010": _profile("Gas Alarm", ("GS870W variant",), (_GAS,), "BB000000", mains=True),
    "015": _profile("Gas Alarm", ("GS871A",), (_GAS,), "BB000000", mains=True),
    "017": _profile("Gas Alarm", ("GS870W new",), (_GAS,), "BB000000", mains=True),
    # ── combination ──────────────────────────────────────────────────────────
    "014": _profile("CO + Gas Alarm", ("GS891A",), (_CO, _GAS), "BB000000"),
    # ── heat ─────────────────────────────────────────────────────────────────
    "003": _profile("Heat Alarm", ("GS412D", "GS412A"), (_HEAT,), "BB000000"),
    "00B": _profile("Heat Alarm", ("GS412 variant",), (_HEAT,), "BB000000"),
    "011": _profile("Heat Alarm", ("GS412 variant",), (_HEAT,), "BB000000"),
    # ── water ────────────────────────────────────────────────────────────────
    "004": _profile("Water Alarm", ("GS156D", "GS156A"), (_MOISTURE,), "BB000000"),
    "00C": _profile("Water Alarm", ("GS156 variant",), (_MOISTURE,), "BB000000"),
    "012": _profile("Water Alarm", ("GS156 variant",), (_MOISTURE,), "BB000000"),
    # ── CO2 / temp / humidity ────────────────────────────────────────────────
    "018": _profile("CO2/Temperature/Humidity Detector", ("GS241A",), (
        _sensor("co2", "carbon_dioxide", "CO2", "ppm"),
        _sensor("temperature", "temperature", "Temperature", "°C"),
        _sensor("humidity", "humidity", "Humidity", "%"),
    ), "02BB0000", "02500000", mains=True),
    # ── PIR ──────────────────────────────────────────────────────────────────
    "01":  _profile("PIR Motion Sensor", (), (_MOTION,), None),
    "02":  _profile("PIR Motion Sensor", (), (_MOTION,), None),
    "03":  _profile("PIR Motion Sensor", (), (_MOTION,), None),
    "109": _profile("PIR Motion Sensor", (), (_MOTION,), None),
    # ── door / contact ───────────────────────────────────────────────────────
    "08":  _profile("Door/Contact Sensor", (), (_DOOR,), None),
    "09":  _profile("Door/Contact Sensor", (), (_DOOR,), None),
    "0A":  _profile("Door/Contact Sensor", (), (_DOOR,), None),
    "101": _profile("Door/Contact Sensor", (), (_DOOR,), None),
    "10A": _profile("Door/Contact Sensor", (), (_DOOR,), None),
    # ── temperature / humidity ───────────────────────────────────────────────
    "102": _profile("Temperature/Humidity Checker", (), (
        _sensor("temperature", "temperature", "Temperature", "°C"),
        _sensor("humidity", "humidity", "Humidity", "%"),
    ), None),
    # ── sockets / lighting / control ─────────────────────────────────────────
    "18":  _profile("Smart Socket", (), (), None, mains=True),
    "19":  _profile("Smart Socket", (), (), None, mains=True),
    "214": _profile("Smart Socket", (), (), None, mains=True),
    "218": _profile("Smart Socket", (), (), None, mains=True),
    "1A":  _profile("Lighting Module", (), (), None, mains=True),
    "1B":  _profile("Lighting Module", (), (), None, mains=True),
    "216": _profile("Lighting Module", (), (), None, mains=True),
    "1C":  _profile("Radiator Thermostat", (), (_VALVE_OPEN, _SETPOINT, _WINDOW_OPEN_DETECTED, _TRV_TEMPERATURE, _TRV_MODE), None),
    "215": _profile("Radiator Thermostat", ("GS361",), (_VALVE_OPEN, _SETPOINT, _WINDOW_OPEN_DETECTED, _TRV_TEMPERATURE, _TRV_MODE), None),
    # ── siren ────────────────────────────────────────────────────────────────
    "20E": _profile("Outdoor Siren", ("GS380D", "GS380A"), (), "51000000", mains=True),
    # ── vibration / flash ────────────────────────────────────────────────────
    "28":  _profile("Flash/Vibration Alarm", (), (_VIBRATION,), None),
    "212": _profile("Flash/Vibration Alarm", (), (_VIBRATION,), None),
    # ── SOS / buttons / scene ────────────────────────────────────────────────
    "211": _profile("SOS Button", (), (), None),
    "0F":  _profile("Button", (), (), None),
    "11":  _profile("Button", (), (), None),
    "301": _profile("Button", (), (), None),
    "0C":  _profile("Scene Switch", (), (), None),
    "305": _profile("Scene Switch", (), (), None),
    # ── door lock ────────────────────────────────────────────────────────────
    "12":  _profile("Door Lock", (), (), None),
    "13":  _profile("Door Lock", (), (), None),
    "213": _profile("Door Lock", (), (), None),
    # ── manipulator / valve ──────────────────────────────────────────────────
    "2B":  _profile("Manipulator", (), (), None, mains=True),
    "2C":  _profile("Manipulator", (), (), None, mains=True),
    "208": _profile("Manipulator", (), (), None, mains=True),
    "29":  _profile("Solenoid Valve", (), (), None, mains=True),
    "2A":  _profile("Solenoid Valve", (), (), None, mains=True),
    "217": _profile("Solenoid Valve", (), (), None, mains=True),
    # ── repeater ─────────────────────────────────────────────────────────────
    "401": _profile("Repeater", (), (), None),
    # ── test / reminder ──────────────────────────────────────────────────────
    "2D":  _profile("Test/Reminder", (), (), None),
}

UNKNOWN_PROFILE = DeviceProfile(
    name="Unknown Device",
    model_hints=(),
    capabilities=(),
    test_action=None,
    mute_action=None,
)


def get_profile(device_type: str) -> DeviceProfile:
    return DEVICE_PROFILES.get(device_type.upper(), UNKNOWN_PROFILE)
