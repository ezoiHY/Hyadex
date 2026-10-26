#!/bin/bash
# Builds dist/Hyadex.app with SwiftPM and the Command Line Tools only (no Xcode).
#   scripts/build-app.sh            release build for this Mac's architecture
#   CONFIG=debug scripts/build-app.sh
set -euo pipefail

cd "$(dirname "$0")/.."
CONFIG="${CONFIG:-release}"
APP="dist/Hyadex.app"
VERSION="${APP_VERSION:-0.1.0}"

echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG" --product Hyadex
BIN="$(swift build -c "$CONFIG" --show-bin-path)/Hyadex"

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Hyadex"

# SwiftPM's default build system records the deployment target as the SDK
# version (sdk 15.0). AppKit then runs the app in compatibility mode with the
# old window chrome. Record the SDK we actually built against.
SDK_VERSION="$(xcrun --show-sdk-version)"
MIN_VERSION="$(vtool -show-build "$APP/Contents/MacOS/Hyadex" | awk '/minos/ {print $2; exit}')"
vtool -set-build-version macos "$MIN_VERSION" "$SDK_VERSION" -replace \
    -output "$APP/Contents/MacOS/Hyadex" "$APP/Contents/MacOS/Hyadex"
echo "    build version: macOS $MIN_VERSION, SDK $SDK_VERSION"

if [ ! -d Resources/AppIcon.iconset ]; then
    swift scripts/make-icon.swift Resources/AppIcon.iconset
fi
iconutil -c icns Resources/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

cp Resources/Info.plist "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$(date +%Y%m%d%H%M)" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

echo "==> ad-hoc signing"
codesign --force --sign - --entitlements Resources/Hyadex.entitlements --options runtime "$APP"
codesign --verify --strict "$APP"

echo "==> $APP ($(du -sh "$APP" | cut -f1))"
