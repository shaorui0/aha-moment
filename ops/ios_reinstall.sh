#!/bin/bash
# Rebuild + reinstall the iOS app on the USB-connected iPhone.
# Free Apple ID signing expires every 7 days; plug in the phone, unlock it, run this. Recordings/queue are kept.
set -euo pipefail
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
IOS="$(cd "$(dirname "$0")/../ios" && pwd)"
DD="${HOME}/Library/Caches/aha_ios_build"
BUNDLE=com.shaorui0.ahamoment

# Usage: ios_reinstall.sh [name-or-udid-substring]   (default: every USB-connected, paired iPhone)
WANT="${1:-}"
J=$(mktemp); trap 'rm -f "$J"' EXIT
xcrun devicectl list devices --json-output "$J" >/dev/null 2>&1
TARGETS=$(python3 -c '
import json, sys
want = sys.argv[2].lower()
for d in json.load(open(sys.argv[1]))["result"]["devices"]:
    h, p, c = d.get("hardwareProperties", {}), d.get("deviceProperties", {}), d.get("connectionProperties", {})
    if h.get("deviceType") != "iPhone" or c.get("transportType") != "wired":
        continue
    name = p.get("name", "")
    if want and want not in name.lower() and want not in h.get("udid", "").lower():
        continue
    print(d["identifier"], h["udid"], name.replace(" ", "_"))' "$J" "$WANT")
[ -n "$TARGETS" ] || { echo "No iPhone found${WANT:+ matching $WANT}: connect via USB, unlock, trust this Mac." >&2; exit 1; }

cd "$IOS"
xcodegen generate >/dev/null
while read -r DEV UDID NAME; do
  echo "== $NAME ($UDID)"
  xcodebuild -project AhaMoment.xcodeproj -scheme AhaMoment -destination "id=$UDID" -derivedDataPath "$DD" \
    -allowProvisioningUpdates -allowProvisioningDeviceRegistration build 2>&1 | grep -E "error:|BUILD (SUCCEEDED|FAILED)" | sort -u
  xcrun devicectl device install app --device "$DEV" "$DD/Build/Products/Debug-iphoneos/AhaMoment.app" | grep -i installed
  xcrun devicectl device process launch --terminate-existing --device "$DEV" "$BUNDLE" | grep -i launched || true
done <<< "$TARGETS"
echo "OK: signed for 7 more days ($(date -v+7d +%F))"
