# ELRO Connects K2 — protocol reference

The wire protocol spoken by the K2 (SF50GA) gateway on UDP port 1025. Everything here is either
confirmed against real hardware or marked as unconfirmed. For how this came about, see
[`research.md`](research.md).

Java class names below (`SendCommand`, `ReceiveHandler`, …) refer to the vendor's ELRO Connects
2.0 Android app, which was decompiled to work the protocol out. That app is proprietary and is
not included in this repository — the names are citations, not code.

## Confirmed against hardware

Reference setup: one K2 gateway with **three GS559A photoelectric smoke alarms** paired as
sub-devices 1, 2, 3 (type `013`).

Status records from CMD_CODE 55 (`sync-status`):

| sub_id | type | raw_type | status |
| --- | --- | --- | --- |
| 1 | 013 (GS559A) | 0013 | `0457AA55` |
| 2 | 013 (GS559A) | 0013 | `035FAA34` |
| 3 | 013 (GS559A) | 0013 | `035FAA9E` |

Status nibble A differs on sub 1 (`4`) vs. subs 2 and 3 (`3`). Middle byte also differs (`57` vs. `5F`). The trailing `AA` prefix is constant; the last two hex chars vary (`55`, `34`, `9E`) — possibly RF signal strength or sequence counter. Exact bit meanings not yet decoded; treat as opaque diagnostic until confirmed.

### Protocol corrections from live data

Several things differed from the static-analysis hypothesis:

1. **Discovery response is `NODE_ACK`, not `device_id/token`.** In normal (already-paired) operation, the K2 responds to `IOT_KEY?` with `{"action":"NODE_ACK","devID":"<name>","msg":{"CMD_CODE":0}}`. The `device_id/token` response only appears during initial AP onboarding.

