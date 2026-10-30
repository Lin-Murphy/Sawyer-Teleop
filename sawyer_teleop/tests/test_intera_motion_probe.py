"""Saved ROS graph checks only; never count these as native controller validation."""
import importlib.util
import json
from pathlib import Path
import sys
import pytest

spec = importlib.util.spec_from_file_location('intera_probe', Path(__file__).resolve().parents[1]/'scripts/probe_intera_motion.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def graph():
    pubs = [[probe.ACTION+'/'+s, ['/native']] for s in ('status','feedback','result')]
    subs = [[probe.ACTION+'/'+s, ['/native']] for s in ('goal','cancel')]
    pubs.append(['/clock', ['/gazebo']])
    return dict(system_state=[pubs,subs,[['/gazebo/get_world_properties',['/gazebo']]]],
                topic_types=[[probe.ACTION+'/'+s,t] for s,t in probe.TYPES.items()], use_sim_time=True)


def test_complete_graph_is_only_a_candidate_not_motion_permission():
    result = probe.evaluate(graph())
    assert result['interface_advertised'] and result['simulation_graph_signals']
    assert result['server_nodes'] == ['/native']
    assert result['motion_authorized'] is False


@pytest.mark.parametrize('suffix', list(probe.TYPES))
def test_missing_or_wrong_action_topic_cannot_pass(suffix):
    data = graph()
    data['topic_types'] = [[topic, 'wrong/Type' if topic.endswith('/'+suffix) else kind]
                           for topic,kind in data['topic_types']]
    assert not probe.evaluate(data)['interface_advertised']


def test_client_advertisements_are_not_an_action_server():
    data = graph()
    data['system_state'][0],data['system_state'][1] = data['system_state'][1],data['system_state'][0]
    assert not probe.evaluate(data)['interface_advertised']


def test_unrelated_nodes_cannot_be_combined_into_one_server():
    data = graph()
    data['system_state'][1][0][1] = ['/other']
    assert not probe.evaluate(data)['interface_advertised']


def test_real_or_unknown_environment_cannot_satisfy_simulation_precondition():
    data = graph()
    data['use_sim_time'] = False
    result = probe.evaluate(data)
    assert result['interface_advertised'] and not result['simulation_graph_signals']
    assert result['motion_authorized'] is False


def test_unavailable_ros_writes_a_failure_report(tmp_path, monkeypatch):
    def unavailable():
        raise ImportError('ROS unavailable')
    output = tmp_path/'probe.json'
    monkeypatch.setattr(probe, 'collect', unavailable)
    monkeypatch.setattr(sys, 'argv', ['probe', '--output', str(output)])
    assert probe.main() == 2
    result = json.loads(output.read_text())
    assert result['error'] == 'ImportError: ROS unavailable'
    assert not result['interface_advertised'] and not result['motion_authorized']


def test_saved_graph_never_calls_ros(tmp_path, monkeypatch):
    def unexpected():
        raise AssertionError('Offline probe must not connect')
    source, output = tmp_path/'graph.json', tmp_path/'result.json'
    source.write_text(json.dumps(graph()))
    monkeypatch.setattr(probe, 'collect', unexpected)
    monkeypatch.setattr(sys, 'argv', ['probe', '--graph', str(source), '--output', str(output)])
    assert probe.main() == 0
    result = json.loads(output.read_text())
    assert result['evidence'] == 'saved graph only' and result['motion_authorized'] is False
