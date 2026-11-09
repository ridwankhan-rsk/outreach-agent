#!/bin/bash
# Builds the notifier app into ~/.outreach-agent/Outreach.app (needs Xcode command line tools).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${OUTREACH_DATA_DIR:-$HOME/.outreach-agent}/Outreach.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
swiftc -O "$HERE/main.swift" -o "$APP/Contents/MacOS/Outreach" \
  -framework Cocoa -framework UserNotifications
codesign --force --deep --sign - "$APP"      # ad-hoc signature: required for notifications
echo "Built $APP"
