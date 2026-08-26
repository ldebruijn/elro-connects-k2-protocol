"""Async K2 gateway client.

Maintains a persistent UDP socket on port 1025 for the lifetime of the
connection.  Incoming messages are routed by (action, CMD_CODE, payload
shape) — never by device type — matching how the official Android app's
``ReceiveHandler`` works.

## CMD_CODE 19 push routing

Every sub-device sends unsolicited status updates via CMD_CODE 19, but
there are two distinct payload shapes:

  • 8-char ``data_str2`` — the universal alarm/status format used by all
    alarm and sensor devices (smoke, CO, gas, heat, water, PIR, door, socket,
    button, …).  Routed to ``_on_push_update`` → ``parse_push_update``.

  • 6-char ``data_str2`` — used exclusively by the CO2/temp/humidity detector
    (type "018").  It carries one measurement at a time, tagged with a 2-char
    prefix, and cannot be decoded into a full ``SubDevice`` without merging
    with existing cached state.  Routed to ``_on_co2_th_push`` →
    ``decode_co2_th_measurement``.

This split is confirmed in ``ReceiveHandler.uploadDeviceStatus``,
which dispatches on ``len(data_str2)`` before doing anything else.

## CMD_CODE 55/56 sync routing

All device types use the same 14-char record layout in sync responses, so
``parse_sync_response`` handles them uniformly.  After the sync, any device
whose profile includes CO2/TH capabilities is queried individually via
CMD_CODE 16 → 66 (``get_sub_device_info``) because the 14-char record only
carries signal + battery, not measurement values.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import socket as _socket
import time
from collections.abc import Callable
from typing import Any

from elro_connects_k2_protocol.models import (
    GatewayInfo,
    PairingResult,
    SubDevice,
    UpdateSource,
)
from elro_connects_k2_protocol.parser import (
    decode_co2_th_measurement,
    decode_device_name,
    decode_thermostat_status,
    parse_add_sub_device,
    parse_gateway_info,
    parse_push_update,
    parse_sub_device_info_response,
    parse_sync_response,
)
from elro_connects_k2_protocol.protocol import (
    UDP_PORT,
    build_ack,
    build_activation,
    build_app_send,
    build_discovery,
    decrypt_message,
    encrypt_message,
    timezone_offset_code,
)

_LOGGER = logging.getLogger(__name__)

UpdateCallback = Callable[[int, SubDevice, UpdateSource], None]

# Optional SubDevice fields hold state that arrives out-of-band from the frames
# that rebuild the device: CO2/TH measurements arrive one per 6-char CMD_CODE 19
# push, thermostat detail rides its own push, and nicknames come from
# CMD_CODE 24 → 17.  Neither a 14-char CMD_CODE 55 sync record nor an 8-char
# status push carries any of them, so rebuilding a device from those frames
# alone would discard everything accumulated so far and entities would flip to
# "unknown" until the next measurement push.  Deriving the list from the
# dataclass means new optional fields become sticky automatically.
_STICKY_FIELDS: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(SubDevice) if f.default is None
)


def _carry_forward(existing: SubDevice | None, incoming: SubDevice) -> SubDevice:
    """Return ``incoming`` with out-of-band fields it did not carry restored.

    A field is only inherited when the incoming frame left it unset, so a frame
    that genuinely reports a new value always wins.
    """
    if existing is None:
        return incoming
    retained = {
        name: value
        for name in _STICKY_FIELDS
        if getattr(incoming, name) is None and (value := getattr(existing, name)) is not None
    }
    return dataclasses.replace(incoming, **retained) if retained else incoming


# How long to collect CMD_CODE 55/56 packets before declaring sync complete.
_SYNC_COLLECT_SECONDS: float = 3.0
# Length of a well-formed CMD_CODE 17 name record: 2-byte sub_id plus a
# 16-byte GBK name field, hex-encoded.  Kept here rather than inlined so the
# diagnostic in _on_device_name and the parser agree on what "expected" means.
_NAME_RECORD_LEN: int = 36

# Name sync is bounded by silence, not by a total budget.  The hub answers
# CMD_CODE 24 with one CMD_CODE 17 frame per sub-device at its own pace (the
# vendor app never ACKs those frames -- ReceiveHandler only ACKs CMD_CODE 11 --
# so nothing we do speeds them up), terminated by NAME_OVER.  A fixed overall
# budget therefore has to grow with the size of the device table, and silently
# truncates the tail of the batch when it does not.  Waiting for a gap in the
# stream instead makes the cost independent of device count.
#
# How long the hub may stay silent before name sync is considered finished.
_NAME_IDLE_SECONDS: float = 2.0
# Absolute cap on one name sync, so a hub that streams forever still returns.
_NAME_MAX_SECONDS: float = 30.0
# How long to wait for the hub's CMD_CODE 11 ACK confirming a command was taken.
_ACK_TIMEOUT: float = 5.0
# How long to wait for the NODE_ACK confirming the hub processed an activation
# ping.  Hubs have been observed answering in anywhere from 4 ms to 344 ms.
_ACTIVATION_TIMEOUT: float = 2.0
# How many activation pings to send before giving up and sending anyway.
_ACTIVATION_ATTEMPTS: int = 3
# How many times to send CMD_CODE 54 when the hub returns no status records.
# Only ever reached when activation went unacknowledged -- see sync_devices().
_SYNC_ATTEMPTS: int = 2
# How long to hold a pairing window open.  Matches the vendor app's countdown
# for adding a sub-device (DistributeNetRequest.onStartCountDown(false) → 60 s;
# the 120 s branch is the gateway-onboarding flow, not this one).
PAIRING_TIMEOUT_SECONDS: float = 60.0


def _decode_acked_code(data_str1: str) -> int | None:
    """Return the CMD_CODE that a CMD_CODE 11 ACK refers to.

    The app reads the first two bytes of ``data_str1`` as hex
    (``CoderUtils.getAnswerResult``), which is the interpretation that holds
    for the hub's own longer ACK payloads.  Shorter values are accepted too so
    a bare ``"2"`` or ``"02"`` still resolves — all three spellings agree for
    the low command codes anything here actually waits on.
    """
    head = data_str1[:4] if len(data_str1) >= 4 else data_str1
    try:
        return int(head, 16)
    except ValueError:
        return None


class _K2Protocol(asyncio.DatagramProtocol):
    def __init__(self, gateway: K2Gateway) -> None:
        self._gateway = gateway
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(data)
        if obj is not None:
            self._gateway._on_message(obj, addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.warning("UDP connection lost: %s", exc)


class K2Gateway:
    """Async client for the ELRO Connects K2 local UDP protocol."""

    def __init__(self, ip: str, device_name: str) -> None:
        self._ip = ip
        self._device_name = device_name
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _K2Protocol | None = None
        self._devices: dict[int, SubDevice] = {}
        self._callbacks: list[UpdateCallback] = []
        self._msg_id: int = 0
        # Sync state: set while CMD_CODE 54 is in flight
        self._sync_buffer: dict[int, SubDevice] = {}
        self._sync_event: asyncio.Event | None = None
        # Name sync state: set while CMD_CODE 24 is in flight
        self._name_buffer: dict[int, str] = {}
        self._name_event: asyncio.Event | None = None
        # Monotonic timestamp of the last CMD_CODE 17 frame, for the idle timeout
        self._name_last_frame: float = 0.0
        # Sub-device info state: one future per sub_id, set while CMD_CODE 16 is in flight
        self._pending_sub_info: dict[int, asyncio.Future[SubDevice]] = {}
        # Gateway info state: set while CMD_CODE 12 is in flight
        self._pending_gateway_info: asyncio.Future[GatewayInfo] | None = None
        # Command ACK state: one future per awaited CMD_CODE, resolved by CMD_CODE 11
        self._pending_acks: dict[int, asyncio.Future[bool]] = {}
        # Pairing state: set while a CMD_CODE 2 join window is open
        self._pending_new_device: asyncio.Future[PairingResult | None] | None = None
        # Activation state: set while a targeted IOT_KEY? awaits its NODE_ACK
        self._activation_ack: asyncio.Future[None] | None = None
        # True once the hub has acked an activation ping, i.e. the session is
        # armed and the hub will act on APP_SEND rather than dropping it.
        self._activated: bool = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Bind the persistent UDP socket and send the activation ping.

        SO_REUSEADDR is set so that a reload (unload + immediate setup) can
        bind to the same port before the OS fully releases the previous socket.
        Without it, reloading the integration raises EADDRINUSE because
        asyncio's transport.close() is non-blocking and the OS may still
        consider the port in use until the next event-loop iteration processes
        the close.
        """
        loop = asyncio.get_running_loop()
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", UDP_PORT))
        # allow_broadcast cannot be passed alongside sock= in Python 3.14+;
        # SO_BROADCAST is set on the socket directly above instead.
        self._transport, protocol = await loop.create_datagram_endpoint(
            lambda: _K2Protocol(self),
            sock=sock,
        )
        self._protocol = protocol
        _LOGGER.debug("UDP socket bound to port %d", UDP_PORT)
        await self.activate()

    async def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        _LOGGER.debug("Disconnected from K2 gateway")

    # ── commands ──────────────────────────────────────────────────────────────

    async def activate(self) -> bool:
        """Send a targeted IOT_KEY? and wait for the hub to acknowledge it.

        The K2 ignores APP_SEND commands until it has received a targeted
        IOT_KEY? from the controlling host, and it does not arm the session at
        the moment the ping arrives but at the moment it finishes processing
        it — the NODE_ACK is the only observable signal that this has happened.

        Waiting for that ACK is what makes this correct rather than merely
        usually-correct.  Hubs have been seen taking anywhere from 4 ms to
        344 ms to answer, so a caller that sends its first APP_SEND immediately
        after this returns would otherwise race ahead of activation on a slow
        hub, and the hub drops such commands *silently* — no error, no ACK, no
        response at all.  That failure mode looks exactly like "the hub has no
        devices", which is precisely how it was reported.

        This doubles as a session keepalive.  Returns True once the hub has
        acked; False if it never did, in which case the caller may still try to
        send — an un-acked ping is not proof the hub missed it.
        """
        loop = asyncio.get_running_loop()
        # Distinguishes "the session just came up" from a routine keepalive, so
        # the first arming is visible at INFO without the per-minute re-pings
        # spamming the log.
        was_activated = self._activated

        for attempt in range(1, _ACTIVATION_ATTEMPTS + 1):
            self._activation_ack = loop.create_future()
            started = loop.time()
            self._send(build_activation(self._device_name))
            _LOGGER.debug(
                "Sent activation ping to %s (attempt %d/%d)",
                self._ip, attempt, _ACTIVATION_ATTEMPTS,
            )
            try:
                await asyncio.wait_for(self._activation_ack, timeout=_ACTIVATION_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug(
                    "No NODE_ACK from %s within %.1f s (attempt %d/%d)%s",
                    self._ip, _ACTIVATION_TIMEOUT, attempt, _ACTIVATION_ATTEMPTS,
                    "; retrying" if attempt < _ACTIVATION_ATTEMPTS else "",
                )
                continue
            finally:
                self._activation_ack = None

            # Worth logging even on success: this number is the hub's real
            # processing latency, and it is what distinguishes a hub that arms
            # comfortably from one that only just makes the timeout.
            elapsed_ms = (loop.time() - started) * 1000
            self._activated = True
            if was_activated:
                _LOGGER.debug(
                    "Gateway %s re-acknowledged activation in %.0f ms (attempt %d/%d)",
                    self._ip, elapsed_ms, attempt, _ACTIVATION_ATTEMPTS,
                )
            else:
                _LOGGER.info(
                    "Gateway %s activated in %.0f ms (attempt %d/%d); session is armed "
                    "and the hub will now accept commands",
                    self._ip, elapsed_ms, attempt, _ACTIVATION_ATTEMPTS,
                )
            return True

        _LOGGER.warning(
            "Gateway %s did not acknowledge any of %d activation pings (%.1f s each). "
            "The hub silently ignores commands until it acks, so device syncs will "
            "come back empty. Check that devID %r exactly matches the hub, that "
            "UDP port %d is reachable in both directions, and that nothing is "
            "silently dropping the hub's own outbound internet traffic — while a "
            "blocked call home is stalling it, the hub stops answering locally "
            "until that attempt times out on its own",
            self._ip, _ACTIVATION_ATTEMPTS, _ACTIVATION_TIMEOUT,
            self._device_name, UDP_PORT,
        )
        self._activated = False
        return False

    async def sync_devices(self) -> dict[int, SubDevice]:
        """Send CMD_CODE 54 and collect the CMD_CODE 55/56 responses.

        Waits up to _SYNC_COLLECT_SECONDS for the K2 to finish sending
        status records. Returns whatever was collected (may be partial if
        the gateway is slow or the timeout is short).
        """
        self._sync_buffer = {}

        crc = "00020000"
        tz = timezone_offset_code()
        # A hub that never armed its session drops CMD_CODE 54 without a reply,
        # so an empty buffer is indistinguishable from "no devices paired".
        # Re-activate and re-ask rather than reporting zero devices on one miss;
        # the buffer is keyed by sub_id, so a duplicate answer is harmless.
        for attempt in range(1, _SYNC_ATTEMPTS + 1):
            self._sync_event = asyncio.Event()
            self._send(build_app_send(self._device_name, self._next_msg_id(), 54, crc, tz, ""))
            _LOGGER.debug(
                "Sent CMD_CODE 54 sync request (attempt %d/%d)", attempt, _SYNC_ATTEMPTS
            )

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sync_event.wait(), timeout=_SYNC_COLLECT_SECONDS)

            self._sync_event = None
            if self._sync_buffer or attempt == _SYNC_ATTEMPTS:
                break
            # An empty buffer after a *confirmed* activation is a real answer:
            # the hub is armed and says it has no sub-devices paired, which is
            # the normal state of a brand-new hub.  Retrying that just triples
            # the time to report the truth, since nothing ever sets _sync_event
            # and each attempt burns the full collect window.  Only retry when
            # the activation went unacknowledged, because then the hub may
            # never have armed and dropped the request without a trace.
            if self._activated:
                break
            _LOGGER.debug(
                "No CMD_CODE 55 records from %s and activation was never acked; "
                "re-activating and retrying", self._ip
            )
            await self.activate()

        result = dict(self._sync_buffer)
        self._devices.update(result)
        _LOGGER.debug("Sync complete (source=POLL): %d devices", len(result))

        # The universal 14-char sync records only carry signal + battery for
        # CO2/TH devices — not actual measurements.  Query each one individually
        # via CMD_CODE 16 → 66 to populate co2_ppm, temperature_c, humidity_pct.
        co2_th_ids = [
            sid for sid, dev in result.items()
            if any(c.key in {"co2", "temperature", "humidity"} for c in dev.profile.capabilities)
        ]
        if co2_th_ids:
            _LOGGER.debug("Querying CMD_CODE 16 for %d CO2/TH device(s): %s", len(co2_th_ids), co2_th_ids)
            infos = await asyncio.gather(
                *(self.get_sub_device_info(sid) for sid in co2_th_ids),
                return_exceptions=True,
            )
            for sid, info in zip(co2_th_ids, infos, strict=True):
                if isinstance(info, SubDevice):
                    result[sid] = info
                    self._devices[sid] = info

        names = await self.sync_device_names()
        for sid, name in names.items():
            if sid in result:
                result[sid] = dataclasses.replace(result[sid], nickname=name)
                self._devices[sid] = result[sid]

        return result

    async def sync_device_names(self) -> dict[int, str]:
        """Send CMD_CODE 24 and collect the CMD_CODE 17 name responses.

        Sends a "no known names" CRC block (``"00020000"``) which causes the
        hub to push back all stored sub-device names one per CMD_CODE 17 frame,
        terminating with ``data_str2 == "NAME_OVER"``.

        The hub does NOT push name changes unsolicited — names only update
        when this method is called (i.e. on startup / integration reload).

        Collection ends on the NAME_OVER sentinel, after _NAME_IDLE_SECONDS
        without a name frame, or at the _NAME_MAX_SECONDS cap -- whichever comes
        first.  Names already collected are always returned, so a hub that goes
        quiet mid-batch still yields the names it did send.

        Completion is deliberately not defined as "a name for every known
        sub_id": the hub only stores names that were explicitly set, so a
        sub-device the owner never renamed produces no frame at all and that
        condition would never be satisfied.

        Returns a sub_id → nickname mapping; empty dict if the hub has no
        custom names set or never answered.
        """
        self._name_buffer = {}
        self._name_event = asyncio.Event()
        self._name_last_frame = time.monotonic()
        hard_deadline = self._name_last_frame + _NAME_MAX_SECONDS

        self._send(build_app_send(self._device_name, self._next_msg_id(), 24, "00020000", "", ""))
        _LOGGER.debug("Sent CMD_CODE 24 name sync request")

        complete = False
        while True:
            # Re-read _name_last_frame each pass: every name frame that arrives
            # pushes the idle deadline out, so a slow hub is waited out as long
            # as it keeps talking.
            timeout = (
                min(self._name_last_frame + _NAME_IDLE_SECONDS, hard_deadline)
                - time.monotonic()
            )
            if timeout <= 0:
                break
            try:
                await asyncio.wait_for(self._name_event.wait(), timeout=timeout)
            except TimeoutError:
                continue
            complete = True
            break

        self._name_event = None
        result = dict(self._name_buffer)
        if complete:
            _LOGGER.debug("Name sync complete: %d nickname(s) found", len(result))
        elif result:
            # Partial results are indistinguishable from "the hub has no names
            # set" to the caller, so say so loudly rather than at debug level.
            _LOGGER.warning(
                "CMD_CODE 24 name sync ended without NAME_OVER after %d nickname(s); "
                "the device list may be missing names", len(result),
            )
        else:
            _LOGGER.debug("CMD_CODE 24 name sync timed out (no CMD_CODE 17 frames received)")
        return result

    def send_device_action(self, sub_id: int, action: str) -> None:
        """Send CMD_CODE 1 to trigger a device action (test, mute/silence, etc.).

        ``action`` is the 8-char hex payload placed in ``rev_str2``, e.g.:
          ``"BB000000"`` — trigger alarm test on most smoke/CO/gas/heat/water detectors
          ``"50000000"`` — silence an active alarm on those same devices
          ``"02BB0000"`` — trigger test on CO2/TH detector (GS241A)
          ``"02500000"`` — silence an active alarm on the CO2/TH detector

        These payloads come from ``DeviceProfile.test_action`` and
        ``DeviceProfile.mute_action``; the profiles are the single source of
        truth so callers never need to hardcode them.

        Fire-and-forget: no ACK or response is expected from the K2 beyond the
        standard CMD_CODE 11 ACK that the gateway emits for all APP_SEND frames.
        """
        sub_id_hex = f"{sub_id:04X}"
        self._send(build_app_send(
            self._device_name,
            self._next_msg_id(),
            1,
            rev_str1=sub_id_hex,
            rev_str2=action,
        ))
        _LOGGER.debug("Sent device action: sub_id=%d action=%s", sub_id, action)

    async def get_gateway_info(self) -> GatewayInfo | None:
        """Send CMD_CODE 12 and return the first CMD_CODE 13 response."""
        future: asyncio.Future[GatewayInfo] = asyncio.get_running_loop().create_future()
        self._pending_gateway_info = future
        self._send(build_app_send(self._device_name, self._next_msg_id(), 12, "00", "00", ""))
        try:
            return await asyncio.wait_for(future, timeout=5.0)
        except TimeoutError:
            return None
        finally:
            self._pending_gateway_info = None

    async def get_sub_device_info(self, sub_id: int) -> SubDevice | None:
        """Send CMD_CODE 16 and return the CMD_CODE 66 response for a CO2/TH device.

        CMD_CODE 16 (GET_SUB_DEVICE_INFO) requests the full current state of a
        single sub-device.  For CO2/TH detectors the response (CMD_CODE 66)
        carries all three measurement values in one 30-char payload, which
        ``parse_sub_device_info_response`` decodes.

        This is called automatically by ``sync_devices`` for every device whose
        profile includes CO2/TH capabilities, because the universal 14-char
        CMD_CODE 55/56 sync records only carry signal + battery — not actual
        measurement readings.

        The ``existing`` device from the cache is passed to the parser so that
        ``raw_type`` and ``profile`` are inherited rather than re-derived, and
        any fields absent from the CMD_CODE 66 response are preserved.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SubDevice] = loop.create_future()
        self._pending_sub_info[sub_id] = future
        sub_id_hex = f"{sub_id:04X}"
        self._send(build_app_send(self._device_name, self._next_msg_id(), 16, sub_id_hex, "", ""))
        _LOGGER.debug("Sent CMD_CODE 16 sub-device info request for sub_id=%d", sub_id)
        try:
            return await asyncio.wait_for(future, timeout=5.0)
        except TimeoutError:
            _LOGGER.debug("CMD_CODE 66 response timed out for sub_id=%d", sub_id)
            return None
        finally:
            self._pending_sub_info.pop(sub_id, None)

    # ── pairing ───────────────────────────────────────────────────────────────

    async def start_pairing(self) -> bool:
        """Open a join window on the hub (CMD_CODE 2) and wait for its ACK.

        ``rev_str1`` is always ``"00"``, exactly as the app sends it in
        ``BootSubDeviceActivity.onStartNetworking``.  The device-type picker in
        the app's add-device screen is never transmitted — it only selects which
        physical-trigger instructions to display, and is compared afterwards
        against the type that actually joined.  So there is nothing to choose
        here: the hub accepts whatever detector shows up.

        Returns True when the hub answered ``"OK"``.  The app only starts its
        countdown on that ACK (``getAnswerResult`` returning the command code),
        so a False means the hub is not listening and pressing the detector's
        pairing button would achieve nothing.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending_acks[2] = future
        self._send(build_app_send(self._device_name, self._next_msg_id(), 2, "00", "", ""))
        _LOGGER.debug("Sent CMD_CODE 2 to open a pairing window")
        try:
            accepted = await asyncio.wait_for(future, timeout=_ACK_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning("Hub did not ACK the pairing request (CMD_CODE 2)")
            return False
        finally:
            self._pending_acks.pop(2, None)
        if not accepted:
            _LOGGER.warning("Hub refused the pairing request (CMD_CODE 2 ACK was not OK)")
        return accepted

    def cancel_pairing(self) -> None:
        """Close an open join window (CMD_CODE 7, all fields empty).

        Fire-and-forget.  The app sends this when the user backs out of the
        add-device screen, but deliberately *not* when the window times out
        (``onCancelNetworking`` is guarded by ``mTimeOut``) — the hub is
        expected to close an expired window by itself.
        """
        self._send(build_app_send(self._device_name, self._next_msg_id(), 7, "", "", ""))
        _LOGGER.debug("Sent CMD_CODE 7 to close the pairing window")

    async def wait_for_new_device(
        self, timeout: float = PAIRING_TIMEOUT_SECONDS
    ) -> PairingResult | None:
        """Wait for a sub-device to announce itself (CMD_CODE 62).

        Returns None on timeout.  Only one wait can be outstanding at a time;
        starting a second one supersedes the first, which then also returns
        None.  The loser is resolved rather than cancelled so that it stays
        distinguishable from the caller itself being cancelled — which is the
        one case that has to close the join window.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PairingResult | None] = loop.create_future()
        previous = self._pending_new_device
        if previous is not None and not previous.done():
            _LOGGER.debug("A pairing wait was already in flight; superseding it")
            previous.set_result(None)
        self._pending_new_device = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            _LOGGER.debug("No sub-device joined within %.0f s", timeout)
            return None
        finally:
            if self._pending_new_device is future:
                self._pending_new_device = None

    async def pair_new_device(
        self, timeout: float = PAIRING_TIMEOUT_SECONDS
    ) -> PairingResult | None:
        """Run one full pairing round: open the window, wait for a device.

        The window is only useful while someone is physically triggering the
        detector's pairing action, so this is inherently an interactive,
        long-running call.

        Returns None if the hub refused to open the window or nothing joined in
        time.  If the caller is cancelled the window is closed explicitly; a
        plain timeout leaves it to expire on its own, matching the app.
        """
        if not await self.start_pairing():
            return None
        try:
            return await self.wait_for_new_device(timeout)
        except asyncio.CancelledError:
            self.cancel_pairing()
            raise

    # ── subscriptions ─────────────────────────────────────────────────────────

    def add_update_callback(self, cb: UpdateCallback) -> None:
        self._callbacks.append(cb)

    def remove_update_callback(self, cb: UpdateCallback) -> None:
        self._callbacks.remove(cb)

    @property
    def devices(self) -> dict[int, SubDevice]:
        return dict(self._devices)

    @property
    def ip(self) -> str:
        return self._ip

    @property
    def device_name(self) -> str:
        return self._device_name

    # ── internal message routing ──────────────────────────────────────────────

    def _on_message(self, obj: dict[str, Any], source_ip: str) -> None:
        action = obj.get("action")
        msg = obj.get("msg")
        cmd_code = msg.get("CMD_CODE") if isinstance(msg, dict) else None

        # NODE_ACK/CMD_CODE 0 is the hub confirming it processed an IOT_KEY?.
        # It carries no device data, but it is the signal activate() waits on.
        if action == "NODE_ACK" and cmd_code == 0:
            if self._activation_ack is not None and not self._activation_ack.done():
                self._activation_ack.set_result(None)
            return

        # Auto-ACK every NODE_SEND to stop K2 retransmission
        if action == "NODE_SEND":
            device_name = obj.get("devID")
            if isinstance(device_name, str):
                ack = build_ack(device_name, self._next_msg_id(), cmd_code or 11)
                self._send(ack)

        if cmd_code == 19:
            self._on_push_update(obj)
        elif cmd_code in {55, 56}:
            self._on_sync_response(obj)
        elif cmd_code == 17:
            self._on_device_name(obj)
        elif cmd_code == 13:
            self._on_gateway_info(obj, source_ip)
        elif cmd_code == 66:
            self._on_sub_device_info(obj)
        elif cmd_code == 11:
            self._on_ack(obj)
        elif cmd_code == 62:
            self._on_add_sub_device(obj)

    def _on_push_update(self, obj: dict[str, Any]) -> None:
        """Route a CMD_CODE 19 push to the correct handler based on payload shape.

        The K2 protocol distinguishes push types by the length of ``data_str2``,
        not by device type — this is confirmed in ``ReceiveHandler.uploadDeviceStatus``
        in the vendor Android app.  See the module docstring for the full rationale.

          6-char  →  CO2/TH one-measurement push  →  ``_on_co2_th_push``
          8-char  →  universal alarm/status push   →  ``parse_push_update``
          "NULL"  →  device offline                →  ``parse_push_update``
          other   →  unknown shape, logged only
        """
        msg = obj.get("msg", obj)
        data_str2 = msg.get("data_str2") or msg.get("rev_str2") if isinstance(msg, dict) else None

        if isinstance(data_str2, str) and len(data_str2) == 6:
            self._on_co2_th_push(obj)
            return

        device = parse_push_update(obj)
        if device is None:
            return

        # Thermostat devices (type 215 / GS361) repurpose the standard alarm bytes
        # to carry valve state + temperature setpoint.  Enrich the SubDevice with
        # the decoded thermostat fields so HA entities can read them directly.
        if any(c.key == "valve" for c in device.profile.capabilities):
            status = decode_thermostat_status(device.raw_status)
            if status is not None:
                device = dataclasses.replace(
                    device,
                    temperature_setpoint=status.setpoint_c,
                    valve_open=status.valve_open,
                    window_open=status.window_open,
                    thermostat_mode=status.mode,
                    # The TRV reports its measured room temperature in the same
                    # field CO2/TH detectors use, so HA's temperature sensor
                    # needs no device-specific wiring.
                    temperature_c=status.current_temperature_c,
                )

        # An 8-char status push carries only signal/battery/alarm, so anything
        # accumulated out-of-band (CO2/TH measurements, nickname) must survive.
        device = _carry_forward(self._devices.get(device.sub_id), device)

        self._devices[device.sub_id] = device
        _LOGGER.info(
            "Push update received: sub_id=%d type=%s alarm=%s battery=%d%% signal=%d bars source=PUSH",
            device.sub_id,
            device.device_type,
            device.alarm_state.name,
            device.battery_pct,
            device.signal_bars,
        )
        for cb in self._callbacks:
            cb(device.sub_id, device, UpdateSource.PUSH)

    def _on_co2_th_push(self, obj: dict[str, Any]) -> None:
        """Handle a CMD_CODE 19 push from a CO2/temp/humidity device.

        The CO2/TH detector sends one measurement at a time in a 6-char
        ``data_str2``.  Because a single push carries only one field, we merge
        the new value into the existing cached ``SubDevice`` using
        ``dataclasses.replace`` rather than constructing a fresh object.

        If the device is not yet in the cache (e.g. push arrived before the
        initial sync completed), the update is dropped with a debug log.  The
        next ``sync_devices`` call will fetch the full state including a
        CMD_CODE 16 → 66 query for the measurement values.
        """
        msg = obj.get("msg", obj)
        if not isinstance(msg, dict):
            return

        data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
        data_str2: Any = msg.get("data_str2") or msg.get("rev_str2")
        if not isinstance(data_str1, str) or len(data_str1) < 4:
            return
        if not isinstance(data_str2, str):
            return

        try:
            sub_id = int(data_str1[0:4], 16)
        except ValueError:
            return

        result = decode_co2_th_measurement(data_str2)
        if result is None:
            _LOGGER.debug("CMD_CODE 19 CO2/TH: unknown tag in data_str2=%r sub_id=%d", data_str2, sub_id)
            return

        existing = self._devices.get(sub_id)
        if existing is None:
            _LOGGER.debug("CMD_CODE 19 CO2/TH: sub_id=%d not in cache yet, skipping", sub_id)
            return

        field, value = result
        if field == "co2_ppm":
            device = dataclasses.replace(existing, co2_ppm=int(value))
        elif field == "temperature_c":
            device = dataclasses.replace(existing, temperature_c=float(value))
        elif field == "humidity_pct":
            device = dataclasses.replace(existing, humidity_pct=float(value))
        else:
            return
        self._devices[sub_id] = device
        _LOGGER.info(
            "Push update (CO2/TH) received: sub_id=%d %s=%s source=PUSH",
            sub_id, field, value,
        )
        for cb in self._callbacks:
            cb(sub_id, device, UpdateSource.PUSH)

    def _on_sub_device_info(self, obj: dict[str, Any]) -> None:
        """Handle a CMD_CODE 66 sub-device info response.

        This arrives in response to a CMD_CODE 16 request sent by
        ``get_sub_device_info``.  For CO2/TH devices the 30-char payload
        carries the current signal, battery, and all three measurement values.

        The existing cached device is passed to the parser so that fields
        not present in the response (e.g. ``raw_type``, ``profile``) are
        inherited rather than re-derived.  The resolved ``SubDevice`` is
        stored in the cache and used to resolve the pending future so that
        ``get_sub_device_info`` can return it to the caller.
        """
        msg: Any = obj.get("msg", obj)
        if not isinstance(msg, dict):
            return
        data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
        if not isinstance(data_str1, str) or len(data_str1) < 4:
            return
        try:
            sub_id = int(data_str1[0:4], 16)
        except ValueError:
            return

        existing = self._devices.get(sub_id)
        device = parse_sub_device_info_response(obj, existing)
        if device is None:
            return

        self._devices[sub_id] = device
        _LOGGER.debug(
            "Sub-device info received: sub_id=%d co2=%s temp=%s humidity=%s",
            sub_id, device.co2_ppm, device.temperature_c, device.humidity_pct,
        )
        future = self._pending_sub_info.get(sub_id)
        if future is not None and not future.done():
            future.set_result(device)

    def _on_ack(self, obj: dict[str, Any]) -> None:
        """Resolve a pending command ACK (CMD_CODE 11) from the hub.

        ``data_str1`` names the command being acknowledged and ``data_str2``
        carries ``"OK"`` on success — the pair the app checks in
        ``CoderUtils.getAnswerResult`` before letting a pairing round proceed.

        ACKs for commands nobody is waiting on are ignored; most commands here
        are fire-and-forget and every APP_SEND draws one of these.
        """
        msg: Any = obj.get("msg", obj)
        if not isinstance(msg, dict):
            return
        data_str1: Any = msg.get("data_str1") or msg.get("rev_str1")
        data_str2: Any = msg.get("data_str2") or msg.get("rev_str2")
        if not isinstance(data_str1, str):
            return
        acked_code = _decode_acked_code(data_str1)
        if acked_code is None:
            return
        future = self._pending_acks.get(acked_code)
        if future is None or future.done():
            return
        accepted = isinstance(data_str2, str) and data_str2.upper() == "OK"
        _LOGGER.debug("ACK for CMD_CODE %d: %s", acked_code, "OK" if accepted else data_str2)
        future.set_result(accepted)

    def _on_add_sub_device(self, obj: dict[str, Any]) -> None:
        """Handle a CMD_CODE 62 frame announcing that a sub-device joined.

        Arrives unsolicited during a pairing window opened by CMD_CODE 2.  The
        frame carries only the slot and the device type, so the SubDevice is
        seeded with the same placeholder status the app uses; the caller is
        expected to follow up with a sync for real signal/battery values.
        """
        device = parse_add_sub_device(obj)
        if device is None:
            return

        already_known = device.sub_id in self._devices
        # A detector re-paired into its old slot keeps what was learned about it
        # before — the join frame carries no measurements and no nickname.
        device = _carry_forward(self._devices.get(device.sub_id), device)
        self._devices[device.sub_id] = device
        _LOGGER.info(
            "Sub-device joined: sub_id=%d type=%s profile=%s already_known=%s source=PAIRED",
            device.sub_id,
            device.device_type,
            device.profile.name,
            already_known,
        )

        for cb in self._callbacks:
            cb(device.sub_id, device, UpdateSource.PAIRED)

        future = self._pending_new_device
        if future is not None and not future.done():
            future.set_result(PairingResult(device=device, already_known=already_known))

    def _on_device_name(self, obj: dict[str, Any]) -> None:
        """Handle a CMD_CODE 17 sub-device name frame from the hub.

        Arrives in response to CMD_CODE 24.  Each frame carries one name record
        in ``data_str2`` until the hub sends the sentinel ``"NAME_OVER"``.

        Every record is logged raw before decoding, and a record that does not
        decode is reported rather than dropped in silence.  Without that, a
        nickname that never turns up is indistinguishable from one the hub
        never sent -- and only one of those is a bug on this side.  See
        docs/research.md on the missing sub_id=1 nickname.
        """
        msg: Any = obj.get("msg", obj)
        if not isinstance(msg, dict):
            return
        data_str2: Any = msg.get("data_str2") or msg.get("rev_str2")
        # Any frame at all means the hub is still streaming, so push the idle
        # deadline out -- including one that fails to decode below, which is
        # evidence of activity even though it yields no name.
        self._name_last_frame = time.monotonic()
        if data_str2 == "NAME_OVER":
            _LOGGER.debug("Name record: NAME_OVER sentinel")
            if self._name_event is not None:
                self._name_event.set()
            return
        if not isinstance(data_str2, str):
            _LOGGER.warning("CMD_CODE 17 frame carried no name record: %r", data_str2)
            return
        # Raw first: a record we cannot decode is still evidence, and the hex is
        # what makes it diagnosable off a user's log.
        _LOGGER.debug(
            "Name record: sub_id_field=%s len=%d raw=%s",
            data_str2[:4] or "(empty)", len(data_str2), data_str2,
        )
        result = decode_device_name(data_str2)
        if result is None:
            # A record of the expected size that yields nothing is usually a
            # sub-device the owner never renamed -- the hub stores no name and
            # the field is all padding.  That is normal, so it stays at debug
            # rather than warning on every sync.  A record of any other size is
            # a real anomaly: the vendor encoder only produces 36 when the name
            # fits in 15 GBK bytes, and its pairing-time naming screen enforces
            # no such limit.
            if len(data_str2) == _NAME_RECORD_LEN:
                _LOGGER.debug(
                    "Name record yielded no nickname (no name set, or unparseable): %s",
                    data_str2,
                )
            else:
                _LOGGER.warning(
                    "CMD_CODE 17 name record is %d chars, expected %d: %s -- "
                    "sub-device %s will have no nickname",
                    len(data_str2), _NAME_RECORD_LEN, data_str2, data_str2[:4] or "(empty)",
                )
        else:
            sub_id, name = result
            self._name_buffer[sub_id] = name
            _LOGGER.debug("Name received: sub_id=%d nickname=%r", sub_id, name)

    def _on_sync_response(self, obj: dict[str, Any]) -> None:
        devices = parse_sync_response(obj)
        self._sync_buffer.update(devices)
        _LOGGER.debug(
            "Sync response received: %d device records in this packet source=POLL",
            len(devices),
        )
        for sub_id, device in devices.items():
            # A 14-char sync record reports only signal/battery/alarm; without
            # this the periodic re-sync would wipe measurements and nicknames.
            device = _carry_forward(self._devices.get(sub_id), device)
            self._devices[sub_id] = device
            for cb in self._callbacks:
                cb(sub_id, device, UpdateSource.POLL)

    def _on_gateway_info(self, obj: dict[str, Any], source_ip: str) -> None:
        info = parse_gateway_info(obj)
        if info is not None:
            info.ip = source_ip
        pending = getattr(self, "_pending_gateway_info", None)
        if pending is not None and not pending.done() and info is not None:
            pending.set_result(info)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _send(self, message: str) -> None:
        if self._transport is None:
            _LOGGER.warning("Attempted to send before connect(); message dropped")
            return
        self._transport.sendto(encrypt_message(message), (self._ip, UDP_PORT))

    def _next_msg_id(self) -> int:
        self._msg_id = (self._msg_id + 1) % 1_000_000
        return self._msg_id


# ── discovery ─────────────────────────────────────────────────────────────────

class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, result: asyncio.Future[tuple[str, str]]) -> None:
        self._result = result
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(data)
        if obj is None:
            return
        action = obj.get("action")
        dev_id = obj.get("devID")
        msg = obj.get("msg")
        cmd_code = msg.get("CMD_CODE") if isinstance(msg, dict) else None
        if (
            action == "NODE_ACK"
            and isinstance(dev_id, str)
            and dev_id != "NULL"
            and cmd_code == 0
            and not self._result.done()
        ):
            self._result.set_result((addr[0], dev_id))

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Discovery UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


async def discover_gateway(
    broadcast: str = "255.255.255.255",
    timeout: float = 5.0,
) -> K2Gateway | None:
    """Broadcast IOT_KEY? and return a K2Gateway for the first responder.

    The returned gateway is not yet connected — call gateway.connect() before
    sending commands.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[str, str]] = loop.create_future()

    # Built by hand rather than with local_addr= so SO_REUSEADDR can be set, as
    # K2Gateway.connect() already does.  asyncio has not set it on UDP sockets
    # since Python 3.8 (bpo-37228), so local_addr= raised EADDRINUSE even when
    # the only other holder of port 1025 was one of our own sockets — a
    # connected K2Gateway, or the not-yet-reaped socket of a previous one.
    # Discovery run from a config flow while an entry is already set up hit
    # exactly that.
    #
    # Linux shares an addr:port across UDP sockets only when *every* socket
    # bound to it sets SO_REUSEADDR, so this deliberately does not paper over
    # an unrelated process squatting on 1025: that still raises EADDRINUSE,
    # which is the correct answer.  Note also that while two sockets do share
    # the port, an inbound unicast reply is delivered to just one of them, so
    # discovery and an active session should not be run concurrently.
    #
    # SO_BROADCAST replaces allow_broadcast=, which cannot be passed alongside
    # sock= in Python 3.14+.
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", UDP_PORT))

    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(result),
        sock=sock,
    )

    try:
        payload = encrypt_message(build_discovery())
        transport.sendto(payload, (broadcast, UDP_PORT))
        _LOGGER.debug("Sent discovery broadcast to %s", broadcast)
        ip, device_name = await asyncio.wait_for(result, timeout=timeout)
        _LOGGER.info("Discovered K2 at %s devID=%s", ip, device_name)
        return K2Gateway(ip, device_name)
    except TimeoutError:
        _LOGGER.debug("Discovery timed out after %.1f s", timeout)
        return None
    finally:
        transport.close()
