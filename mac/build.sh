#!/bin/bash
# Build AhaMac.app, install to ~/Applications, and start it at login (LaunchAgent com.aha.mac).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/AhaMac.app"
BUILD="$HERE/build/AhaMac.app"

rm -rf "$HERE/build"
mkdir -p "$BUILD/Contents/MacOS"
swiftc -O -o "$BUILD/Contents/MacOS/AhaMac" "$HERE/AhaMac.swift"
cat > "$BUILD/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.shaorui0.ahamac</string>
  <key>CFBundleName</key><string>AhaMac</string>
  <key>CFBundleExecutable</key><string>AhaMac</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>录下你说的话，交给本机 Aha 服务转写。</string>
</dict></plist>
EOF
codesign --force --sign - "$BUILD"

pkill -x AhaMac 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$APP" && cp -R "$BUILD" "$APP"

PLIST="$HOME/Library/LaunchAgents/com.aha.mac.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aha.mac</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-W</string><string>$APP</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/com.aha.mac" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $APP (menu bar mic; click or tap Ctrl+Shift)"
