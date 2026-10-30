import time
import numpy as np
import pytest
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.policy_env import JointTargetEnvironment
from sawyer_teleop.server import BridgeServer

ENCODING = {"kind":"affine_dataset_encoding", "center":[.1]*7,"scale":[.2]*7}


class SimulatedTransportDouble(FakeBackend):
    """Test transport only. Physical acceptance uses the real Gazebo container."""
    name = "gazebo"
    def description(self):
        return {"simulation":True,"test_only":True}


@pytest.fixture
def bridge():
    server = BridgeServer(SimulatedTransportDouble(), control_port=0, ik_port=0).start()
    yield server
    server.close()


def environment(bridge, **kwargs):
    return JointTargetEnvironment(ENCODING,[0]*7,control_port=bridge.control_port,
        ik_port=bridge.ik_port,reset_seconds=.2,**kwargs)


def test_decoder_uses_model_range_and_rejects_nonfinite_or_wrong_dimensions():
    env = JointTargetEnvironment(ENCODING,[0]*7)
    assert env.decode([1]*7) == pytest.approx([.3]*7)
    assert env.decode(np.ones(7,dtype=np.float32)) == pytest.approx([.3]*7)
    assert env.decode([-1]*7) == pytest.approx([-.1]*7)
    for action in ([0]*6,[float("nan")]*7,[float("inf")]*7,[1.1]*7):
        with pytest.raises(ValueError):
            env.decode(action)
    with pytest.raises(ValueError):
        JointTargetEnvironment(dict(ENCODING,scale=[0]*7),[0]*7)
    with pytest.raises(ValueError):
        JointTargetEnvironment(ENCODING,[0]*7,joint_names=["wrong"]*7)


def test_reset_step_horizon_hold_and_reconnect(bridge):
    env = environment(bridge,horizon=2)
    try:
        obs = env.reset()
        assert {k:list(v.shape) for k,v in obs.items()} == {
            "joint_pos":[7],"joint_vel":[7],"eef_pos":[3],"eef_quat":[4]}
        session = env.robot.session
        _,reward,done,info = env.step([0]*7)
        assert reward == 0 and not done and info["task_success"] is None
        obs,_,done,info = env.step([.5]*7)
        assert done and info["truncated"] and not bridge.core.armed
        assert np.allclose(obs["joint_pos"],[.2]*7)
        with pytest.raises(RuntimeError,match="reset_required"):
            env.step([0]*7)
        env.reset()
        assert env.robot.session != session
    finally:
        env.close()
    assert not bridge.core.armed


def test_timeout_does_not_rearm_or_replay_policy(bridge):
    env = environment(bridge)
    try:
        env.reset()
        time.sleep(.25)
        before = bridge.core.submitted
        with pytest.raises(RuntimeError,match="disarmed"):
            env.step([0]*7)
        assert env.done and env.robot is None and bridge.core.submitted == before
    finally:
        env.close()


def test_stale_feedback_stops_policy(bridge):
    env = environment(bridge)
    try:
        env.reset()
        bridge.core.backend.frozen = True
        time.sleep(.12)
        before = bridge.core.submitted
        with pytest.raises(ConnectionError):
            env.step([0]*7)
        assert env.done and bridge.core.submitted == before
    finally:
        env.close()


def test_fake_or_hardware_backend_rejected_before_any_policy_target():
    bridge = BridgeServer(FakeBackend(), control_port=0,ik_port=0).start()
    env = environment(bridge)
    try:
        with pytest.raises(ValueError,match="requires_gazebo"):
            env.reset()
        assert bridge.core.backend.publish_count == 0
    finally:
        env.close()
        bridge.close()


def test_own_reconnect_tolerates_early_socket_reset(bridge, monkeypatch):
    from sawyer_teleop.client import RobotClient
    env = environment(bridge)
    try:
        env.reset()
        connect = RobotClient.connect
        calls = []
        def closing_peer(client):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError("old control socket still closing")
            return connect(client)
        monkeypatch.setattr(RobotClient, "connect", closing_peer)
        env.reset()
        assert len(calls) >= 2 and not env.done
    finally:
        env.close()
