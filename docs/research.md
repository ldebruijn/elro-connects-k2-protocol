# ELRO Connects K2 — research notes

Background on how this protocol implementation came about, and why the K2 needed a new one
rather than reusing what already existed. For the actual wire format, see
[`protocol_reference.md`](protocol_reference.md).

Java class and package names below refer to the vendor's ELRO Connects 2.0 Android app, which
was decompiled to work the protocol out. That app is proprietary and is not included in this
repository — the names are citations, not code.

## Why the existing integrations don't cover the K2

The hardware is the ELRO Connects **K2** connector (SF50GA) — a Wi-Fi hub bridging to 868 MHz RF
smoke, CO, heat and water detectors. Its predecessor, the K1, is well supported and discontinued;
the K2 is what you can actually buy.

- **`jbouwh/ha-elro-connects`** — mature HA integration, explicitly K1-only. The author has noted
  the K2 uses a different architecture, with little cooperation from the supplier on adding it.
- **`dib0/ha-elro-connects-realtime`** — claims K1 and K2 support, but has an open, unanswered
  issue matching exactly the failure mode you'd expect: the hub connects and authenticates, then
  discovers no devices and cycles "No data received, reconnecting." A second user reported the
  identical symptom independently.
- **`dib0/elro_connects`** / **`jbouwh/lib-elro-connects`** — the K1 protocol libraries (UDP/JSON
  on port 1025, Hekr-based). `lib-elro-connects` states outright that the K2 adapter is not
  supported.
- **openHAB's `elroconnects` binding** — K1-only, same protocol family.

## Finding 1: K1 and K2 are different protocol stacks, not firmware variants

The original ELRO Connects app (`com.cosa.elro`, K1-only) is built on **Hekr's SDK**
(`me.hekr.sdk`, `me.hekr.sthome`) — the legacy UDP/JSON protocol the K1 libraries above already
implement.

The K2 has a **separate app**, "ELRO Connects 2.0" (`com.elro.connects.v2`) — a different
codebase, not a newer version of the first one. Nothing about the K1 protocol carries over.

## Finding 2: the K2 is an Alibaba IoT device, not a Tuya one

The initial hypothesis — based on Siterwell/Tuya's public partnership announcement and a Siterwell
"Tuya" smoke detector listing — was that the K2 is Tuya-based. That turned out to be wrong; there
are no Tuya SDK references anywhere in the K2 app. What's there instead:

- `com.aliyun.alink.*`, `com.aliyun.iot.aep.*`, `com.alibaba.*` — **Alibaba Cloud's iLOP (IoT
  Living Open Platform) / LinkKit SDK**, a Tuya competitor.
- `com.aliyun.alink.linksdk.cmp.connect.alcs.*` — **ALCS (Alibaba Local Communication Service)**,
  Alibaba's local-LAN device control protocol, running over **CoAP** (RFC 7252). The app bundles
  the open-source `libcoap.so` to implement it.
- The gateway/sub-device split (`SubDeviceInfo`, `GatewayConnectConfig`) matches the physical
  architecture: K2 as gateway, detectors as RF sub-devices.
- Credentials follow Alibaba's **"device triad"** model — `productKey` / `deviceName` /
  `deviceSecret` — roughly the equivalent of Tuya's `local_key`.

No pre-existing open-source ALCS reverse-engineering work turned up in searches, so there was no
shortcut available here.

## Finding 3: day-to-day control never touches ALCS/CoAP

This is the finding that makes a purely local integration practical, and it was the surprise.

The K2 app uses Alibaba's stack for discovery, pairing and cloud fallback — but its **operational
command path is a simple custom UDP protocol** implemented in Siterwell/ELRO's own
`com.ilop.sthome` code, layered alongside the SDK rather than on top of it:

- `UdpSocket` binds UDP port **1025** locally and sends to gateway port **1025**.
- Discovery broadcasts `{"action":"IOT_KEY?","devID":"NULL"}` to the local `/24` broadcast address.
- Activation sends the same action directly to a known gateway IP with the real device name.
- Payloads are obfuscated, not encrypted: one random leading byte `r`, then every UTF-8 JSON byte
  XORed with `(r ^ 0x23)`. Trivial to reproduce — see `protocol.py`.
- `SendCommand` tries this UDP path first whenever a gateway IP is known and online, and only
  falls back to the Alibaba cloud (`PanelDevice.invokeService(..., "data_revive")`) if UDP fails.

That last point is the whole story: the device is **local-first with a cloud fallback**, so an
implementation that only ever speaks the local path is not fighting the design — it is using the
preferred one.

