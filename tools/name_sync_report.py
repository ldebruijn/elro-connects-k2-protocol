#!/usr/bin/env python3
"""Collect a nickname-sync report you can paste into a bug report.

Asks the hub for its sub-device list and its stored nicknames, decodes every
CMD_CODE 17 name record, and prints a verdict.  The point is to separate the two
causes of a missing nickname, which look identical from outside:

  * the hub never stored a name for that sub-device -- normal, and not a bug.
    The vendor app still shows a name for it, because it falls back to its own
    local database rather than the hub's copy.
  * the hub sent a name record the library could not decode -- a real bug.

Everything runs on one socket and one activated session, so the hub is asked
once and answers both queries in sequence.

The gateway IP and devID are replaced with placeholders; pass --raw to keep them.

Usage:
    tools/name_sync_report.py [--gateway-ip IP] [--device-name ST_...] [--raw]
    tools/name_sync_report.py --check-name "Kitchen alarm"

Stdlib only, like k2_udp_probe.py it borrows the wire format from -- no venv
needed.
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import random
import socket
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from k2_udp_probe import (
    UDP_PORT,
    build_ack,
    build_activation,
    build_app_send,
    build_discovery,
    decrypt_message,
    encrypt_message,
    extract_msg,
    get_message_cmd_code,
    get_message_device_name,
    timezone_offset_code,
)

NAME_RECORD_LEN = 36
# The hub only answers commands sent from this source port.
LOCAL_PORT = 1025


# -- name validation (no hub needed) ------------------------------------------

def check_name(name: str) -> int:
    """Report whether a nickname can survive a round trip through the hub.

    Mirrors CoderUtils.getAscii: the name is left-padded with '@' to 15 bytes
    and terminated with '$', giving a 16-byte field.  Past 15 bytes the padding
    loop does not run and the field simply grows, producing a record no decoder
    accepts -- including the vendor app's own.
    """
    print(f"Name: {name!r}")
    try:
        encoded = name.encode("gbk")
    except UnicodeEncodeError as exc:
        print(f"  FAIL  not encodable in GBK: {exc}")
        return 1

    print(f"  {len(name)} characters, {len(encoded)} GBK bytes")
    problems = []
    if len(encoded) > 15:
        oversized = 4 + 2 * (len(encoded) + 1)
        problems.append(
            f"{len(encoded)} GBK bytes exceeds the 15-byte field, so the vendor encoder "
            f"emits a {oversized}-char record instead of {NAME_RECORD_LEN}"
        )
    problems += [
        f"contains {ch!r}, a padding/terminator sentinel in the encoding"
        for ch in "@$" if ch in name
    ]

    if not problems:
        print(f"  OK    encodes to a well-formed {NAME_RECORD_LEN}-char record")
        return 0
    for problem in problems:
        print(f"  FAIL  {problem}")
    print("  This name cannot survive the round trip. Rename the device to something")
    print("  shorter and free of @ and $, then re-run this report.")
    return 1


# -- decoding ------------------------------------------------------------------

def decode_record(record: str) -> tuple[int | None, str, bool]:
    """Return (sub_id, verdict, is_name) for one CMD_CODE 17 record.

    Mirrors decode_device_name, but reports *why* a record yields nothing --
    which is the whole reason this script exists.
    """
    if len(record) != NAME_RECORD_LEN:
        return None, f"MALFORMED: {len(record)} chars, expected {NAME_RECORD_LEN}", False
    try:
        sub_id = int(record[0:4], 16)
    except ValueError:
        return None, "MALFORMED: sub_id is not hex", False
    try:
        raw = bytes.fromhex(record[4:NAME_RECORD_LEN]).decode("gbk", errors="replace")
    except ValueError:
        return sub_id, "MALFORMED: name field is not hex", False
    if "$" not in raw:
        return sub_id, "MALFORMED: no '$' terminator", False
    name = raw[raw.rfind("@") + 1 : raw.index("$")]
    if not name:
        return sub_id, "no name stored on the hub", False
    return sub_id, f"nickname={name!r}", True


def status_sub_ids(obj: dict[str, Any]) -> set[int]:
    """Pull sub_ids out of a CMD_CODE 55/56 status frame (14-char records)."""
    msg = extract_msg(obj)
    if not msg or get_message_cmd_code(obj) not in {55, 56}:
        return set()
    found = set()
    for field in ("data_str1", "data_str2", "rev_str1", "rev_str2", "data_str3", "rev_str3"):
        value = msg.get(field)
        if isinstance(value, str) and value and len(value) % 14 == 0:
            for offset in range(0, len(value), 14):
                with contextlib.suppress(ValueError):
                    found.add(int(value[offset : offset + 2], 16))
    return found


# -- the session ---------------------------------------------------------------

class Session:
    """One socket, one activation, both queries."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.frames: list[tuple[str, dict[str, Any] | None]] = []

    def send(self, host: str, message: str) -> None:
        self.sock.sendto(encrypt_message(message), (host, UDP_PORT))

    def collect(self, duration: float, *, auto_ack: bool = True) -> list[dict[str, Any]]:
        """Read frames for `duration` seconds, acking as the app does."""
        deadline = time.monotonic() + duration
        objs: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                packet, (host, _port) = self.sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            text, obj = decrypt_message(packet)
            self.frames.append((text, obj))
            if obj is None:
                continue
            objs.append(obj)
            if auto_ack and obj.get("action") in ("APP_SEND", "NODE_SEND"):
                device_name = get_message_device_name(obj)
                if device_name:
                    code = get_message_cmd_code(obj) or 11
                    self.send(host, build_ack(device_name, random.randrange(1_000_000), code))
        return objs


