"""Software contracts only: SDK doubles and loopback, never ROS/hardware."""
import copy
import json
import gzip
from pathlib import Path
from types import SimpleNamespace as Obj
import sys
import time

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.direct_pose import DIRECT_POSE_MODE
from sawyer_teleop.intera_cartesian import InteraCartesian, cartesian_options
from sawyer_teleop.protocol import ProtocolError
from test_bridge import Clock
from test_intera_cartesian import Trajectory
from sawyer_teleop.episode_store import open_episode

POSE = dict(p=[.5, .1, .3], q=[0., 0., 0., 1.])

class Native:
    def __init__(self):
        self.options = cartesian_options()
        self.sent = []
        self.stops = 0
        self.state = 'idle'
    def prepare_pose(self, target, bias):
        return copy.deepcopy(dict(pose=target, bias=bias))
    def prepare(self, q): raise AssertionError('FK path called')
    def send(self, waypoint):
        self.sent.append(copy.deepcopy(waypoint))
        self.state = 'running'
    def stop(self): self.stops += 1
    def status(self):
        if isinstance(self.state, Exception): raise self.state
        return self.state

def setup():
    clock = Clock()
    backend = FakeBackend(clock)
    backend.name = 'intera'
    backend.cartesian = Native()
    backend.cartesian_capability = dict(available=True, schema=1, direct_pose_50hz=True,
        send_hz_choices=[10,20,50,100])
    core = BridgeCore(backend, clock=clock)
    core.connect()
    arm(core)
    return clock, backend, core

def arm(core, **kw):
    return core.request(dict(type='arm', session=core.session,
        feedback_seq=core.latest['seq'], execution_mode=DIRECT_POSE_MODE,
        cartesian_send_hz=50, **kw))

def target(core, seq=1, **kw):
    msg = dict(type='pose_target', session=core.session, generation=core.generation,
        seq=seq, feedback_seq=core.latest['seq'], pose=copy.deepcopy(POSE),
        frame_id='base', tip_frame='right_hand', source_sample_ns=core.clock(),
        input_deadline_b_ns=core.clock()+150_000_000)
    msg.update(kw)
    return core.request(msg)

def test_exact_50hz_latest_only_no_fk_and_fixed_bias():
    clock, backend, core = setup()
    configuration = core.last_arm_configuration['configuration']
    assert configuration['execution_mode'] == DIRECT_POSE_MODE
    assert configuration['cartesian_options']['send_hz'] == 50
    assert configuration['joint_bias_policy'] == 'fixed_at_arm'
    assert configuration['joint_bias'] == [0.]*7
    target(core, 1)
    r, _ = core.tick()
    assert r['command_kind'] == 'direct_cartesian_pose' and 'q' not in r
    assert not r['native_cartesian']['fk_called']
    for i in range(1,101):
        clock.advance(.001)
        core._refresh()
        target(core, i+1, pose=dict(POSE, p=[.5+i/1000,.1,.3]))
        core.tick()
    assert len(backend.cartesian.sent) == 6
    assert backend.cartesian.sent[-1]['pose']['p'][0] == pytest.approx(.6)
    assert all(x['bias'] == [0.]*7 for x in backend.cartesian.sent)
    assert core.trajectory is None and backend.publish_count == 0

@pytest.mark.parametrize('patch', [dict(frame_id='world'), dict(tip_frame='wrong'),
    dict(pose=dict(p=[float('nan'),0,0], q=[0,0,0,1])),
    dict(pose=dict(p=[.5,0,0], q=[0,0,0,0])),
    dict(pose=dict(p=[.5,-.5,0], q=[0,0,0,1])),
    dict(input_deadline_b_ns=0), dict(input_deadline_b_ns=2_000_000_000),
    dict(gripper=2), dict(source_sample_ns=-1)])
def test_bad_pose_or_timing_stops_without_submission(patch):
    clock, backend, core = setup()
    with pytest.raises((ValueError, RuntimeError)): target(core, **patch)
    assert not core.armed and not backend.cartesian.sent and backend.cartesian.stops == 1

