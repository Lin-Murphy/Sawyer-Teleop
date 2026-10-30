#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" != --once ]]; then
  printf '%s\n' 'Sawyer Simulation Joint Monitor: waiting for simulation...' 'Ctrl+C exits the monitor; simulation keeps running.'
  deadline=$((SECONDS + 90))
  until [[ "$(docker inspect --format '{{.State.Running}}' sawyer-phase3-sim 2>/dev/null || true)" == true ]]; do
    if (( SECONDS >= deadline )); then
      echo 'No running simulation detected. Check the simulation launch terminal.' >&2
      if [[ -t 0 ]]; then read -r -p 'Press Enter to close...' || true; fi
      exit 1
    fi
    sleep .5
  done
fi
# Read the existing RAM snapshot inside the container. Never connect as another A.
tty_args=()
if [[ -t 0 && -t 1 && "${1:-}" != --once ]]; then tty_args=(-it); fi
exec docker exec "${tty_args[@]}" --env PYTHONPATH=/phase3/src \
  --env PYTHONDONTWRITEBYTECODE=1 sawyer-phase3-sim \
  python3 -m sawyer_teleop.cli monitor --status-file /dev/shm/phase3-status.json "$@"