def run_report(gateway_ip: str, device_name: str) -> tuple[set[int], list[str]]:
    """Activate once, then ask for the device list and the names in sequence.

    Reusing a single activated session is not just tidier than invoking the
    probe twice: a hub re-activated in quick succession answers the activation
    but not the command that follows it, which reads as "this hub has no names".
    """
    with make_report_socket() as sock:
        session = Session(sock)

        session.send(gateway_ip, build_activation(device_name))
        session.collect(2.0, auto_ack=False)

        session.send(gateway_ip, build_app_send(
            device_name, random.randrange(1_000_000), 54, "00020000", timezone_offset_code(), "",
        ))
        paired: set[int] = set()
        for obj in session.collect(5.0):
            paired |= status_sub_ids(obj)

        session.send(gateway_ip, build_app_send(
            device_name, random.randrange(1_000_000), 24, "00020000", "", "",
        ))
        records: list[str] = []
        for obj in session.collect(10.0):
            msg = extract_msg(obj)
            if msg and get_message_cmd_code(obj) == 17:
                value = msg.get("data_str2") or msg.get("rev_str2")
                if isinstance(value, str):
                    records.append(value)
                    if value == "NAME_OVER":
                        break

        return paired, records


def make_report_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", LOCAL_PORT))
    except OSError:
        sock.close()
        raise SystemExit(
            f"UDP port {LOCAL_PORT} is already in use, and the hub ignores commands from any\n"
            "other source port. Something else is talking to it -- usually Home Assistant.\n"
            "Stop that (e.g. 'docker stop <your-home-assistant-container>', or disable the\n"
            "ELRO Connects integration) and re-run this report."
        ) from None
    # Short timeout so collect() polls its deadline rather than blocking past it.
    sock.settimeout(0.5)
    return sock


