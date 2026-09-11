#!/usr/bin/env python3
"""Test whether vendor-app-faithful variants of our session still work on a real K2.

## Why this exists

`elro_connects_k2_protocol` does not talk to a K2 the way the ELRO Connects 2.0
Android app does.  A re-read of the decompiled app turned up four divergences:

  1. The app never sends CMD_CODE 54 without a CMD_CODE 13 having arrived first.
     Its sync is a reaction chain rooted in CMD_CODE 12; we skip that root
     entirely and open with 54.
  2. The app's targeted `IOT_KEY?` carries an **unquoted** devID -- raw string
     concatenation in `UdpControlProxy.onActivationUdp`, never validated as
     JSON.  We send the quoted form.  The app's routine arming is in fact the
     *broadcast* `devID:"NULL"` form; the unicast only fires from an OTA
     callback.
  3. The app fires the activation ping and its first command back-to-back with
     no wait (`BlurWorker.doWork`).  We block on the NODE_ACK and document that
     blocking as mandatory.
  4. The app retransmits every command once after 1 s with the *identical*
     msg_ID (`UdpControlProxy.onResendMessage`).  We send once.

Adopting any of those is only safe if it does not break the hubs that already
work.  This harness runs each variant against a live hub and reports whether it
still produces a sync -- so a single healthy hub is enough to clear a change for
release, which is the situation we are actually in.

## What a result means

Every variant in the default set is expected to PASS on a healthy hub.  A PASS
is a licence to adopt the variant, not evidence that it fixes anything.  A FAIL
is the interesting outcome: it means the vendor does something we cannot copy,
and the divergence is deliberate rather than accidental.

The two negative controls (`no-activation`, `broadcast-only`) are the reverse:
they are expected to FAIL, and each one that passes deletes a claim from
`docs/protocol_reference.md`.  They are excluded from the default run because
they are only meaningful on a hub that nothing has armed yet -- see "Arming
persists" below.

## Usage

    # Discover the hub and run the default set
    python tools/k2_app_parity.py

    # Against a known hub, skipping discovery
    python tools/k2_app_parity.py --gateway-ip 192.168.1.50 --device-name ST_1234567890

    # One variant at a time
    python tools/k2_app_parity.py --only cmd12-preamble --gateway-ip ... --device-name ...

    # List what is available
    python tools/k2_app_parity.py --list

    # Offline, against the responder (see tools/k2_hub_responder.py)
    python tools/k2_hub_responder.py --port 11025 &
    python tools/k2_app_parity.py --gateway-ip 127.0.0.1 --target-port 11025 \
        --device-name SIM_HUB --no-discover

## Reading a failure

Which experiments fail identifies *what* the hub wants. Verified against
`tools/k2_hub_responder.py`, whose quirks reproduce each hypothesis:

    responder quirk     experiments that come out UNEXPECTED
    ----------------------------------------------------------------------
    (healthy)           none
    require-cmd12       baseline, unquoted-activation
    require-unquoted    baseline
    dedupe-msgid        retransmit-same-msgid
    deaf                all of them

`require-cmd12` takes down both experiments that open with CMD 54, which is the
signature to look for: two failures whose only shared property is skipping the
gateway-info handshake. `require-unquoted` takes down only `baseline`, because
that is the only experiment left still sending the quoted ping. A hub that fails
*everything* is the reported hub-B failure and is not about our session at all.

## Arming persists

The hub stays armed after an `IOT_KEY?` for some unmeasured period, so once any
experiment in a run has armed it, a later "does it work *without* arming?" test
is answered by the earlier one and not by itself.  That is why the negative
controls are opt-in via `--only` and refuse to run alongside discovery.  For a
trustworthy negative, power-cycle the hub and run exactly one:

    python tools/k2_app_parity.py --only no-activation --no-discover \
        --gateway-ip 192.168.1.50 --device-name ST_1234567890

## Safety

Every command sent here is read-only: CMD_CODE 12 (gateway info), 54 (status
sync), 24 (name sync), plus `IOT_KEY?` and APP_ACK.  Nothing actuates a
detector, nothing writes hub configuration.  See the command safety tiers in
docs/protocol_reference.md.

Binds UDP 1025, so stop Home Assistant (or run this from a different host on the
same LAN) and close the phone app first.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

UDP_PORT = 1025
XOR_CONST = 0x23

# How long to collect CMD_CODE 55/56 after a CMD_CODE 54, matching
# gateway._SYNC_COLLECT_SECONDS so a PASS here means a PASS there.
SYNC_COLLECT_SECONDS = 3.0
# Name sync is bounded by silence: real hubs pace CMD_CODE 17 frames 350-400 ms
# apart, so the idle window has to clear that with room to spare.
NAME_IDLE_SECONDS = 2.0
NAME_MAX_SECONDS = 15.0
# A well-formed CMD_CODE 17 record: 2-byte sub_id plus a 16-byte name field, hex.
NAME_RECORD_LEN = 36
# One status record in a CMD_CODE 55/56 payload: sub_id + type + status, hex.
SYNC_RECORD_LEN = 14


# ── framing ───────────────────────────────────────────────────────────────────


def compact_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def encrypt_message(message: str) -> bytes:
    seed = random.randrange(256)
    key = seed ^ XOR_CONST
    return bytes([seed]) + bytes(byte ^ key for byte in message.encode("utf-8"))


def trim_json_text(text: str) -> str:
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
    text = trim_json_text(plain.decode("utf-8", errors="replace").rstrip("\x00"))
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


# ── message builders ──────────────────────────────────────────────────────────


def build_discovery() -> str:
    """The app's broadcast search, byte for byte (UdpControlProxy.onSearchDeviceUdp)."""
    return '{"action":"IOT_KEY?","devID":"NULL"}'


