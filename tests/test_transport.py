"""Two hubs, one socket: frames must reach the gateway that owns them.

Every ``K2Gateway`` used to bind its own socket to ``0.0.0.0:1025``.  Because
``SO_REUSEADDR`` lets the second bind succeed, nothing errored -- but an inbound
unicast datagram reaches only one of the sockets bound to an address, so with
two hubs configured one worked and the other was silently deaf, and the pair
swapped roles every time Home Assistant retried the failing entry.

The socket that *did* win then received frames from both hubs, and nothing
downstream looked at which hub had sent one: hub A's sync response was parsed
straight into hub B's device table, where the two hubs' sub-device ids -- both
numbered from 1 -- overwrote each other.

The vendor app avoids both by never binding twice.  ``UdpSocket`` holds one
socket on 1025 for every gateway the user owns and
``UdpControlProxy.receiveData`` demultiplexes on ``devID``.  These tests pin
that behaviour: one socket, strict devID routing, and nothing delivered to a
gateway that did not ask for it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from elro_connects_k2_protocol import transport as transport_mod
from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.transport import (
    _SharedSocket,
    open_sniffer,
    open_transport,
    shared_socket_is_open,
)

HOUSE = "ST_2247500716"
HOUSE_IP = "192.168.3.221"
GARAGE = "ST_2310400631"
GARAGE_IP = "192.168.3.19"

# Two smoke alarms as sub-devices 1 and 2, from tests/fixtures/sync_status_response.json.
SYNC_DATA_STR1 = "0100134057AA55020013305FAA34"


def _sync_frame(dev_id: str) -> dict[str, Any]:
    return {
        "action": "NODE_SEND",
        "devID": dev_id,
        "msg": {"CMD_CODE": 55, "data_str1": SYNC_DATA_STR1, "data_str2": ""},
    }


class _Recorder:
    """A listener that just remembers what it was handed."""

    def __init__(self) -> None:
        self.frames: list[tuple[dict[str, Any], str]] = []

    def __call__(self, obj: dict[str, Any], source_ip: str) -> None:
        self.frames.append((obj, source_ip))

    @property
    def dev_ids(self) -> list[str | None]:
        return [obj.get("devID") for obj, _ip in self.frames]


# ── routing ───────────────────────────────────────────────────────────────────

def test_each_gateway_only_sees_its_own_hub() -> None:
    """The regression: hub A's frames must never land in hub B's listener."""
    shared = _SharedSocket()
    house, garage = _Recorder(), _Recorder()
    shared.register(HOUSE, house)
    shared.register(GARAGE, garage)

    shared.dispatch(_sync_frame(HOUSE), HOUSE_IP)
    shared.dispatch(_sync_frame(GARAGE), GARAGE_IP)

    assert house.dev_ids == [HOUSE]
    assert garage.dev_ids == [GARAGE]


def test_a_third_hub_is_dropped_rather_than_guessed_at() -> None:
    """An unregistered devID belongs to someone else's app, not to us."""
    shared = _SharedSocket()
    house = _Recorder()
    shared.register(HOUSE, house)

    shared.dispatch(_sync_frame("ST_9999999999"), "192.168.3.77")

    assert house.frames == []


def test_a_frame_without_devid_goes_to_the_only_gateway() -> None:
    """Unlabelled shapes stay deliverable while there is no ambiguity.

    Preserves the pre-routing behaviour for the single-hub case, which is what
    almost every install is.
    """
    shared = _SharedSocket()
    house = _Recorder()
    shared.register(HOUSE, house)

    shared.dispatch({"action": "NODE_SEND", "msg": {"CMD_CODE": 19}}, HOUSE_IP)

    assert len(house.frames) == 1


def test_a_frame_without_devid_is_dropped_when_several_gateways_exist() -> None:
    """With two hubs there is no safe guess, and guessing is the original bug."""
    shared = _SharedSocket()
    house, garage = _Recorder(), _Recorder()
    shared.register(HOUSE, house)
    shared.register(GARAGE, garage)

    shared.dispatch({"action": "NODE_SEND", "msg": {"CMD_CODE": 19}}, HOUSE_IP)

    assert house.frames == []
    assert garage.frames == []


