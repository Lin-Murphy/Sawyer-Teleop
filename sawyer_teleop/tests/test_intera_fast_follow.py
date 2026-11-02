"""Explicit Real Sawyer profile routing tested with software-only loopback B."""
import time
import pytest
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.operator import Operator
from sawyer_teleop.demo import SyntheticQuest,wait_until
from sawyer_teleop.server import BridgeServer


def test_real_fast_arm_negotiates_joint_limits_with_existing_b(tmp_path):
    backend=FakeBackend();backend.name='intera'
    bridge=BridgeServer(backend,control_port=0,ik_port=0).start()
    quest=SyntheticQuest()
    client=RobotClient('127.0.0.1',bridge.control_port,bridge.ik_port,expected_backend='intera')
    op=Operator(client,quest,[],tmp_path,motion_profile='fast_follow',
        trajectory=dict(profile='coordinated',max_acceleration_rad_s2=[1.5,1.5,3,3,3,3,3]),
        gazebo_max_acceleration_rad_s2=[999]*7)
    try:
        op.start()
        wait_until(lambda: op.epoch is not None)
        op.request('calibrate')
        wait_until(lambda: op.mapping is not None and op.release_seen)
        quest.grip=1
        wait_until(lambda: bridge.core.last_arm_configuration is not None)
        cfg=bridge.core.last_arm_configuration['configuration']
        assert cfg['execution_mode']=='joint'
        assert cfg['trajectory']['max_acceleration_rad_s2']==[3,3,6,6,6,6,6]
        assert cfg['timeouts_ms']==dict(latest_feedback=100,source_reference=120,command=200)
        assert cfg['trajectory']['max_following_error_rad']==1.0
        assert cfg['cartesian_options'] is None
        assert op.robot.ik_mode=='remote'
        quest.grip=0
        wait_until(lambda: not bridge.core.armed)
    finally:
        op.close();bridge.close()


def test_web_retains_fast_for_real_sawyer_and_rejects_fake(tmp_path):
    import json
    from sawyer_teleop.web_session import WebSession
    path=tmp_path/'operator.json'
    path.write_text(json.dumps(dict(connection_backend='intera',robot_host='127.0.0.1',
        motion_profile='standard',data_dir=str(tmp_path/'data'))))
    session=WebSession(path)
    try:
        settings=session._settings(dict(backend='intera',host='127.0.0.1',profile='fast_follow'))
        assert settings['profile']=='fast_follow'
        profiles={x['value']:x for x in session.snapshot('observer')['profiles']}
        assert profiles['fast_follow']['reason'] is None
        assert session._settings(dict(backend='fake',host='127.0.0.1',profile='fast_follow'))['profile']=='standard'
    finally:
        session.close()


def test_real_fast_always_uses_acceleration_limited_trajectory(tmp_path):
    from test_ik_schedule import Robot
    robot=Robot();robot.expected_backend='intera';robot.hello={'backend':'intera'}
    op=Operator(robot,SyntheticQuest(),[],tmp_path,
        trajectory={'enabled':False},motion_profile='fast_follow')
    try:
        op._select_trajectory_options()
        assert op.trajectory_options['enabled'] is True
        assert op.trajectory_options['max_acceleration_rad_s2']==[3.]*7
        assert op.base_trajectory_options['enabled'] is False
    finally:
        op.executor.shutdown(wait=True,cancel_futures=True)
