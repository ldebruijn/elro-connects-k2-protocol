"""Pairing flow: CMD_CODE 2 (join window) -> CMD_CODE 62 (device joined).

The hub takes no device type when opening a join window -- the vendor app's
type picker only drives on-screen instructions -- so the only thing that
matters on the wire is that the window opens, that the hub's CMD_CODE 11 ACK
is recognised, and that the CMD_CODE 62 join notification is turned into a
usable SubDevice.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from elro_connects_k2_protocol.gateway import K2Gateway, _decode_acked_code
from elro_connects_k2_protocol.models import AlarmState, PairingResult, UpdateSource
from elro_connects_k2_protocol.parser import parse_add_sub_device
from elro_connects_k2_protocol.protocol import decrypt_message

# sub 4, type 0013 (smoke), trailing room id -- the shortest valid join payload
JOIN_DATA_STR1 = "0004" + "0013" + "01"
# sub 3, type 0018 (CO2/TH), matching tests/test_state_retention.py's device
JOIN_CO2_TH = "0003" + "0018" + "01"


def _join_frame(data_str1: str) -> dict[str, Any]:
    return {"msg": {"CMD_CODE": 62, "data_str1": data_str1, "data_str2": ""}}


def _ack_frame(data_str1: str, data_str2: str = "OK") -> dict[str, Any]:
    return {"msg": {"CMD_CODE": 11, "data_str1": data_str1, "data_str2": data_str2}}


class _FakeTransport:
    """Captures outbound datagrams so sent commands can be asserted on."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def sendto(self, payload: bytes, _addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(payload)
        assert obj is not None, "outbound frame was not decodable JSON"
        self.sent.append(obj)

    def close(self) -> None:
        pass

    def commands(self) -> list[int]:
        return [
            frame["msg"]["CMD_CODE"]
            for frame in self.sent
            if frame.get("action") == "APP_SEND"
        ]


def _connected_gateway() -> tuple[K2Gateway, _FakeTransport]:
    gw = K2Gateway("127.0.0.1", "TEST_DEVICE")
    transport = _FakeTransport()
    gw._transport = transport  # type: ignore[assignment]
    return gw, transport


# -- parsing -------------------------------------------------------------------

def test_parse_join_frame() -> None:
    device = parse_add_sub_device(_join_frame(JOIN_DATA_STR1))
    assert device is not None
    assert device.sub_id == 4
    assert device.raw_type == "0013"
    assert device.device_type == "013"
    # The hub sends no status with a join, so the app's placeholder is used.
    assert device.raw_status == "0464AA00"
    assert device.signal_bars == 4
    assert device.battery_pct == 100
    assert device.alarm_state is AlarmState.CLEAR


@pytest.mark.parametrize(
    "data_str1",
    [
        "00040013",  # exactly 8 chars: no room id, rejected by the app too
        "0004",
        "",
        "0000001301",  # sub_id 0 is the gateway itself, never a sub-device
        "ZZZZ001301",
    ],
)
def test_parse_join_frame_rejects_bad_payloads(data_str1: str) -> None:
    assert parse_add_sub_device(_join_frame(data_str1)) is None


@pytest.mark.parametrize(
    "data_str1,expected",
    [
        ("000200000", 2),  # the hub's 9-char form the app parses as hex
        ("0002", 2),
        ("02", 2),
        ("2", 2),
        ("OK", None),
    ],
)
def test_decode_acked_code(data_str1: str, expected: int | None) -> None:
    assert _decode_acked_code(data_str1) == expected


# -- join handling -------------------------------------------------------------

def test_join_adds_device_and_notifies() -> None:
    gw, _transport = _connected_gateway()
    seen: list[tuple[int, UpdateSource]] = []
    gw.add_update_callback(lambda sub_id, _dev, source: seen.append((sub_id, source)))

    gw._on_add_sub_device(_join_frame(JOIN_DATA_STR1))

    assert gw.devices[4].device_type == "013"
    assert seen == [(4, UpdateSource.PAIRED)]


def test_join_preserves_state_of_a_reused_slot() -> None:
    """Re-pairing into an occupied slot must not wipe what was learned there."""
    gw, _transport = _connected_gateway()
    gw._on_add_sub_device(_join_frame(JOIN_CO2_TH))
    gw._devices[3] = dataclasses.replace(
        gw._devices[3], nickname="Living room", co2_ppm=650
    )

    gw._on_add_sub_device(_join_frame(JOIN_CO2_TH))

    assert gw.devices[3].nickname == "Living room"
    assert gw.devices[3].co2_ppm == 650


# -- command round trips -------------------------------------------------------

