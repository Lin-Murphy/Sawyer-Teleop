#!/usr/bin/env bash
# Read-only dual-camera comparison on the Ubuntu lab computer.
set -eo pipefail
compare_fail() { echo "Camera comparison: $*. See docs/CAMERA_COMPARE.md." >&2; exit 1; }
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
compare_config=${SAWYER_REAL_CONFIG:-"$project/config/real-b.env"}
export SAWYER_LOCAL_HOME=${SAWYER_LOCAL_HOME:-"$project/../ubuntu-local"}
compare_default_python="$SAWYER_LOCAL_HOME/camera-compare/.venv/bin/python"
if [[ ! -x "$compare_default_python" ]]; then
  compare_default_python="$project/.venv/bin/python"
fi
compare_python=${SAWYER_CAMERA_COMPARE_PYTHON:-${SAWYER_PYTHON:-"$compare_default_python"}}
export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}

if ! command -v "$compare_python" >/dev/null; then
  echo "Camera comparison Python not found: $compare_python. See docs/CAMERA_COMPARE.md." >&2
  exit 1
fi

compare_args=("$@")
compare_help=false
for compare_arg in "${compare_args[@]}"; do
  if [[ "$compare_arg" == -h || "$compare_arg" == --help ]]; then compare_help=true; fi
done
if ! "$compare_help"; then
  compare_master_uri=${ROS_MASTER_URI:-}
  compare_ros_ip=${ROS_IP:-}
  [[ -f "$compare_config" ]] || compare_fail "Missing configuration: $compare_config"
  source "$compare_config"
  compare_ros_setup=${SAWYER_ROS_SETUP:-/opt/ros/noetic/setup.bash}
  compare_workspace=${SAWYER_CAMERA_COMPARE_WORKSPACE:-${SAWYER_WORKSPACE:-"$HOME/sawyer_ws"}}
  [[ -f "$compare_ros_setup" ]] || compare_fail "Missing ROS setup: $compare_ros_setup"
  [[ -f "$compare_workspace/devel/setup.bash" ]] || compare_fail "Missing built workspace: $compare_workspace/devel/setup.bash"
  [[ -n "$compare_master_uri" || -n "${SAWYER_ROBOT_HOST:-}" ]] || compare_fail "Set ROS_MASTER_URI or SAWYER_ROBOT_HOST"
  [[ -n "$compare_ros_ip" || -n "${SAWYER_ROS_IP:-}" ]] || compare_fail "Set ROS_IP or SAWYER_ROS_IP"
  # Catkin setup hooks consume positional arguments.
  set --
  source "$compare_ros_setup"
  source "$compare_workspace/devel/setup.bash"
  export ROS_MASTER_URI=${compare_master_uri:-"http://$SAWYER_ROBOT_HOST:11311"}
  export ROS_IP=${compare_ros_ip:-"$SAWYER_ROS_IP"}
  unset ROS_HOSTNAME
fi

compare_stdlib=$("$compare_python" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
compare_site=$("$compare_python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
export PYTHONPATH="$compare_stdlib:$compare_stdlib/lib-dynload:$compare_site:$project/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$compare_python" -m sawyer_teleop.camera_compare "${compare_args[@]}"
