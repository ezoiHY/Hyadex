#!/bin/bash
# Builds dist/Hyadex.apk (release: shrunk by R8, signed with the release key that
# ~/.android/hyadex-release.properties names, or the local debug key without one).
#   scripts/build-apk.sh
# Needs the Android SDK (Android Studio installs it) and a JDK 17+; Android
# Studio's bundled JDK is used when JAVA_HOME is not set.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -z "${JAVA_HOME:-}" ] && [ -d "/Applications/Android Studio.app/Contents/jbr/Contents/Home" ]; then
    export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
fi
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
if [ ! -f local.properties ]; then
    [ -d "$SDK" ] || { echo "Android SDK not found at $SDK (set ANDROID_HOME)" >&2; exit 1; }
    echo "sdk.dir=$SDK" > local.properties
fi

./gradlew --console=plain assembleRelease
mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk dist/Hyadex.apk
echo "==> dist/Hyadex.apk ($(du -h dist/Hyadex.apk | cut -f1 | tr -d ' '))"
# Say which key signed it: a phone only takes an update signed with the same key.
APKSIGNER="$(ls -d "$SDK"/build-tools/*/apksigner 2>/dev/null | tail -1 || true)"
if [ -n "$APKSIGNER" ]; then
    PATH="$JAVA_HOME/bin:$PATH" "$APKSIGNER" verify --print-certs dist/Hyadex.apk \
        | sed -n 's/^Signer #1 certificate DN: /==> signed by /p'
fi