def test_pending_expired_input_never_sent_and_stop_confirmation_required():
    clock, backend, core = setup()
    target(core, input_deadline_b_ns=clock()+5_000_000)
    clock.advance(.006)
    core.tick()
    assert not backend.cartesian.sent and not core.armed
    with pytest.raises(ProtocolError, match='stop_unconfirmed'): arm(core)
    backend.cartesian.state = 'stopped'
    core.tick()
    arm(core)

def test_release_clears_pending_and_old_generation_never_replays():
    clock, backend, core = setup()
    old = core.generation
    target(core)
    core.request(dict(type='hold', session=core.session))
    core.tick()
    assert not backend.cartesian.sent
    assert target(core, 2, generation=old)['accepted'] is False

def test_native_failure_stops_and_no_repeated_send_without_new_input():
    clock, backend, core = setup()
    target(core)
    core.tick()
    for _ in range(4):
        clock.advance(.02)
        core.tick()
    assert len(backend.cartesian.sent) == 1
    backend.cartesian.state = RuntimeError('controller_failed')
    core.tick()
    assert not core.armed and backend.cartesian.stops == 1

def test_old_b_cannot_arm_new_mode_or_select_wrong_rate():
    clock, backend, core = setup()
    core.request(dict(type='hold', session=core.session))
    backend.cartesian.state='stopped'
    core.tick()
    backend.cartesian_capability.pop('direct_pose_50hz')
    with pytest.raises(ProtocolError, match='not_supported'): arm(core)

def test_sdk_direct_pose_uses_cartesian_api_and_never_joint_api(monkeypatch):
    class PoseStamped:
        def __init__(self):
            self.header=Obj(frame_id='')
            self.pose=Obj(position=Obj(x=0,y=0,z=0), orientation=Obj(x=0,y=0,z=0,w=1))
    monkeypatch.setitem(sys.modules, 'geometry_msgs', Obj())
    monkeypatch.setitem(sys.modules, 'geometry_msgs.msg', Obj(PoseStamped=PoseStamped))
    class Waypoint:
        def set_joint_angles(self, **kw): raise AssertionError('FK called')
        def set_cartesian_pose(self, **kw): self.msg=kw
        def to_msg(self): return copy.deepcopy(self.msg)
    traj=Trajectory()
    executor=InteraCartesian(traj, Waypoint(), 'right_hand')
    msg=executor.prepare_pose(POSE, [.1]*7)
    assert msg['pose'].header.frame_id == 'base'
    assert msg['pose'].pose.position.x == .5
    assert msg['joint_angles'] == [.1]*7
    executor.send(msg)
    assert len(traj.sent)==1

def test_all_7177_recorded_inputs_preserved_in_direct_pose_validation(record_property):
    root=Path(__file__).resolve().parent/'fixtures/lab_scene_20261022'
    with gzip.open(root/'events.jsonl.gz', 'rt', encoding='utf-8') as stream:
        events=[json.loads(line) for line in stream]
    inputs=[x for x in events if x.get('reason')=='quest_input']
    assert len(inputs)==7177
    clock, backend, core=setup()
    # Replay every recorded right-hand pose through the new B target path.
    # Historical invalid/unsubmitted samples are counted; this is no physical replay.
    rejected=0
    for seq, event in enumerate(inputs,1):
        clock.advance(.001)
        core._refresh()
        if not core.armed:
            backend.cartesian.state='stopped'
            core.tick()
            arm(core)
        hand=event['packet']['hands']['r']
        try: target(core, seq, pose=hand['pose'])
        except (ValueError, RuntimeError): rejected+=1
        core.tick()
    assert len(inputs)==7177 and len(backend.cartesian.sent)>0
    assert core.accepted + rejected == 7177
    record_property('raw_inputs',7177)
    record_property('accepted',core.accepted)
    record_property('rejected',rejected)
    record_property('submitted_software_only',len(backend.cartesian.sent))


@pytest.mark.parametrize('cause',['reference','control_gap','feedback','input'])
def test_active_native_goal_is_stopped_by_existing_and_input_deadlines(cause):
    clock, backend, core=setup()
    deadline=clock()+(5_000_000 if cause=='input' else 150_000_000)
    target(core,input_deadline_b_ns=deadline)
    core.tick()
    if cause=='reference':
        for _ in range(13):
            clock.advance(.01)
            core.tick()
    elif cause=='control_gap':
        clock.advance(.101)
        core.tick()
    elif cause=='feedback':
        backend.frozen=True
        for _ in range(11):
            clock.advance(.01)
            core.tick()
    else:
        clock.advance(.006)
        core.tick()
    assert not core.armed and backend.cartesian.stops==1
    assert len(backend.cartesian.sent)==1


