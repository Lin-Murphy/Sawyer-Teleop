#!/usr/bin/env bash
set -euo pipefail
# ROG Gazebo experiment only. Missing config retains the standard deadline.
reference_timeout_ms=120
if [[ -f /phase3/config/gazebo-reference-timeout-ms ]]; then
  reference_timeout_ms=$(cat /phase3/config/gazebo-reference-timeout-ms)
fi
case "$reference_timeout_ms" in
  120) reference_timeout=0.12; command_timeout=0.20; command_timeout_ms=200 ;;
  180) reference_timeout=0.18; command_timeout=0.20; command_timeout_ms=200 ;;
  250) reference_timeout=0.25; command_timeout=0.25; command_timeout_ms=250 ;;
  *) echo "Gazebo reference timeout must be 120, 180 or 250 ms" >&2; exit 2 ;;
esac
echo "B Gazebo deadlines: latest 100 ms | reference $reference_timeout_ms ms | command $command_timeout_ms ms"
export ROS_LOG_DIR=/dev/shm/phase3-ros
export PYTHONPATH=/phase3/src${PYTHONPATH:+:$PYTHONPATH}
export PYTHONDONTWRITEBYTECODE=1
mkdir -p /tmp/sawyer-home
trajectory_runtime=$(python3 /phase3/scripts/prepare_trajectory_runtime.py)
export PYTHONPATH="$trajectory_runtime:$PYTHONPATH"
python3 -c "import ruckig; assert ruckig.__version__ == '0.12.2'"
# Wait for controllers before initializing this newly created scene.
python3 - <<'PY'
import xml.etree.ElementTree as ET
tree = ET.parse('/workspaces/lin_ws/src/sawyer_local/launch/simulation.launch')
root = tree.getroot()
for node in list(root):
    if node.tag == 'node' and node.get('name') in ('desk_initial_pose', 'desk_camera_preview'):
        root.remove(node)
tree.write('/tmp/sawyer-b-simulation.launch')
PY
roslaunch -p 11322 /tmp/sawyer-b-simulation.launch scene:=desk "gui:=${SAWYER_SIM_GUI:-false}" &
sim_pid=$!
bridge_pid=
motion_pid=
cleanup() {
  trap - EXIT INT TERM
  kill "$sim_pid" ${motion_pid:+"$motion_pid"} ${bridge_pid:+"$bridge_pid"} 2>/dev/null || true
  wait "$sim_pid" ${motion_pid:+"$motion_pid"} ${bridge_pid:+"$bridge_pid"} 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python3 /phase3/scripts/initialize_gazebo_scene.py
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
