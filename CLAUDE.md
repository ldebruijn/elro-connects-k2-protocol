# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

A standalone, dependency-free async Python library for controlling **ELRO Connects K2**
(SF50GA) Wi-Fi gateways over their local UDP protocol — no vendor cloud. It is consumed by the
`elro-connects-k2-ha` Home Assistant integration, but has no Home Assistant dependency and
must stay that way.

## Essential reading order

Read these before anything else — they are accumulated findings and far denser than
re-deriving from the vendor app:

- **`docs/protocol_reference.md`** — the spec: every `CMD_CODE` (send + receive), UDP framing,
  device-type catalog, status record byte layout, detector test/mute action codes, command
  safety tiers.
- **`docs/research.md`** — the narrative: why K1 ≠ K2, why it's Alibaba ALCS/CoAP (not Tuya),
  and the discovery that day-to-day control uses a simple custom UDP path on port 1025 rather
  than the full CoAP stack.

## Layout

```
elro_connects_k2_protocol/
  gateway.py         K2Gateway — async client, persistent UDP listener on port 1025
  models.py          SubDevice, GatewayInfo, AlarmState, UpdateSource, DeviceCapability, DeviceProfile
  device_profiles.py DEVICE_PROFILES registry — type code → DeviceProfile (capabilities list)
  parser.py          Pure parse functions (testable without a connection)
  protocol.py        XOR framing, encrypt/decrypt, message builders
  __main__.py        CLI: sync / listen / gateway-info / pair
tests/               Fixture-driven; tests/fixtures/*.json hold real captured payloads
tools/k2_simulator.py  Fake hub — sends real UDP packets, nothing mocked
tools/k2_udp_probe.py  Stdlib-only wire probe for raw exploration
```

## Commands

```bash
pytest -v
mypy --strict elro_connects_k2_protocol/
```

Both run in CI (`.github/workflows/ci.yml`). Tests are **fixture-driven**: dropping a new
JSON file with `input` / `expected` keys into `tests/fixtures/` adds it to the parametrized run
automatically — no test code to write. Prefer adding a fixture over adding a test function.

Develop without hardware via `python tools/k2_simulator.py` — it exercises the real parse path.

## Norms

- **Zero third-party runtime dependencies.** Stdlib asyncio only. Dev deps (pytest, mypy) are
  fine; runtime deps are not — HA installs this package and every dep is a support burden.
- `mypy --strict` must pass. The package ships `py.typed`.
- `tools/k2_udp_probe.py` stays a single stdlib-only file, runnable with no venv.
- **Never import Home Assistant** or shape the API around it. Entity/capability decisions
  belong in the integration repo.
- Command safety tiers are in `docs/protocol_reference.md`. Read-only commands (`gateway-info`
   =12, `sync-status`=54, `sub-device-info`=16, alarm syncs 44/47) are safe. **Control**
  (`detector-test`, `detector-mute`, any `CMD_CODE 1`) actuates real alarms — only with
  hardware you intend to trigger. **Never** casually run delete/config/pairing commands
  (4, 10, 39, 46, 49, 53, 61, 77, 254).
- Status/payload bit meanings are only *partially* decoded. Until confirmed against a
  real-device corpus, treat raw status hex as opaque diagnostics rather than inventing
  semantics.
- When you learn something new from hardware, update `docs/protocol_reference.md` so the next
  session inherits it.
