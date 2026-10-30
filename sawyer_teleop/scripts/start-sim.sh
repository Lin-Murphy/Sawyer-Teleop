#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode=auto
check=false
while (( $# )); do
  case "$1" in
    --gui) mode=gui; shift ;;
    --headless) mode=headless; shift ;;
    --bind)
      [[ $# -ge 2 ]] || { echo '--bind requires a B IPv4 address' >&2; exit 2; }
      export SAWYER_SIM_BIND="$2"; shift 2 ;;
    --check) check=true; shift ;;
    --help|-h)
      echo 'Usage: bash scripts/ubuntu/Start-B-Sim.sh [--gui|--headless] [--bind B_IPV4] [--check]'
      echo 'Default: local A only. Use --bind with the B address reachable from Windows A.'
      echo '--check checks local prerequisites without creating a container.'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ "$mode" == auto ]]; then
  if [[ -n "${DISPLAY:-}" ]]; then mode=gui; else mode=headless; fi
fi
command -v python3 >/dev/null || { echo 'Python 3 is required on Ubuntu.' >&2; exit 1; }
python3 - "${SAWYER_SIM_BIND:-127.0.0.1}" <<'PY'
import ipaddress, sys
try:
    address = ipaddress.IPv4Address(sys.argv[1])
    if address.is_multicast:
        raise ValueError('multicast is not a B bind address')
except ValueError as exc:
    raise SystemExit('Invalid --bind IPv4: ' + str(exc))
PY
command -v docker >/dev/null || { echo 'Docker is required for this project scene. See docs/SETUP.md.' >&2; exit 1; }
[[ "$(timeout 10s docker info --format '{{.OSType}}')" == linux ]] || { echo 'A working Linux Docker engine is required.' >&2; exit 1; }
image=${SAWYER_SIM_IMAGE:-sawyer/simulation:20261017}
image_id=$(timeout 10s docker image inspect "$image" --format '{{.Id}}') || {
  echo 'Simulation image missing: download sawyer-simulation-20261017.tar.gz from the GitHub Release, then docker load -i it. See docs/SETUP.md.' >&2; exit 1;
}
[[ "$image_id" == sha256:f5e1e474226438833d57352952d9c347b952a206366b02bb8311477bde665046 ]] || {
  echo 'Simulation image differs from the verified image. No container was started.' >&2; exit 1;
}
if [[ "$check" == true ]]; then
  echo "Local simulation prerequisites passed | bind ${SAWYER_SIM_BIND:-127.0.0.1} | mode $mode"
  echo 'No container started; display, physics and A connection are not tested by --check.'
  exit 0
fi
sim_uid=$(id -u)
sim_gid=$(id -g)
args=()
auth_file=
cleanup() {
  if [[ -n "$auth_file" ]]; then rm -f -- "$auth_file"; fi
}
trap cleanup EXIT
if [[ "$mode" == gui ]]; then
  : "${DISPLAY:?Use --gui from an Ubuntu desktop terminal; use --headless over SSH without a desktop}"
  if ! command -v xauth >/dev/null; then echo 'GUI mode requires xauth on this host' >&2; exit 1; fi
  auth_file=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/sawyer-gazebo-xauth.XXXXXX")
  chmod 600 "$auth_file"
  # A private copy of the current display's cookie; no xhost or global permission changes.
  xauth nlist "$DISPLAY" | sed 's/^..../ffff/' | xauth -f "$auth_file" nmerge -
  if [[ ! -s "$auth_file" ]]; then echo 'Cannot read X11 authorization. Retry from a desktop terminal.' >&2; exit 1; fi
  args+=(--env "DISPLAY=$DISPLAY" --env XAUTHORITY=/run/sawyer-xauth
    --env QT_X11_NO_MITSHM=1 --env SAWYER_SIM_GUI=true
    --mount type=bind,source=/tmp/.X11-unix,target=/tmp/.X11-unix,readonly
    --mount "type=bind,source=$auth_file,target=/run/sawyer-xauth,readonly")
  echo 'Starting Gazebo and simulation B (8875 / 8876)...'
else
  args+=(--env SAWYER_SIM_GUI=false)
  echo 'Starting headless physical simulation and B (8875 / 8876)...'
fi
# Only A/B TCP ports are exposed. ROS and Gazebo remain inside this container.
# Keep the local A usable when an explicit remote address is also published.
port_args=(-p 127.0.0.1:8875:8875 -p 127.0.0.1:8876:8876)
if [[ -n "${SAWYER_SIM_BIND:-}" && "$SAWYER_SIM_BIND" != 127.0.0.1 ]]; then
  # A wildcard bind includes localhost already; avoid overlapping Docker bindings.
  if [[ "$SAWYER_SIM_BIND" == 0.0.0.0 ]]; then port_args=(); fi
  port_args+=(-p "$SAWYER_SIM_BIND:8875:8875" -p "$SAWYER_SIM_BIND:8876:8876")
fi
session_args=()
if [[ -n "$auth_file" ]]; then session_args+=(--auth-file "$auth_file"); fi
# The supervisor owns cleanup after exec, including the private X11 cookie.
exec python3 scripts/sim-session.py "${session_args[@]}" -- \
docker create --rm --init --name sawyer-phase3-sim \
  --user "$sim_uid:$sim_gid" --env HOME=/tmp/sawyer-home \
  --cap-drop ALL --security-opt no-new-privileges:true --shm-size 512m \
  --tmpfs "/logs:rw,uid=$sim_uid,gid=$sim_gid" --tmpfs /tmp:rw \
  "${port_args[@]}" \
  --mount "type=bind,source=$PWD,target=/phase3,readonly" \
  "${args[@]}" \
  "$image_id" \
  bash /phase3/scripts/gazebo-entrypoint.sh