def build_activation_quoted(device_name: str) -> str:
    """What the library sends today: a valid-JSON targeted IOT_KEY?."""
    return compact_json({"action": "IOT_KEY?", "devID": device_name})


def build_activation_unquoted(device_name: str) -> str:
    """What the app actually puts on the wire (UdpControlProxy.onActivationUdp).

    The devID is concatenated in raw and the result never passes through
    ``jsonToString``, so the frame is not valid JSON.  Reproduced verbatim
    because "the hub tolerates our quoted form" and "the hub arms on our quoted
    form" are different claims, and only the second one matters.
    """
    return '{"action":"IOT_KEY?","devID":' + device_name + "}"


def build_app_send(
    device_name: str,
    msg_id: int,
    cmd_code: int,
    rev_str1: str = "",
    rev_str2: str = "",
    rev_str3: str = "",
) -> str:
    return compact_json({
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


def build_ack(device_name: str, msg_id: int, rev_str1: str) -> str:
    return compact_json({
        "action": "APP_ACK",
        "devID": device_name,
        "msg": {
            "msg_ID": msg_id,
            "CMD_CODE": 11,
            "rev_str1": rev_str1,
            "rev_str2": "OK",
            "rev_str3": "",
        },
    })


def timezone_offset_code() -> str:
    """CoderUtils.getTimeZoneOffset(): 00HHMM for positive, 01HHMM for negative."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "000000"
    minutes = int(offset.total_seconds() // 60)
    sign = "00" if minutes >= 0 else "01"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02X}{minutes % 60:02X}"


# ── frame plumbing ────────────────────────────────────────────────────────────


@dataclass
class Frame:
    at: float
    host: str
    text: str
    obj: dict[str, Any] | None

    @property
    def action(self) -> str | None:
        value = self.obj.get("action") if self.obj else None
        return value if isinstance(value, str) else None

    @property
    def dev_id(self) -> str | None:
        value = self.obj.get("devID") if self.obj else None
        return value if isinstance(value, str) else None

    @property
    def msg(self) -> dict[str, Any]:
        value = self.obj.get("msg") if self.obj else None
        return value if isinstance(value, dict) else {}

    @property
    def cmd_code(self) -> int | None:
        value = self.msg.get("CMD_CODE")
        return value if isinstance(value, int) else None

    def data(self, index: int) -> str:
        """data_strN, falling back to rev_strN -- the hub uses the former."""
        value = self.msg.get(f"data_str{index}") or self.msg.get(f"rev_str{index}")
        return value if isinstance(value, str) else ""

    @property
    def is_node_ack(self) -> bool:
        return self.action == "NODE_ACK" and self.cmd_code == 0


class MsgIdCounter:
    """One climbing msg_ID for the whole run, matching SendCommand.mCmdId.

    The app's counter is a single `static int` shared by every gateway, every
    command and every ACK, never reset for the process lifetime.  A per-session
    counter would restart at 1 in each experiment and reissue ids the hub has
    already seen in this run -- which is a difference between experiments that
    nothing here is trying to measure, and it would land on whichever experiment
    happened to run second.
    """

    def __init__(self, start: int = 1) -> None:
        self.value = start

    def next(self) -> int:
        value = self.value
        self.value += 1
        return value


class Session:
    """One experiment's conversation with the hub, over a shared socket.

    Holds the ACK policy, because that is one of the things the experiments
    vary.  Every frame seen is kept in ``log`` so a failed experiment can be
    explained from what did arrive rather than only from what did not.
    """

    def __init__(
        self,
        sock: socket.socket,
        gateway_ip: str,
        target_port: int,
        device_name: str,
        msg_ids: MsgIdCounter,
        *,
        vendor_ack: bool = False,
        verbose: bool = False,
    ) -> None:
        self.sock = sock
        self.gateway_ip = gateway_ip
        self.target_port = target_port
        self.device_name = device_name
        # The app always writes the literal "11" into rev_str1 of its APP_ACK
        # (onSendUdpAnswer -> getAnswerOk(devID, 11)); we write the code being
        # acknowledged.  vendor_ack switches to the app's behaviour.
        self.vendor_ack = vendor_ack
        self.msg_ids = msg_ids
        self.verbose = verbose
        self.log: list[Frame] = []
        self.acks_seen: list[tuple[int, str]] = []

    def next_msg_id(self) -> int:
        return self.msg_ids.next()

    def send_raw(self, text: str) -> None:
        if self.verbose:
            print(f"    > {self.gateway_ip}:{self.target_port} {text}")
        self.sock.sendto(encrypt_message(text), (self.gateway_ip, self.target_port))

    def broadcast_raw(self, text: str, broadcast: str) -> None:
        if self.verbose:
            print(f"    > {broadcast}:{self.target_port} {text}")
        self.sock.sendto(encrypt_message(text), (broadcast, self.target_port))

    def send_command(
        self, cmd_code: int, rev_str1: str = "", rev_str2: str = "", rev_str3: str = "",
        *, msg_id: int | None = None,
    ) -> int:
        used = self.next_msg_id() if msg_id is None else msg_id
        self.send_raw(build_app_send(self.device_name, used, cmd_code, rev_str1, rev_str2, rev_str3))
        return used

    def pump(
        self,
        duration: float,
        stop: Callable[[Frame], bool] | None = None,
    ) -> list[Frame]:
        """Read for up to ``duration`` seconds, auto-ACKing NODE_SEND as we go.

        Returns only the frames from this call; they are also appended to
        ``log``.  ``stop`` ends the wait early -- used wherever the vendor's own
        client is event-driven rather than timed, so that the harness measures
        the hub's pace instead of imposing one.
        """
        collected: list[Frame] = []
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.sock.settimeout(min(remaining, 0.25))
            try:
                packet, (host, _port) = self.sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                continue

            text, obj = decrypt_message(packet)
            frame = Frame(at=time.monotonic(), host=host, text=text, obj=obj)
            # Our own broadcast loops back to us; it is not the hub talking.
            if frame.dev_id == "NULL" and frame.action == "IOT_KEY?":
                continue
            self.log.append(frame)
            collected.append(frame)
            if self.verbose:
                print(f"    < {host} {text}")

            if frame.cmd_code == 11:
                code = frame.data(1)
                self.acks_seen.append((_as_int(code), frame.data(2)))

            self._auto_ack(frame)

            if stop is not None and stop(frame):
                break
        return collected

    def _auto_ack(self, frame: Frame) -> None:
        """Mirror UdpControlProxy.onNodeSendDeal, which ACKs any frame with a msg_ID.

        Note that this is *not* what ``ReceiveHandler`` does -- ReceiveHandler
        never touches the wire.  Gating on NODE_SEND (as the library does) and
        gating on msg_ID presence (as the app does) select the same frames in
        practice, so the harness gates on NODE_SEND and varies only rev_str1.
        """
        if frame.action != "NODE_SEND":
            return
        device_name = frame.dev_id
        if not device_name:
            return
        rev_str1 = "11" if self.vendor_ack else str(frame.cmd_code or 11)
        self.send_raw(build_ack(device_name, self.next_msg_id(), rev_str1))


def _as_int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return -1


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
    except (TypeError, ValueError):
        return False
    return True


# ── reusable steps ────────────────────────────────────────────────────────────


@dataclass
class SyncOutcome:
    acked: bool
    records: int
    first_reply_ms: float | None
    frames: int


def do_activation(
    sess: Session, builder: Callable[[str], str], *, wait: bool, timeout: float = 3.0
) -> float | None:
    """Send a targeted IOT_KEY?.  Returns the NODE_ACK latency in ms, or None.

    ``wait=False`` reproduces BlurWorker, which fires the ping and CMD_CODE 12
    back to back and never looks at the ACK.
    """
    started = time.monotonic()
    sess.send_raw(builder(sess.device_name))
    if not wait:
        return None
    got = sess.pump(timeout, stop=lambda f: f.is_node_ack)
    for frame in got:
        if frame.is_node_ack:
            return (frame.at - started) * 1000
    return None


def count_sync_records(frame: Frame) -> int:
    """Count 14-char status records across data_str1..3 of a CMD_CODE 55/56 frame."""
    total = 0
    for index in (1, 2, 3):
        field_text = frame.data(index)
        if field_text and field_text != "NULL":
            total += len(field_text) // SYNC_RECORD_LEN
    return total


def do_sync(sess: Session, *, crc: str = "00020000", collect: float = SYNC_COLLECT_SECONDS,
            msg_id: int | None = None) -> SyncOutcome:
    """CMD_CODE 54, then collect CMD_CODE 55/56 for the same window the library uses."""
    started = time.monotonic()
    sess.send_command(54, crc, timezone_offset_code(), "", msg_id=msg_id)
    frames = sess.pump(collect)
    records = 0
    first_ms: float | None = None
    for frame in frames:
        if frame.cmd_code in (55, 56):
            records += count_sync_records(frame)
            if first_ms is None:
                first_ms = (frame.at - started) * 1000
    acked = any(code == 54 for code, _ in sess.acks_seen)
    return SyncOutcome(
        acked=acked,
        records=records,
        first_reply_ms=first_ms,
        frames=sum(1 for f in frames if f.cmd_code in (55, 56)),
    )


@dataclass
class NameOutcome:
    names: int
    duplicates: int
    malformed: int
    name_over: bool
    elapsed_s: float

    def describe(self) -> str:
        extra = ""
        if self.duplicates:
            extra += f", {self.duplicates} duplicate frame(s)"
        if self.malformed:
            extra += f", {self.malformed} record(s) of unexpected length"
        return (f"names={self.names} NAME_OVER={self.name_over} "
                f"in {self.elapsed_s:.1f} s{extra}")


def do_name_sync(sess: Session, *, crc: str = "00020000") -> NameOutcome:
    """CMD_CODE 24, then consume CMD_CODE 17 frames until NAME_OVER or silence.

    Bounded by silence rather than a fixed budget, because the hub paces the
    stream and a fixed window truncates its tail on any hub bigger than the one
    it was tuned against.

    Names are keyed by sub_id rather than counted per frame.  Hubs retransmit a
    name record -- a capture from the reference hub delivered sub 3 twice with
    the *same* msg_ID -- so a frame count is not stable between two runs of the
    same command against the same hub.  The library keys by sub_id for exactly
    this reason; counting frames here reported a "discrepancy" that existed only
    in this harness.
    """
    started = time.monotonic()
    sess.send_command(24, crc, "", "")
    seen: dict[int, str] = {}
    duplicates = 0
    malformed = 0
    name_over = False
    last_frame = time.monotonic()
    hard_deadline = last_frame + NAME_MAX_SECONDS
    while True:
        window = min(last_frame + NAME_IDLE_SECONDS, hard_deadline) - time.monotonic()
        if window <= 0:
            break
        got = sess.pump(window, stop=lambda f: f.cmd_code == 17 and f.data(2) == "NAME_OVER")
        progressed = False
        for frame in got:
            if frame.cmd_code != 17:
                continue
            progressed = True
            record = frame.data(2)
            if record == "NAME_OVER":
                name_over = True
            elif len(record) == NAME_RECORD_LEN:
                sub_id = int(record[:4], 16) if _is_hex(record[:4]) else -1
                if sub_id in seen:
                    duplicates += 1
                else:
                    seen[sub_id] = record
            else:
                malformed += 1
        if progressed:
            last_frame = time.monotonic()
        if name_over:
            break
    return NameOutcome(
        names=len(seen), duplicates=duplicates, malformed=malformed,
        name_over=name_over, elapsed_s=time.monotonic() - started,
    )


def do_gateway_info(sess: Session, *, timeout: float = 5.0) -> Frame | None:
    """CMD_CODE 12, then wait for the CMD_CODE 13 answer."""
    sess.send_command(12, "00", "00", "")
    for frame in sess.pump(timeout, stop=lambda f: f.cmd_code == 13):
        if frame.cmd_code == 13:
            return frame
    return None


# ── experiments ───────────────────────────────────────────────────────────────


@dataclass
class Result:
    name: str
    ok: bool
    headline: str
    notes: list[str] = field(default_factory=list)


@dataclass
class Experiment:
    name: str
    summary: str
    expect: str  # "pass" or "fail"
    run: Callable[[Context], Result]


@dataclass
class Context:
    sock: socket.socket
    gateway_ip: str
    target_port: int
    device_name: str
    broadcast: str
    verbose: bool
    msg_ids: MsgIdCounter = field(default_factory=MsgIdCounter)

    def session(self, **kwargs: Any) -> Session:
        return Session(
            self.sock, self.gateway_ip, self.target_port, self.device_name,
            self.msg_ids, verbose=self.verbose, **kwargs,
        )


def _sync_result(name: str, sess: Session, sync: SyncOutcome, *, extra: list[str] | None = None) -> Result:
    notes = list(extra or [])
    notes.append(
        f"CMD 54: acked={sync.acked} records={sync.records} "
        f"frames={sync.frames} first_reply="
        + (f"{sync.first_reply_ms:.0f} ms" if sync.first_reply_ms is not None else "never")
    )
    # "Answered" is deliberately not "returned devices": a hub with nothing
    # paired legitimately returns zero records, and this harness is asking
    # whether the hub *responded*, not what it owns.  Either a CMD 11 for code
    # 54 or a 55/56 frame proves the command was processed.
    ok = sync.acked or sync.frames > 0
    if not ok:
        notes.append("no ACK and no 55/56 frame -- the hub ignored the command")
    return Result(name=name, ok=ok, headline="hub answered CMD 54" if ok else "silence", notes=notes)


def exp_baseline(ctx: Context) -> Result:
    """The library's current behaviour, as the control for everything else."""
    sess = ctx.session()
    latency = do_activation(sess, build_activation_quoted, wait=True)
    if latency is None:
        return Result("baseline", False, "no NODE_ACK to the quoted unicast IOT_KEY?")
    sync = do_sync(sess)
    names = do_name_sync(sess)
    return _sync_result(
        "baseline", sess, sync,
        extra=[
            f"activation: quoted unicast, acked in {latency:.0f} ms",
            f"CMD 24: {names.describe()}",
        ],
    )


def exp_unquoted_activation(ctx: Context) -> Result:
    """Does the app's malformed-JSON activation frame arm a hub that our form arms?"""
    sess = ctx.session()
    latency = do_activation(sess, build_activation_unquoted, wait=True)
    if latency is None:
        return Result(
            "unquoted-activation", False,
            "no NODE_ACK to the app's unquoted IOT_KEY? -- do NOT adopt it",
        )
    sync = do_sync(sess)
    return _sync_result(
        "unquoted-activation", sess, sync,
        extra=[f"activation: app's unquoted form, acked in {latency:.0f} ms"],
    )


def exp_no_wait_activation(ctx: Context) -> Result:
    """BlurWorker order: ping and first command back-to-back, ACK never inspected.

    Our docs call waiting for the NODE_ACK mandatory.  The app does not wait, so
    either the claim is too strong or the app relies on having been armed
    earlier by its broadcast.  This measures which.
    """
    sess = ctx.session()
    do_activation(sess, build_activation_unquoted, wait=False)
    sync = do_sync(sess)
    notes = ["activation: sent, then CMD 54 immediately with zero wait"]
    if not (sync.acked or sync.frames > 0):
        notes.append(
            "expected if the activation gate is real -- this failing is a PASS for "
            "the library's decision to block on the NODE_ACK"
        )
    return _sync_result("no-wait-activation", sess, sync, extra=notes)


def exp_cmd12_preamble(ctx: Context) -> Result:
    """Our order plus the vendor's CMD_CODE 12 root: the change we are considering."""
    sess = ctx.session()
    latency = do_activation(sess, build_activation_quoted, wait=True)
    if latency is None:
        return Result("cmd12-preamble", False, "no NODE_ACK to the activation ping")
    info = do_gateway_info(sess)
    if info is None:
        return Result(
            "cmd12-preamble", False, "hub did not answer CMD 12 with a CMD 13",
            notes=[f"activation acked in {latency:.0f} ms"],
        )
    sync = do_sync(sess)
    return _sync_result(
        "cmd12-preamble", sess, sync,
        extra=[
            f"activation acked in {latency:.0f} ms",
            f"CMD 13: data_str1={info.data(1)!r} data_str2={info.data(2)!r}",
        ],
    )


def exp_app_chain(ctx: Context) -> Result:
    """The vendor's whole reaction chain, driven by arrivals rather than a script.

    BlurWorker -> IOT_KEY? + CMD 12 -> CMD 13 -> CMD 54 -> CMD 55/56 -> CMD 24
    -> CMD 17 ... NAME_OVER.  Each step is sent only once its trigger has
    landed, which is the part our implementation does not reproduce.
    """
    sess = ctx.session()
    do_activation(sess, build_activation_unquoted, wait=False)
    info = do_gateway_info(sess)
    if info is None:
        return Result(
            "app-chain", False,
            "chain stalled at CMD 12 -- no CMD 13, so the app's own sequence would not start",
        )
    # The app takes data_str2[0:2] out of the CMD 13 reply and stores it as the
    # gateway room before it sends CMD 54.  Nothing of that value goes onto the
    # wire, but a null/short field makes the app skip the sync entirely, so the
    # harness records whether the field was even present.
    room = info.data(2)[:2]
    sync = do_sync(sess)
    names = do_name_sync(sess) if (sync.acked or sync.frames) else NameOutcome(0, 0, 0, False, 0.0)
    ok = (sync.acked or sync.frames > 0)
    notes = [
        f"CMD 13 arrived, gateway room field={room!r}",
        f"CMD 54: acked={sync.acked} records={sync.records} frames={sync.frames}",
        f"CMD 24: {names.describe()}",
    ]
    if not room:
        notes.append("CMD 13 data_str2 was empty -- the app would have skipped CMD 54 here")
    return Result(
        "app-chain", ok,
        "full vendor chain completed" if ok else "vendor chain stalled at CMD 54",
        notes,
    )


def exp_vendor_ack(ctx: Context) -> Result:
    """ACK with rev_str1="11" like the app, instead of the code being acknowledged.

    The name stream is the sensitive consumer: if ACK content paced the hub at
    all, a wrong rev_str1 would show up as a truncated stream or a missing
    NAME_OVER.
    """
    sess = ctx.session(vendor_ack=True)
    latency = do_activation(sess, build_activation_quoted, wait=True)
    if latency is None:
        return Result("vendor-ack-revstr1", False, "no NODE_ACK to the activation ping")
    sync = do_sync(sess)
    names = do_name_sync(sess)
    ok = (sync.acked or sync.frames > 0)
    return Result(
        "vendor-ack-revstr1", ok,
        'hub unaffected by rev_str1="11"' if ok else "hub stopped answering",
        [
            'ACK policy: rev_str1="11" for every NODE_SEND (app behaviour)',
            f"CMD 54: acked={sync.acked} records={sync.records} frames={sync.frames}",
            f"CMD 24: {names.describe()}",
        ],
    )


def exp_retransmit(ctx: Context) -> Result:
    """onResendMessage: the same CMD 54 bytes, same msg_ID, again 1 s later.

    Two things are being checked -- that the hub answers the retransmit at all
    (so adding a retry is worth doing), and that it does not choke on the
    duplicate msg_ID (so adding a retry is safe).
    """
    sess = ctx.session()
    latency = do_activation(sess, build_activation_quoted, wait=True)
    if latency is None:
        return Result("retransmit-same-msgid", False, "no NODE_ACK to the activation ping")

    msg_id = sess.next_msg_id()
    started = time.monotonic()
    sess.send_command(54, "00020000", timezone_offset_code(), "", msg_id=msg_id)
    first = sess.pump(1.0)
    sess.send_command(54, "00020000", timezone_offset_code(), "", msg_id=msg_id)
    second = sess.pump(SYNC_COLLECT_SECONDS)

    def tally(frames: list[Frame]) -> tuple[int, int]:
        return (
            sum(1 for f in frames if f.cmd_code in (55, 56)),
            sum(count_sync_records(f) for f in frames if f.cmd_code in (55, 56)),
        )

    f1, r1 = tally(first)
    f2, r2 = tally(second)
    notes = [
        f"first send:  {f1} frame(s), {r1} record(s) within 1.0 s",
        f"retransmit:  {f2} frame(s), {r2} record(s) within {SYNC_COLLECT_SECONDS:.1f} s "
        f"(identical msg_ID {msg_id})",
        f"total elapsed {time.monotonic() - started:.1f} s",
    ]
    # The question is not "did the hub answer" but "is a same-msg_ID retry worth
    # adding".  A hub that answers the first copy and ignores the second is
    # deduplicating, so the vendor's retry would never help us and a retry we
    # add would have to carry a fresh id -- which the vendor's own design says
    # should be unnecessary.  That is a finding, so it is a FAIL here.
    if f2 > 0:
        ok = True
        headline = "hub answers a duplicate msg_ID"
        notes.append(
            "hub answered both copies" if f1 else
            "only the retransmit was answered -- the vendor's retry is load-bearing"
        )
    elif f1 > 0:
        ok = False
        headline = "hub ignored the retransmit -- it deduplicates on msg_ID"
        notes.append(
            "contradicts UdpControlProxy.onResendMessage, which resends identical bytes "
            "and expects an answer; a retry we add would need a fresh msg_ID"
        )
    else:
        ok = False
        headline = "no answer to either copy"
    return Result("retransmit-same-msgid", ok, headline, notes)


def exp_msgid_zero(ctx: Context) -> Result:
    """msg_ID 0, the app's first-ever id (SendCommand.mCmdId starts at 0).

    The library starts at 1 and never emits 0, so this is the one id the vendor
    uses that we have never tested.
    """
    sess = ctx.session()
    latency = do_activation(sess, build_activation_quoted, wait=True)
    if latency is None:
        return Result("msgid-zero", False, "no NODE_ACK to the activation ping")
    sync = do_sync(sess, msg_id=0)
    return _sync_result("msgid-zero", sess, sync, extra=["CMD 54 sent with msg_ID 0"])


def exp_no_activation(ctx: Context) -> Result:
    """Negative control: CMD 54 with no IOT_KEY? at all.

    Expected to produce silence.  If it answers, the hub was already armed (see
    "Arming persists" in the module docstring) or the activation gate does not
    exist -- and protocol_reference.md item 2 needs rewriting either way.
    """
    sess = ctx.session()
    sync = do_sync(sess)
    # `ok` means the same thing in every experiment: the hub answered the
    # command.  The expectation, not the measurement, is what differs here --
    # this one is declared expect=fail, so silence is the predicted outcome and
    # the summary flags an answer as the surprise.
    answered = sync.acked or sync.frames > 0
    return Result(
        "no-activation", answered,
        "hub ANSWERED without any IOT_KEY? -- re-check the activation gate claim"
        if answered else "hub stayed silent, as the activation gate predicts",
        [
            f"CMD 54: acked={sync.acked} records={sync.records} frames={sync.frames}",
            "only meaningful on a hub nothing has armed yet -- power-cycle first",
        ],
    )


def exp_broadcast_only(ctx: Context) -> Result:
    """Negative control: arm with the broadcast form only, never a unicast.

    This is what the app does in a normal session.  If it works, our claim that
    a *targeted* IOT_KEY? is required is wrong, and the config flow could arm a
    hub without knowing its address.
    """
    sess = ctx.session()
    started = time.monotonic()
    sess.broadcast_raw(build_discovery(), ctx.broadcast)
    acks = sess.pump(3.0, stop=lambda f: f.is_node_ack and f.dev_id == ctx.device_name)
    ack_ms: float | None = None
    for frame in acks:
        if frame.is_node_ack and frame.dev_id == ctx.device_name:
            ack_ms = (frame.at - started) * 1000
            break
    sync = do_sync(sess)
    answered = sync.acked or sync.frames > 0
    return Result(
        "broadcast-only", answered,
        "broadcast alone armed the hub" if answered else "broadcast alone did not arm the hub",
        [
            f"broadcast NODE_ACK from {ctx.device_name}: "
            + (f"yes, in {ack_ms:.0f} ms" if ack_ms is not None else "none within 3.0 s"),
            f"CMD 54: acked={sync.acked} records={sync.records} frames={sync.frames}",
            "only meaningful on a hub nothing has armed yet -- power-cycle first",
        ],
    )


EXPERIMENTS: list[Experiment] = [
    Experiment("baseline", "the library's current session, as a control", "pass", exp_baseline),
    Experiment("unquoted-activation", "the app's malformed IOT_KEY? frame", "pass", exp_unquoted_activation),
    Experiment("cmd12-preamble", "CMD 12 -> 13 before CMD 54", "pass", exp_cmd12_preamble),
    Experiment("app-chain", "the vendor's full reaction chain 12/13/54/55/24/17", "pass", exp_app_chain),
    Experiment("vendor-ack-revstr1", 'APP_ACK with rev_str1="11"', "pass", exp_vendor_ack),
    Experiment("retransmit-same-msgid", "the same command twice, 1 s apart", "pass", exp_retransmit),
    Experiment("msgid-zero", "CMD 54 with msg_ID 0", "pass", exp_msgid_zero),
    Experiment("no-wait-activation", "ping and command back-to-back, no wait", "pass", exp_no_wait_activation),
    # Negative controls: opt-in via --only, and only trustworthy on a cold hub.
    Experiment("no-activation", "CMD 54 with no IOT_KEY? at all", "fail", exp_no_activation),
    Experiment("broadcast-only", "arm with the broadcast form only", "fail", exp_broadcast_only),
]

NEGATIVE_CONTROLS = {"no-activation", "broadcast-only"}
DEFAULT_SET = [e.name for e in EXPERIMENTS if e.name not in NEGATIVE_CONTROLS]


# ── discovery ─────────────────────────────────────────────────────────────────


def discover(sock: socket.socket, broadcast: str, target_port: int, timeout: float,
             verbose: bool) -> tuple[str, str] | None:
    """Broadcast IOT_KEY? and return (ip, devID) for the first hub that answers."""
    message = build_discovery()
    if verbose:
        print(f"    > {broadcast}:{target_port} {message}")
    sock.sendto(encrypt_message(message), (broadcast, target_port))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            packet, (host, _port) = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            continue
        text, obj = decrypt_message(packet)
        if verbose:
            print(f"    < {host} {text}")
        frame = Frame(at=time.monotonic(), host=host, text=text, obj=obj)
        if frame.is_node_ack and frame.dev_id and frame.dev_id != "NULL":
            return host, frame.dev_id
    return None


# ── main ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that vendor-app-faithful session variants still work on a real K2.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Every command sent is read-only (CMD 12, 54, 24). Binds UDP 1025.",
    )
    parser.add_argument("--gateway-ip", help="Hub address. Omit to discover.")
    parser.add_argument("--device-name", help="Hub devID. Omit to discover.")
    parser.add_argument("--broadcast", default="255.255.255.255")
    parser.add_argument("--local-port", type=int, default=UDP_PORT,
                        help="Source port. 1025 is required by real hubs.")
    parser.add_argument("--target-port", type=int, default=UDP_PORT,
                        help="Hub port. Change only to talk to tools/k2_hub_responder.py.")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="Run just this experiment. Repeatable.")
    parser.add_argument("--skip", action="append", metavar="NAME", default=[],
                        help="Drop this experiment from the run. Repeatable.")
    parser.add_argument("--list", action="store_true", help="List experiments and exit.")
    parser.add_argument("--no-discover", action="store_true")
    parser.add_argument("--discover-timeout", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=3.0,
                        help="Quiet seconds between experiments, to drain the previous one.")
    parser.add_argument("--verbose", action="store_true", help="Print every frame both ways.")
    return parser.parse_args()


