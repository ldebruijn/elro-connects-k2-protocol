#!/usr/bin/env python3
"""Probe the ELRO Connects K2 local UDP protocol.

This reproduces the UDP framing found in the Android app:

* bind/send UDP port 1025
* first byte is random
* each JSON UTF-8 byte is XORed with (first_byte ^ 0x23)

Default mode sends only the discovery query. Explicit command flags are needed
for gateway status/info queries.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from datetime import datetime
from typing import Any

UDP_PORT = 1025
XOR_CONST = 0x23
GATEWAY_PRODUCT_KEY = "a2AdG2E0EHL"

DEVICE_TYPES = {
    "001": ("smoke alarm", "GS530D", "BB000000"),
    "009": ("smoke alarm", "GS530D variant", "BB000000"),
    "00F": ("smoke alarm", "GS530D variant", "BB000000"),
    "005": ("photoelectric smoke alarm", "GS559A", "17000000"),
    "00D": ("photoelectric smoke alarm", "GS559A variant", "17000000"),
    "013": ("photoelectric smoke alarm", "GS559A variant", "17000000"),
    "01A": ("photoelectric smoke alarm", "GS592A", "02FFFFFF"),
    "025": ("smoke alarm", "GS556 family", "BB000000"),
    "000": ("CO alarm", "GS816A", "BB000000"),
    "008": ("CO alarm", "GS816A variant", "BB000000"),
    "00E": ("CO alarm", "GS816A variant", "BB000000"),
    "019": ("CO alarm", "GS818A", "02FF0000"),
    "030": ("CO alarm", "GS827W", "BB000000"),
    "002": ("gas alarm", "GS870W", "BB000000"),
    "006": ("gas alarm", "GS870W variant", "BB000000"),
    "00A": ("gas alarm", "GS870W variant", "BB000000"),
    "010": ("gas alarm", "GS870W variant", "BB000000"),
    "015": ("gas alarm", "GS871A", "BB000000"),
    "017": ("new gas alarm", "GS870W", "BB000000"),
    "014": ("CO + gas alarm", "GS891A", "BB000000"),
    "003": ("heat alarm", "GS412D/GS412A", "BB000000"),
    "00B": ("heat alarm", "GS412 variant", "BB000000"),
    "011": ("heat alarm", "GS412 variant", "BB000000"),
    "004": ("water alarm", "GS156D/GS156A", "BB000000"),
    "00C": ("water alarm", "GS156 variant", "BB000000"),
    "012": ("water alarm", "GS156 variant", "BB000000"),
    "20E": ("outdoor siren", "GS380D/GS380A", "51000000"),
}

CMD_LABELS = {
    11: "ACK",
    1: "EQUIPMENT_CONTROL",
    12: "QUERY_GATEWAY_INFO",
    13: "UPLOAD_GATEWAY_INFO",
    16: "GET_SUB_DEVICE_INFO",
    19: "UPLOAD_DEVICE_STATUS",
    44: "ALARM_LIST_SYNC",
    45: "UPLOAD_ALARM_LOGS_INFO",
    47: "SUB_DEVICE_ALARM_LIST_SYNC",
    48: "UPLOAD_SUB_DEVICE_ALARM_LOGS_INFO",
    54: "SYN_ALL_DEVICE_STATUS",
    55: "UPLOAD_ALL_DEVICE_STATUS",
    56: "UPLOAD_ALL_DEVICE_STATUS_2",
    66: "UPLOAD_SUB_DEVICE_INFO",
}

# K2 uses NODE_SEND (not APP_SEND) for device-to-app messages.
# Activation requires a targeted IOT_KEY? sent directly to the gateway IP after discovery.


def compact_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def encrypt_message(message: str) -> bytes:
    seed = random.randrange(256)
    key = seed ^ XOR_CONST
    return bytes([seed]) + bytes(byte ^ key for byte in message.encode("utf-8"))


def trim_json_text(text: str) -> str:
    """Mimic the app's rough trim-to-first-JSON-object behavior."""
    if "}}" in text:
        return text[: text.index("}}") + 2]
    if "}" in text:
        return text[: text.index("}") + 1]
    return text


def decrypt_message(packet: bytes) -> tuple[str, dict[str, Any] | None]:
    if not packet:
        return "", None
    key = packet[0] ^ XOR_CONST
    plain = bytes(byte ^ key for byte in packet[1:])
    text = plain.decode("utf-8", errors="replace").rstrip("\x00")
    text = trim_json_text(text)
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


