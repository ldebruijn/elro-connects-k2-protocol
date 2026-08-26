#!/usr/bin/env bash
#
# Collect a nickname-sync report for a bug report.
#
# Asks the hub for its sub-device list and its stored nicknames, decodes every
# CMD_CODE 17 name record, and prints a summary you can paste into a GitHub
# issue.  The point is to distinguish the two causes of a missing nickname that
# look identical from the outside:
#
#   * the hub never stored a name for that sub-device -- normal, and not a bug;
#     the vendor app still shows a name for it from its own local database
#   * the hub sent a name record the library could not decode -- a real bug
#
# The gateway IP and devID are replaced with placeholders in the output.  Pass
# --raw to keep them.
#
# Usage:
#   tools/name_sync_report.sh [--gateway-ip IP] [--device-name ST_...] [--raw]
#
# Needs only python3 -- tools/k2_udp_probe.py is stdlib-only.

set -euo pipefail

PROBE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/k2_udp_probe.py"
PYTHON="${PYTHON:-python3}"
GATEWAY_IP=""
DEVICE_NAME=""
REDACT=1
CHECK_NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        --gateway-ip)  GATEWAY_IP="$2"; shift 2 ;;
        --device-name) DEVICE_NAME="$2"; shift 2 ;;
        --raw)         REDACT=0; shift ;;
        --check-name)  CHECK_NAME="$2"; shift 2 ;;
        -h|--help)     sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Checking a name needs no hub, so handle it before anything touches the network.
if [ -n "$CHECK_NAME" ]; then
    "$PYTHON" - "$CHECK_NAME" <<'NAMECHECK'
import sys
name = sys.argv[1]
print(f"Name: {name!r}")
try:
    encoded = name.encode("gbk")
except UnicodeEncodeError as exc:
    print(f"  FAIL  not encodable in GBK: {exc}")
    raise SystemExit(1)
print(f"  {len(name)} characters, {len(encoded)} GBK bytes")
problems = []
if len(encoded) > 15:
    problems.append(
        f"{len(encoded)} GBK bytes exceeds the 15-byte field. The vendor encoder emits an "
        f"oversized record ({4 + 2 * (len(encoded) + 1)} chars instead of 36) that no decoder accepts."
    )
for ch in "@$":
    if ch in name:
        problems.append(f"contains {ch!r}, a padding/terminator sentinel in the encoding")
if problems:
    for problem in problems:
        print(f"  FAIL  {problem}")
    print("  This name cannot survive the round trip. Rename the device to something")
    print("  shorter and free of @ and $, then re-run the report.")
    raise SystemExit(1)
print("  OK    this name encodes to a well-formed 36-char record")
NAMECHECK
    exit $?
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The hub only answers commands sent from source port 1025, so a running Home
# Assistant integration will be holding it.  Say so plainly rather than failing
# with a bare EADDRINUSE.
if ! "$PYTHON" - <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("", 1025))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
then
    cat >&2 <<'EOF'
UDP port 1025 is already in use, and the hub ignores commands from any other
source port.  Something else is talking to it -- usually Home Assistant.

Stop it for the duration of this report, e.g.:
    docker stop <your-home-assistant-container>
or stop the ELRO Connects integration, then re-run this script.
EOF
    exit 1
fi

echo "== ELRO Connects K2 nickname sync report =="
echo

if [ -z "$GATEWAY_IP" ] || [ -z "$DEVICE_NAME" ]; then
    echo "Discovering hub ..."
    "$PYTHON" "$PROBE" --timeout 8 > "$WORK/discover.txt" 2>&1 || true
    # The probe prints: "Activating gateway <ip> devID=<name>"
    line="$(grep -m1 '^Activating gateway ' "$WORK/discover.txt" || true)"
    if [ -z "$line" ]; then
        echo "No hub found on the LAN." >&2
        echo "Re-run with --gateway-ip and --device-name if you know them." >&2
        exit 1
    fi
    GATEWAY_IP="${GATEWAY_IP:-$(echo "$line" | awk '{print $3}')}"
    DEVICE_NAME="${DEVICE_NAME:-$(echo "$line" | sed 's/.*devID=//')}"
fi

echo "Hub found. Querying sub-devices and nicknames ..."
echo

# --retries 1 keeps the capture to a single exchange; with the default the two
# batches run together and the NAME_OVER boundary is easy to misread.
#
# The sleeps are not padding.  Each probe run re-activates the session, and
# firing three of them back to back leaves the hub answering the activation but
# not the command -- which reads as "this hub has no names at all".
sleep 2
"$PYTHON" "$PROBE" --command sync-status --gateway-ip "$GATEWAY_IP" \
    --device-name "$DEVICE_NAME" --timeout 15 --retries 1 > "$WORK/status.txt" 2>&1 || true
