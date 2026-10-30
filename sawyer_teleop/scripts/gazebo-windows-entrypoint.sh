#!/usr/bin/env bash
set -euo pipefail
# An explicit local Gazebo experiment; never change the shared bridge defaults.
reference_timeout=${SAWYER_SIM_REFERENCE_TIMEOUT:-0.12}
case "$reference_timeout" in
  0.12|0.18) command_timeout=0.20 ;;
  0.25) command_timeout=0.25 ;;
  *) echo "Unsupported simulation reference timeout: $reference_timeout" >&2; exit 2 ;;
esac
export ROS_LOG_DIR=/dev/shm/phase3-ros
export PYTHONPATH=/phase3/src${PYTHONPATH:+:$PYTHONPATH}
export PYTHONDONTWRITEBYTECODE=1
mkdir -p /tmp/sawyer-home
trajectory_runtime=$(python3 /phase3/scripts/prepare_trajectory_runtime.py)
export PYTHONPATH="$trajectory_runtime:$PYTHONPATH"
python3 -c "import ruckig; assert ruckig.__version__ == '0.12.2'"
# The original initializer assumes fast startup and can miss the spawn posture.
# Keep the image immutable; initialize this new local scene before exposing B.
python3 - <<'PY'
import os
import xml.etree.ElementTree as ET
tree = ET.parse('/workspaces/lin_ws/src/sawyer_local/launch/simulation.launch')
root = tree.getroot()
for node in list(root):
    if node.tag == 'node' and node.get('name') in ('desk_initial_pose', 'desk_camera_preview'):
        root.remove(node)
tree.write('/tmp/windows-simulation.launch')
PY
roslaunch -p 11322 /tmp/windows-simulation.launch scene:=desk "gui:=${SAWYER_SIM_GUI:-false}" &
sim_pid=$!
bridge_pid=
motion_pid=
cleanup() {
  trap - EXIT INT TERM
  kill "$sim_pid" ${motion_pid:+"$motion_pid"} ${bridge_pid:+"$bridge_pid"} 2>/dev/null || true
  wait "$sim_pid" ${motion_pid:+"$motion_pid"} ${bridge_pid:+"$bridge_pid"} 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python3 /phase3/scripts/initialize_windows_sim.py
# Simulated /motion/motion_command (project planner, not Intera) for the SDK Cartesian path.
python3 -m sawyer_teleop.sim_motion_server &
motion_pid=$!
python3 - <<'PY'
import time, rospy
deadline = time.time()+30
while not rospy.get_param('/motion/motion_command/simulated', False):
    if time.time() > deadline:
        raise SystemExit('Simulated Motion Controller did not start')
    time.sleep(.2)
PY
python3 -m sawyer_teleop.cli bridge --backend gazebo --host 0.0.0.0 --control-port 8875 --ik-port 8876 --reference-timeout "$reference_timeout" --command-timeout "$command_timeout" --status-file /dev/shm/phase3-status.json &
bridge_pid=$!
wait -n "$sim_pid" "$motion_pid" "$bridge_pid"
