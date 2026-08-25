"""Activation gate: the hub arms on NODE_ACK, not on receipt of IOT_KEY?.

The K2 ignores APP_SEND until it has *finished processing* a targeted IOT_KEY?,
and it drops such commands silently -- no error, no ACK, no response.  The
NODE_ACK is the only observable signal that the session is armed.

This is not a theoretical ordering concern.  Real hubs have been measured
answering an activation ping in anywhere from 4 ms to 344 ms (median 66 ms),
while the library used to send its first APP_SEND 2 ms later -- losing the race
every time on a slow hub and reporting the result as "0 devices", which is
indistinguishable from a hub with nothing paired to it.

The vendor app enforces the same gate structurally rather than sequentially:
``UdpControlProxy.onNodeAckDeal`` is the only writer that marks a gateway
online, and ``SendCommand.onSendCommand`` sends over UDP only when that flag is
set (falling back to the Alibaba cloud otherwise).  The app therefore cannot
exhibit this bug, which is why the requirement is easy to miss when porting.

``_FakeHub`` reproduces the hub's semantics -- delayed ACK, silent drop while
un-armed -- so these tests fail against a fire-and-forget activate().
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from elro_connects_k2_protocol import gateway as gateway_mod
from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.protocol import build_activation, decrypt_message

DEVICE = "ST_1234567890"
IP = "192.168.1.50"

# How long the fake hub takes to answer, scaled down from the real 4-344 ms.
HUB_LATENCY = 0.05

# Two smoke alarms, borrowed from tests/fixtures/sync_status_response.json.
SYNC_DATA_STR1 = "0100134057AA55020013305FAA34"


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scale the collect windows down so the suite stays fast.

    Every window still comfortably exceeds HUB_LATENCY, so the race these tests
    exercise is decided by the activation gate rather than by a starved timeout.
    """
    monkeypatch.setattr(gateway_mod, "_ACTIVATION_TIMEOUT", 0.2)
    monkeypatch.setattr(gateway_mod, "_SYNC_COLLECT_SECONDS", 0.2)
    monkeypatch.setattr(gateway_mod, "_NAME_IDLE_SECONDS", 0.05)
    yield


