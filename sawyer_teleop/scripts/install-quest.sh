#!/usr/bin/env bash
# Install the verified Quest APK with adb from PATH (or --adb <path>).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
exec "${SAWYER_PYTHON:-python3}" "$root/scripts/install_quest_120.py" "$@"