## Finding 4: adding a detector is local too, and takes no device type

Finding 3 above says the app "uses Alibaba's stack for discovery, pairing and cloud fallback".
That needs splitting in two, because the app has **two unrelated things called pairing**:

- **Gateway onboarding** — getting the K2 itself onto Wi-Fi and an account. Alibaba's stack,
  cloud-dependent, one-time. Still out of scope.
- **Adding a detector to an already-onboarded K2** — entirely on the local UDP path:
  `CMD_CODE 2` opens a join window, `CMD_CODE 62` reports what joined, `CMD_CODE 7` closes it.
  No cloud, no ALCS.

So the "add device" workflow that looks like it must belong to the app doesn't. It is
implemented here as `K2Gateway.pair_new_device` and the `elro_connects_k2.start_pairing`
service; see the "Pairing" section of `protocol_reference.md` for the sequence.

The non-obvious part: **the app's device-type picker is never sent to the hub.** `increaseEquipment`
always transmits `"00"`. Picking "GS559A smoke alarm" only chooses which physical-trigger
instructions to show on screen, and gives the app something to compare the joined device's
reported type against so it can warn about a mismatch. The hub itself just accepts whatever
joins during the window — which is why the HA service needs no device selector, and why a join
window left open is a real (if mild) side effect rather than a no-op.

## Confirmed against hardware

Verified with a real K2 and three GS559A smoke detectors:

- **Local control needs no cloud after pairing.** Once the K2 is set up via the official app, it
  responds to local UDP on port 1025 with no Alibaba credentials involved at all.
- **ALCS/CoAP is not needed.** The custom UDP path covers status sync, push events and control.
- **No special pairing mode is needed.** Any host on the LAN can discover and command the K2 by
  following the activation sequence: broadcast `IOT_KEY?` → targeted `IOT_KEY?` → `APP_SEND`.
- **The targeted `IOT_KEY?` is mandatory.** After broadcast discovery, the K2 will not answer
  `APP_SEND` until it receives an activation addressed to it directly. This was the single
  correction live testing forced on the static analysis, and the library re-activates on a timer
  for the same reason.

Two details differ from what the send path alone suggests, and cost some time to work out:

- Discovery responses in paired mode use `action:"NODE_ACK"` with `CMD_CODE:0`. The
  `device_id`/`token` response documented in the app's onboarding flow only appears during
  initial AP setup, never in normal operation.
- The K2 answers commands with `action:"NODE_SEND"`. `APP_SEND` is app-to-gateway only — the
  direction matters.

## The activation gate, and why "it works on my hub" proved nothing

The mandatory targeted `IOT_KEY?` above turned out to be understated in a way that caused a real
bug. It is not enough to *send* the activation before the first command — you have to wait for
the `NODE_ACK` it produces. The hub arms when it finishes processing the ping, and that ack is
the only way to know it has.

This surfaced as user reports of "the hub connects but no devices appear", which for a while
looked like a Home Assistant OS networking problem. It wasn't; HAOS was a red herring. A debug
log from an affected user settled it: 57 frames received over 45 minutes, **every one of them a
`NODE_ACK`**, not a single `NODE_SEND`. The hub was answering every ping and ignoring every
command. Timing the same log showed the hub taking a median of 66 ms (max 344 ms) to answer an
activation ping, while the library was sending `CMD_CODE 54` a flat 2 ms later — losing the race
24 times out of 24.

Three things made this hard to see:

- **The failure is silent.** An un-armed hub drops `APP_SEND` with no error and no ack, so the
  symptom is an empty device list, which is exactly what a hub with nothing paired reports.
- **It is a race, so it is environment-dependent.** Hubs answering in 4 ms armed in time; hubs
  answering in 300 ms did not. The same code genuinely worked for some users and never worked
  for others, which kept pointing suspicion at hosting and networking.
- **The app cannot exhibit it.** The app gates on a persisted "gateway online" flag that only a
  received `NODE_ACK` sets, and falls back to the Alibaba cloud when it is unset. The gate is
  therefore split across two files and never looks like a wait, so reading the send path alone
  suggests the ordering is all that matters. `tools/k2_udp_probe.py` had been reproducing the
  gate by accident — it drains a receive loop after activating — and porting the sequence into
  the library dropped that incidental wait.

The lesson worth carrying: for this protocol, a handshake step that "works on my hardware" is
weak evidence. Latency varies enough between hubs that a timing bug can be invisible on one
system and total on another. The `NODE_ACK` is cheap to wait for; wait for it.
