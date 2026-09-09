"""Silence on the wire and silence in the log must not look the same.

A two-hub report ("both hubs connect, one finds no devices") could not be
answered from a full debug log, because three receive paths discarded frames
without saying so: a datagram that would not decode, a frame that reached its
gateway but matched no handler, and a CMD_CODE 11 ACK nobody was waiting on.

Each of those, when it happens, means the hub *is* talking -- which calls for
the opposite investigation from a hub that is not.  For CMD_CODE 54 the
distinction is the whole diagnosis: an ACKed sync that returns no records means
the hub processed the request and its device table is genuinely empty, while an
un-ACKed one means it never listened.  These tests pin that each path leaves a
trace.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.protocol import encrypt_message
from elro_connects_k2_protocol.transport import _SharedSocket

DEVICE = "ST_1234567890"
IP = "192.168.1.50"


class _NullTransport:
    """Absorbs the auto-ACK a NODE_SEND draws, so it is not the thing tested."""

    def sendto(self, payload: bytes, addr: tuple[str, int]) -> None:
        pass

    def close(self) -> None:
        pass


def _gateway() -> K2Gateway:
    gateway = K2Gateway(ip=IP, device_name=DEVICE)
    gateway._transport = _NullTransport()  # type: ignore[assignment]
    return gateway


def test_a_datagram_that_does_not_decode_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hub answering with something unreadable is not a hub staying silent."""
    shared = _SharedSocket()

    with caplog.at_level(logging.DEBUG, logger="elro_connects_k2_protocol.transport"):
        shared.datagram_received(encrypt_message("not json at all"), (IP, 1025))

    assert any("Undecodable" in r.getMessage() for r in caplog.records), (
        "a datagram that failed to decode should say so, with its source"
    )
    assert any(IP in r.getMessage() for r in caplog.records), (
        "the source address is what tells the reader which hub sent it"
    )


def test_a_frame_with_no_handler_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Reaching the gateway and matching no handler is not the same as silence."""
    gateway = _gateway()
    frame: dict[str, Any] = {
        "action": "NODE_SEND",
        "devID": DEVICE,
        "msg": {"CMD_CODE": 99, "data_str1": "", "data_str2": ""},
    }

    with caplog.at_level(logging.DEBUG, logger="elro_connects_k2_protocol.gateway"):
        gateway._on_message(frame, IP)

    assert any("Unhandled frame" in r.getMessage() for r in caplog.records), (
        "an unhandled CMD_CODE means the hub is talking and we are not listening"
    )
    assert any("CMD_CODE=99" in r.getMessage() for r in caplog.records), (
        "the code is what makes the report actionable"
    )


def test_an_ack_nobody_awaits_is_still_logged(caplog: pytest.LogCaptureFixture) -> None:
    """CMD_CODE 54 is fire-and-forget, so its ACK has no waiter -- and is the
    one frame that separates "armed but empty" from "never listened"."""
    gateway = _gateway()
    frame: dict[str, Any] = {
        "action": "NODE_SEND",
        "devID": DEVICE,
        "msg": {"CMD_CODE": 11, "data_str1": "0036", "data_str2": "OK"},
    }

    with caplog.at_level(logging.DEBUG, logger="elro_connects_k2_protocol.gateway"):
        gateway._on_message(frame, IP)

    assert any("ACK for CMD_CODE 54: OK" in r.getMessage() for r in caplog.records), (
        "an ACK for a command nobody waits on is exactly the one worth logging"
    )