def discover_hub(broadcast: str, timeout: float) -> tuple[str, str] | None:
    """Broadcast IOT_KEY? and return (ip, devID) of the first hub that answers."""
    with make_report_socket() as sock:
        sock.sendto(encrypt_message(build_discovery()), (broadcast, UDP_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, (host, _port) = sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            _text, obj = decrypt_message(packet)
            name = get_message_device_name(obj) if obj else None
            if name and name != "NULL":
                return host, name
    return None


# -- reporting -----------------------------------------------------------------

def print_report(paired: set[int], records: list[str]) -> None:
    print(f"Paired sub-devices (CMD_CODE 55): {sorted(paired) or 'none returned'}")
    print(f"Name records received (CMD_CODE 17): {len(records)} "
          f"({len(set(records))} unique)")
    print()

    # The hub always closes a name sync with NAME_OVER, even with nothing to
    # send, so silence means the exchange did not happen.  Concluding "nothing
    # is named" from that would be exactly backwards.
    if not records:
        print("The hub sent no CMD_CODE 17 frames at all, not even the NAME_OVER that ends")
        print("an empty name sync. The capture failed rather than your devices being")
        print("unnamed -- usually the hub was still busy. Please run the report again.")
        return

    # The hub retransmits, so the same record can arrive more than once.  Collapse
    # them rather than listing each copy, but say how many arrived -- a report
    # showing six frames for three names invites the wrong conclusion.
    repeats: dict[str, int] = {}
    for record in records:
        repeats[record] = repeats.get(record, 0) + 1

    named: list[int] = []
    problems: list[tuple[int | None, str]] = []
    for record, count in repeats.items():
        seen = f"  (retransmitted, {count} copies)" if count > 1 else ""
        if record == "NAME_OVER":
            print(f"  NAME_OVER          (end of stream){seen}")
            continue
        sub_id, verdict, is_name = decode_record(record)
        label = f"sub_id={sub_id}" if sub_id is not None else "sub_id=?"
        print(f" {' ' if is_name else '!'} {label:<11} len={len(record):<3} {verdict}{seen}")
        print(f"      raw={record}")
        if is_name and sub_id is not None:
            named.append(sub_id)
        else:
            problems.append((sub_id, verdict))

    print()
    print("-- Summary --")
    if not paired:
        print("No CMD_CODE 55 status records were captured, so the nicknames above cannot be")
        print("checked against the full device list. Re-run to compare them.")
        return

    if problems:
        print("Records the library could not decode -- these are library bugs:")
        for sub_id, verdict in problems:
            print(f"  sub_id={sub_id}: {verdict}")

    silent = sorted(s for s in paired if s not in named and s not in {p for p, _ in problems})
    if silent:
        print(f"Paired but the hub sent no name record at all: {silent}")
        print("  The hub stores no nickname for these. The ELRO app may still show a name")
        print("  for them -- it falls back to its own local database. Renaming the device")
        print("  in the app pushes the name to the hub and fixes it.")

    if not problems and not silent:
        print("Every paired sub-device has a nickname. Nothing wrong here.")

    print()
    print("-- Please also answer --")
    print("1. Is it the SAME sub-device missing its nickname every run, or a different one")
    print("   each time? Run this report two or three times to check. A different one each")
    print("   time points at timing; the same one every time points at that device's name.")
    print("2. What exact name did you give the affected device in the ELRO app? Check it")
    print('   with:  tools/name_sync_report.py --check-name "the name"')
    print("   Names over 15 bytes, or containing @ or $, cannot survive the round trip --")
    print("   and the app's naming screen during pairing enforces neither rule.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect a pasteable nickname-sync report from an ELRO Connects K2 hub.",
    )
    parser.add_argument("--gateway-ip", help="Skip discovery and use this gateway IP.")
    parser.add_argument("--device-name", help="Gateway devID, e.g. ST_1234567890.")
    parser.add_argument("--broadcast", default="255.255.255.255", help="Discovery broadcast address.")
    parser.add_argument("--timeout", type=float, default=8.0, help="Discovery timeout in seconds.")
    parser.add_argument("--raw", action="store_true", help="Keep the gateway IP and devID in the output.")
    parser.add_argument("--check-name", metavar="NAME",
                        help="Validate a nickname against the wire encoding and exit. Needs no hub.")
    args = parser.parse_args()

    if args.check_name is not None:
        return check_name(args.check_name)

    print("== ELRO Connects K2 nickname sync report ==")
    print()

    gateway_ip, device_name = args.gateway_ip, args.device_name
    if not gateway_ip or not device_name:
        print("Discovering hub ...")
        found = discover_hub(args.broadcast, args.timeout)
        if found is None:
            print("No hub found on the LAN.", file=sys.stderr)
            print("Re-run with --gateway-ip and --device-name if you know them.", file=sys.stderr)
            return 1
        gateway_ip = gateway_ip or found[0]
        device_name = device_name or found[1]

    print("Hub found. Querying sub-devices and nicknames ...")
    print()
    paired, records = run_report(gateway_ip, device_name)
    print_report(paired, records)

    if not args.raw:
        print()
        print("(gateway IP and devID omitted; pass --raw to include them)")
    else:
        print()
        print(f"Gateway: {device_name} @ {gateway_ip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
