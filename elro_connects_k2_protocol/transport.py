"""The process-wide UDP socket on port 1025, shared by every gateway.

## Why this is shared rather than per-gateway

Each ``K2Gateway`` used to bind its own socket to ``0.0.0.0:1025``.  With
``SO_REUSEADDR`` set the second bind *succeeds*, so nothing errors — but an
inbound unicast datagram is delivered to only one of the sockets bound to that
address, and it is the most recently bound one that wins.  Two gateways
therefore meant one working hub and one permanently deaf hub, with the pair
swapping roles every time Home Assistant retried the failing entry.

Worse, the socket that did win received frames from *both* hubs, and nothing
downstream checked which hub a frame came from: hub A's sync response was
parsed into hub B's device table, where the two hubs' sub-device ids — both
numbered from 1 — overwrote each other.

The vendor Android app has neither problem because it never binds more than
once.  ``domain/udp/UdpSocket.java`` is a singleton holding one socket on 1025
for every gateway the user owns, and ``domain/udp/UdpControlProxy.receiveData``
demultiplexes each frame on the ``devID`` it carries, using the datagram's
source address only to record where that hub currently lives
(``onNodeAckDeal`` → ``IntranetDaoUtil.insertGateway``).  This module is that
design: one socket, a ``devID`` → listener table, refcounted by its users.

## Routing rules

A frame whose ``devID`` matches a registered gateway goes to that gateway.

A frame carrying a ``devID`` that matches *no* registered gateway is dropped.
That is a third hub on the network, or one this process is not configured for,
and delivering it anywhere is how the cross-talk bug above happened.

A frame with no ``devID`` at all is delivered when exactly one gateway is
registered, and dropped when several are.  Every frame the hub is known to send
carries ``devID`` — the app reads it unconditionally before dispatching — so
this only covers shapes that have not been observed, and it keeps the
single-gateway case behaving exactly as it did before routing existed.

Sniffers (used by discovery) see every frame regardless, since a discovery
broadcast is answered by hubs that are by definition not yet registered.
"""

from __future__ import annotations

import asyncio
import logging
import socket as _socket
import weakref
from collections.abc import Callable
from typing import Any

from elro_connects_k2_protocol.protocol import UDP_PORT, decrypt_message

_LOGGER = logging.getLogger(__name__)

#: Called with a decoded inbound frame and the IP address it arrived from.
Listener = Callable[[dict[str, Any], str], None]