def test_sniffers_see_everything_including_unregistered_hubs() -> None:
    """Discovery depends on this: a responding hub is not registered yet."""
    shared = _SharedSocket()
    house, sniffer = _Recorder(), _Recorder()
    shared.register(HOUSE, house)
    shared.add_sniffer(sniffer)

    shared.dispatch(_sync_frame(HOUSE), HOUSE_IP)
    shared.dispatch(_sync_frame(GARAGE), GARAGE_IP)

    assert sniffer.dev_ids == [HOUSE, GARAGE]
    assert house.dev_ids == [HOUSE]


def test_unregister_stops_delivery() -> None:
    shared = _SharedSocket()
    house = _Recorder()
    shared.register(HOUSE, house)
    shared.unregister(HOUSE, house)

    shared.dispatch(_sync_frame(HOUSE), HOUSE_IP)

    assert house.frames == []


def test_a_stale_handle_cannot_unregister_its_replacement() -> None:
    """A reload registers the new listener before the old handle is closed."""
    shared = _SharedSocket()
    old, new = _Recorder(), _Recorder()
    shared.register(HOUSE, old)
    shared.register(HOUSE, new)
    shared.unregister(HOUSE, old)

    shared.dispatch(_sync_frame(HOUSE), HOUSE_IP)

    assert new.dev_ids == [HOUSE]


# ── the socket itself ─────────────────────────────────────────────────────────

async def test_two_gateways_bind_the_port_once() -> None:
    """The whole point: N gateways, one socket."""
    house = await open_transport(HOUSE, _Recorder())
    garage = await open_transport(GARAGE, _Recorder())
    try:
        assert shared_socket_is_open()
        assert house._shared is garage._shared
    finally:
        house.close()
        garage.close()


async def test_the_socket_survives_until_the_last_gateway_leaves() -> None:
    """Unloading one entry must not pull the port out from under the other."""
    house = await open_transport(HOUSE, _Recorder())
    garage = await open_transport(GARAGE, _Recorder())

    house.close()
    assert shared_socket_is_open(), "closing one gateway closed the shared socket"

    garage.close()
    assert not shared_socket_is_open()


async def test_closing_a_handle_twice_does_not_drop_someone_elses_reference() -> None:
    house = await open_transport(HOUSE, _Recorder())
    garage = await open_transport(GARAGE, _Recorder())

    house.close()
    house.close()

    assert shared_socket_is_open(), "a double close released a reference it did not hold"
    garage.close()


async def test_a_sniffer_keeps_the_socket_open_on_its_own() -> None:
    """Discovery runs with no gateways registered at all."""
    sniffer = await open_sniffer(_Recorder())
    try:
        assert shared_socket_is_open()
    finally:
        sniffer.close()
    assert not shared_socket_is_open()


async def test_discovery_can_run_while_a_gateway_is_connected() -> None:
    """The old implementation bound a second socket here and went deaf."""
    house = await open_transport(HOUSE, _Recorder())
    try:
        sniffer = await open_sniffer(_Recorder())
        sniffer.close()
        assert shared_socket_is_open(), "discovery took the port with it on the way out"
    finally:
        house.close()


# ── end to end through two real gateways ──────────────────────────────────────

async def test_two_gateways_do_not_share_a_device_table() -> None:
    """Hub A's sync response used to populate hub B's devices, ids and all."""
    shared = _SharedSocket()
    house = K2Gateway(HOUSE_IP, HOUSE)
    garage = K2Gateway(GARAGE_IP, GARAGE)
    shared.register(HOUSE, house._on_message)
    shared.register(GARAGE, garage._on_message)
    # The auto-ACK for a NODE_SEND goes out over the shared socket, which is
    # never bound here; both gateways keep the null transport they start with.
    house._transport = garage._transport = shared  # type: ignore[assignment]

    shared.dispatch(_sync_frame(HOUSE), HOUSE_IP)

    assert sorted(house._devices) == [1, 2]
    assert garage._devices == {}, "the garage hub ingested the house hub's devices"


async def test_a_gateway_follows_its_hub_to_a_new_address() -> None:
    """DHCP moved the hub; the vendor app rewrites its stored address the same way."""
    gw = K2Gateway(HOUSE_IP, HOUSE)
    gw._activation_ack = asyncio.get_running_loop().create_future()

    gw._on_message({"action": "NODE_ACK", "devID": HOUSE, "msg": {"CMD_CODE": 0}}, "192.168.3.99")

    assert gw.ip == "192.168.3.99"


