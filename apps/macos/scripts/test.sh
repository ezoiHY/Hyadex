#!/bin/bash
# Runs the Swift Testing suites with only the Command Line Tools installed.
# The default build system crashes on test targets here, and the Testing
# framework lives outside the default search paths, hence the extra flags.
set -euo pipefail

cd "$(dirname "$0")/.."
FRAMEWORKS="$(xcode-select -p)/Library/Developer/Frameworks"
swift test --build-system native \
    -Xswiftc -F"$FRAMEWORKS" \
    -Xlinker -F"$FRAMEWORKS" -Xlinker -rpath -Xlinker "$FRAMEWORKS" \
    "$@"
