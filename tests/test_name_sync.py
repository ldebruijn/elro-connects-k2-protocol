"""Name sync must not be bounded by a fixed overall budget.

The hub answers CMD_CODE 24 with one CMD_CODE 17 frame per *named* sub-device,
at its own pace, terminated by NAME_OVER.  The vendor app never ACKs those
frames (``ReceiveHandler`` only ACKs CMD_CODE 11), so nothing the client does
speeds the stream up -- it can only wait correctly.

The library used to spend one fixed 3 s window on the whole batch, which made
the required window scale with the size of the device table and silently
truncated its tail when it did not fit.  That is issue #1: with eight devices
the last nickname went missing until the constant was raised to 6 s, which
would only have moved the cliff further out.

Collection is now bounded by NAME_OVER and by a gap in the stream, neither of
which grows with device count.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from elro_connects_k2_protocol import gateway as gateway_mod
from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.protocol import decrypt_message

DEVICE = "ST_1234567890"
IP = "192.168.1.50"

# Real hubs pace name frames at roughly 350-400 ms; scaled down to keep the
# suite fast while preserving the ratio to the windows below.
FRAME_INTERVAL = 0.02


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scale the windows down, preserving idle >> frame interval."""
    monkeypatch.setattr(gateway_mod, "_NAME_IDLE_SECONDS", 0.1)
    monkeypatch.setattr(gateway_mod, "_NAME_MAX_SECONDS", 1.0)
    yield


def _name_record(sub_id: int, name: str) -> str:
    """Encode one CMD_CODE 17 name record the way the vendor app does.

    Mirrors ``CoderUtils.getAscii``: left-pad with '@' to 15 bytes, terminate
    with '$', for a 16-byte field.
    """
    encoded = name.encode("gbk")
    field = b"@" * (15 - len(encoded)) + encoded + b"$"
    return f"{sub_id:04X}" + field.hex().upper()


class _NamingHub:
    """A hub that streams name frames one at a time, slowly.

    ``names`` is the sub_id -> nickname table it has stored.  ``send_name_over``
    models a hub whose sentinel is lost or never sent.
    """

    def __init__(
        self,
        gateway: K2Gateway,
        names: dict[int, str],
        *,
        send_name_over: bool = True,
        interval: float = FRAME_INTERVAL,
    ) -> None:
        self._gateway = gateway
        self._names = names
        self._send_name_over = send_name_over
        self._interval = interval
        self.sent: list[dict[str, Any]] = []
        self.frames_sent = 0
        self._task: asyncio.Task[None] | None = None

    def sendto(self, payload: bytes, _addr: tuple[str, int]) -> None:
        _text, obj = decrypt_message(payload)
        assert obj is not None, "outbound frame was not decodable JSON"
        self.sent.append(obj)
        if obj.get("action") == "APP_SEND" and obj["msg"]["CMD_CODE"] == 24:
            self._task = asyncio.get_running_loop().create_task(self._stream())

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _stream(self) -> None:
        for sub_id, name in self._names.items():
            await asyncio.sleep(self._interval)
            self._push(_name_record(sub_id, name))
        if self._send_name_over:
            await asyncio.sleep(self._interval)
            self._push("NAME_OVER")

    def _push(self, data_str2: str) -> None:
        self.frames_sent += 1
        self._gateway._on_message(
            {
                "action": "NODE_SEND",
                "devID": DEVICE,
                "msg": {"CMD_CODE": 17, "data_str1": "", "data_str2": data_str2},
            },
            IP,
        )


class _EndlessHub(_NamingHub):
    """A hub that never stops talking, to prove the absolute cap holds."""

    async def _stream(self) -> None:
        sub_id = 1
        while True:
            await asyncio.sleep(self._interval)
            self._push(_name_record(sub_id, f"Device {sub_id}"))
            sub_id += 1


def _make(
    names: dict[int, str], hub_cls: type[_NamingHub] = _NamingHub, **kw: Any
) -> K2Gateway:
    """Wire a gateway to a fake hub holding ``names``."""
    gw = K2Gateway(IP, DEVICE)
    gw._transport = hub_cls(gw, names, **kw)  # type: ignore[assignment]
    return gw


# Willy's hub from issue #1: eight devices, sub_ids sparse up to 12.
EIGHT_DEVICES = {
    1: "Hallway", 2: "Kitchen", 3: "Living room", 5: "Bedroom",
    7: "Attic", 9: "Garage", 11: "Shed", 12: "Cellar",
}


# -- the regression ------------------------------------------------------------

async def test_slow_hub_yields_every_name() -> None:
    """The batch outlasts any single fixed window, and no name is lost."""
    gw = _make(EIGHT_DEVICES)

    names = await gw.sync_device_names()

    assert names == EIGHT_DEVICES


async def test_the_tail_of_a_long_batch_survives() -> None:
    """A table twice as large must not cost the last names.

    This is what a fixed budget cannot do: doubling the device count doubles
    the time the hub spends streaming.
    """
    table = {sub_id: f"Device {sub_id}" for sub_id in range(1, 25)}
    gw = _make(table)

    names = await gw.sync_device_names()

    assert names == table
    assert max(names) == 24, "the last device in the table was truncated"


# -- the completion signals ----------------------------------------------------

async def test_a_silent_tail_returns_what_arrived(caplog: pytest.LogCaptureFixture) -> None:
    """Partial results are returned, and said out loud.

    Without the warning a truncated batch is indistinguishable to the caller
    from a hub with no names set -- which is how issue #1 reached the tracker
    as a bug report rather than a log line.
    """
    gw = _make(EIGHT_DEVICES, send_name_over=False)

    with caplog.at_level(logging.WARNING, logger="elro_connects_k2_protocol.gateway"):
        names = await gw.sync_device_names()

    assert names == EIGHT_DEVICES
    assert any("without NAME_OVER" in r.getMessage() for r in caplog.records), (
        "a truncated name sync was not reported"
    )


async def test_a_hub_that_never_stops_hits_the_cap() -> None:
    """The idle window must not let an endless stream run forever."""
    gw = _make({}, _EndlessHub, send_name_over=False)

    loop = asyncio.get_running_loop()
    started = loop.time()
    names = await gw.sync_device_names()
    elapsed = loop.time() - started

    assert names, "the endless hub sent nothing at all"
    assert elapsed < gateway_mod._NAME_MAX_SECONDS * 2, "overshot the absolute cap"


async def test_a_hub_with_no_names_returns_empty() -> None:
    """NAME_OVER with nothing before it is a valid, non-alarming answer."""
    gw = _make({})

    names = await gw.sync_device_names()

    assert names == {}
