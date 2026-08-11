#!/usr/bin/env python3
"""Simulates the K2 hub on localhost for HA / library development without hardware.

Sends real XOR-framed UDP packets to port 1025, exercising the full production
parse path of the elro_connects_k2_protocol library — nothing is mocked.

## How it works

The production K2Gateway binds to 0.0.0.0:1025 and listens.  This script sends
packets FROM an OS-assigned ephemeral port TO 127.0.0.1:1025, which the gateway
receives and processes exactly as if they came from real hardware.

The gateway also sends outbound commands (IOT_KEY? activation, CMD_CODE 54 sync
requests, ACKs) back to 127.0.0.1:1025, which loops them to itself.  The gateway
ignores its own messages, so this is harmless.

## Setup

Configure the HA integration (or python -m elro_connects_k2_protocol) with:
  host        = 127.0.0.1
  device_name = DEMO_DEVICE   (must match --device-name here)

### Standalone
  python -m elro_connects_k2_protocol --gateway-ip 127.0.0.1 --device-name DEMO_DEVICE listen &
  python tools/k2_simulator.py

### Driving Home Assistant
The elro-connects-k2-ha repo's compose file maps host port 1025 → container
port 1025, so the simulator runs on the host and HA runs in the container — no
changes needed here.  See that repo's README.

## Simulated devices

  Sub 1  Photoelectric Smoke Alarm  (type 013 / GS559A)
  Sub 2  CO + Gas Alarm             (type 014 / GS891A)
  Sub 3  CO2/Temp/Humidity detector (type 018)
  Sub 4  Water Alarm                (type 004 / GS156A)
  Sub 5  Door/Window Sensor         (type 101 / GS320D)
  Sub 6  Radiator Thermostat        (type 215 / GS361)

## Event sequence

  t=0s    CMD_CODE 55  — initial sync response (all 6 devices, CLEAR)
  t=1.5s  CMD_CODE 66  — CO2/TH full status (populates co2/temp/humidity values)
  then loop every ~5 s, rotating through:
    CMD_CODE 19  temperature push for sub 3
    CMD_CODE 19  humidity push for sub 3
    CMD_CODE 19  CO2 level push for sub 3 (slowly rising from 600→1200→600 ppm)
    CMD_CODE 19  smoke alarm TRIGGERS on sub 1
    CMD_CODE 19  smoke alarm CLEARS on sub 1  (10 s later)
    CMD_CODE 19  door OPENS on sub 5
    CMD_CODE 19  door CLOSES on sub 5  (5 s later)
    CMD_CODE 19  thermostat valve OPENS on sub 6  (starts heating)
    CMD_CODE 19  thermostat valve CLOSES on sub 6  (5 s later, setpoint reached)
    CMD_CODE 55  periodic re-sync
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time

# Allow running from repo root without installing the package.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from elro_connects_k2_protocol.protocol import (
    encrypt_message,
)

# ── demo configuration ────────────────────────────────────────────────────────

_DEFAULT_DEVICE_NAME = "DEMO_DEVICE"
_DEFAULT_TARGET = ("127.0.0.1", 1025)

# 14-char CMD_CODE 55 sync record layout (from parse_status_record):
#   [0:2]   sub_id     (1-byte hex)
#   [2:6]   raw_type   (2-byte hex, e.g. "0013")
#   [6]     signal nibble A  (1 hex char; feeds into deviceStatus[0:2] as "0"+char)
#   [7]     room nibble B    (1 hex char; ignored by parser)
#   [8:10]  battery    (1-byte hex; decoded as int & 0x7F → %)
#   [10:12] alarm state (e.g. "AA"=clear, "55"=alarm)
#   [12:14] sub-state   (opaque; preserved as raw_status[6:8])
#
# For example, sub 1 with signal=4, battery=0x57 (87%), CLEAR:
#   "01" + "0013" + "4" + "5" + "57" + "AA" + "55"  =  "0100134557AA55"

_SYNC_RECORDS_CLEAR = (
    # sub_id  raw_type  sig room battery alarm  sub
    "01"    + "0013"  + "4" + "5" + "57" + "AA" + "55",  # smoke alarm
    "02"    + "0014"  + "3" + "5" + "64" + "AA" + "00",  # CO + gas alarm
    "03"    + "0018"  + "3" + "5" + "5F" + "AA" + "00",  # CO2/temp/humidity
    "04"    + "0004"  + "4" + "5" + "70" + "AA" + "00",  # water alarm
    # Door/window sensor (type 101, GS320D) — CLOSED: alarm byte = AA
    "05"    + "0101"  + "4" + "5" + "64" + "AA" + "00",  # door sensor CLOSED
    # Thermostat (type 215, GS361) — valve closed, setpoint 21°C:
    #   alarm byte = b2 = setpoint_floor & 0x1F = 21 = 0x15 (valve bit 0x40 clear)
    #   sub-state   = b3 = (21 << 2) | mode=2 (manual) = 86 = 0x56
    "06"    + "0215"  + "4" + "5" + "5F" + "15" + "56",  # thermostat valve CLOSED
)

_SYNC_RECORDS_SUB1_ALARM = (
    "01"    + "0013"  + "4" + "5" + "57" + "55" + "00",  # smoke ALARM
    "02"    + "0014"  + "3" + "5" + "64" + "AA" + "00",
    "03"    + "0018"  + "3" + "5" + "5F" + "AA" + "00",
    "04"    + "0004"  + "4" + "5" + "70" + "AA" + "00",
    "05"    + "0101"  + "4" + "5" + "64" + "AA" + "00",
    "06"    + "0215"  + "4" + "5" + "5F" + "15" + "56",
)

# CMD_CODE 66 sub-device info for sub 3 (CO2/TH), 30-char data_str2:
#   [0:6]   6-char signal/battery/alarm  ("035FAA" = signal 3, battery 95%, CLEAR)
#   [6:12]  "00" tag + 4-char temperature value  (int=(temp*10+300), e.g. 22.5°C → 525 = 0x020D)
#   [12:18] "04" tag + 4-char humidity value     (int=humidity*10,   e.g. 48.0% → 480 = 0x01E0)
#   [18:24] "08" tag + 4-char CO2 value          (int=ppm,           e.g. 650ppm → 650 = 0x028A)
#   [24:30] 6 unknown bytes (sent as zeros; not decoded by app or library)
_CO2_TH_STATUS_INITIAL = (
    "035FAA"              # signal=3, battery=95%, alarm=CLEAR
    + "00" + "020D"       # temperature: (0x020D=525 → (525-300)/10 = 22.5°C)
    + "04" + "01E0"       # humidity:    (0x01E0=480 → 480/10 = 48.0%)
    + "08" + "028A"       # CO2:         (0x028A=650 ppm)
    + "000000"            # unknown trailing bytes
)
assert len(_CO2_TH_STATUS_INITIAL) == 30

# ── message builders ──────────────────────────────────────────────────────────

def _node_send(device_name: str, cmd_code: int, data_str1: str, data_str2: str) -> bytes:
    """Build an XOR-framed NODE_SEND packet as the K2 would send it."""
    payload = json.dumps({
        "action": "NODE_SEND",
        "devID": device_name,
        "msg": {
            "msg_ID": int(time.time() * 1000) % 1_000_000,
            "CMD_CODE": cmd_code,
            "data_str1": data_str1,
            "data_str2": data_str2,
            "rev_str1": "",
            "rev_str2": "",
            "rev_str3": "",
        },
    })
    return encrypt_message(payload)


def _cmd55_sync(device_name: str, records: tuple[str, ...]) -> bytes:
    """Build a CMD_CODE 55 sync response from a tuple of 14-char status records.

    The parser accepts records split across data_str1 and data_str2, so we put
    the first two records in data_str1 and the rest in data_str2.
    """
    all_records = "".join(records)
    split = len(records[0]) * 2  # first two records → data_str1
    return _node_send(device_name, 55, all_records[:split], all_records[split:])


def _cmd66_sub_device_info(device_name: str, sub_id: int, status_30: str) -> bytes:
    """Build a CMD_CODE 66 sub-device info response (used for CO2/TH devices)."""
    sub_id_hex = f"{sub_id:04X}"
    return _node_send(device_name, 66, sub_id_hex, status_30)


def _cmd19_alarm_push(device_name: str, sub_id: int, raw_type: str, device_status_8: str) -> bytes:
    """Build a CMD_CODE 19 push event for an alarm/status device (8-char status).

    data_str1 layout: sub_id(4) + raw_type(4) + room_byte(2)
    data_str2: 8-char deviceStatus hex
    """
    data_str1 = f"{sub_id:04X}" + raw_type + "05"  # room byte "05" matches live captures
    return _node_send(device_name, 19, data_str1, device_status_8)


def _cmd11_ack(device_name: str, acked_code: int) -> bytes:
    """Build a CMD_CODE 11 ACK for a command the gateway sent.

    ``data_str1`` is padded to 9 characters because that is the length
    ``CoderUtils.getAnswerResult`` requires before it will parse the leading two
    bytes as the acknowledged command code — the app would ignore anything
    shorter, so the hub is assumed to send this shape.
    """
    return _node_send(device_name, 11, f"{acked_code:04X}" + "00000", "OK")


def _cmd62_add_sub_device(device_name: str, sub_id: int, raw_type: str, room: str = "05") -> bytes:
    """Build a CMD_CODE 62 frame announcing that a sub-device joined.

    data_str1 layout: sub_id(4) + raw_type(4) + room id.  The frame carries no
    status — the app substitutes a placeholder until the next sync.
    """
    return _node_send(device_name, 62, f"{sub_id:04X}" + raw_type + room, "")


def _cmd19_co2_th_push(device_name: str, sub_id: int, raw_type: str, tag: str, value_hex: str) -> bytes:
    """Build a CMD_CODE 19 push event for a CO2/TH measurement (6-char status).

    tag:       "00"=temperature, "04"=humidity, "08"=CO2
    value_hex: 4-char hex encoding of the measurement value
    """
    data_str1 = f"{sub_id:04X}" + raw_type + "05"
    data_str2 = tag + value_hex  # 6 chars total
    return _node_send(device_name, 19, data_str1, data_str2)


# ── simulator ─────────────────────────────────────────────────────────────────

class K2Simulator:
    def __init__(self, device_name: str, target: tuple[str, int]) -> None:
        self._device_name = device_name
        self._target = target
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # CO2 oscillates between 600 and 1200 ppm to make the chart interesting
        self._co2_ppm = 650
        self._co2_direction = 1

    def _send(self, packet: bytes) -> None:
        self._sock.sendto(packet, self._target)

    def send_sync(self, records: tuple[str, ...] = _SYNC_RECORDS_CLEAR) -> None:
        self._send(_cmd55_sync(self._device_name, records))
        print(f"  → CMD_CODE 55  sync  ({len(records)} devices)")

    def send_co2_th_info(self) -> None:
        pkt = _cmd66_sub_device_info(self._device_name, 3, _CO2_TH_STATUS_INITIAL)
        self._send(pkt)
        print("  → CMD_CODE 66  CO2/TH initial values  (22.5°C, 48.0%, 650 ppm)")

    def send_temperature(self, temp_c: float) -> None:
        # temperature encoding: int = temp * 10 + 300
        val = int(temp_c * 10 + 300)
        self._send(_cmd19_co2_th_push(self._device_name, 3, "0018", "00", f"{val:04X}"))
        print(f"  → CMD_CODE 19  sub=3  temperature={temp_c}°C")

    def send_humidity(self, humidity_pct: float) -> None:
        # humidity encoding: int = humidity * 10
        val = int(humidity_pct * 10)
        self._send(_cmd19_co2_th_push(self._device_name, 3, "0018", "04", f"{val:04X}"))
        print(f"  → CMD_CODE 19  sub=3  humidity={humidity_pct}%")

    def send_co2(self, ppm: int) -> None:
        self._send(_cmd19_co2_th_push(self._device_name, 3, "0018", "08", f"{ppm:04X}"))
        print(f"  → CMD_CODE 19  sub=3  co2={ppm} ppm")

    def send_alarm_trigger(self, sub_id: int, raw_type: str, signal: str, battery: str) -> None:
        # ALARM state: alarm byte = "55"
        status = "0" + signal + battery + "5500"
        self._send(_cmd19_alarm_push(self._device_name, sub_id, raw_type, status))
        print(f"  → CMD_CODE 19  sub={sub_id}  ALARM  🔴")

    def send_alarm_clear(self, sub_id: int, raw_type: str, signal: str, battery: str) -> None:
        # CLEAR state: alarm byte = "AA"
        status = "0" + signal + battery + "AA00"
        self._send(_cmd19_alarm_push(self._device_name, sub_id, raw_type, status))
        print(f"  → CMD_CODE 19  sub={sub_id}  CLEAR  ✓")

    def send_door_open(self) -> None:
        # Door open: alarm byte = "55" (standard ALARM encoding for contact sensors)
        status = "0" + "4" + "64" + "5500"
        self._send(_cmd19_alarm_push(self._device_name, 5, "0101", status))
        print("  → CMD_CODE 19  sub=5  door OPEN")

    def send_door_close(self) -> None:
        # Door closed: alarm byte = "AA"
        status = "0" + "4" + "64" + "AA00"
        self._send(_cmd19_alarm_push(self._device_name, 5, "0101", status))
        print("  → CMD_CODE 19  sub=5  door CLOSED")

    def send_thermostat(
        self,
        setpoint_c: float,
        valve_open: bool,
        window_open: bool = False,
        room_temp_c: int = 19,
        mode: int = 2,
    ) -> None:
        """Send a GS361 thermostat push (CMD_CODE 19, 8-char status).

        Status byte layout (ThermostatVModel encoding):
          [0:2] = signal ("04")
          [2:4] = battery ("5F" = 95%)
          [4:6] = b2: bit7=window, bit6=valve(0x40), bit5=+0.5°C(0x20), bits0-4=floor
          [6:8] = b3: bits2-7=measured room temperature, bits0-1=mode

        ``room_temp_c`` deliberately defaults to a value distinct from any
        setpoint the scenario uses, so that a decoder confusing the two (they
        live in adjacent bytes) fails visibly instead of looking correct.
        """
        floor = int(setpoint_c)
        half = int((setpoint_c - floor) >= 0.5)
        b2 = floor & 0x1F
        if half:
            b2 |= 0x20
        if valve_open:
            b2 |= 0x40
        if window_open:
            b2 |= 0x80
        b3 = ((room_temp_c & 0x3F) << 2) | (mode & 0x03)
        status = f"045F{b2:02X}{b3:02X}"
        self._send(_cmd19_alarm_push(self._device_name, 6, "0215", status))
        state = "VALVE OPEN (heating)" if valve_open else "valve closed"
        print(
            f"  → CMD_CODE 19  sub=6  thermostat  setpoint={setpoint_c}°C  "
            f"room={room_temp_c}°C  mode={mode}  {state}"
        )

    def _next_co2(self) -> int:
        """Slowly oscillate CO2 between 600 and 1200 ppm."""
        self._co2_ppm += self._co2_direction * 50
        if self._co2_ppm >= 1200:
            self._co2_direction = -1
        elif self._co2_ppm <= 600:
            self._co2_direction = 1
        return self._co2_ppm

    def send_pair_ack(self) -> None:
        self._send(_cmd11_ack(self._device_name, 2))
        print("  → CMD_CODE 11  ACK for CMD_CODE 2  (join window open)")

    def send_join(self, sub_id: int, raw_type: str) -> None:
        self._send(_cmd62_add_sub_device(self._device_name, sub_id, raw_type))
        print(f"  → CMD_CODE 62  sub={sub_id} type={raw_type} joined  🔗")

    async def run_once(self) -> None:
        """Fire one initial sync + CO2/TH info then exit."""
        self.send_sync()
        await asyncio.sleep(1.5)
        self.send_co2_th_info()
        print("Done.")

    async def run_pair(self, sub_id: int, raw_type: str, delay: float) -> None:
        """Play the hub's side of a pairing round, then exit.

        This is blind-fired on a timer rather than triggered by the gateway's
        CMD_CODE 2, because on loopback the gateway's outbound commands go to
        127.0.0.1:1025 — itself — and never reach the simulator.  So start the
        pairing request first, then run this within the join window.
        """
        print(f"Waiting {delay:.0f} s for a pairing request to be in flight …")
        await asyncio.sleep(delay)
        self.send_pair_ack()
        # A real detector takes a moment to complete its RF handshake.
        await asyncio.sleep(1.5)
        self.send_join(sub_id, raw_type)
        # The device is expected to show up in the next inventory sync too.
        # Signal and battery here deliberately differ from the placeholder the
        # join frame implies (4 bars / 100 %), so a client that fails to replace
        # the placeholder with real values shows it.
        await asyncio.sleep(1.0)
        joined = f"{sub_id:02X}" + raw_type + "3" + "5" + "51" + "AA" + "00"
        self.send_sync((*_SYNC_RECORDS_CLEAR, joined))
        print("Done.  (synced values: signal=3, battery=81% — placeholder was 4 / 100%)")

    async def run_forever(self) -> None:
        """Fire events continuously until Ctrl-C."""
        print(f"Simulator started → {self._target[0]}:{self._target[1]}")
        print(f"  device_name={self._device_name!r}")
        print("Configure HA (or python -m elro_connects_k2_protocol) with:")
        print(f"  host={self._target[0]}   device_name={self._device_name}")
        print()

        # Initial burst: sync first, then CO2/TH detail after 1.5 s so it
        # arrives within get_sub_device_info's 5-second timeout.
        self.send_sync()
        await asyncio.sleep(1.5)
        self.send_co2_th_info()

        smoke_alarm_active = False
        door_open = False
        valve_open = False
        thermo_mode = 2  # start in manual
        step = 0

        try:
            while True:
                await asyncio.sleep(5)
                step += 1
                ts = time.strftime("%H:%M:%S")
                print(f"\n[{ts}]")

                if step % 12 == 0:
                    # Periodic re-sync every ~60 s
                    records = _SYNC_RECORDS_SUB1_ALARM if smoke_alarm_active else _SYNC_RECORDS_CLEAR
                    self.send_sync(records)

                elif step % 8 == 7 and not smoke_alarm_active:
                    smoke_alarm_active = True
                    self.send_alarm_trigger(1, "0013", "4", "57")

                elif smoke_alarm_active:
                    smoke_alarm_active = False
                    self.send_alarm_clear(1, "0013", "4", "57")

                elif step % 7 == 4 and not door_open:
                    door_open = True
                    self.send_door_open()

                elif door_open:
                    door_open = False
                    self.send_door_close()

                elif step % 5 == 3:
                    # Toggle thermostat valve, and cycle the operating mode so
                    # the mode sensor is exercised too.  Room temperature drifts
                    # below the 21.0 °C setpoint, which is why the valve opens.
                    valve_open = not valve_open
                    thermo_mode = (thermo_mode + 1) % 4
                    self.send_thermostat(
                        21.0, valve_open, room_temp_c=18 if valve_open else 20, mode=thermo_mode
                    )

                else:
                    # Rotate through CO2/TH measurements
                    cycle = step % 3
                    if cycle == 0:
                        self.send_temperature(22.5 + (step % 4) * 0.5)
                    elif cycle == 1:
                        self.send_humidity(48.0 + (step % 5) * 1.0)
                    else:
                        self.send_co2(self._next_co2())

        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nSimulator stopped.")
        finally:
            self._sock.close()


# ── entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python tools/k2_simulator.py",
        description="Simulate the K2 hub by firing real UDP packets to localhost.",
    )
    p.add_argument(
        "--device-name",
        default=_DEFAULT_DEVICE_NAME,
        help=f"Device name reported in packets (default: {_DEFAULT_DEVICE_NAME}). "
             "Must match the device_name you enter in the HA config flow.",
    )
    p.add_argument(
        "--target",
        default=f"{_DEFAULT_TARGET[0]}:{_DEFAULT_TARGET[1]}",
        help="HOST:PORT to send packets to (default: 127.0.0.1:1025).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Fire one initial sync + CO2/TH info then exit (useful for scripting).",
    )
    p.add_argument(
        "--pair",
        action="store_true",
        help="Play the hub's side of a pairing round (CMD_CODE 11 ACK, then 62, then a "
             "sync including the new device) and exit. Start the pairing request first.",
    )
    p.add_argument(
        "--pair-sub-id",
        type=int,
        default=7,
        help="Sub-device slot the simulated detector joins on (default: 7).",
    )
    p.add_argument(
        "--pair-type",
        default="0013",
        help="Raw device type of the simulated detector (default: 0013, smoke alarm).",
    )
    p.add_argument(
        "--pair-delay",
        type=float,
        default=8.0,
        help="Seconds to wait before answering, so the pairing request is already in "
             "flight (default: 8).",
    )
    return p.parse_args()


async def _main() -> None:
    args = _parse_args()
    host, _, port_str = args.target.partition(":")
    target = (host, int(port_str))
    sim = K2Simulator(args.device_name, target)
    if args.pair:
        await sim.run_pair(args.pair_sub_id, args.pair_type, args.pair_delay)
    elif args.once:
        await sim.run_once()
    else:
        await sim.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
