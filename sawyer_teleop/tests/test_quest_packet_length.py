"""Full hand poses can cross Android printf's 1023-byte JSON boundary."""
import copy
import json
from pathlib import Path

import pytest

from sawyer_teleop.operator import Operator
from test_ingress_recording import reader, feed
from test_ik_schedule import Robot, ManualExecutor

RAW = (Path(__file__).parent/'fixtures/quest_long_packet.json').read_bytes().rstrip(b'\n')


def sample(seq=12345, grip=0., buttons=0):
    value = json.loads(RAW)
    value['seq'], value['sample_ns'] = seq, 1_000_000_000 + seq
    value['hands']['r'].update(grip=grip, buttons=buttons)
    return value


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
def test_exact_old_logger_boundary_is_invalid_and_requires_quest_update(monkeypatch, ending):
    assert len(RAW) == 1024
    with pytest.raises(json.JSONDecodeError) as error:
        json.loads(RAW[:1023] + ending)
    if ending == b'\r\n':
        assert str(error.value) == "Expecting ',' delimiter: line 2 column 1 (char 1025)"
    quest = reader(monkeypatch)
    feed(quest, [RAW[:1023] + ending.rstrip(b'\n')])
    assert not quest.latest['valid'] and not quest.poll()['events']
    assert 'Setup.cmd quest' in quest.latest['reason']


def test_full_large_packets_keep_pose_button_edges_and_release(monkeypatch):
    quest = reader(monkeypatch)
    complete = sample(grip=.812345678901, buttons=1)
    complete['hands']['l']['pose'] = copy.deepcopy(complete['head'])
    complete['session'] += '-long-frame'
    assert len(json.dumps(complete, separators=(',', ':'))) > 1024
    feed(quest, [complete])
    assert quest.latest['valid'] and quest.poll()['events'] == ['save']
    assert quest.latest['packet'] == complete
    complete['seq'] += 1
    complete['sample_ns'] += 1
    complete['hands']['r']['grip'] = 0.
    feed(quest, [complete])
    assert quest.latest['valid'] and quest.latest['grip'] == 0.
    assert quest.poll()['events'] == []


def test_bad_packet_pauses_and_never_resumes_without_release(tmp_path, monkeypatch):
    monkeypatch.setattr('sawyer_teleop.operator.ns', lambda: 1_010_000_000)
    quest, robot = reader(monkeypatch), Robot()
    operator = Operator(robot, quest, [], tmp_path)
    operator.executor.shutdown(wait=True)
    operator.executor = ManualExecutor()
    feed(quest, [sample()])
    operator.tick()
    operator.request('calibrate')
    operator.tick()
    feed(quest, [sample(12346, grip=1.)])
    operator.tick()
    assert operator.following
    pending = operator.future
    feed(quest, [RAW[:1023]])
    operator.tick()
    assert not operator.following and not robot.armed and not robot.targets
    # A running solve need not be cancellable; its late result must never send.
    pending.set_result(([0.]*7, 1_010_000_000, 1_010_000_000))
    feed(quest, [sample(12347, grip=1.)])
    operator.tick()
    assert not operator.following and not robot.targets
    feed(quest, [sample(12348)])
    operator.tick()
    feed(quest, [sample(12349, grip=1.)])
    operator.tick()
    assert operator.following and not robot.targets and not list(tmp_path.iterdir())