async def test_an_unlabelled_frame_does_not_move_a_gateway() -> None:
    """Only a frame that names this hub is evidence of where this hub lives."""
    gw = K2Gateway(HOUSE_IP, HOUSE)

    gw._on_message({"action": "NODE_SEND", "msg": {"CMD_CODE": 19}}, "192.168.3.99")

    assert gw.ip == HOUSE_IP


# ── discovery ─────────────────────────────────────────────────────────────────

@pytest.fixture
def _fake_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Answer any discovery broadcast with the hubs in the returned list."""
    hubs: list[dict[str, Any]] = []

    real_open_sniffer = transport_mod.open_sniffer

    async def _patched(listener: transport_mod.Listener) -> transport_mod.K2Transport:
        handle = await real_open_sniffer(listener)

        def _sendto(payload: bytes, addr: tuple[str, int]) -> None:
            # Swallow the real broadcast; nothing on the test host answers it.
            del payload, addr
            for hub in hubs:
                listener(
                    {"action": "NODE_ACK", "devID": hub["dev_id"], "msg": {"CMD_CODE": 0}},
                    hub["ip"],
                )

        handle.sendto = _sendto  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr("elro_connects_k2_protocol.gateway.open_sniffer", _patched)
    return hubs


async def test_discover_gateways_returns_every_responder(
    _fake_broadcast: list[dict[str, Any]],
) -> None:
    from elro_connects_k2_protocol.gateway import discover_gateways

    _fake_broadcast.extend([
        {"dev_id": HOUSE, "ip": HOUSE_IP},
        {"dev_id": GARAGE, "ip": GARAGE_IP},
    ])

    found = await discover_gateways(timeout=0.1)

    assert {(gw.device_name, gw.ip) for gw in found} == {
        (HOUSE, HOUSE_IP),
        (GARAGE, GARAGE_IP),
    }


async def test_discover_gateway_still_returns_one(
    _fake_broadcast: list[dict[str, Any]],
) -> None:
    """The singular form stays available and stays fast."""
    from elro_connects_k2_protocol.gateway import discover_gateway

    _fake_broadcast.extend([
        {"dev_id": HOUSE, "ip": HOUSE_IP},
        {"dev_id": GARAGE, "ip": GARAGE_IP},
    ])

    async with asyncio.timeout(1.0):
        gw = await discover_gateway(timeout=30.0)

    assert gw is not None
    assert gw.device_name == HOUSE


async def test_discover_gateways_returns_empty_when_nothing_answers(
    _fake_broadcast: list[dict[str, Any]],
) -> None:
    from elro_connects_k2_protocol.gateway import discover_gateways

    assert await discover_gateways(timeout=0.1) == []


# ── what the integration's manual-entry validation rests on ───────────────────

async def test_a_mistyped_device_name_never_activates() -> None:
    """Routing turns a wrong devID from a silent failure into a detectable one.

    The hub names *itself* in the NODE_ACK it answers a targeted IOT_KEY? with,
    so a gateway registered under a name the hub does not have receives nothing
    and stays un-activated.  The config flow uses exactly this to reject
    hand-entered details before creating an entry, instead of producing one that
    loads, creates no entities and explains itself only in a repair issue.
    """
    shared = _SharedSocket()
    gw = K2Gateway(HOUSE_IP, "ST_TYPO")
    shared.register("ST_TYPO", gw._on_message)
    gw._activation_ack = asyncio.get_running_loop().create_future()

    # The real hub answers with its own name, not the one it was called by.
    shared.dispatch({"action": "NODE_ACK", "devID": HOUSE, "msg": {"CMD_CODE": 0}}, HOUSE_IP)

    assert not gw._activation_ack.done()
    assert gw.activated is False


async def test_the_right_device_name_does_activate() -> None:
    """Guards the test above: the same frame under the right name must land."""
    shared = _SharedSocket()
    gw = K2Gateway(HOUSE_IP, HOUSE)
    shared.register(HOUSE, gw._on_message)
    gw._activation_ack = asyncio.get_running_loop().create_future()

    shared.dispatch({"action": "NODE_ACK", "devID": HOUSE, "msg": {"CMD_CODE": 0}}, HOUSE_IP)

    assert gw._activation_ack.done()