@pytest.mark.asyncio
async def test_start_pairing_sends_cmd_2_and_waits_for_ack() -> None:
    gw, transport = _connected_gateway()

    task = asyncio.ensure_future(gw.start_pairing())
    await asyncio.sleep(0)

    sent = transport.sent[-1]
    assert sent["action"] == "APP_SEND"
    assert sent["msg"]["CMD_CODE"] == 2
    # Always "00" -- the device type the user picked is never transmitted.
    assert sent["msg"]["rev_str1"] == "00"

    gw._on_ack(_ack_frame("000200000"))
    assert await task is True


@pytest.mark.asyncio
async def test_start_pairing_returns_false_when_hub_refuses() -> None:
    gw, _transport = _connected_gateway()

    task = asyncio.ensure_future(gw.start_pairing())
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000", data_str2="FAIL"))

    assert await task is False


@pytest.mark.asyncio
async def test_start_pairing_ignores_acks_for_other_commands() -> None:
    gw, _transport = _connected_gateway()

    task = asyncio.ensure_future(gw.start_pairing())
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("003600000"))  # 0x36 = 54, the status sync
    await asyncio.sleep(0)

    assert not task.done()
    gw._on_ack(_ack_frame("000200000"))
    assert await task is True


@pytest.mark.asyncio
async def test_pair_new_device_end_to_end() -> None:
    gw, transport = _connected_gateway()

    task = asyncio.ensure_future(gw.pair_new_device(timeout=5))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))
    await asyncio.sleep(0)
    gw._on_add_sub_device(_join_frame(JOIN_DATA_STR1))

    result = await task
    assert isinstance(result, PairingResult)
    assert result.device.sub_id == 4
    assert result.already_known is False
    # A successful round never sends the cancel; only CMD_CODE 2 went out.
    assert transport.commands() == [2]


@pytest.mark.asyncio
async def test_pair_new_device_reports_a_reused_slot() -> None:
    gw, _transport = _connected_gateway()
    gw._on_add_sub_device(_join_frame(JOIN_DATA_STR1))

    task = asyncio.ensure_future(gw.pair_new_device(timeout=5))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))
    await asyncio.sleep(0)
    gw._on_add_sub_device(_join_frame(JOIN_DATA_STR1))

    result = await task
    assert result is not None
    assert result.already_known is True


@pytest.mark.asyncio
async def test_pair_new_device_gives_up_when_window_never_opens() -> None:
    """No ACK means the hub is not listening, so don't wait out the window."""
    gw, transport = _connected_gateway()

    import elro_connects_k2_protocol.gateway as gateway_module

    original = gateway_module._ACK_TIMEOUT
    gateway_module._ACK_TIMEOUT = 0.01
    try:
        assert await gw.pair_new_device(timeout=30) is None
    finally:
        gateway_module._ACK_TIMEOUT = original

    assert transport.commands() == [2]


@pytest.mark.asyncio
async def test_pair_new_device_timeout_leaves_window_to_expire() -> None:
    """The app deliberately skips the cancel on timeout; so do we."""
    gw, transport = _connected_gateway()

    task = asyncio.ensure_future(gw.pair_new_device(timeout=0.01))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))

    assert await task is None
    assert 7 not in transport.commands()


@pytest.mark.asyncio
async def test_a_second_round_supersedes_the_first_without_closing_it() -> None:
    """The loser returns None; only a cancelled caller may close the window.

    Resolving rather than cancelling the superseded wait is what keeps the two
    cases apart -- a CancelledError here would reach pair_new_device's cancel
    handler and shut the *winner's* window.
    """
    gw, transport = _connected_gateway()

    first = asyncio.ensure_future(gw.pair_new_device(timeout=30))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))
    await asyncio.sleep(0)

    second = asyncio.ensure_future(gw.pair_new_device(timeout=30))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))
    await asyncio.sleep(0)

    assert await first is None
    assert not first.cancelled()

    gw._on_add_sub_device(_join_frame(JOIN_DATA_STR1))
    result = await second
    assert result is not None and result.device.sub_id == 4
    assert transport.commands() == [2, 2]


@pytest.mark.asyncio
async def test_cancelling_the_caller_closes_the_window() -> None:
    gw, transport = _connected_gateway()

    task = asyncio.ensure_future(gw.pair_new_device(timeout=30))
    await asyncio.sleep(0)
    gw._on_ack(_ack_frame("000200000"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.commands() == [2, 7]


def test_cancel_pairing_sends_cmd_7_with_empty_fields() -> None:
    gw, transport = _connected_gateway()
    gw.cancel_pairing()

    msg = transport.sent[-1]["msg"]
    assert msg["CMD_CODE"] == 7
    assert (msg["rev_str1"], msg["rev_str2"], msg["rev_str3"]) == ("", "", "")
