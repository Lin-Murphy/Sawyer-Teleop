#!/usr/bin/env bash
# Connect to an existing lab ROS master; never enable or home the robot.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
config="$root/config/real-b.env"
check=false
while (( $# )); do
  case "$1" in
    --config) [[ $# -ge 2 ]] || { echo '--config requires a file' >&2; exit 2; }; config=$2; shift 2 ;;
    --check) check=true; shift ;;
    --help|-h)
      echo 'Usage: bash scripts/ubuntu/Start-B-Real.sh [--config FILE] [--check]'
      echo 'Fill config/real-b.env from its .example first. --check only checks local files/imports; no ROS connection.'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
fail() { echo "Real B: $*" >&2; exit 1; }
[[ -f "$config" ]] || fail "Missing $config. Copy config/real-b.env.example to config/real-b.env and fill the addresses."
# Deliberately local, user-authored shell configuration.
source "$config"
export SAWYER_GRIPPER_PORT="${SAWYER_GRIPPER_PORT:-}"
cd "$root"
: "${SAWYER_ROBOT_HOST:?Fill SAWYER_ROBOT_HOST in real-b.env}"
: "${SAWYER_ROS_IP:?Fill SAWYER_ROS_IP in real-b.env}"
: "${SAWYER_BIND_IP:?Fill SAWYER_BIND_IP in real-b.env}"
python3 - "$SAWYER_ROBOT_HOST" "$SAWYER_ROS_IP" "$SAWYER_BIND_IP" <<'PY'
import ipaddress, re, sys
host, ros_ip, bind_ip = sys.argv[1:]
try:
    for label, value in [('SAWYER_ROS_IP', ros_ip), ('SAWYER_BIND_IP', bind_ip)]:
        address = ipaddress.IPv4Address(value)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError(label + ' must be a concrete B network IPv4 address')
    if (host.lower().rstrip('.') == 'localhost' or len(host) > 253 or
            not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host)):
        raise ValueError('SAWYER_ROBOT_HOST must be a robot hostname or IPv4, without http:// or a port')
    if re.fullmatch(r'[0-9.]+', host):
        address = ipaddress.IPv4Address(host)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('SAWYER_ROBOT_HOST cannot be a local simulation address')
except ValueError as exc:
    raise SystemExit(str(exc))
PY
ros_setup=${SAWYER_ROS_SETUP:-/opt/ros/noetic/setup.bash}
workspace=${SAWYER_WORKSPACE:-$HOME/sawyer_ws}
[[ -f "$ros_setup" ]] || fail "Missing ROS setup: $ros_setup"
[[ -f "$workspace/devel/setup.bash" ]] || fail "Missing built workspace: $workspace/devel/setup.bash"
# Catkin environment hooks may read unset variables.
set +u
source "$ros_setup"
source "$workspace/devel/setup.bash"
set -u
export ROS_MASTER_URI="http://$SAWYER_ROBOT_HOST:11311"
export ROS_IP="$SAWYER_ROS_IP"
unset ROS_HOSTNAME
export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
b_python=${SAWYER_B_PYTHON:-}
if [[ -z "$b_python" ]]; then
  if [[ -x "$root/.venv-b/bin/python" ]]; then b_python="$root/.venv-b/bin/python"; else b_python=python3; fi
fi
command -v "$b_python" >/dev/null || fail "Python not found: $b_python"
if [[ "$("$b_python" -c 'import sys; print("%s.%s" % sys.version_info[:2])')" == 3.8 ]]; then
  planner=$("$b_python" "$root/scripts/prepare_trajectory_runtime.py")
  export PYTHONPATH="$planner:$PYTHONPATH"
fi
"$b_python" - <<'PY'
import os, sys
if sys.version_info < (3, 8):
    raise SystemExit('B needs Python >= 3.8 with the lab ROS packages')
try:
    import rospy, intera_core_msgs.msg, intera_core_msgs.srv, ruckig
    from sawyer_teleop import cli, intera_backend
    if os.environ.get('SAWYER_GRIPPER_PORT'):
        import dynamixel_sdk
    if ruckig.__version__ != '0.12.2':
        raise ImportError('ruckig must be 0.12.2')
except ImportError as exc:
    raise SystemExit('B dependency check failed: %s. See docs/DEPLOYMENT_B.md.' % exc)
PY
echo "Real Sawyer | ROS master: $ROS_MASTER_URI | ROS_IP: $ROS_IP"
echo "A: Real Sawyer | B address: $SAWYER_BIND_IP | TCP 8765 / 8766 | remote IK"
neutral_pose="$root/config/lab-neutral-pose.yaml"
[[ -f "$neutral_pose" ]] || fail "Missing lab Reset pose: $neutral_pose"
if [[ "$check" == true ]]; then
  echo 'Local checks passed. Robot connectivity, hardware and motion have NOT been checked.'
  exit 0
fi
# Read-only graph checks, bounded even if the configured master is unreachable.
timeout 10s "$b_python" - <<'PY'
import rospy
if rospy.get_param('/use_sim_time', False):
    raise SystemExit('Real B refuses a simulation ROS master')
rospy.wait_for_service('/ExternalTools/right/PositionKinematicsNode/IKService', timeout=5)
print('ROS master and IK service found. No enable or motion command sent.', flush=True)
PY
# Lab-confirmed Reset target; A reads it at handshake. Loading a parameter never moves the arm.
rosparam load "$neutral_pose"
echo 'Starting real B bridge. Ctrl+C stops this bridge. Robot enabling follows the lab procedure.'
gripper_args=()
if [[ -n "$SAWYER_GRIPPER_PORT" ]]; then
  gripper_args=(--gripper-port "$SAWYER_GRIPPER_PORT")
fi
# Reference/command 0.25 s (was 0.12/0.2), aligned with the Gazebo 250 ms profile:
# 2026-10-29 user prefers occasional lag over pauses that require re-gripping.
exec "$b_python" -m sawyer_teleop.cli bridge --backend intera --host "$SAWYER_BIND_IP" \
  --control-port 8765 --ik-port 8766 --state-timeout 0.1 --reference-timeout 0.25 \
  --command-timeout 0.25 --hz 100 --tip right_hand \
  --status-file "/dev/shm/sawyer-teleop-$(id -u)/intera-b.json" "${gripper_args[@]}"
