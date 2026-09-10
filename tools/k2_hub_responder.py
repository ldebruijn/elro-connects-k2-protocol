#!/usr/bin/env python3
"""A request/response fake K2 hub, with switchable firmware quirks.

`tools/k2_simulator.py` is a *pusher*: it emits frames on a schedule and never
answers anything.  That is the right shape for developing entity handling, and
the wrong shape for testing a session, because a session is defined by what the
hub says back.  This responder answers.

## What it is for

Two jobs, both of which a healthy hub cannot do:

1. **Prove the parity harness discriminates.** `tools/k2_app_parity.py` is only
   worth running against hardware if a failure would actually show up in it.
   A hub that works passes everything, so it cannot demonstrate that.  Start
   this responder with a quirk enabled, point the harness at it, and check that
   the matching experiment is the one that fails.

2. **Reproduce a deaf hub locally.** `--quirk deaf` is the reported failure
   verbatim: acks every `IOT_KEY?`, answers no command.  Useful for checking
   what the library logs and what Home Assistant surfaces in that state, without
   needing the hub that does it.

## Quirks

  (none)             a healthy hub: arms on any IOT_KEY?, answers everything
  deaf               acks IOT_KEY?, silently drops every APP_SEND
  require-cmd12      drops CMD 54 and CMD 24 until a CMD 12 has been answered
  require-unquoted   arms only on the app's unquoted-devID IOT_KEY?
  broadcast-arms     a devID:"NULL" IOT_KEY? arms it too, not just a targeted one
  require-broadcast  arms only on a devID:"NULL" IOT_KEY?
  dedupe-msgid       drops an APP_SEND whose msg_ID was already seen
  no-activation-gate arms from the start; APP_SEND works with no IOT_KEY? at all

Quirks compose, so `--quirk require-cmd12 --quirk dedupe-msgid` is a hub that
wants its gateway-info handshake *and* refuses retransmits.

## Usage

    # healthy hub on a spare port, so it does not fight anything on 1025
    python tools/k2_hub_responder.py --port 11025

    # in another shell
    python tools/k2_app_parity.py --no-discover --gateway-ip 127.0.0.1 \
        --target-port 11025 --device-name SIM_HUB

    # a hub that needs the vendor's CMD 12 root -- cmd12-preamble and app-chain
    # should pass while baseline fails
    python tools/k2_hub_responder.py --port 11025 --quirk require-cmd12

Stdlib only, single file, no venv -- same rules as the probe.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any

XOR_CONST = 0x23

# Paced like real hardware: a field report on an eight-device hub put the gap
# between CMD_CODE 17 frames at 350-400 ms.  A responder that answers instantly
# would let a client with too tight an idle timeout pass here and truncate the
# stream against real hardware.
NAME_FRAME_GAP = 0.38
# Real hubs take a few milliseconds to tens of milliseconds to answer an
# IOT_KEY?; instant replies would hide races that hardware exposes.
ACK_DELAY = 0.02

# Three photoelectric smoke alarms, the same records tests/fixtures uses.
SYNC_RECORDS = ("0100134057AA55", "020013305FAA34", "030013305FAA9E")
# sub_id -> nickname.  Sub 3 is deliberately unnamed: the hub only stores names
# that were explicitly set, so a paired-but-unnamed device produces no frame,
# and a client that waits for "a name for every sub_id" hangs forever.
NICKNAMES = {1: "Hallway", 2: "Kitchen"}


# ── framing ───────────────────────────────────────────────────────────────────


def compact_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def encrypt_message(message: str) -> bytes:
    seed = random.randrange(256)
    key = seed ^ XOR_CONST
    return bytes([seed]) + bytes(byte ^ key for byte in message.encode("utf-8"))


def decrypt_message(packet: bytes) -> tuple[str, dict[str, Any] | None]:
    if not packet:
        return "", None
    key = packet[0] ^ XOR_CONST
    plain = bytes(byte ^ key for byte in packet[1:])
    text = plain.decode("utf-8", errors="replace").rstrip("\x00")
    if "}}" in text:
        text = text[: text.index("}}") + 2]
    elif "}" in text:
        text = text[: text.index("}") + 1]
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


def encode_name_record(sub_id: int, name: str) -> str:
    """Build a 36-char CMD_CODE 17 record the library's decoder accepts.

    16-byte GBK field: '@' padding, then the name, then '$' as terminator --
    the scheme CoderUtils.getAscii writes and getStringFromAscii reads back.
    """
    body = name.encode("gbk", errors="replace") + b"$"
    padded = b"@" * (16 - len(body)) + body
    return f"{sub_id:04X}" + padded.hex().upper()


# ── the hub ───────────────────────────────────────────────────────────────────


@dataclass
class Pending:
    """A frame the hub has decided to send, and when."""
    at: float
    payload: str
    peer: tuple[str, int]


@dataclass
class Hub:
    device_name: str
    quirks: set[str]
    verbose: bool
    sock: socket.socket

    armed: bool = False
    answered_cmd12: bool = False
    seen_msg_ids: set[int] = field(default_factory=set)
    queue: list[Pending] = field(default_factory=list)
    msg_id: int = 1000

    def __post_init__(self) -> None:
        if "no-activation-gate" in self.quirks:
            self.armed = True

    # ── outbound ──────────────────────────────────────────────────────────────

    def next_msg_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    def schedule(self, payload: str, peer: tuple[str, int], delay: float = 0.0) -> None:
        self.queue.append(Pending(at=time.monotonic() + delay, payload=payload, peer=peer))

    def node_ack(self, peer: tuple[str, int]) -> None:
        """The IOT_KEY? answer.  Carries CMD_CODE 0 and no msg_ID."""
        self.schedule(
            compact_json({
                "action": "NODE_ACK",
                "devID": self.device_name,
                "msg": {"CMD_CODE": 0},
            }),
            peer,
            ACK_DELAY,
        )

    def node_send(
        self, peer: tuple[str, int], cmd_code: int, data_str1: str = "",
        data_str2: str = "", delay: float = 0.0,
    ) -> None:
        self.schedule(
            compact_json({
                "action": "NODE_SEND",
                "devID": self.device_name,
                "msg": {
                    "msg_ID": self.next_msg_id(),
                    "CMD_CODE": cmd_code,
                    "data_str1": data_str1,
                    "data_str2": data_str2,
                    "data_str3": "",
                },
            }),
            peer,
            delay,
        )

    def command_ack(self, peer: tuple[str, int], acked_code: int) -> None:
        """The CMD_CODE 11 every APP_SEND draws before its real answer."""
        self.node_send(peer, 11, str(acked_code), "OK", delay=0.01)

    def flush(self) -> None:
        now = time.monotonic()
        due = [p for p in self.queue if p.at <= now]
        self.queue = [p for p in self.queue if p.at > now]
        for pending in due:
            if self.verbose:
                print(f"  > {pending.peer[0]}:{pending.peer[1]} {pending.payload}")
            self.sock.sendto(encrypt_message(pending.payload), pending.peer)

    @property
    def next_deadline(self) -> float | None:
        return min((p.at for p in self.queue), default=None)

    # ── inbound ───────────────────────────────────────────────────────────────

    def handle(self, text: str, obj: dict[str, Any] | None, peer: tuple[str, int]) -> None:
        if self.verbose:
            print(f"  < {peer[0]}:{peer[1]} {text}")

        # IOT_KEY? is matched on the raw text, not the parsed object, because the
        # app's targeted form is not valid JSON -- an unquoted devID.  A hub that
        # only ever saw parsed frames could not tell the two forms apart, which
        # is the whole point of the require-unquoted quirk.
        if "IOT_KEY?" in text:
            self.handle_iot_key(text, obj, peer)
            return

        if obj is None:
            print(f"  !! undecodable frame from {peer[0]}: {text!r}")
            return

        action = obj.get("action")
        if action == "APP_ACK":
            return  # the client acknowledging us; nothing to do
        if action == "APP_SEND":
            self.handle_app_send(obj, peer)

    def handle_iot_key(self, text: str, obj: dict[str, Any] | None, peer: tuple[str, int]) -> None:
        """Answer every ping, but arm only when the quirks say so.

        The unconditional NODE_ACK is the documented behaviour: IOT_KEY? is
        handled below the command dispatcher, which is exactly why "the hub acks
        but ignores commands" is a reachable state rather than a contradiction.

        The default arming rule is what protocol_reference.md item 2 claims a K2
        does -- a *targeted* ping arms it, a broadcast one does not. That is a
        belief, not a measurement, and encoding it here is deliberate: with the
        default responder the harness's negative controls come out as predicted,
        so a surprise against hardware is a surprise about the hub rather than
        about this file.
        """
        quoted = f'"devID":"{self.device_name}"' in text
        unquoted = f'"devID":{self.device_name}' in text
        broadcast = '"devID":"NULL"' in text
        targeted = quoted or unquoted

        self.node_ack(peer)

        if not (targeted or broadcast):
            print("  .. IOT_KEY? acked but NOT arming: devID is not mine")
            return
        if "require-unquoted" in self.quirks and not unquoted:
            print("  .. IOT_KEY? acked but NOT arming: devID was quoted (require-unquoted)")
            return
        if "require-broadcast" in self.quirks and not broadcast:
            print("  .. IOT_KEY? acked but NOT arming: not the broadcast form "
                  "(require-broadcast)")
            return
        if broadcast and not ("broadcast-arms" in self.quirks or "require-broadcast" in self.quirks):
            print("  .. IOT_KEY? acked but NOT arming: broadcast form, and a targeted "
                  "ping is required")
            return

        if not self.armed:
            print(f"  ** armed by IOT_KEY? ({'broadcast' if broadcast else 'unicast'}"
                  f"{', unquoted' if unquoted else ''})")
        self.armed = True

    def handle_app_send(self, obj: dict[str, Any], peer: tuple[str, int]) -> None:
        msg = obj.get("msg")
        if not isinstance(msg, dict):
            return
        cmd_code = msg.get("CMD_CODE")
        msg_id = msg.get("msg_ID")
        if not isinstance(cmd_code, int):
            return

        if obj.get("devID") != self.device_name:
            print(f"  .. CMD {cmd_code} dropped: addressed to {obj.get('devID')!r}")
            return
        if "deaf" in self.quirks:
            print(f"  .. CMD {cmd_code} dropped: quirk 'deaf'")
            return
        if not self.armed:
            print(f"  .. CMD {cmd_code} dropped: session not armed")
            return
        if "dedupe-msgid" in self.quirks and isinstance(msg_id, int):
            if msg_id in self.seen_msg_ids:
                print(f"  .. CMD {cmd_code} dropped: msg_ID {msg_id} already seen "
                      f"(quirk 'dedupe-msgid')")
                return
            self.seen_msg_ids.add(msg_id)

        if cmd_code in (54, 24) and "require-cmd12" in self.quirks and not self.answered_cmd12:
            print(f"  .. CMD {cmd_code} dropped: no CMD 12 handshake yet "
                  f"(quirk 'require-cmd12')")
            return

        if cmd_code == 12:
            self.command_ack(peer, 12)
            # data_str2 leads with the gateway room, which the app slices off as
            # data_str2[0:2] before it will send CMD 54.
            self.node_send(peer, 13, "01", "05" + "0" * 30, delay=0.05)
            self.answered_cmd12 = True
            print("  -> CMD 13 (gateway info)")
            return

        if cmd_code == 54:
            self.command_ack(peer, 54)
            self.node_send(
                peer, 55,
                "".join(SYNC_RECORDS[:2]),
                SYNC_RECORDS[2],
                delay=0.06,
            )
            print(f"  -> CMD 55 ({len(SYNC_RECORDS)} records)")
            return

        if cmd_code == 24:
            self.command_ack(peer, 24)
            delay = 0.1
            for sub_id, nickname in NICKNAMES.items():
                self.node_send(peer, 17, "", encode_name_record(sub_id, nickname), delay=delay)
                delay += NAME_FRAME_GAP
            self.node_send(peer, 17, "", "NAME_OVER", delay=delay)
            print(f"  -> CMD 17 x{len(NICKNAMES)} + NAME_OVER over {delay:.1f} s")
            return

        # Anything else still draws the ACK a real hub sends for every APP_SEND.
        self.command_ack(peer, cmd_code)
        print(f"  -> CMD 11 only (no handler for CMD {cmd_code})")


QUIRKS = (
    "deaf",
    "require-cmd12",
    "require-unquoted",
    "broadcast-arms",
    "require-broadcast",
    "dedupe-msgid",
    "no-activation-gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A request/response fake K2 hub with switchable firmware quirks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device-name", default="SIM_HUB")
    parser.add_argument("--port", type=int, default=11025,
                        help="Port to listen on. Default 11025 stays clear of a real 1025.")
    parser.add_argument("--quirk", action="append", choices=QUIRKS, default=[],
                        help="Enable a firmware quirk. Repeatable.")
    parser.add_argument("--verbose", action="store_true", help="Print every frame both ways.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", args.port))
    except OSError as exc:
        print(f"Could not bind UDP port {args.port}: {exc}", file=sys.stderr)
        return 1

    quirks = set(args.quirk)
    hub = Hub(device_name=args.device_name, quirks=quirks, verbose=args.verbose, sock=sock)

    print(f"Fake K2 {args.device_name} listening on UDP {args.port}")
    print(f"Quirks: {', '.join(sorted(quirks)) if quirks else '(none -- healthy hub)'}")
    print(f"Armed: {hub.armed}\n")
    print("Point the harness at it with:")
    print("  python tools/k2_app_parity.py --no-discover --gateway-ip 127.0.0.1 \\")
    print(f"      --target-port {args.port} --device-name {args.device_name}\n")
    print("Ctrl-C to stop.\n")

    with sock:
        try:
            while True:
                deadline = hub.next_deadline
                timeout = 0.1 if deadline is None else max(0.0, min(0.1, deadline - time.monotonic()))
                sock.settimeout(timeout)
                try:
                    packet, peer = sock.recvfrom(4096)
                except TimeoutError:
                    hub.flush()
                    continue
                except OSError:
                    hub.flush()
                    continue
                text, obj = decrypt_message(packet)
                hub.handle(text, obj, peer)
                hub.flush()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
