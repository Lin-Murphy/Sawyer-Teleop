"""Read-only ROS graph check for the action required by go_to_cartesian_pose.

Does not instantiate an action client or publish goals, stops, or joint commands.
An advertised interface is not proof of motion support or simulation provenance.
"""
import argparse
import json
from pathlib import Path
import socket

ACTION = '/motion/motion_command'
TYPES = {
    'goal': 'intera_motion_msgs/MotionCommandActionGoal',
    'cancel': 'actionlib_msgs/GoalID',
    'status': 'actionlib_msgs/GoalStatusArray',
    'feedback': 'intera_motion_msgs/MotionCommandActionFeedback',
    'result': 'intera_motion_msgs/MotionCommandActionResult',
}


def evaluate(graph):
    publishers, subscribers, services = [dict(rows) for rows in graph['system_state']]
    types = dict(graph['topic_types'])
    checks, owners = {}, []
    for suffix, expected in TYPES.items():
        topic = ACTION+'/'+suffix
        nodes = (subscribers if suffix in ('goal', 'cancel') else publishers).get(topic, [])
        checks[suffix] = bool(nodes) and types.get(topic) == expected
        owners.append(set(nodes))
    common = sorted(set.intersection(*owners))
    protocol_present = all(checks.values()) and bool(common)
    simulation_signals = (graph.get('use_sim_time') is True and bool(publishers.get('/clock'))
                          and bool(services.get('/gazebo/get_world_properties')))
    return dict(action=ACTION, interface_advertised=protocol_present,
                simulation_graph_signals=simulation_signals,
                server_nodes=common, topic_checks=checks, motion_authorized=False,
                next_step=('Inspect server implementation/provenance and bounded simulation handover test'
                           if protocol_present and simulation_signals else
                           'Native Cartesian test unavailable; do not start the example or relabel a substitute'),
                limitations=['Graph registration does not prove server liveness, Cartesian support or smooth preemption.',
                             'use_sim_time and Gazebo presence do not prove this action controls only simulation.',
                             'This probe never sends motion.'])


def collect():
    import rosgraph
    master = rosgraph.Master('/sawyer_intera_read_only_probe')
    try:
        use_sim_time = master.getParam('/use_sim_time')
    except rosgraph.MasterError:
        use_sim_time = None
    return dict(system_state=master.getSystemState(), topic_types=master.getTopicTypes(),
                use_sim_time=use_sim_time)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--graph', type=Path, help='Inspect a saved graph offline; no ROS connection')
    args = parser.parse_args()
    previous_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(3.0)
        graph = json.loads(args.graph.read_text()) if args.graph else collect()
        report = evaluate(graph)
        report['graph'] = graph
        report['evidence'] = 'saved graph only' if args.graph else 'live read-only ROS master graph'
    except Exception as exc:
        report = dict(interface_advertised=False, motion_authorized=False, error=type(exc).__name__+': '+str(exc),
                      next_step='Run inside the ROG ROS environment and inspect its graph; no motion was sent')
    finally:
        socket.setdefaulttimeout(previous_timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('interface_advertised') and report.get('simulation_graph_signals') else 2


if __name__ == '__main__':
    raise SystemExit(main())