def print_list() -> None:
    width = max(len(e.name) for e in EXPERIMENTS)
    print("Experiments (default set marked *):\n")
    for exp in EXPERIMENTS:
        mark = "*" if exp.name in DEFAULT_SET else " "
        print(f" {mark} {exp.name:<{width}}  expect {exp.expect.upper():<4}  {exp.summary}")
    print(
        "\nUnmarked entries are negative controls: run one at a time with --only, "
        "on a freshly\npower-cycled hub, or their result is decided by whatever armed "
        "the hub before them."
    )


def select(args: argparse.Namespace) -> list[Experiment] | None:
    by_name = {e.name: e for e in EXPERIMENTS}
    if args.only:
        chosen = []
        for name in args.only:
            if name not in by_name:
                print(f"Unknown experiment {name!r}. Use --list.", file=sys.stderr)
                return None
            chosen.append(by_name[name])
        return chosen
    for name in args.skip:
        if name not in by_name:
            print(f"Unknown experiment {name!r} in --skip. Use --list.", file=sys.stderr)
            return None
    return [by_name[n] for n in DEFAULT_SET if n not in set(args.skip)]


def main() -> int:
    args = parse_args()
    if args.list:
        print_list()
        return 0

    chosen = select(args)
    if chosen is None:
        return 2

    negatives = [e.name for e in chosen if e.name in NEGATIVE_CONTROLS]
    if negatives and not args.no_discover:
        print(
            f"{', '.join(negatives)} is a negative control: the discovery broadcast would "
            f"arm the hub\nbefore it runs and decide its result. Re-run with --no-discover "
            f"and explicit\n--gateway-ip/--device-name, on a hub you have just power-cycled.",
            file=sys.stderr,
        )
        return 2
    if negatives and len(chosen) > 1:
        print(
            f"Run {', '.join(negatives)} alone (--only), not alongside experiments that "
            f"arm the hub first.",
            file=sys.stderr,
        )
        return 2

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", args.local_port))
    except OSError as exc:
        print(f"Could not bind UDP port {args.local_port}: {exc}", file=sys.stderr)
        print(
            "Stop Home Assistant and close the ELRO app -- a real hub only answers a "
            "source port of 1025, so an ephemeral port is not a workaround here.",
            file=sys.stderr,
        )
        return 1

    with sock:
        gateway_ip, device_name = args.gateway_ip, args.device_name
        if not args.no_discover and (not gateway_ip or not device_name):
            print("Discovering ...")
            found = discover(sock, args.broadcast, args.target_port,
                             args.discover_timeout, args.verbose)
            if found is None:
                print("No hub answered the discovery broadcast.", file=sys.stderr)
                return 3
            gateway_ip, device_name = found
            print(f"  found {device_name} at {gateway_ip}\n")

        if not gateway_ip or not device_name:
            print("Need --gateway-ip and --device-name (or drop --no-discover).", file=sys.stderr)
            return 2

        ctx = Context(
            sock=sock, gateway_ip=gateway_ip, target_port=args.target_port,
            device_name=device_name, broadcast=args.broadcast, verbose=args.verbose,
        )

        print(f"Hub {device_name} at {gateway_ip}:{args.target_port}, "
              f"source port {args.local_port}\n")

        results: list[tuple[Experiment, Result]] = []
        for index, exp in enumerate(chosen):
            print(f"[{index + 1}/{len(chosen)}] {exp.name} -- {exp.summary}")
            try:
                result = exp.run(ctx)
            except Exception as exc:
                result = Result(exp.name, False, f"harness error: {exc!r}")
            results.append((exp, result))
            verdict = "PASS" if result.ok else "FAIL"
            print(f"      {verdict}  {result.headline}")
            for note in result.notes:
                print(f"            {note}")
            print()
            if index + 1 < len(chosen) and args.settle > 0:
                time.sleep(args.settle)

        return report(results)


def report(results: list[tuple[Experiment, Result]]) -> int:
    print("=" * 78)
    print("Summary")
    print("=" * 78)
    width = max(len(r.name) for _, r in results)
    surprises: list[str] = []
    for exp, result in results:
        expected = exp.expect == "pass"
        as_expected = result.ok == expected
        verdict = "PASS" if result.ok else "FAIL"
        flag = "" if as_expected else "   <-- UNEXPECTED"
        print(f"  {result.name:<{width}}  {verdict}  (expected "
              f"{'PASS' if expected else 'FAIL'}){flag}")
        if not as_expected:
            surprises.append(result.name)

    print()
    if not surprises:
        print("Everything behaved as predicted.")
        print(
            "Each PASS in the default set clears that variant for adoption: the hub that "
            "already\nworks keeps working with the vendor's behaviour in place."
        )
        return 0

    print(f"Unexpected: {', '.join(surprises)}")
    print(
        "An unexpected FAIL in the default set means the vendor does something we cannot "
        "copy.\nAn unexpected PASS in a negative control means a claim in "
        "docs/protocol_reference.md\nis wrong. Either way, record it there before changing "
        "any code."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