class _FakeHub:
    """Stands in for the transport *and* the hub it talks to.

    Models the two behaviours that matter: the ACK arrives after a delay, and
    APP_SEND sent before that ACK is dropped without any reply at all.
    """

    def __init__(self, gateway: K2Gateway, latency: float = HUB_LATENCY) -> None:
        self._gateway = gateway
        self._latency = latency
        self.armed = False
        # False models a hub that is armed but has nothing paired to it
        self.has_devices = True
        self.sent: list[dict[str, Any]] = []
        # APP_SEND CMD_CODEs the hub discarded because it had not armed yet
        self.dropped: list[int] = []

    # -- transport interface ---------------------------------------------------

    def sendto(self, payload: bytes, _addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(payload)
        assert obj is not None, "outbound frame was not decodable JSON"
        self.sent.append(obj)

        if obj.get("action") == "IOT_KEY?":
            self._later(self._reply_ack)
            return

        if obj.get("action") == "APP_SEND":
            cmd_code = obj["msg"]["CMD_CODE"]
            if not self.armed:
                self.dropped.append(cmd_code)
                return
            if cmd_code == 54 and self.has_devices:
                self._later(self._reply_sync)

    def close(self) -> None:
        pass

    # -- hub behaviour ---------------------------------------------------------

    def _later(self, fn: Any) -> None:
        asyncio.get_running_loop().call_later(self._latency, fn)

    def _reply_ack(self) -> None:
        # The hub arms as it answers, so ordering here mirrors the real device.
        self.armed = True
        self._gateway._on_message(
            {"action": "NODE_ACK", "devID": DEVICE, "msg": {"CMD_CODE": 0}}, IP
        )

    def _reply_sync(self) -> None:
        self._gateway._on_message(
            {
                "action": "NODE_SEND",
                "devID": DEVICE,
                "msg": {"CMD_CODE": 55, "data_str1": SYNC_DATA_STR1, "data_str2": ""},
            },
            IP,
        )

    def app_send_codes(self) -> list[int]:
        return [f["msg"]["CMD_CODE"] for f in self.sent if f.get("action") == "APP_SEND"]


class _SilentHub(_FakeHub):
    """A hub that never answers anything -- unreachable, or a mismatched devID."""

    def sendto(self, payload: bytes, _addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(payload)
        assert obj is not None
        self.sent.append(obj)


def _gateway(hub_cls: type[_FakeHub] = _FakeHub) -> tuple[K2Gateway, _FakeHub]:
    gw = K2Gateway(IP, DEVICE)
    hub = hub_cls(gw)
    gw._transport = hub  # type: ignore[assignment]
    return gw, hub


def _fire_and_forget_activation(gw: K2Gateway) -> None:
    """Reproduce the pre-fix activate(): send the ping, don't wait for the ACK."""
    gw._send(build_activation(DEVICE))


# -- the gate ------------------------------------------------------------------

async def test_activate_waits_for_the_ack_before_returning() -> None:
    """activate() must not return until the hub has actually armed."""
    gw, hub = _gateway()

    assert await gw.activate() is True
    assert hub.armed, "activate() returned while the hub was still un-armed"


async def test_sync_is_not_sent_before_the_hub_arms() -> None:
    """The regression: CMD_CODE 54 must never be dropped for racing activation."""
    gw, hub = _gateway()

    await gw.activate()
    devices = await gw.sync_devices()

    assert hub.dropped == [], f"hub silently discarded APP_SEND {hub.dropped}"
    assert sorted(devices) == [1, 2]


async def test_unguarded_activation_loses_the_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the fake hub itself: without the gate the sync really is dropped.

    If this ever stops failing to find devices, _FakeHub has stopped modelling
    the bug and the tests above would no longer prove anything.  The retry
    safety net is disabled here to isolate the race.
    """
    monkeypatch.setattr(gateway_mod, "_SYNC_ATTEMPTS", 1)
    gw, hub = _gateway()

    _fire_and_forget_activation(gw)
    devices = await gw.sync_devices()

    assert hub.dropped == [54], "expected the un-armed hub to discard CMD_CODE 54"
    assert devices == {}


async def test_retry_recovers_when_activation_was_never_confirmed() -> None:
    """Second line of defence: an unconfirmed activation earns a re-ask."""
    gw, hub = _gateway()

    _fire_and_forget_activation(gw)
    devices = await gw.sync_devices()

    assert hub.dropped == [54], "expected the first CMD_CODE 54 to be dropped"
    assert sorted(devices) == [1, 2], "retry should have recovered the sync"


async def test_node_ack_is_routed() -> None:
    """CMD_CODE 0 used to fall through _on_message unrouted."""
    gw, _hub = _gateway()
    gw._activation_ack = asyncio.get_running_loop().create_future()

    gw._on_message({"action": "NODE_ACK", "devID": DEVICE, "msg": {"CMD_CODE": 0}}, IP)

    assert gw._activation_ack.done()


# -- degradation ---------------------------------------------------------------

async def test_activation_retries_then_gives_up_without_hanging() -> None:
    """An unreachable hub must fail fast rather than block setup forever."""
    gw, hub = _gateway(_SilentHub)

    async with asyncio.timeout(2.0):
        assert await gw.activate() is False

    assert len(hub.sent) > 1, "expected the activation ping to be retried"
    assert gw._activated is False


async def test_empty_sync_on_an_armed_hub_is_not_retried() -> None:
    """A hub with nothing paired is a real answer, not a failure to retry.

    Retrying it would multiply the time to report the truth, because nothing
    ever sets _sync_event and each attempt burns the whole collect window.
    """
    gw, hub = _gateway()
    gw._activated = True
    hub.armed = True
    hub.has_devices = False

    devices = await gw.sync_devices()

    assert devices == {}
    assert hub.app_send_codes().count(54) == 1
