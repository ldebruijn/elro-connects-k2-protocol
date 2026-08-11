"""UDP framing for the ELRO Connects K2 local protocol.

Wire format (from ByteUtil.java):
  byte 0      random seed r
  bytes 1..n  each UTF-8 JSON byte XORed with (r ^ 0x23)

This is obfuscation, not encryption — the seed is transmitted in plaintext.
"""

from __future__ import annotations

import json
import random
from typing import Any

UDP_PORT: int = 1025
XOR_CONST: int = 0x23
GATEWAY_PRODUCT_KEY: str = "a2AdG2E0EHL"


def encrypt_message(text: str) -> bytes:
    seed = random.randrange(256)
    key = seed ^ XOR_CONST
    return bytes([seed]) + bytes(b ^ key for b in text.encode("utf-8"))


def decrypt_message(packet: bytes) -> tuple[str, dict[str, Any] | None]:
    if not packet:
        return "", None
    key = packet[0] ^ XOR_CONST
    plain = bytes(b ^ key for b in packet[1:])
    text = plain.decode("utf-8", errors="replace").rstrip("\x00")
    text = _trim_to_first_json_object(text)
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


def _trim_to_first_json_object(text: str) -> str:
    """Reproduce the app's rough trim-to-first-JSON-object behavior."""
    if "}}" in text:
        return text[: text.index("}}") + 2]
    if "}" in text:
        return text[: text.index("}") + 1]
    return text


def _compact(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def build_discovery(device_name: str = "NULL") -> str:
    return _compact({"action": "IOT_KEY?", "devID": device_name})


def build_activation(device_name: str) -> str:
    return _compact({"action": "IOT_KEY?", "devID": device_name})


def build_app_send(
    device_name: str,
    msg_id: int,
    cmd_code: int,
    rev_str1: str = "",
    rev_str2: str = "",
    rev_str3: str = "",
) -> str:
    return _compact({
        "action": "APP_SEND",
        "devID": device_name,
        "msg": {
            "msg_ID": msg_id,
            "CMD_CODE": cmd_code,
            "rev_str1": rev_str1,
            "rev_str2": rev_str2,
            "rev_str3": rev_str3,
        },
    })


def build_ack(device_name: str, msg_id: int, ack_for_code: int = 11) -> str:
    return _compact({
        "action": "APP_ACK",
        "devID": device_name,
        "msg": {
            "msg_ID": msg_id,
            "CMD_CODE": 11,
            "rev_str1": str(ack_for_code),
            "rev_str2": "OK",
            "rev_str3": "",
        },
    })


def timezone_offset_code() -> str:
    """Return timezone offset in the format expected by CMD_CODE 54 rev_str2.

    Matches CoderUtils.getTimeZoneOffset(): 00HHMM for positive, 01HHMM for negative.
    """
    from datetime import datetime
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "000000"
    minutes = int(offset.total_seconds() // 60)
    sign = "00" if minutes >= 0 else "01"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02X}{minutes % 60:02X}"