def test_enabled_feedback_jump_guard_stops_actual_discontinuity():
    clock, backend, core=setup()
    from sawyer_teleop.joint_protection import protection_options
    core.joint_protection=protection_options(True)
    target(core)
    core.tick()
    backend.target=[1.]*7
    clock.advance(.01)
    core.tick()
    assert not core.armed and backend.cartesian.stops==1


def test_client_pose_deadline_subtracts_both_clock_uncertainties(monkeypatch):
    from sawyer_teleop.client import RobotClient
    import sawyer_teleop.client as mod
    client=RobotClient('unused')
    client.snapshot=lambda: None
    client.offset_ns=10_000_000
    client.uncertainty_ns=2_000_000
    client.session='s'
    client.generation=1
    client.hello={'tip_frame':'right_hand'}
    sent=[]
    client.connection=Obj(send=lambda msg,**kw: sent.append(msg))
    monkeypatch.setattr(mod,'ns',lambda: 1_000_000_000)
    # Caller already removed Quest clock uncertainty from the A-side deadline.
    client.pose_target(POSE,1,1_100_000_000,1)
    assert sent[0]['input_deadline_b_ns']==1_088_000_000
    assert sent[0]['type']=='pose_target' and 'q' not in sent[0]


def test_loopback_operator_bypasses_ik_records_pose_and_releases(tmp_path):
    from sawyer_teleop.protocol import ns
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.operator import Operator
    from sawyer_teleop.server import BridgeServer
    from sawyer_teleop.demo import SyntheticQuest, wait_until
    class Quest(SyntheticQuest):
        def poll(self):
            value=super().poll()
            value['mapped_ns']=value['received_ns']
            value['uncertainty_ns']=0
            value['packet']['sample_ns']=value['received_ns']
            return value
    backend=FakeBackend()
    backend.name='intera'
    backend.cartesian=Native()
    backend.cartesian_capability=dict(available=True, schema=1, direct_pose_50hz=True,
        send_hz_choices=[50], options=cartesian_options())
    def no_ik(*args, **kwargs): raise AssertionError('application IK was called')
    backend.solve=no_ik
    server=BridgeServer(backend, control_port=0, ik_port=0).start()
    client=RobotClient('127.0.0.1',server.control_port,server.ik_port,expected_backend='intera',defer_remote_ik=True)
    client.solve=no_ik
    quest=Quest()
    operator=Operator(client,quest,[],tmp_path,hz=120,motion_profile=DIRECT_POSE_MODE)
    try:
        operator.start()
        wait_until(lambda: operator.epoch is not None)
        operator.request('calibrate')
        wait_until(lambda: operator.mapping is not None)
        wait_until(lambda: operator.release_seen)
        quest.grip=1
        try:
            wait_until(lambda: len(backend.cartesian.sent)>=2)
        except TimeoutError:
            pytest.fail(str(operator.view())+' B='+str(server.core.telemetry()))
        assert operator.future is None and operator.following
        assert client.ik_socket is None
        operator.request('start_recording',name='direct',count=1,seconds=20,pause=0)
        wait_until(lambda: operator.episode is not None)
        ep=operator.episode
        wait_until(lambda: len(backend.cartesian.sent)>=7)
        quest.grip=0
        wait_until(lambda: not operator.following and backend.cartesian.stops>=1)
        assert backend.cartesian.stops>=1
        operator._end_recording()
        assert ep.finish(True,{'capture_closed':True}).wait(5)
        events=[json.loads(x) for x in (ep.result/'events.jsonl').read_text().splitlines()]
        submits=[x for x in events if x['reason']=='submitted_pose']
        assert submits and all('q' not in x['receipt'] for x in submits)
        import h5py
        with open_episode(ep.result) as capture:
            assert len(capture['commands'])==0
            assert len(capture['states/ns'])>0
        assert ep.metadata['ik']['mode']=='bypassed'
    finally:
        backend.cartesian.state='stopped'
        operator.close()
        server.close()