sleep 2
"$PYTHON" "$PROBE" --command sync-names --gateway-ip "$GATEWAY_IP" \
    --device-name "$DEVICE_NAME" --timeout 15 --retries 1 > "$WORK/names.txt" 2>&1 || true

"$PYTHON" - "$WORK/status.txt" "$WORK/names.txt" <<'PY'
"""Decode the captured frames and print the verdict."""
import re, sys

status_txt = open(sys.argv[1], encoding="utf-8", errors="replace").read()
names_txt = open(sys.argv[2], encoding="utf-8", errors="replace").read()

# The probe annotates sync-status frames with "device hint: sub_id=N ..."
paired = sorted({int(m) for m in re.findall(r"device hint: sub_id=(\d+)", status_txt)})
records = re.findall(r'"data_str2":\s*"([^"]*)"', names_txt)

def decode(rec):
    """Mirror decode_device_name, but report *why* a record yields nothing."""
    if len(rec) != 36:
        return None, f"MALFORMED: {len(rec)} chars, expected 36"
    try:
        sub_id = int(rec[0:4], 16)
    except ValueError:
        return None, "MALFORMED: sub_id is not hex"
    try:
        raw = bytes.fromhex(rec[4:36]).decode("gbk", errors="replace")
    except ValueError:
        return sub_id, "MALFORMED: name field is not hex"
    if "$" not in raw:
        return sub_id, "MALFORMED: no '$' terminator"
    name = raw[raw.rfind("@") + 1 : raw.index("$")]
    if not name:
        return sub_id, "no name stored on the hub"
    return sub_id, f"nickname={name!r}"

print(f"Paired sub-devices (CMD_CODE 55): {paired if paired else 'none returned'}")
print(f"Name records received (CMD_CODE 17): {len(records)}")
print()

# No frames at all means the exchange did not happen -- the hub always closes a
# name sync with NAME_OVER, even when it has no names to send.  Concluding
# "nothing is named" from silence would be exactly backwards.
if not records:
    print("The hub sent no CMD_CODE 17 frames at all, not even the NAME_OVER that")
    print("ends an empty name sync. That means the capture failed rather than that")
    print("your devices are unnamed -- usually the hub was still busy with the")
    print("previous query. Please just run the script again.")
    raise SystemExit(0)

named, problems = [], []
for rec in records:
    if rec == "NAME_OVER":
        print("  NAME_OVER          (end of stream)")
        continue
    sub_id, verdict = decode(rec)
    flag = " " if verdict.startswith("nickname=") else "!"
    label = f"sub_id={sub_id}" if sub_id is not None else "sub_id=?"
    print(f" {flag} {label:<11} len={len(rec):<3} {verdict}")
    print(f"      raw={rec}")
    if verdict.startswith("nickname="):
        named.append(sub_id)
    else:
        problems.append((sub_id, verdict))

print()
print("-- Summary --")
if not paired:
    print("No CMD_CODE 55 status records were captured, so the nicknames above")
    print("cannot be checked against the full device list. Re-run to compare them.")
    raise SystemExit(0)
missing = [s for s in paired if s not in named]
if problems:
    print("Records the library cannot decode (these are library bugs):")
    for sub_id, verdict in problems:
        print(f"  sub_id={sub_id}: {verdict}")
if missing:
    silent = [s for s in missing if s not in [p for p, _ in problems]]
    if silent:
        print(f"Paired but the hub sent no name record at all: {silent}")
        print("  The hub stores no nickname for these. The ELRO app may still show")
        print("  a name for them -- it falls back to its own local database. Renaming")
        print("  the device in the app pushes the name to the hub and fixes it.")
if not problems and not missing:
    print("Every paired sub-device has a nickname. Nothing wrong here.")
PY

cat <<'QUESTIONS'

-- Please also answer --
1. Is it the SAME sub-device missing its nickname every run, or a different one
   each time? Run this script two or three times to check. A different one each
   time points at timing; the same one every time points at that device's name.
2. What exact name did you give the affected device in the ELRO app? Check it
   with:  tools/name_sync_report.sh --check-name "the name"
   Names over 15 bytes, or containing @ or $, cannot survive the round trip --
   and the app's naming screen during pairing enforces neither rule.
QUESTIONS

echo
echo "-- Raw frames --"
if [ "$REDACT" -eq 1 ]; then
    sed -e "s/$GATEWAY_IP/<gateway-ip>/g" -e "s/$DEVICE_NAME/<device-name>/g" "$WORK/names.txt"
else
    cat "$WORK/names.txt"
fi
