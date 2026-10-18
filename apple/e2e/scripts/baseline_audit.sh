#!/usr/bin/env bash
# Headless baseline visual-audit capture (T003-06).
# Navigates each phone tab / watch screen via the --nerve-tab / --nerve-view
# launch-arg router, screenshots via simctl, and pulls the app's data-dump.
#
# Usage: baseline_audit.sh
# Writes to docs/audit/<stamp>/<phone|watch>/...
set -u
export PATH="/opt/homebrew/bin:$PATH"

ROOT="/Users/celes/sources/celesrenata/nervcenter"
STAMP="2026-10-18-baseline"
OUT="$ROOT/docs/audit/$STAMP"
mkdir -p "$OUT/phone" "$OUT/watch"

PHONE_ID=5B74CB08-37DC-4F13-AF58-92CA077C3D8B
WATCH_ID=DBF4BE03-8DFA-402C-808A-89D552AFA7EE
PHONE_BID=com.celesrenata.NervePhone
WATCH_BID=com.celesrenata.NervePhone.watch

PHONE_CPATH="$(xcrun simctl get_app_container "$PHONE_ID" "$PHONE_BID" data 2>/dev/null)"
WATCH_CPATH="$(xcrun simctl get_app_container "$WATCH_ID" "$WATCH_BID" data 2>/dev/null)"
echo "phone container: $PHONE_CPATH"
echo "watch container: $WATCH_CPATH"

# --- Phone: six §56 tabs -----------------------------------------------------
for TAB in Overview Goals Incidents Agents Systems Ask; do
  xcrun simctl terminate "$PHONE_ID" "$PHONE_BID" 2>/dev/null
  xcrun simctl launch "$PHONE_ID" "$PHONE_BID" --nerve-tab="$TAB" --nerve-dump=1 >/dev/null 2>&1
  sleep 2.3
  xcrun simctl io "$PHONE_ID" screenshot "$OUT/phone/${TAB}.png" >/dev/null 2>&1
  if [ -n "$PHONE_CPATH" ] && [ -f "$PHONE_CPATH/Documents/nerve-audit-dump.json" ]; then
    cp "$PHONE_CPATH/Documents/nerve-audit-dump.json" "$OUT/phone/${TAB}.json"
  fi
  png_bytes=$(stat -f%z "$OUT/phone/${TAB}.png" 2>/dev/null || echo 0)
  have_json=$([ -f "$OUT/phone/${TAB}.json" ] && echo yes || echo no)
  echo "phone ${TAB}: png=${png_bytes}B json=${have_json}"
done

# --- Watch: home + off-home screens ------------------------------------------
for VIEW in home alert query acknowledge approvals stop; do
  if [ "$VIEW" = "home" ]; then
    ARGS="--nerve-dump=1"
    NAME="home"
  else
    ARGS="--nerve-view=$VIEW --nerve-dump=1"
    NAME="$VIEW"
  fi
  xcrun simctl terminate "$WATCH_ID" "$WATCH_BID" 2>/dev/null
  xcrun simctl launch "$WATCH_ID" "$WATCH_BID" $ARGS >/dev/null 2>&1
  sleep 2.3
  xcrun simctl io "$WATCH_ID" screenshot "$OUT/watch/${NAME}.png" >/dev/null 2>&1
  if [ -n "$WATCH_CPATH" ] && [ -f "$WATCH_CPATH/Documents/nerve-audit-dump.json" ]; then
    cp "$WATCH_CPATH/Documents/nerve-audit-dump.json" "$OUT/watch/${NAME}.json"
  fi
  png_bytes=$(stat -f%z "$OUT/watch/${NAME}.png" 2>/dev/null || echo 0)
  have_json=$([ -f "$OUT/watch/${NAME}.json" ] && echo yes || echo no)
  echo "watch ${NAME}: png=${png_bytes}B json=${have_json}"
done

echo "=== output tree ==="
ls -la "$OUT/phone" "$OUT/watch"