def classify_message(obj: dict[str, Any] | None) -> str:
    if not obj:
        return "unparsed"
    if "device_id" in obj:
        if "token" in obj:
            return "discovery/token"
        if obj.get("ack") == "ok":
            return "ap-ack"
        return "device"
    msg = obj.get("msg")
    if isinstance(msg, dict):
        code = msg.get("CMD_CODE")
        label = CMD_LABELS.get(code, f"CMD_CODE_{code}") if isinstance(code, int) else "CMD_CODE_?"
        action = obj.get("action", "unknown-action")
        return f"{action}/{label}"
    return "json"


def is_node_send(obj: dict[str, Any] | None) -> bool:
    """K2 sends NODE_SEND (not APP_SEND) for device-to-app data messages."""
    return bool(obj and obj.get("action") == "NODE_SEND")


def normalize_device_type(device_type: str) -> str:
    return device_type[1:].upper() if len(device_type) == 4 else device_type.upper()


def device_type_info(device_type: str) -> tuple[str, str, str] | None:
    return DEVICE_TYPES.get(normalize_device_type(device_type))


def extract_msg(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not obj:
        return None
    msg = obj.get("msg")
    if isinstance(msg, dict):
        return msg
    return obj


def get_message_device_name(obj: dict[str, Any] | None) -> str | None:
    if not obj:
        return None
    value = obj.get("devID") or obj.get("device_id")
    return value if isinstance(value, str) and value else None


def get_message_cmd_code(obj: dict[str, Any] | None) -> int | None:
    msg = extract_msg(obj)
    if not msg:
        return None
    value = msg.get("CMD_CODE")
    return value if isinstance(value, int) else None


def data_field(msg: dict[str, Any], data_name: str, rev_name: str) -> str | None:
    value = msg.get(data_name)
    if value is None:
        value = msg.get(rev_name)
    return value if isinstance(value, str) else None


def describe_status_records(obj: dict[str, Any] | None) -> None:
    msg = extract_msg(obj)
    if not msg:
        return
    code = get_message_cmd_code(obj)
    if code not in {19, 55, 56, 66}:
        return

    chunks: list[tuple[int | None, str, str]] = []
    if code in {55, 56}:
        for field in ("data_str1", "data_str2", "rev_str1", "rev_str2", "data_str3", "rev_str3"):
            value = msg.get(field)
            if isinstance(value, str) and len(value) % 14 == 0:
                for offset in range(0, len(value), 14):
                    record = value[offset : offset + 14]
                    try:
                        sub_id = int(record[0:2], 16)
                    except ValueError:
                        sub_id = None
                    chunks.append((sub_id, record[2:6], record))
    elif code in {19, 66}:
        value = data_field(msg, "data_str1", "rev_str1")
        if isinstance(value, str) and len(value) >= 8:
            try:
                sub_id = int(value[0:4], 16)
            except ValueError:
                sub_id = None
            chunks.append((sub_id, value[4:8], value))

    for sub_id, raw_type, record in chunks:
        info = device_type_info(raw_type)
        norm = normalize_device_type(raw_type)
        if info:
            label, model, action = info
            print(
                f"  device hint: sub_id={sub_id} raw_type={raw_type} type={norm} "
                f"{label} ({model}), test_action={action}, record={record}"
            )
        else:
            print(f"  device hint: sub_id={sub_id} raw_type={raw_type} type={norm}, record={record}")


def build_discovery(device_name: str = "NULL") -> str:
    return compact_json({"action": "IOT_KEY?", "devID": device_name})


def build_activation(device_name: str) -> str:
    """Targeted IOT_KEY? sent directly to a known gateway IP to activate command processing.

    The app's onActivationUdp sends this without quotes around the devID value (a bug), but
    the K2 accepts both quoted and unquoted forms. We send the quoted (valid JSON) form.
    """
    return compact_json({"action": "IOT_KEY?", "devID": device_name})


def build_app_send(
    device_name: str,
    msg_id: int,
    cmd_code: int,
    rev_str1: str = "",
    rev_str2: str = "",
    rev_str3: str = "",
) -> str:
    return compact_json(
        {
            "action": "APP_SEND",
            "devID": device_name,
            "msg": {
                "msg_ID": msg_id,
                "CMD_CODE": cmd_code,
                "rev_str1": rev_str1,
                "rev_str2": rev_str2,
                "rev_str3": rev_str3,
            },
        }
    )


def build_ack(device_name: str, msg_id: int, ack_for_code: int = 11) -> str:
    return compact_json(
        {
            "action": "APP_ACK",
            "devID": device_name,
            "msg": {
                "msg_ID": msg_id,
                "CMD_CODE": 11,
                "rev_str1": str(ack_for_code),
                "rev_str2": "OK",
                "rev_str3": "",
            },
        }
    )


def build_token_ack(device_name: str) -> str:
    return compact_json({"device_id": device_name, "app_ack": "ok"})


def two_byte_hex(value: int) -> str:
    if value < 0 or value > 0xFFFF:
        raise argparse.ArgumentTypeError("value must fit in two bytes")
    return f"{value:04X}"


def one_byte_hex(value: int) -> str:
    if value < 0 or value > 0xFF:
        raise argparse.ArgumentTypeError("value must fit in one byte")
    return f"{value:02X}"


def timezone_offset_code() -> str:
    """Match CoderUtils.getTimeZoneOffset(): 00HHMM for +, 01HHMM for -."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "000000"
    minutes = int(offset.total_seconds() // 60)
    sign = "00" if minutes >= 0 else "01"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02X}{minutes % 60:02X}"


def command_payload(args: argparse.Namespace, msg_id: int) -> str | None:
    if args.command == "gateway-info":
        return build_app_send(args.device_name, msg_id, 12, "00", "00", "")
    if args.command == "sync-status":
        crc = args.device_crc.upper()
        tz = args.timezone_code.upper()
        return build_app_send(args.device_name, msg_id, 54, crc, tz, "")
    if args.command == "sync-names":
        # The hub answers with one CMD_CODE 17 frame per *named* sub-device,
        # ending with data_str2 == "NAME_OVER".  Raise --timeout to see the
        # whole stream: real hubs pace these ~350-400 ms apart.
        return build_app_send(args.device_name, msg_id, 24, args.device_crc.upper(), "", "")
    if args.command == "sub-device-info":
        return build_app_send(args.device_name, msg_id, 16, two_byte_hex(args.sub_id), "", "")
    if args.command == "gateway-alarms":
        return build_app_send(args.device_name, msg_id, 44, one_byte_hex(args.page), "", "")
    if args.command == "sub-device-alarms":
        return build_app_send(
            args.device_name,
            msg_id,
            47,
            one_byte_hex(args.page),
            two_byte_hex(args.sub_id),
            "",
        )
    if args.command == "detector-test":
        return build_app_send(
            args.device_name,
            msg_id,
            1,
            two_byte_hex(args.sub_id),
            args.action_code.upper(),
            "",
        )
    if args.command == "detector-mute":
        return build_app_send(
            args.device_name,
            msg_id,
            1,
            two_byte_hex(args.sub_id),
            "50000000",
            "",
        )
    return None


def make_socket(local_port: int, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", local_port))
    sock.settimeout(timeout)
    return sock


def send_json(sock: socket.socket, host: str, port: int, message: str, verbose: bool) -> None:
    if verbose:
        print(f"> {host}:{port} {message}")
    sock.sendto(encrypt_message(message), (host, port))


def receive_loop(
    sock: socket.socket,
    duration: float,
    *,
    auto_ack: bool,
    verbose: bool,
) -> list[tuple[str, int, str, dict[str, Any] | None]]:
    deadline = time.monotonic() + duration
    seen: set[tuple[str, str]] = set()
    replies: list[tuple[str, int, str, dict[str, Any] | None]] = []

    while time.monotonic() < deadline:
        try:
            packet, (host, port) = sock.recvfrom(4096)
        except TimeoutError:
            continue

        text, obj = decrypt_message(packet)
        key = (host, text)
        if key in seen:
            continue
        seen.add(key)
        replies.append((host, port, text, obj))

        parsed = json.dumps(obj, indent=2, ensure_ascii=False) if obj is not None else text
        print(f"\n< {host}:{port} [{classify_message(obj)}]")
        print(parsed)
        describe_status_records(obj)

        if auto_ack and obj and obj.get("action") in ("APP_SEND", "NODE_SEND"):
            device_name = get_message_device_name(obj)
            code = get_message_cmd_code(obj)
            if device_name:
                ack = build_ack(device_name, random.randrange(1_000_000), code or 11)
                send_json(sock, host, UDP_PORT, ack, verbose)
        elif auto_ack and obj and "device_id" in obj and "token" in obj:
            device_name = get_message_device_name(obj)
            if device_name:
                send_json(sock, host, UDP_PORT, build_token_ack(device_name), verbose)

    return replies


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe ELRO Connects K2 UDP discovery/control on port 1025."
    )
    parser.add_argument("--local-port", type=int, default=UDP_PORT)
    parser.add_argument("--target-port", type=int, default=UDP_PORT)
    parser.add_argument("--broadcast", default="255.255.255.255")
    parser.add_argument("--gateway-ip", help="Gateway IP for direct command tests.")
    parser.add_argument("--device-name", help="Gateway deviceName/devID for command tests.")
    parser.add_argument(
        "--command",
        choices=(
            "gateway-info",
            "sync-status",
            "sync-names",
            "sub-device-info",
            "gateway-alarms",
            "sub-device-alarms",
            "detector-test",
            "detector-mute",
        ),
        help="Optional read-only-ish command to test after or instead of discovery.",
    )
    parser.add_argument("--sub-id", type=int, default=1, help="Sub-device id for sub-device commands.")
    parser.add_argument("--page", type=int, default=0, help="History page for alarm sync commands.")
    parser.add_argument(
        "--action-code",
        default="BB000000",
        help="Action code for detector-test. App default for most detectors is BB000000.",
    )
    parser.add_argument(
        "--device-crc",
        default="00020000",
        help=(
            "rev_str1 for sync-status and sync-names. App uses 00020000 when no sub-device "
            "cache exists. For sync-names the app instead sends a 2-byte length followed by "
            "one 2-byte name CRC per sub_id from 1..max (CoderUtils.getDeviceNameCRC)."
        ),
    )
    parser.add_argument(
        "--timezone-code",
        default=timezone_offset_code(),
        help="rev_str2 for sync-status. Format is 00HHMM for positive, 01HHMM for negative.",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--no-discover", action="store_true", help="Skip discovery broadcast.")
    parser.add_argument("--no-auto-ack", action="store_true", help="Do not ACK APP_SEND messages.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command and args.no_discover and (not args.gateway_ip or not args.device_name):
        print("--command with --no-discover requires --gateway-ip and --device-name", file=sys.stderr)
        return 2

    try:
        sock = make_socket(args.local_port, timeout=0.5)
    except OSError as exc:
        print(f"Could not bind UDP port {args.local_port}: {exc}", file=sys.stderr)
        print("Close the ELRO app or rerun with --local-port 0 for an ephemeral port.", file=sys.stderr)
        return 1

    with sock:
        replies: list[tuple[str, int, str, dict[str, Any] | None]] = []

        if not args.no_discover:
            discovery = build_discovery()
            for _ in range(args.retries):
                send_json(sock, args.broadcast, args.target_port, discovery, args.verbose)
                replies.extend(
                    receive_loop(
                        sock,
                        max(0.5, args.timeout / max(args.retries, 1)),
                        auto_ack=not args.no_auto_ack,
                        verbose=args.verbose,
                    )
                )

        gateway_ip = args.gateway_ip
        device_name = args.device_name
        if not gateway_ip or not device_name:
            for host, _port, _text, obj in replies:
                candidate = get_message_device_name(obj)
                if candidate and candidate != "NULL":
                    gateway_ip = host
                    device_name = candidate
                    break

        # Activation: send targeted IOT_KEY? directly to the gateway so it enters
        # command-processing mode.  The K2 ignores APP_SEND until it has received
        # a targeted IOT_KEY? from the controlling host.  Send activation even in
        # --no-discover mode when --gateway-ip and --device-name are known.
        if gateway_ip and device_name:
            print(f"\nActivating gateway {gateway_ip} devID={device_name}")
            activation = build_activation(device_name)
            for _ in range(args.retries):
                send_json(sock, gateway_ip, args.target_port, activation, args.verbose)
                receive_loop(
                    sock,
                    max(0.5, args.timeout / max(args.retries, 1)),
                    auto_ack=not args.no_auto_ack,
                    verbose=args.verbose,
                )

        if args.command:
            if not gateway_ip or not device_name:
                print("No gateway/device name found. Re-run with --gateway-ip and --device-name.", file=sys.stderr)
                return 3
            args.gateway_ip = gateway_ip
            args.device_name = device_name
            payload = command_payload(args, random.randrange(1_000_000))
            if payload is None:
                return 2
            print(f"\nSending {args.command} to {gateway_ip} devID={device_name}")
            for _ in range(args.retries):
                send_json(sock, gateway_ip, args.target_port, payload, args.verbose)
                receive_loop(
                    sock,
                    max(0.5, args.timeout / max(args.retries, 1)),
                    auto_ack=not args.no_auto_ack,
                    verbose=args.verbose,
                )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
