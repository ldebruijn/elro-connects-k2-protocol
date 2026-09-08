# elro-connects-k2-protocol

Local-first Python control of the **ELRO Connects K2** (SF50GA) Wi-Fi gateway for smoke, CO,
heat, and water detectors — no cloud, no vendor app required.

The K2 hub talks a simple XOR-framed UDP JSON protocol on port 1025. This is a standalone
async library with no third-party runtime dependencies — it works with or without Home
Assistant.

> Looking for the Home Assistant integration built on this library? See
> [elro-connects-k2-ha](https://github.com/ldebruijn/elro-connects-k2-ha).

| Path | What it is |
|---|---|
| `elro_connects_k2_protocol/` | The library |
| `tools/k2_simulator.py` | Fake K2 hub — develop without hardware |
| `tools/k2_udp_probe.py` | Raw wire-level probe (stdlib only, no deps) for debugging |
| `docs/protocol_reference.md` | Full wire protocol reference |
| `docs/research.md` | How the protocol was worked out |

---

## Requirements

- Python 3.12+
- No third-party runtime dependencies — asyncio stdlib only

## Installation

```bash
git clone https://github.com/ldebruijn/elro-connects-k2-protocol.git
cd elro-connects-k2-protocol

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Library has no runtime deps; install dev deps for tests and type checking
pip install -r requirements-dev.txt
```

## Quick start

```python
import asyncio
from elro_connects_k2_protocol import K2Gateway, AlarmState, UpdateSource

async def main():
    # Skip broadcast discovery if you already know the gateway IP
    gw = K2Gateway("192.168.1.50", "ST_1234567890")

    def on_update(sub_id, device, source):
        tag = "[PUSH]" if source == UpdateSource.PUSH else "[POLL]"
        print(f"{tag} sub={sub_id}  {device.profile.name}  alarm={device.alarm_state.name}  battery={device.battery_pct}%")

    gw.add_update_callback(on_update)
    await gw.connect()

    # Initial state fetch (CMD_CODE 54 → 55/56 response)
    devices = await gw.sync_devices()
    for d in devices.values():
        print(f"Sub {d.sub_id}: {d.profile.name}  signal={d.signal_bars}  battery={d.battery_pct}%")

    # Wait for live push events (CMD_CODE 19 — fires when a detector alarms)
    await asyncio.sleep(60)
    await gw.disconnect()

asyncio.run(main())
```

Or let the library discover the gateway automatically on the LAN:

```python
from elro_connects_k2_protocol import discover_gateway

async def main():
    gw = await discover_gateway(broadcast="255.255.255.255", timeout=5.0)
    if gw is None:
        print("No K2 found")
        return
    await gw.connect()
    devices = await gw.sync_devices()
    ...
```

## CLI

```bash
source .venv/bin/activate

# Discover, connect, print all devices with decoded fields, exit
python -m elro_connects_k2_protocol sync

# Skip discovery when IP is already known (faster)
python -m elro_connects_k2_protocol --gateway-ip 192.168.1.50 --device-name ST_1234567890 sync

# Watch for live push events; trigger a detector to see [PUSH] lines appear
python -m elro_connects_k2_protocol --gateway-ip 192.168.1.50 --device-name ST_1234567890 listen

# Show raw gateway info fields
python -m elro_connects_k2_protocol --gateway-ip 192.168.1.50 --device-name ST_1234567890 gateway-info

# Add a new detector: opens a 60 s join window, then waits for you to trigger
# the detector's pairing action. No device type to pick — see "Adding devices".
python -m elro_connects_k2_protocol --gateway-ip 192.168.1.50 --device-name ST_1234567890 pair

# Add -v / --verbose to enable library DEBUG logging (shows source=PUSH/POLL)
python -m elro_connects_k2_protocol --gateway-ip 192.168.1.50 --device-name ST_1234567890 --verbose listen
```

`sync` output:

```
Gateway: ST_1234567890 @ 192.168.1.50
  Sub  1  Photoelectric Smoke Alarm (GS559A)   signal=4  battery=87%  status=CLEAR
  Sub  2  Photoelectric Smoke Alarm (GS559A)   signal=3  battery=95%  status=CLEAR
  Sub  3  Photoelectric Smoke Alarm (GS559A)   signal=3  battery=95%  status=CLEAR
```

`listen` output (trigger a detector to see PUSH lines):

```
14:22:58  [POLL]  sub=1  Photoelectric Smoke Alarm (GS559A)  alarm=CLEAR   battery=87%  signal=4
14:23:01  [PUSH]  sub=1  Photoelectric Smoke Alarm (GS559A)  alarm=ALARM   battery=87%  signal=4
14:23:05  [PUSH]  sub=1  Photoelectric Smoke Alarm (GS559A)  alarm=CLEAR   battery=87%  signal=4
```

## Adding devices

`pair_new_device()` opens a join window on the gateway and blocks until a detector joins —
the ELRO app is not needed.

**There is no device type to choose.** The vendor app's type picker is never transmitted —
it only selects which on-screen instructions to show. The gateway accepts whichever detector
joins during the window, which also means a window left open will adopt the next detector
triggered in range. See "Finding 4" in [`docs/research.md`](docs/research.md).

Removing a device is not implemented: it needs `CMD_CODE 4`, which is destructive and
untested here. Use the ELRO app for that.

## Running tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

Tests are fixture-driven: real decoded UDP payloads captured from live hardware live under
`tests/fixtures/` as JSON files with `input` (raw gateway message) and `expected` (parsed
field values). Adding a new fixture automatically adds it to the parametrized test run — no
test code to write.

Lint and type checking:

```bash
ruff check .
mypy --strict .        # whole repo: package, tests, and tools
```

CI additionally runs the suite on Python 3.12, 3.13, and 3.14, and builds the
wheel and installs it into a clean environment to confirm the distribution is
complete.

`ruff format` is deliberately not enforced — it collapses the hand-aligned
protocol byte-layout tables (see `_SYNC_RECORDS_CLEAR` in `tools/k2_simulator.py`)
that are meant to be read against their header comment.

## Library API

```
elro_connects_k2_protocol/
  gateway.py         K2Gateway — async gateway client, persistent UDP listener
  models.py          SubDevice, GatewayInfo, AlarmState, UpdateSource, DeviceCapability, DeviceProfile
  device_profiles.py DEVICE_PROFILES registry — type code → DeviceProfile (capabilities list)
  parser.py          Pure parse functions (testable without a gateway connection)
  protocol.py        XOR framing, encrypt/decrypt, message builders
  transport.py       The shared UDP socket on port 1025 and its devID routing table
```

**`K2Gateway`** holds a reference to the shared UDP socket for the lifetime of a session.
Its core methods:

| Method | Protocol | Description |
|---|---|---|
| `connect()` | IOT_KEY? | Attach to the shared port-1025 socket, activate session |
| `disconnect()` | — | Detach; the socket closes once the last gateway leaves |
| `activate()` | IOT_KEY? | Re-activate / keepalive (call every ~60 s) |
| `sync_devices()` | CMD_CODE 54 → 55/56, then 24 → 17 | Fetch all detector states and custom names |
| `sync_device_names()` | CMD_CODE 24 → 17 | Fetch sub-device nicknames from the hub |
| `get_gateway_info()` | CMD_CODE 12 → 13 | Fetch gateway metadata |
| `pair_new_device(timeout)` | CMD_CODE 2 → 11, then 62 | Open a join window and wait for a detector; returns `PairingResult \| None` |
| `start_pairing()` | CMD_CODE 2 → 11 | Open a join window; `False` if the hub didn't accept |
| `cancel_pairing()` | CMD_CODE 7 | Close an open join window |
| `add_update_callback(cb)` | — | Register `(int, SubDevice, UpdateSource) → None` |

Push events (CMD_CODE 19) arrive asynchronously and fire callbacks with `UpdateSource.PUSH`
immediately when the K2 sends them — no polling required.

### Several hubs

K2 hubs do not mesh — a house with an outbuilding runs one hub per building, each with its
own detectors, each numbering them from 1. Create one `K2Gateway` per hub and connect them
all; they share a single socket on port 1025 and frames are routed to the right gateway by
the `devID` each one carries, exactly as the vendor app does it.

This has to be shared rather than one socket each: the hub only ever sends to port 1025 and
ignores commands from an ephemeral source port, so there is one port to bind and binding it
twice does not divide the traffic — one gateway would receive everything and the other
nothing. See `docs/protocol_reference.md` → *Several gateways share one socket*.

```python
gateways = await discover_gateways()          # every hub that answers the broadcast
for gw in gateways:
    gw.add_update_callback(on_update)
    await gw.connect()
```

`discover_gateway()` (singular) is still there and returns the first responder.

**`SubDevice`** fields:

| Field | Type | Description |
|---|---|---|
| `sub_id` | int | Sub-device index (1-based) |
| `raw_type` | str | 4-char hex type code from gateway, e.g. `"0013"` |
| `device_type` | str | Normalized 3-char code, e.g. `"013"` |
| `profile` | DeviceProfile | Resolved capabilities for this device type |
| `signal_bars` | int | Signal strength 1–4 |
| `battery_pct` | int | Battery 0–100 % |
| `alarm_state` | AlarmState | `CLEAR`, `ALARM`, `SILENCED`, `FAULT`, or `UNKNOWN` |
| `raw_status` | str | Full 8-char hex status, for diagnostics |
| `co2_ppm` | int \| None | CO₂ concentration in ppm (type `018` only) |
| `temperature_c` | float \| None | Temperature in °C (type `018` only) |
| `humidity_pct` | float \| None | Relative humidity in % (type `018` only) |
| `nickname` | str \| None | Custom name set in the ELRO app, fetched from the hub at startup via CMD_CODE 24 → 17. `None` when no name has been set. |

The hub does **not** push name changes — a rename in the ELRO app only shows up after the
next `sync_devices()` call.

---

## Simulator — develop without hardware

`tools/k2_simulator.py` sends real XOR-framed UDP packets to `127.0.0.1:1025`, exercising
every production code path in the library without physical hardware. Nothing is mocked — the
gateway receives and decodes the packets exactly as if they came from a real K2 hub.

### Simulated devices

| Sub | Type | Model |
|---|---|---|
| 1 | `013` | Photoelectric Smoke Alarm (GS559A) |
| 2 | `014` | CO + Gas Alarm (GS891A) |
| 3 | `018` | CO2 / Temperature / Humidity detector |
| 4 | `004` | Water / Flood Alarm (GS156A) |
| 5 | `101` | Door/Window Sensor (GS320D) |
| 6 | `215` | Radiator Thermostat (GS361) |

### Event sequence

```
t=0s     CMD_CODE 55  initial sync   (all 6 devices, CLEAR state)
t=1.5s   CMD_CODE 66  CO2/TH detail  (populates co2 / temp / humidity)
then every ~5 s, cycling through:
  CMD_CODE 19  temperature push for sub 3
  CMD_CODE 19  humidity push for sub 3
  CMD_CODE 19  CO2 push for sub 3  (slowly rising 600→1200→600 ppm)
  CMD_CODE 19  smoke ALARM trigger on sub 1, CLEAR 10 s later
  CMD_CODE 19  door OPENS on sub 5, CLOSES 5 s later
  CMD_CODE 19  thermostat valve OPENS on sub 6, CLOSES 5 s later
  CMD_CODE 55  periodic re-sync
```

### Usage

```bash
source .venv/bin/activate

# Terminal 1 — library listener (plays the role of a consumer)
python -m elro_connects_k2_protocol --gateway-ip 127.0.0.1 --device-name DEMO_DEVICE listen &

# Terminal 2 — simulator
python tools/k2_simulator.py
```

To drive the Home Assistant integration instead of the CLI listener, see the Docker section
in the [elro-connects-k2-ha](https://github.com/ldebruijn/elro-connects-k2-ha) README —
the simulator runs on the host and HA connects to `127.0.0.1`.

### Flags

```
--device-name NAME   Device name in packets (default: DEMO_DEVICE). Must match the
                     --device-name given to python -m elro_connects_k2_protocol.
--target HOST:PORT   Where to send packets (default: 127.0.0.1:1025).
--once               Fire one sync + CO2/TH info then exit — useful for scripting or CI.
--pair               Play the hub's side of a pairing round (CMD_CODE 11 ACK → 62 → a sync
                     including the new device) then exit. See below.
--pair-sub-id N      Slot the simulated detector joins on (default: 7).
--pair-type HEX      Raw device type that joins (default: 0013, smoke alarm).
--pair-delay SEC     Seconds to wait before answering (default: 8).
```

### Simulating a pairing round

On loopback the gateway's outbound commands go to `127.0.0.1:1025` — itself — so the
simulator never sees the `CMD_CODE 2`. It therefore answers on a timer instead: start the
pairing request first, then run the simulator inside the join window.

```bash
# Terminal 1 — the client asks to pair, then waits
python -m elro_connects_k2_protocol --gateway-ip 127.0.0.1 --device-name DEMO_DEVICE pair

# Terminal 2 — the hub answers ~8 s later
python tools/k2_simulator.py --pair
```

```
Joined: Sub  7  Photoelectric Smoke Alarm (GS559A variant)   signal=4  battery=100%  status=CLEAR
Re-syncing for real signal/battery values …
  Sub  7  Photoelectric Smoke Alarm (GS559A variant)   signal=3  battery=81%  status=CLEAR
```

The two signal/battery readings differ on purpose: the first is the placeholder implied by
the join frame, the second is what the follow-up sync reports.

---

## Wire probe (no deps, stdlib only)

`tools/k2_udp_probe.py` is a standalone single-file tool for raw protocol exploration.
It requires no venv and no dependencies:

```bash
# Broadcast discovery
python3 tools/k2_udp_probe.py --verbose

# Direct query once you know the gateway IP
python3 tools/k2_udp_probe.py --no-discover --gateway-ip 192.168.1.50 \
    --device-name ST_1234567890 --command sync-status --verbose
```

See [`docs/protocol_reference.md`](docs/protocol_reference.md) for command safety tiers.
Read-only commands (`sync-status`, `gateway-info`, `sub-device-info`) are safe. Commands that
actuate real alarms (`detector-test`, `detector-mute`) should only be used with hardware you
own. Pairing/config/delete commands should never be run casually.

---

## Protocol overview

The K2 uses a simple XOR-framed UDP JSON protocol on port **1025**, not Tuya. The hub is an
Alibaba IoT (LinkKit/ALCS) device, but day-to-day local control bypasses the cloud entirely.

**Framing:** byte 0 is a random seed `r`; every subsequent UTF-8 JSON byte is XORed with `(r ^ 0x23)`. This is obfuscation, not cryptography.

**Key message flows:**

```
App → K2:  {"action":"IOT_KEY?","devID":"NULL"}          # broadcast discovery
K2  → App: {"action":"NODE_ACK","devID":"ST_xxx",...}    # discovery response

App → K2:  {"action":"IOT_KEY?","devID":"ST_xxx"}        # targeted activation / keepalive
K2  → App: {"action":"APP_SEND","msg":{"CMD_CODE":55,…}} # sync-all response
K2  → App: {"action":"APP_SEND","msg":{"CMD_CODE":19,…}} # unsolicited push (alarm/status change)
App → K2:  {"action":"APP_ACK","msg":{"CMD_CODE":11,…}}  # ACK for any NODE_SEND
```

**Status byte layout** (8-char hex `deviceStatus`):

```
[0:2]  signal bars    04=4, 03=3, 02=2, 01/00=1
[2:4]  battery        int(hex, 16) & 0x7F  → 0–100 %
[4:6]  alarm state    AA=CLEAR, 55=ALARM, 50=SILENCED, 11=FAULT
[6:8]  sub-state      opaque; preserved as raw_status for diagnostics
```

Full protocol reference: [`docs/protocol_reference.md`](docs/protocol_reference.md).
Full reverse-engineering narrative: [`docs/research.md`](docs/research.md).
