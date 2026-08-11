"""python -m elro_connects_k2_protocol — CLI for the ELRO Connects K2 library.

Exercises the library's own async API end-to-end without a HA layer.
Complements tools/k2_udp_probe.py (raw wire-level JSON) — this shows
parsed, human-readable output and is the quickest way to confirm the
library works against real hardware.

Usage:
    python -m elro_connects_k2_protocol [--gateway-ip IP --device-name NAME] [--verbose] COMMAND

Commands:
    sync            (default) Connect, sync all devices, print a table, exit.
    listen          Connect, sync once, then print every push event until Ctrl-C.
    gateway-info    Query CMD_CODE 12 and print the raw gateway info response.
    pair            Open a join window and wait for a detector to be paired.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from elro_connects_k2_protocol.gateway import (
    PAIRING_TIMEOUT_SECONDS,
    K2Gateway,
    discover_gateway,
)
from elro_connects_k2_protocol.models import SubDevice, UpdateSource


def _fmt_device(device: SubDevice) -> str:
    model = ", ".join(device.profile.model_hints) if device.profile.model_hints else device.device_type
    return (
        f"Sub {device.sub_id:2d}  {device.profile.name} ({model})"
        f"   signal={device.signal_bars}  battery={device.battery_pct}%"
        f"  status={device.alarm_state.name}"
    )


def _on_update(sub_id: int, device: SubDevice, source: UpdateSource) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    tag = f"[{source.name:4s}]"
    model = ", ".join(device.profile.model_hints) if device.profile.model_hints else device.device_type
    print(
        f"{ts}  {tag}  sub={sub_id}"
        f"  {device.profile.name} ({model})"
        f"  alarm={device.alarm_state.name}"
        f"  battery={device.battery_pct}%"
        f"  signal={device.signal_bars}"
    )


async def _get_gateway(args: argparse.Namespace) -> K2Gateway | None:
    if args.gateway_ip and args.device_name:
        return K2Gateway(args.gateway_ip, args.device_name)
    print(f"Searching for K2 on {args.broadcast} …", flush=True)
    gw = await discover_gateway(broadcast=args.broadcast, timeout=args.timeout)
    if gw is None:
        print("No K2 found. Use --gateway-ip and --device-name to skip discovery.", file=sys.stderr)
    return gw


async def cmd_sync(args: argparse.Namespace) -> int:
    gw = await _get_gateway(args)
    if gw is None:
        return 1
    await gw.connect()
    try:
        print(f"Gateway: {gw.device_name} @ {gw.ip}")
        devices = await gw.sync_devices()
        if not devices:
            print("  (no devices returned)")
        for device in sorted(devices.values(), key=lambda d: d.sub_id):
            print(" ", _fmt_device(device))
    finally:
        await gw.disconnect()
    return 0


async def cmd_listen(args: argparse.Namespace) -> int:
    gw = await _get_gateway(args)
    if gw is None:
        return 1
    gw.add_update_callback(_on_update)
    await gw.connect()
    print(f"Gateway: {gw.device_name} @ {gw.ip}")
    print("Performing initial sync …")
    await gw.sync_devices()
    print("Listening for push events. Press Ctrl-C to stop.\n")
    try:
        # Run forever; push events arrive via _on_update callback
        await asyncio.get_running_loop().create_future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gw.disconnect()
    return 0


async def cmd_gateway_info(args: argparse.Namespace) -> int:
    gw = await _get_gateway(args)
    if gw is None:
        return 1
    await gw.connect()
    try:
        info = await gw.get_gateway_info()
        if info is None:
            print("No response from gateway (CMD_CODE 12 timed out)", file=sys.stderr)
            return 1
        print(f"Gateway: {info.device_name}")
        print(f"  data_str1: {info.raw_data_str1!r}")
        print(f"  data_str2: {info.raw_data_str2!r}")
    finally:
        await gw.disconnect()
    return 0


async def cmd_pair(args: argparse.Namespace) -> int:
    """Interactive pairing round — needs someone at the detector."""
    gw = await _get_gateway(args)
    if gw is None:
        return 1
    await gw.connect()
    try:
        print(f"Gateway: {gw.device_name} @ {gw.ip}")
        # Knowing the occupied slots up front makes it obvious afterwards
        # whether the detector took a fresh slot or replaced an old one.
        before = await gw.sync_devices()
        print(f"Currently paired: {sorted(before)}")
        print(f"\nOpening join window for {args.pair_timeout:.0f} s …")
        print("Trigger the detector's pairing action now "
              "(usually holding its test button; see the manual for your model).\n")
        result = await gw.pair_new_device(timeout=args.pair_timeout)
        if result is None:
            print("Nothing joined — the window closed with no new device.", file=sys.stderr)
            return 1
        note = " (slot was already in use)" if result.already_known else ""
        print(f"Joined: {_fmt_device(result.device)}{note}")
        print("Re-syncing for real signal/battery values …")
        after = await gw.sync_devices()
        device = after.get(result.device.sub_id)
        if device is not None:
            print(" ", _fmt_device(device))
    except KeyboardInterrupt:
        gw.cancel_pairing()
        print("\nCancelled; join window closed.", file=sys.stderr)
        return 1
    finally:
        await gw.disconnect()
    return 0


_COMMANDS = {
    "sync": cmd_sync,
    "listen": cmd_listen,
    "gateway-info": cmd_gateway_info,
    "pair": cmd_pair,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m elro_connects_k2_protocol",
        description="ELRO Connects K2 library CLI — parsed output, no HA required.",
    )
    parser.add_argument("command", nargs="?", default="sync", choices=list(_COMMANDS))
    parser.add_argument("--gateway-ip", help="Skip discovery and use this gateway IP.")
    parser.add_argument("--device-name", help="Gateway device name (e.g. ST_1234567890).")
    parser.add_argument("--broadcast", default="255.255.255.255", help="Broadcast address for discovery.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Discovery timeout in seconds.")
    parser.add_argument(
        "--pair-timeout",
        type=float,
        default=PAIRING_TIMEOUT_SECONDS,
        help="How long the 'pair' command holds the join window open, in seconds.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging from the library (shows source=PUSH/POLL lines).",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(name)s %(levelname)s %(message)s")
    handler = _COMMANDS[args.command]
    return await handler(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