class _SharedSocket(asyncio.DatagramProtocol):
    """The real socket on port 1025, plus the routing table over it.

    One instance per event loop.  Users acquire and release it rather than
    opening and closing it, so the last gateway to go away is what actually
    closes the port.
    """

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._listeners: dict[str, Listener] = {}
        self._sniffers: list[Listener] = []
        self._refs = 0
        # Distinguishes a close we asked for from one the OS forced on us, so
        # a routine unload does not log a warning about losing the connection.
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._transport is not None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def acquire(self) -> None:
        """Take a reference, binding the socket if this is the first one."""
        async with self._lock:
            if self._transport is not None:
                self._refs += 1
                return
            await self._bind()
            self._refs += 1

    def release(self) -> None:
        """Drop a reference, closing the socket once the last one goes."""
        self._refs = max(0, self._refs - 1)
        if self._refs > 0 or self._transport is None:
            return
        self._closing = True
        self._transport.close()
        self._transport = None
        _LOGGER.debug("Shared UDP socket on port %d closed", UDP_PORT)

    async def _bind(self) -> None:
        """Bind 0.0.0.0:1025.

        SO_REUSEADDR is kept from the per-gateway implementation so that a
        reload (unload then immediate setup) can rebind before the OS has fully
        released the previous socket: ``transport.close()`` is non-blocking and
        the port can still count as in use until the next event-loop iteration
        processes it.  Note that it no longer papers over *this* library
        double-binding — there is only ever one socket now — so an EADDRINUSE
        here means something else on the host holds port 1025, which is a real
        error worth surfacing.
        """
        loop = asyncio.get_running_loop()
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        # Replaces allow_broadcast=, which cannot be passed alongside sock= in
        # Python 3.14+.  Needed for the discovery broadcast.
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", UDP_PORT))
        self._closing = False
        await loop.create_datagram_endpoint(lambda: self, sock=sock)
        _LOGGER.debug("Shared UDP socket bound to port %d", UDP_PORT)

    # ── registration ──────────────────────────────────────────────────────────

    def register(self, device_name: str, listener: Listener) -> None:
        if device_name in self._listeners:
            _LOGGER.warning(
                "A gateway named %r is already receiving on port %d; the new "
                "registration replaces it",
                device_name, UDP_PORT,
            )
        self._listeners[device_name] = listener

    def unregister(self, device_name: str, listener: Listener) -> None:
        # Compared by identity so a stale handle cannot unregister the gateway
        # that replaced it.
        if self._listeners.get(device_name) is listener:
            del self._listeners[device_name]

    def add_sniffer(self, listener: Listener) -> None:
        self._sniffers.append(listener)

    def remove_sniffer(self, listener: Listener) -> None:
        if listener in self._sniffers:
            self._sniffers.remove(listener)

    # ── asyncio.DatagramProtocol ──────────────────────────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text, obj = decrypt_message(data)
        if obj is None:
            # Dropping these without a word made "the hub answered with
            # something we could not read" indistinguishable from "the hub
            # never answered", which is precisely the question a hub that acks
            # every ping while returning no devices raises.  Logged with the
            # decoded text so a framing or truncation problem is readable
            # straight from the log.
            _LOGGER.debug(
                "Undecodable %d-byte datagram from %s: %r",
                len(data), addr[0], text[:200],
            )
            return
        self.dispatch(obj, addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._closing:
            return
        _LOGGER.warning("UDP connection lost: %s", exc)
        self._transport = None

    # ── routing ───────────────────────────────────────────────────────────────

    def dispatch(self, obj: dict[str, Any], source_ip: str) -> None:
        """Hand one decoded frame to whichever gateway owns it.

        Separate from ``datagram_received`` so the routing rules can be tested
        without a socket.  See the module docstring for the rules themselves.
        """
        for sniffer in list(self._sniffers):
            sniffer(obj, source_ip)

        dev_id = obj.get("devID")

        if isinstance(dev_id, str) and dev_id != "NULL":
            listener = self._listeners.get(dev_id)
            if listener is not None:
                listener(obj, source_ip)
            elif self._listeners:
                # Another hub on the same network, answering its own app or
                # broadcasting to everyone.  Feeding it to a gateway that did
                # not ask is exactly the cross-talk this routing exists to stop.
                _LOGGER.debug(
                    "Dropping frame from %s for unknown devID %r (registered: %s)",
                    source_ip, dev_id, ", ".join(sorted(self._listeners)) or "none",
                )
            return

        # No devID: a shape the hub has not been observed to send.  Deliverable
        # only while it is unambiguous who it belongs to.
        if len(self._listeners) == 1:
            next(iter(self._listeners.values()))(obj, source_ip)
        elif self._listeners:
            _LOGGER.debug(
                "Dropping frame from %s with no devID; %d gateways are registered "
                "so there is no way to tell which one it belongs to",
                source_ip, len(self._listeners),
            )

    # ── sending ───────────────────────────────────────────────────────────────

    def sendto(self, payload: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None:
            _LOGGER.warning("Send attempted on a closed shared socket; message dropped")
            return
        self._transport.sendto(payload, addr)


class K2Transport:
    """One user's handle on the shared socket.

    Deliberately exposes the same ``sendto``/``close`` pair as an
    ``asyncio.DatagramTransport``, so callers — and the fake transports in the
    test suite — cannot tell the difference.  ``close()`` unregisters this
    user's listener and drops its reference; the socket itself survives as long
    as any other gateway still holds one.
    """

    def __init__(
        self,
        shared: _SharedSocket,
        listener: Listener,
        device_name: str | None,
    ) -> None:
        self._shared = shared
        self._listener = listener
        self._device_name = device_name
        self._closed = False

    def sendto(self, payload: bytes, addr: tuple[str, int]) -> None:
        self._shared.sendto(payload, addr)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._device_name is None:
            self._shared.remove_sniffer(self._listener)
        else:
            self._shared.unregister(self._device_name, self._listener)
        self._shared.release()


# ── the per-loop singleton ────────────────────────────────────────────────────

# Keyed by event loop rather than being a plain module global: a test suite
# runs each test on a fresh loop, and a socket bound to a loop that has since
# closed is useless.  The weak keys mean a finished loop takes its entry with
# it.
_SHARED: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _SharedSocket] = (
    weakref.WeakKeyDictionary()
)


def _shared_socket() -> _SharedSocket:
    loop = asyncio.get_running_loop()
    shared = _SHARED.get(loop)
    if shared is None:
        shared = _SharedSocket()
        _SHARED[loop] = shared
    return shared


async def open_transport(device_name: str, listener: Listener) -> K2Transport:
    """Attach a gateway to the shared socket, binding it if needed.

    Frames carrying ``devID == device_name`` are routed to ``listener``.
    """
    shared = _shared_socket()
    await shared.acquire()
    try:
        shared.register(device_name, listener)
    except BaseException:
        shared.release()
        raise
    return K2Transport(shared, listener, device_name)


async def open_sniffer(listener: Listener) -> K2Transport:
    """Attach an unrouted listener that sees every inbound frame.

    Discovery needs this: a broadcast is answered by hubs that are, by
    definition, not registered yet.  It also lets discovery run while gateways
    are live, which the old private-socket implementation could not do.
    """
    shared = _shared_socket()
    await shared.acquire()
    try:
        shared.add_sniffer(listener)
    except BaseException:
        shared.release()
        raise
    return K2Transport(shared, listener, None)


def shared_socket_is_open() -> bool:
    """Whether this loop's shared socket is currently bound.  For diagnostics."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    shared = _SHARED.get(loop)
    return shared is not None and shared.is_open