2. **Activation step is required before commands.** A broadcast `IOT_KEY?` alone is not enough. The K2 ignores `APP_SEND` until it has received a *targeted* `IOT_KEY?` sent directly to its unicast IP with the correct `devID`. The probe now handles this automatically. Note that this is not merely an ordering requirement — you must **wait for the resulting `NODE_ACK`** before sending anything. See [The activation gate](#the-activation-gate).

3. **K2 response action is `NODE_SEND`, not `APP_SEND`.** App-to-gateway messages use `APP_SEND`; gateway-to-app messages use `NODE_SEND`. The probe's `classify_message` and auto-ack now handle `NODE_SEND`.

4. **Response fields are `data_str1`/`data_str2`/`data_str3`** (not `rev_str*`). The `ReceiveHandler` reads these directly from the `msg` sub-object.

5. **K2 pushes unsolicited CMD_CODE 19 updates** when first activated. Connecting to the gateway triggers real-time status pushes for recently-changed sub-devices. This means a persistent listener will receive live alarm/status events without polling.

6. **Source port 1025 is required.** Sending `APP_SEND` from an ephemeral port gets no response at all, not even `NODE_ACK`.

### Name sync is a paced stream, not a request/response (CMD_CODE 24 → 17)

`CMD_CODE 24` does not return a single answer. The hub replies with **one `CMD_CODE 17` frame per
*named* sub-device**, emitted at its own pace, terminated by a frame whose `data_str2` is the
literal `NAME_OVER`. Three consequences that are easy to get wrong:

1. **The batch takes seconds, and scales with the device table.** A field report on an eight-device
   hub ([protocol issue #1]) put the gap between frames at roughly 350–400 ms, so that hub needed
   over 3 s to finish. A client that budgets a fixed wall-clock window for the whole batch will
   silently truncate its tail on any hub larger than the one it was tuned against.

2. **The client cannot speed the stream up.** `ReceiveHandler.run` ACKs only `CMD_CODE 11`; name
   frames are not acknowledged, so there is no per-frame handshake driving the hub forward. The
   vendor app applies no timeout at all — it consumes frames until `NAME_OVER`, then fires
   sync-finished event state `2`.

3. **Unnamed sub-devices produce no frame — confirmed against hardware.** The hub only stores
   names that were explicitly set (`CMD_CODE 5`, `modifyEquipmentName`), so the frame count is the
   number of *named* devices, not the number of paired ones. On the reference hub, four paired
   sub-devices (1, 2, 3, 6) yield exactly three name frames: sub_id 6 was never renamed, so the hub
   sends nothing for it and it has no nickname. This rules out the obvious completion test: "wait
   until every known sub_id has a name" is never satisfied on a hub with any unnamed device.

   It also means **a missing nickname is not by itself a client bug**. The vendor app shows a name
   for such a device regardless, because it falls back to its own local row — so "the app shows a
   name but the integration does not" is the expected outcome for a device whose naming step was
   skipped at pairing time, not evidence of a decoding fault.

4. **The hub retransmits name frames.** A capture of a single `CMD_CODE 24` on the reference hub
   delivered sub_id 3's record twice. Collecting into a dict keyed by sub_id makes this harmless,
   but a client that counts frames rather than keying them will over-count.

The safe shape for a client is to end collection on whichever comes first: `NAME_OVER`, or a gap in
the stream longer than the hub's inter-frame pacing — plus an absolute cap. Neither grows with
device count.

[protocol issue #1]: https://github.com/ldebruijn/elro-connects-k2-protocol/issues/1

### Full connection sequence (confirmed)

```
1. Bind local UDP socket to port 1025
2. Broadcast {"action":"IOT_KEY?","devID":"NULL"} → K2 replies NODE_ACK (devID, IP now known)
3. Send {"action":"IOT_KEY?","devID":"<name>"} unicast to gateway IP
4. WAIT for the NODE_ACK. Do not skip this — see "The activation gate" below
5. Send APP_SEND commands → K2 replies NODE_SEND (CMD_CODE + data_str1/data_str2/data_str3)
6. For each NODE_SEND received, reply APP_ACK (CMD_CODE 11) to stop K2 retries
```

The probe (`tools/k2_udp_probe.py`) implements this sequence automatically. Use it as the canonical reference.

### The activation gate

Step 4 is load-bearing and easy to get wrong. The K2 arms its session when it **finishes
processing** the targeted `IOT_KEY?`, not when the packet arrives, and the `NODE_ACK` is the
only observable signal that this has happened. A client that sends `APP_SEND` immediately after
transmitting the ping is racing the hub.

Losing that race is **silent**: an un-armed hub discards `APP_SEND` with no error, no `CMD_CODE
11` ACK, and no response of any kind. `IOT_KEY?` keeps working throughout, because the hub
answers it unconditionally. The observable result is a session that looks healthy — the hub is
discovered, it acks every ping — but in which `CMD_CODE 54` returns nothing, which is
indistinguishable from a hub that genuinely has no sub-devices paired.

Measured from a real hub over a 45-minute session (Home Assistant debug log, 2026-08-20):

| | value |
| --- | --- |
| `IOT_KEY?` → `NODE_ACK` round trip | min 4 ms, **median 66 ms**, max 344 ms |
| Frames received in the session | 57, **all** of them `NODE_ACK`; zero `NODE_SEND` |

A client that sends its first command a couple of milliseconds after the ping will therefore
lose this race on most hubs, and consistently on a slow one. Response latency varies enough
between hubs and networks that the same code can work reliably on one system and never work on
another — so treat "it works on my hub" as no evidence at all that the gate is respected.

**The vendor app enforces the gate structurally rather than sequentially**, which is why it is
easy to miss when reading the app: nothing in it looks like "wait for the ack". Receiving a
`NODE_ACK` is the only thing that marks a gateway online (`UdpControlProxy.onNodeAckDeal`
persists the flag), and the send path checks that stored flag before every command
(`SendCommand.onSendCommand`), falling back to the Alibaba cloud when it is unset. So the app
never sends `APP_SEND` over UDP to an un-armed hub — and because it silently uses the cloud
instead, the app cannot exhibit this failure at all. A local-only client has no such fallback,
so for it the race is fatal rather than invisible.

The app also **retries on silence** rather than trusting a single datagram: `UdpControlProxy`'s
resend loop sends up to three times, spaced one second apart, stopping early once anything is
received.

Two consequences for any client:

- Treat the `NODE_ACK` as a required handshake step, not as optional telemetry. `CMD_CODE 0` is
  the frame to watch for; it carries no device data, which makes it tempting to leave unrouted.
- An empty `CMD_CODE 54` result is only trustworthy if activation was **confirmed**. Unconfirmed
  and empty means "ask again"; confirmed and empty means "this hub really has no devices".

Note that a confirmed activation is not proof the hub is healthy — a hub stalled on a blocked
call home produces the same "acks pings, ignores commands" signature. See [The hub's call home,
and why *how* you block it matters](research.md#the-hubs-call-home-and-why-how-you-block-it-matters).

## Working assumption (confirmed)

Local-first UDP control is viable. No cloud interaction is needed after initial device setup through the official app. The K2 responds to any host on port 1025 that follows the activation sequence — no per-command authentication, no IP whitelist beyond the activation handshake.

The operational path in `SendCommand` is:

1. Check whether the gateway is known as intranet/online.
2. Send an XOR-framed UDP JSON command to gateway port `1025`.
3. Retry up to three times.
4. Fall back to Alibaba `PanelDevice.invokeService(...)` with identifier `data_revive` only if UDP fails.

The receive path is shared: UDP and Alibaba downstream messages both end up in `ReceiveHandler`. `SiterJobService` explicitly suppresses duplicate TCP/cloud messages if the same payload already arrived over UDP.

## Running the probe

```bash
# Discovery + activation + gateway info
python3 tools/k2_udp_probe.py --command gateway-info --verbose

# Discovery + activation + full device sync (lists all sub-devices)
python3 tools/k2_udp_probe.py --command sync-status --verbose

# Skip discovery when gateway IP and device name are already known
python3 tools/k2_udp_probe.py --no-discover --gateway-ip 192.168.1.50 --device-name ST_1234567890 --command gateway-info --verbose
python3 tools/k2_udp_probe.py --no-discover --gateway-ip 192.168.1.50 --device-name ST_1234567890 --command sync-status --verbose
python3 tools/k2_udp_probe.py --no-discover --gateway-ip 192.168.1.50 --device-name ST_1234567890 --command sub-device-info --sub-id 1 --verbose
```

Note: `--no-discover` still sends the activation `IOT_KEY?` when `--gateway-ip` and `--device-name` are provided — this is required.

## UDP framing

Known from `ByteUtil` and `UdpSocket`:

- UDP port: `1025` local and remote.
- Payload is UTF-8 JSON with simple XOR framing.
- Byte 0 is random seed `r`.
- Every JSON byte is XORed with `(r ^ 0x23)`.
- The app trims received plaintext to the first JSON object and parses it.

Discovery query:

```json
{"action":"IOT_KEY?","devID":"NULL"}
```

Direct activation/search query to known gateway:

```json
{"action":"IOT_KEY?","devID":"<deviceName>"}
```

Operational command shape:

```json
{
  "action": "APP_SEND",
  "devID": "<deviceName>",
  "msg": {
    "msg_ID": 1,
    "CMD_CODE": 12,
    "rev_str1": "00",
    "rev_str2": "00",
    "rev_str3": ""
  }
}
```

ACK shape:

```json
{
  "action": "APP_ACK",
  "devID": "<deviceName>",
  "msg": {
    "msg_ID": 2,
    "CMD_CODE": 11,
    "rev_str1": "11",
    "rev_str2": "OK",
    "rev_str3": ""
  }
}
```

The app sends an ACK for inbound device messages before dispatching them to observers.

## Commands found in `SendCommand`

Fields are always sent as `rev_str1`, `rev_str2`, `rev_str3`.

| Code | Name | App method | `rev_str1` | `rev_str2` | `rev_str3` | HA relevance |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `EQUIPMENT_CONTROL` | `onDeviceControl`, `gatewayOperate`, `gatewaySilence` | sub-device id as 2-byte hex, or `0000` for gateway | action/status code | empty | Control. Must test carefully. |
| 2 | `INCREASE_EQUIPMENT` | `increaseEquipment` | always `"00"` | empty | empty | Opens a join window. Implemented — see "Pairing". |
| 3 | `REPLACE_EQUIPMENT` | `replaceEquipment` | sub-device id | replacement code | empty | Pairing/maintenance. |
| 4 | `DELETE_EQUIPMENT` | `deleteEquipment` | sub-device id | empty | empty | Destructive; avoid initially. |
| 5 | `MODIFY_EQUIPMENT_NAME` | `modifyEquipmentName` | sub-device id | name/code | empty | Config; avoid initially. |
| 6 | `CHOOSE_SCENE` | `choseScene` | scene id as 1-byte hex | empty | empty | Scene activation; possible HA button later. |
| 7 | `CANCEL_INCREASE_EQUIPMENT` | `cancelIncreaseDevice` | empty | empty | empty | Closes a join window. Implemented — see "Pairing". |
| 8 | `INCREASE_AUTOMATION` | `increaseAutomation` | empty | automation code | empty | Automation management; not first HA target. |
| 9 | `MODIFY_AUTOMATION` | `updateAutomation` | empty | automation code | empty | Automation management. |
| 10 | `DELETE_AUTOMATION` | `deleteAutomation` | automation id | empty | empty | Destructive; avoid. |
| 11 | `SEND_ACK` | `getAnswerOk` | acknowledged code | `OK` | empty | Required receiver hygiene. |
| 12 | `SET_GATEWAY_INFO` | `queryGatewayInfo`, `queryOrSetGatewayInfo` | `00`/setting selector | `00`/setting value | empty | Query plus config. `00`,`00` is read-style. |
| 16 | `GET_SUB_DEVICE_INFO` | `getSubDeviceInfo` | sub-device id | empty | empty | Useful read query. |
| 23 | `INCREASE_SCENE` | `increaseScene` | scene code | empty | empty | Scene management. |
| 24 | `SYN_DEVICE_NAME` | `synGetDeviceName` | device-name CRC block | empty | empty | Useful after status sync if names matter. |
| 29 | `SYN_DEVICE_STATUS` | `synGetDeviceStatus` | status CRC block | empty | empty | Deprecated in app. |
| 30 | `SYN_AUTOMATION` | `synAutomation` | automation CRC/page | automation data/page | optional page 2 | Automation sync. |
| 32 | `SYN_SCENE` | `synScene` | empty | scene sync data | empty | Scene sync. |
| 38 | `SCENE_HANDLE` | `autoClick` | automation id | empty | empty | Manual automation trigger. |
| 39 | `DELETE_SCENE` | `deleteScene` | scene id | empty | empty | Destructive; avoid. |
| 44 | `ALARM_LIST_SYNC` | `syncAlarms` | page as 1-byte hex | empty | empty | Useful history query. |
| 46 | `DELETE_GATEWAY_LIST` | `deleteGatewayAlarms` | page or `FF` | empty | empty | Destructive; avoid. |
| 47 | `SUB_DEVICE_ALARM_LIST_SYNC` | `syncSubAlarms` | page as 1-byte hex | sub-device id | empty | Useful history query. |
| 49 | `DELETE_SUB_DEVICE_ALARM_LIST` | `deleteSubAlarms` | page or `FF` | sub-device id | empty | Destructive; avoid. |
| 53 | `MODIFY_SUB_ROOM` | `modifySubDeviceRoom` | sub-device id | room id | empty | Config; avoid initially. |
| 54 | `SYN_ALL_DEVICE_STATUS` | `synAllDeviceStatus` | device CRC block | timezone code | empty | Primary status sync command. |
| 57 | `MODIFY_AUTOMATION_NAME` | `modifyAutomationName` | automation id | name/code | empty | Automation management. |
| 58 | `SYN_AUTOMATION_NAME` | `synAutomationName` | automation-name CRC/page | automation-name data/page | optional page 2 | Automation sync. |
| 61 | `SET_GATEWAY_VOICE` | `queryOrSetGatewayVoice` | value | empty | empty | Gateway setting. |
| 65 | `DELETE_CO2_TH_2_4_CHART` | `deleteCo2ThChartHistory` | sub-device id + `FFFF` | sub-device id | empty | Destructive; avoid. |
| 67 | `GET_All_CO2_TH_2_4` | `synAllCo2ThHistory` | sub-device id | empty | empty | CO2/temp/humidity history query. |
| 70 | `SET_SIM_CODE_PHONE` | constant only in this class | unknown | unknown | unknown | Not traced to method. |
| 71 | `SET_SIM_CODE_MESSAGE` | constant only in this class | unknown | unknown | unknown | Not traced to method. |
| 75 | `GET_ALL_REPEATER_SUB_DEVICE` | `getAllRepeaterSubDevice` | repeater sub-device id | empty | empty | Repeater inventory query. |
| 77 | `SET_GATEWAY_WIFI` | `setGatewayWifiInfo` | SSID/code | password/code | empty | Config; avoid. |
| 254 | Gateway restart | `gatewayRestart` | `00` | `00` | empty | Control; do not test casually. |

Low-risk commands for initial validation:

- `12` with `00`,`00`: gateway info.
- `54` with empty-cache CRC `00020000` and timezone code: all device status sync.
- `16`: sub-device info.
- `44` and `47`: alarm history reads.
- `67` and `75`: later, if relevant devices exist.

Commands to defer until the decode model is solid:

- `1`: control/silence/operate.
- `4`, `10`, `39`, `46`, `49`, `65`: delete operations.
- `53`, `61`, `77`: configuration changes.
- `3` (replace) and automation mutation commands. `2` / `7` (add, cancel-add) are
  implemented — see "Pairing" below.

## Detector test and mute commands

Important distinction:

- Sub-device id is the gateway slot/id, for example `1`, `2`, `3`; this becomes `rev_str1` as `0001`, `0002`, `0003`.
- Device type is the product/protocol type, for example `001`, `005`, `01A`; this determines which test action code to use.

The app has an explicit detector test button in `DetectorActivity`. It calls `SettingRequest.onSendDeviceControl`, which calls `SendCommand.onDeviceControl`, so the wire command is:

```text
CMD_CODE = 1
rev_str1 = sub-device id as 2-byte hex
rev_str2 = detector action code
rev_str3 = empty
```

For normal detector classes, including the common smoke/CO/gas/heat/water alarm path, the app test action is:

```text
BB000000
```

Special detector test action codes found in `DetectorActivity.getTestOperation()`:

| Device enum | Product/model from app | Type codes | Test action |
| --- | --- | --- | --- |
| default detector path | GS530D smoke, GS816A CO, GS412 heat, GS156 water, GS870/GS871 gas, etc. | `001`, `009`, `00F`, `000`, `008`, `00E`, `030`, `002`, `006`, `00A`, `010`, `015`, `017`, `014`, `003`, `00B`, `011`, `004`, `00C`, `012`, `025` | `BB000000` |
| `EE_TEMP_OUTDOOR_SIREN` | GS380 outdoor siren | `20E` | `51000000` |
| `EE_DEV_SX_SM_ALARM` | GS559A photoelectric smoke alarm | `005`, `00D`, `013` | `17000000` |
| `EE_DEV_CO_ALARM_GS818` | GS818A CO alarm | `019` | `02FF0000` |
| `EE_DEV_SX_ALARM_GS592` | GS592A photoelectric smoke alarm | `01A` | `02FFFFFF` |

How to pick the right action for your detector:

1. Pair the detector with the K2.
2. Run `sync-status` and/or `sub-device-info`.
3. Read the printed `sub_id` and `type` from the probe output. The probe now prints hints like `sub_id=1 type=001 smoke alarm ... test_action=BB000000`.
4. Use that `sub_id` and the suggested action code for `detector-test`.

If the sync response gives a 4-character raw type like `0001`, normalize it the way the app does: drop the first character, so `0001` maps to type `001`.

Detector mute/silence from the detector detail screen sends:

```text
CMD_CODE = 1
rev_str1 = sub-device id as 2-byte hex
rev_str2 = 50000000
rev_str3 = empty
```

Gateway silence, from the alarm dialog, is different:

```text
CMD_CODE = 1
rev_str1 = 0000
rev_str2 = 00000000
rev_str3 = empty
```

Probe commands added for deliberate live testing:

```bash
# Generic detector test command used by most detector classes.
python3 tools/k2_udp_probe.py --no-discover --gateway-ip <ip> --device-name <device_id> --command detector-test --sub-id 1 --verbose

# Override action code for special detector variants.
python3 tools/k2_udp_probe.py --no-discover --gateway-ip <ip> --device-name <device_id> --command detector-test --sub-id 1 --action-code 17000000 --verbose

# Detector mute/silence command.
python3 tools/k2_udp_probe.py --no-discover --gateway-ip <ip> --device-name <device_id> --command detector-mute --sub-id 1 --verbose
```

For using smoke detectors as HA sirens, this gives a plausible `turn_on` command: `CMD_CODE 1`, sub-device id, action `BB000000` for common smoke detectors. The required `turn_off` behavior is less certain: detector mute is likely `50000000`, while gateway-wide silence is `0000`/`00000000`. Both should be tested with real hardware before exposing this as a HA siren entity.

## Pairing (adding a sub-device)

Traced from `page/config/BootSubDeviceActivity.java`. The flow is far simpler than the
app's UI implies, and it needs no cloud involvement.

**The device-type picker in the app is cosmetic.** `onStartNetworking` always sends
`increaseEquipment("00")` — the selected type is never transmitted. It only chooses which
physical-trigger instructions to display (`showIncreaseOrReplaceView`), and is compared
against whatever type actually joined so the app can warn about a mismatch
(`receiveNewDeviceInfo` → `onNewlyDifferentTypesOfDevice`). So there is no "add a GS559A"
command; there is only "accept the next device that joins".

Sequence:

1. **App → GW** `CMD_CODE 2`, `rev_str1 = "00"` — open a join window.
2. **GW → App** `CMD_CODE 11` ACK. The app only starts its countdown when
   `getAnswerResult` returns 2 or 3, i.e. `data_str2 == "OK"`. No ACK means the hub is
   not listening and triggering the detector achieves nothing.
3. **60-second window** — `DistributeNetRequest.onStartCountDown(false)` → 60 s. (The
   120 s branch is gateway onboarding, not this flow.) The user physically triggers the
   detector's pairing action; what that is varies by type, which is all the type picker
   was ever for.
4. **GW → App** `CMD_CODE 62` — see the receiver table for the `data_str1` layout. Carries
   no status, so the app substitutes the placeholder `0464AA00` (4 bars, 100 %, clear).
   Real values only arrive on the next `CMD_CODE 55` sync or status push.
5. **Cancel** `CMD_CODE 7`, all fields empty. The app sends this when the user backs out,
   but deliberately **not** on timeout (`onCancelNetworking` is guarded by `mTimeOut`),
   so an expired window is expected to close itself.
6. **Wrong device joined** — the app sends `CMD_CODE 4` (delete) for that slot and
   re-issues `CMD_CODE 2`. Not implemented here; `4` is destructive and untested.

Implemented in `K2Gateway.start_pairing` / `cancel_pairing` / `wait_for_new_device` /
`pair_new_device`, exposed as the HA services `elro_connects_k2.start_pairing` and
`.cancel_pairing`, and driveable from the CLI:

```bash
python3 -m elro_connects_k2_protocol pair --gateway-ip <ip> --device-name <device_id> --verbose
```

`CMD_CODE 2` is the mildest of the mutation commands — the worst case of a stray one is a
join window that sits open for 60 s and then expires. It is still a mutation: leaving one
open means the next detector to be triggered anywhere in range gets adopted.

## Receivers found in `ReceiveHandler`

The received payload processed by `ReceiveHandler` uses:

- `CMD_CODE`
- `data_str1`
- `data_str2`
- optional `alarmMessage`

UDP messages seen by `UdpControlProxy` contain `msg`; the probe should log the raw shape exactly. The app's downstream/cloud path already provides `data_str*`. It is not yet proven whether the gateway's UDP `msg` keys are `data_str*` or a mixed shape, so raw probe logs matter.

| Code | Receiver method | Main fields | App behavior | HA mapping |
| ---: | --- | --- | --- | --- |
| 11 | `sendAck` | `data_str1`, `data_str2` | Emits `UPLOAD_ANSWER`. `CoderUtils.getAnswerResult` reads `data_str1[0:4]` as hex to get the command being acknowledged (it checks for a 9-char `data_str1`), and treats `data_str2 == "OK"` as success. Despite the method name nothing is sent. | Confirm a command was accepted — required before a pairing round starts. |
| 13 | `uploadGatewayInfo` | `data_str1`, `data_str2` | Emits `UPLOAD_GATEWAY`. | Gateway diagnostics and settings. |
| 17 | `uploadDeviceName` | `data_str2` | Updates sub-device names until `NAME_OVER`, then sync-finished event state `2`. | Device names. |
| 19 | `uploadDeviceStatus` | `data_str1`, `data_str2` | Parses one device status update. | Realtime state/alarm updates. |
| 26 | `uploadSceneInfo` | `data_str1`, `data_str2` | Syncs scene info, sync-finished event state `3`. | Optional scenes. |
| 27 | `uploadAutoInfo` | `data_str1`, `data_str2` | Syncs automation info, `FFFF` marks final block, sync-finished state `4`. | Optional diagnostics only. |
| 28 | `uploadCurrentScene` | `data_str1` | Updates selected scene. | Current scene sensor/diagnostic. |
| 45 | `uploadAlarmLogsInfo` | `data_str1`, `data_str2` | `data_str1` is page, `data_str2` is history data; `OVER` marks end. | Alarm history/event log. |
| 48 | `uploadSubDeviceAlarmLogsInfo` | `data_str1`, `data_str2` | `data_str1[0:2]` page, `data_str1[2:6]` sub-device id; `data_str2` history data. | Per-device history/event log. |
| 55 | `uploadAllDeviceStatus` | `data_str1`, `data_str2` | Each string is split into 14-hex-char device records. | Primary inventory/status sync. |
| 56 | `uploadAllDeviceStatus` | `data_str1`, `data_str2` | Same as `55`. | Primary inventory/status sync. |
| 60 | `uploadAutomationName` | `data_str1`, `data_str2` | Syncs automation names, `FFFF` marks final block, sync-finished state `5`. | Optional diagnostics only. |
| 62 | `uploadAddSubDevice` | `data_str1` | `[0:4]` sub-device id, `[4:8]` device type, `[8:]` room id. Payloads of 8 chars or shorter are discarded. No status bytes — the app substitutes the placeholder `0464AA00`. | Dynamic discovery after pairing. Implemented — see "Pairing". |
| 64 | `uploadCOThHistory` | `data_str1`, `data_str2` | `data_str1[0:4]` sub-device id, `data_str1[4:6]` type; `data_str2` is 12-hex-char history records. | CO2/temp/humidity history. |
| 66 | `uploadSubDeviceInfo` | `data_str1`, `data_str2` | `data_str1[0:4]` sub-device id; `data_str2` status/info. Special 30-char CO2/temp/humidity payload. | Per-device attributes and sensors. |
| 69 | `uploadGSMInfo` | `data_str1`, `data_str2` | `NULL` means no SIM; otherwise first byte is signal, `99` means SIM fault; `data_str2` module model. | GSM gateway diagnostics if present. |
| 76 | `uploadRepeaterSubDevice` | `data_str2` | Emits repeater sub-device data. | Repeater inventory/status. |

`alarmMessage` is handled before the `CMD_CODE` switch. The app decodes it as a push/event frame:

- Byte/hex slice `alarmMessage[4:6] == AC`: scene event.
- `alarmMessage[4:6] == BC`: reminder event.
- Otherwise:
  - `alarmMessage[6:10]` is parsed as sub-device id. `0` means gateway event.
  - `alarmMessage[10:14]` is device type for sub-device events.
  - `alarmMessage[14:22]` is status for normal devices.
  - Device type suffix `401` is repeater; then status uses `alarmMessage[14:24]`.
  - Status with bytes `status[2:4] == 00` is ignored.

Gateway alert status strings from `DeviceStatusUtil.getGatewayAlert`:

| Status | Meaning from app string key |
| --- | --- |
| `00000000` | mains power off |
| `00000001` | mains power normal |
| `00000002` | battery normal |
| `00000003` | battery low |
| `00000004` | gateway restart |
| `00000005` | network disconnected |
| `00000006` | network OK |
| `00000007` | gateway alarm |
| `00000008` | gateway initialized |
| `00000009` | gateway config |
| `0000000A` | gateway stop alarm/silence |
| `0000000C` | firmware upgrade started |
| `0000000D` | OTA upgrade success |

## Device status sync record format

Known from `ReceiveHandler.updateDeviceStatus` for `CMD_CODE 55/56`:

Each device record is 14 hex characters:

```text
0:2    sub-device id, 1 byte
2:6    device type, 2 bytes
6:7    status nibble A, becomes "0" + char
7:8    room/status nibble B, becomes "0" + char and is stored as room id
8:10   middle status byte
10:14  trailing status word
```

The app builds `deviceStatus` as:

```text
"0" + record[6] + record[8:10] + record[10:14]
```

Except for device type containing `301` where trailing status starting with `01` is normalized to `AAFF`.

If trailing status is `0000`, or device type is `FFFF`, the app deletes the sub-device unless the device type is `0025`.

**Signal strength (confirmed against live app UI):** `deviceStatus[0:2]` is the RF signal bar level. The first char is always `"0"` (constant prefix added by the app); the second char is the raw nibble from the sync record. `LocalResUtil.getSignal` maps:

| value | bars |
| --- | --- |
| `00` or `01` | 1 bar |
| `02` | 2 bars |
| `03` | 3 bars |
| `04` | 4 bars |
| other | 0 / no-signal icon |

Live example: sub 1 = `"04"` (4 bars), subs 2 and 3 = `"03"` (3 bars). Confirmed in app UI.

**Alarm state (confirmed against live app UI):** `deviceStatus[4:6]` is the alarm verdict:

| value | meaning |
| --- | --- |
| `AA` | no anomaly detected ("normal") |
| `55` | **device alarm** — smoke/CO/heat/water triggered |
| `50` | silenced |
| `BB` | alert (matches BB test action code) |
| `11` | fault / tamper |
| other | offline / unknown |

This matches `DevDetailStatusUtil.getAlarmDeviceStatusMsg`. When `status[4:6] == "AA"`, the app additionally calls `getDefaultStatusMsg` which then checks the battery field — so "Low battery" overrides "normal" in the display but the alarm field stays `AA`.

**Battery percentage (confirmed against live app UI):** `ByteUtil.getQuantityStatus(deviceStatus[2:4])` gives the battery percentage shown in the app. `getQuantityStatus` converts the hex byte to decimal, stripping the MSB first if it is set (i.e. if byte ≥ 0x80, return `byte & 0x7F`). Low-battery threshold is ≤ 15 (%).

Live example (GS559A detectors, all fields confirmed in app UI):

| sub_id | deviceStatus | signal `[0:2]` | battery `[2:4]` | alarm `[4:6]` | unknown `[6:8]` |
| --- | --- | --- | --- | --- | --- |
| 1 | `0457AA55` | `04` → 4 bars | `0x57` → 87% | `AA` → clear | `55` |
| 2 | `035FAA34` | `03` → 3 bars | `0x5F` → 95% | `AA` → clear | `34` |
| 3 | `035FAA9E` | `03` → 3 bars | `0x5F` → 95% | `AA` → clear | `9E` |

Known from `ReceiveHandler.uploadDeviceStatus` for `CMD_CODE 19`:

- `data_str1[0:4]`: sub-device id.
- `data_str1[4:8]`: device type.
- `data_str1[8:]`: room id or extra status, depending on case.
- `data_str2 == "NULL"`: refresh with status `"NULL"` and room `"00"`.
- `len(data_str2) == 6`: CO2/temp/humidity style event.
- `len(data_str2) == 8`: normal status update.
- `len(data_str2) == 10`: alternate status update shape.

Known from `ReceiveHandler.uploadSubDeviceInfo` for `CMD_CODE 66`:

- `data_str1[0:4]`: sub-device id.
- If `len(data_str2) == 30`, this is special CO2/temp/humidity info:
  - status becomes `data_str2[0:6] + "FF"`.
  - if `data_str2[6:8] == "00"`, `data_str2[8:12]` is converted to a decimal value by `(hex - 300) / 10.0`.
  - if `data_str2[12:14] == "04"`, `data_str2[14:18]` is converted by `hex / 10.0`.
  - if `data_str2[18:20] == "08"`, `data_str2[20:24]` is parsed as integer.
- Otherwise `data_str2` is stored directly as device status.

### GS361 radiator thermostat (type `215` / `1C`) status bytes

The thermostat repurposes **both** of the last two status bytes. Source:
`ThermostatVModel.onAnalysisStatus`, constants from
`com.alibaba.ailabs.iot.aisbase.Constants.CMD_TYPE` (`CMD_DEV_LOG_NOTIFY` = 64 =
`0x40`, `CMD_GET_FIRMWARE_VERSION` = 32 = `0x20`).

`deviceStatus[4:6]` — replaces the standard alarm byte:

| Bits | Meaning |
|---|---|
| `0x80` | open-window detected (the TRV infers this from a temperature drop — it is **not** a contact sensor) |
| `0x40` | valve currently open (actively heating) |
| `0x20` | setpoint has a +0.5 °C increment |
| `0x1F` | setpoint floor in whole °C (0–31) |

`deviceStatus[6:8]` — the sub-state byte, unused by standard devices:

| Bits | Meaning |
|---|---|
| `0x03` | operating mode: 0 = anti-frost, 1 = timer, 2 = manual, 3 = manual + test |
| `0xFC` | measured room temperature in whole °C (0–63) |

**Confidence.** The mode bits are confirmed in both directions — read as
`b3 & 3`, written back as `modelState.get() & 3`. The temperature bits are
read-path only: the app renders `((b3 >> 2) & 63) + "°C"` under a
`current_temperature` label on the thermostat screen.

Do **not** try to cross-check this against `getSettingStateCode`. That method
builds a *control* payload and reads from `[0:2]`, a different offset than the
status decoder's `[4:8]`, so its bit usage describes the command encoding, not
the status encoding. It also re-encodes valve and window bits that are not
user-settable, which marks it as blind byte-preservation rather than a reliable
description of the layout. Reading it as a status-layout reference suggests a
conflicting decoding (`(b3 >> 3) & 31` plus a half-degree flag at `0x04`) that
is most likely wrong. Still worth confirming against a real GS361 by comparing
the app's displayed temperature with the decoded value.

### Consequence: measurement state must be accumulated, not rebuilt

CO2/temp/humidity values are only ever delivered **one field per frame** — a
6-char `CMD_CODE 19` push carries a single 2-char tag + 4-char value, and the
30-char `CMD_CODE 66` response is the only frame carrying all three at once.
Critically, the 14-char `CMD_CODE 55` sync record carries **no measurements at
all** (only signal, battery, and alarm state), and neither does the 8-char
status push.

So a client must treat these fields as sticky and carry them forward whenever a
sync record or status push rebuilds a device. Rebuilding from those frames alone
resets the measurements to null — which surfaces in Home Assistant as sensors
going "unknown" and gaps in the history graph every time the hub re-syncs. Same
applies to nicknames (`CMD_CODE 24` → `17`) and the thermostat detail fields.

Note that an *explicit* `sync_devices()` is self-healing, because it follows the
sync with per-device `CMD_CODE 16` → `66` queries and a name re-fetch. Only the
hub's **unsolicited** `CMD_CODE 55` broadcasts hit the lossy path.

## Device type catalog

Hardcoded gateway product key:

```text
a2AdG2E0EHL
```

Device type codes from `CellsEnum` / `SmartDevice`:

| Category | Type codes |
| --- | --- |
| Gateway | `GATEWAY` |
| Repeater | `401` |
| Smoke alarm | `001`, `009`, `00F` |
| Smoke alarm variants | `005`, `00D`, `013`, `01A`, `025` |
| CO alarm | `000`, `008`, `00E`, `019`, `030` |
| Gas alarm | `002`, `006`, `00A`, `010`, `015`, `017` |
| CO + gas alarm | `014` |
| Heat alarm | `003`, `00B`, `011` |
| Water alarm | `004`, `00C`, `012` |
| CO2/temp/humidity detector | `018` |
| PIR | `01`, `02`, `03`, `109` |
| Door/contact | `08`, `09`, `0A`, `101`, `10A` |
| Temperature/humidity checker | `102` |
| Socket | `18`, `19`, `214`, `218` |
| Lighting module | `1A`, `1B`, `216` |
| Temperature controller | `1C`, `215` |
| Outdoor siren | `20E` |
| SOS | `211` |
| Button | `0F`, `11`, `301` |
| Scene switch | `0C`, `305` |
| Door lock | `12`, `13`, `213` |
| Flash/vibration alarm | `28`, `212` |
| Manipulator | `2B`, `2C`, `208` |
| Solenoid valve | `29`, `2A`, `217` |
| Test/reminder common function | `2D` |

`SmartDevice.getType` has a quirk: if the device type string length is 4, it compares `str.substring(1)` against three-character type codes. That means a record type like `0001` maps to `001`.


## Command safety tiers

Introduce control in this order, and never skip ahead on hardware you are not prepared to set off:

1. **Read-only.** `12` (gateway info), `16` (sub-device info), `44` / `47` (alarm history),
   `54` (status sync), `67` (CO2/TH history), `75` (repeater inventory). Safe.
2. **Gateway silence.** `CMD_CODE 1`, `rev_str1 = "0000"`, `rev_str2 = "00000000"`.
3. **Gateway operate on/off.** `CMD_CODE 1`, `rev_str1 = "0000"`,
   `rev_str2 = "33000000"` (on) or `"00000000"` (off).
4. **Controllable non-safety devices.** Sockets, lighting modules, solenoid valves, manipulators.
5. **Never casually.** Delete, config and pairing commands — `4`, `10`, `39`, `46`, `49`, `53`,
   `61`, `65`, `77`, `254`. There is no restore path if you get one of these wrong.

Detector test and mute (`CMD_CODE 1` with a detector action code) actuate a real alarm on real
hardware. Only use them on a device you own and intend to trigger.

For smoke and CO alarms, assume sub-devices are read-only until proven otherwise.

## Known unknowns

Deliberately not guessed at — treat as opaque rather than inventing semantics:

- **`deviceStatus[6:8]`** varies per device (`55`, `34`, `9E` observed) and is not surfaced
  anywhere in the vendor app UI. Preserved as `raw_status` for diagnostics; decode it only if a
  pattern emerges across a wider device corpus.
- **Does `CMD_CODE 54` with CRC `00020000` always return the full device list?** It matches the
  app's empty-cache behaviour and holds for three devices, but has not been confirmed with a
  larger installation.
- **How long the K2 accepts `APP_SEND` after activation** is not precisely known. The library
  re-activates on a 60 s timer, which is comfortably inside the working window, but the actual
  expiry has not been measured.
- **How long the hub stalls when its call home to `aliyun.com` is silently dropped** — its own
  connect timeout — has not been measured, nor has which parts of the local path stall with it.
  The library's activation timeout is a guess at that number, not a fit to it. See
  [research.md](research.md#the-hubs-call-home-and-why-how-you-block-it-matters).
