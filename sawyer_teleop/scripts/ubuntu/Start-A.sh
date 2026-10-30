#!/usr/bin/env bash
# Ubuntu operator A (local Web console).
# Python: $SAWYER_PYTHON, else sawyer_teleop/.venv/bin/python (see docs/SETUP.md).
# Local config / recordings / logs: $SAWYER_LOCAL_HOME, else ../ubuntu-local/.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
python=${SAWYER_PYTHON:-"$root/.venv/bin/python"}
home=${SAWYER_LOCAL_HOME:-"$root/../ubuntu-local"}
[[ -x "$python" ]] || {
  echo "Python environment missing: $python. See docs/SETUP.md." >&2
  exit 1
}
if [[ ! -f "$home/config/operator.json" ]]; then
  mkdir -p "$home/config"
  cp "$root/config/operator.json" "$home/config/operator.json"
fi
export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
cd "$root"
exec "$python" -m sawyer_teleop.cli web --config "$home/config/operator.json" "$@"
