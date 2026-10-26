#!/bin/bash
# Debug build, bundle and open the app.
set -euo pipefail

cd "$(dirname "$0")/.."
CONFIG=debug scripts/build-app.sh
open dist/Hyadex.app
