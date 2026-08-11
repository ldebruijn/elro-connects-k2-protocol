"""ELRO Connects K2 local protocol library."""

from elro_connects_k2_protocol.device_profiles import (
    DEVICE_PROFILES,
    UNIVERSAL_CAPABILITIES,
    UNKNOWN_PROFILE,
    get_profile,
)
from elro_connects_k2_protocol.gateway import (
    PAIRING_TIMEOUT_SECONDS,
    K2Gateway,
    discover_gateway,
)
from elro_connects_k2_protocol.models import (
    AlarmState,
    DeviceCapability,
    DeviceProfile,
    GatewayInfo,
    PairingResult,
    SubDevice,
    ThermostatMode,
    UpdateSource,
)
from elro_connects_k2_protocol.parser import (
    decode_co2_th_measurement,
    decode_device_status,
    decode_thermostat_status,
    normalize_type,
    parse_add_sub_device,
    parse_push_update,
    parse_sub_device_info_response,
    parse_sync_response,
)

__all__ = [
    "DEVICE_PROFILES",
    "PAIRING_TIMEOUT_SECONDS",
    "UNIVERSAL_CAPABILITIES",
    "UNKNOWN_PROFILE",
    "AlarmState",
    "DeviceCapability",
    "DeviceProfile",
    "GatewayInfo",
    "K2Gateway",
    "PairingResult",
    "SubDevice",
    "ThermostatMode",
    "UpdateSource",
    "decode_co2_th_measurement",
    "decode_device_status",
    "decode_thermostat_status",
    "discover_gateway",
    "get_profile",
    "normalize_type",
    "parse_add_sub_device",
    "parse_push_update",
    "parse_sub_device_info_response",
    "parse_sync_response",
]
