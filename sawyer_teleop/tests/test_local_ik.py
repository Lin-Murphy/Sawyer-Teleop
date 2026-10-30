"""Independent FK/derivative checks, continuity and fail-closed local IK transport."""
import copy
import json
from pathlib import Path
import time

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.kinematics_model import read_kinematics_model, model_digest
from sawyer_teleop.local_ik import LocalIK
from sawyer_teleop.protocol import ProtocolError, JOINTS, encode
from sawyer_teleop.server import BridgeServer
from test_bridge import Clock, target
from test_recorded_path_case import case_module

ROOT = Path(__file__).resolve().parents[1]
URDF = (ROOT/'tests/fixtures/sideways_lift_1_2_11/robot.urdf').read_text()
MODEL = read_kinematics_model(URDF)
HOME = [-.3, -1., .1, 1.7, -.1, .8, -1.6]


def target_pose(q):
    t = case_module.forward([q])[0]
    return {'p': t[:3, 3].tolist(), 'q': Rotation.from_matrix(t[:3, :3]).as_quat().tolist()}


def test_model_export_roundtrip_and_pure_stdlib_b_contract():
    assert MODEL['available'] and MODEL['joints'] == JOINTS
    assert len(encode({'kinematics': MODEL})) < 8192
    assert LocalIK(json.loads(json.dumps(MODEL))).model['sha256'] == MODEL['sha256']
    # Robot collision/visual geometry is not needed or transferred to solve FK/IK.
    assert 'mesh' not in json.dumps(MODEL) and 'gazebo' not in json.dumps(MODEL)
    import subprocess, sys
    result = subprocess.run([sys.executable, '-I', '-S', '-c',
        'import sys;sys.path.insert(0,'+repr(str(ROOT/'src'))+');'
        'from sawyer_teleop.kinematics_model import read_kinematics_model;'
        'assert "numpy" not in sys.modules and "scipy" not in sys.modules'], capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('urdf', ['', '<robot>', URDF.replace('right_j3', 'unexpected_joint'),
    URDF.replace('upper="3.14"', 'upper="nan"'), URDF.replace('type="revolute"', 'type="continuous"')])
def test_invalid_models_unavailable(urdf):
    assert not read_kinematics_model(urdf)['available']


def test_tampered_model_is_not_accepted():
    damaged = copy.deepcopy(MODEL)
    damaged['chain'][0]['xyz'][0] += .01
    with pytest.raises(ValueError, match='model_unavailable_or_invalid'):
        LocalIK(damaged)


def test_fk_and_analytic_jacobian_match_independent_captured_model():
    ik = LocalIK(MODEL)
    rng = np.random.default_rng(125)
    for q in rng.uniform(ik.lower*.8, ik.upper*.8, (40, 7)):
        actual, jac = ik.forward(q.tolist(), True)
        expected = case_module.forward([q])[0]
        np.testing.assert_allclose(actual, expected, atol=2e-12)
        numerical = []
        for j in range(7):
            after = q.copy(); after[j] += 1e-6
            t = case_module.forward([after])[0]
            numerical.append(np.r_[(t[:3, 3]-expected[:3, 3])/1e-6,
                Rotation.from_matrix(t[:3, :3]@expected[:3, :3].T).as_rotvec()/1e-6])
        np.testing.assert_allclose(jac, np.array(numerical).T, atol=6e-7)


def test_full_recording_warm_start_without_remote_reseeding():
    ik = LocalIK(MODEL)
    _, data = case_module.load_case()
    indices = np.r_[0, np.flatnonzero(np.diff(data['source_seq']))+1]
    seed = data['goal_q'][0].tolist()
    for i in indices:
        target = {'p': data['target_p'][i].tolist(), 'q': data['target_quat'][i].tolist()}
        q = ik.solve(target, seed, timeout=.08)
        assert q is not None, i
        independent = case_module.forward([q])[0]
        assert np.linalg.norm(independent[:3, 3]-target['p']) <= 1.01e-5
        assert (Rotation.from_matrix(independent[:3, :3]).inv()*Rotation.from_quat(target['q'])).magnitude() <= 1.01e-4
        assert np.max(abs(np.array(q)-seed)) <= ik.max_seed_distance
        assert np.all(q >= ik.lower) and np.all(q <= ik.upper)
        seed = q


def test_small_multi_axis_targets_and_near_limit_reversals():
    ik = LocalIK(MODEL); rng = np.random.default_rng(126)
    seeds = rng.uniform(ik.lower*.75, ik.upper*.75, (150, 7))
    for j in range(7):
        for edge in (ik.lower[j]+.001, ik.upper[j]-.001):
            q = np.array(HOME); q[j] = edge; seeds = np.vstack((seeds, q))
    for seed in seeds:
        goal = np.clip(seed+rng.uniform(-.01, .01, 7), ik.lower, ik.upper)
        solved = ik.solve(target_pose(goal), seed.tolist(), timeout=.08)
        assert solved is not None, (seed, goal)
        assert max(abs(np.array(solved)-seed)) < .1


def test_impossible_target_timeout_cancellation_and_no_branch_jump():
    ik = LocalIK(MODEL)
    far = target_pose(HOME); far['p'][0] += 10
    start = time.perf_counter()
    assert ik.solve(far, HOME, timeout=.005) is None
    assert time.perf_counter()-start < .08
    assert ik.solve(target_pose(HOME), HOME, cancelled=lambda: True) is None
    invalid = HOME[:]; invalid[6] = 3.3161
    with pytest.raises(ValueError, match='seed_outside_limits'):
        ik.solve(target_pose(HOME), invalid)
    # Same exact target should not cause redundant-joint drift.
    seed = HOME[:]
    for _ in range(100):
        seed = ik.solve(target_pose(HOME), seed)
        np.testing.assert_array_equal(seed, HOME)


class ModelBackend(FakeBackend):
    """Instantaneous software transport fixture with the real FK, no dynamics."""
    kinematics = MODEL
    def __init__(self, clock=None):
        super().__init__(**({'clock': clock} if clock else {}))
        self.q = HOME[:]; self.target = HOME[:]
        self.ik_calls = 0
    def snapshot(self):
        sample = super().snapshot()
        sample['pose'] = target_pose(sample['q'])
        return sample
    def solve(self, *args):
        self.ik_calls += 1
        raise AssertionError('Local IK must never call the remote solver')


def test_local_tcp_needs_no_ik_port_and_keeps_model_binding_and_source_reference():
    backend = ModelBackend(); server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, 1, ik_mode='local')
    try:
        client.connect(); client.arm()
        sample = client.snapshot()['sample']
        goal = HOME[:]; goal[0] += .01
        q = client.solve(target_pose(goal), sample['q'], sample['seq'])
        assert client.ik_socket is None and backend.ik_calls == 0
        client.target(q, sample['seq'])
        deadline = time.monotonic()+.2
        while server.core.submitted < 1 and time.monotonic() < deadline: time.sleep(.001)
        assert server.core.submitted == 1
        assert server.core.active_model_sha256 == MODEL['sha256']
        client.hold(); count = server.core.submitted; time.sleep(.03)
        assert count == server.core.submitted
    finally:
        client.close(); server.close()


def test_missing_model_never_falls_back_to_remote():
    server = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port, ik_mode='local')
    try:
        with pytest.raises(ValueError, match='model_unavailable'):
            client.connect()
        assert client.ik_socket is None and not server.core.armed
    finally:
        client.close(); server.close()


def test_b_rejects_model_mismatch_and_out_of_limit_local_goals():
    clock = Clock(); core = BridgeCore(ModelBackend(clock), clock=clock); core.connect()
    with pytest.raises(ProtocolError, match='kinematic_model_mismatch'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], model_sha256='wrong'))
    assert not core.armed
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], model_sha256=MODEL['sha256']))
    with pytest.raises(ProtocolError, match='joint_model_limit_exceeded'):
        target(core, q=[0., -1., 0., 2., 0., .5, 3.3161])
    assert not core.armed and core.pending is None


def test_b_rejects_interpolated_overshoot_before_publishing():
    clock = Clock(); backend = ModelBackend(clock); core = BridgeCore(backend, clock=clock); core.connect()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                      model_sha256=MODEL['sha256'], trajectory={}))
    target(core, q=HOME)
    core.trajectory.sample = lambda _: ([0., -1., 0., 2., 0., .5, 3.15], [0.]*7, [0.]*7)
    core.trajectory.retarget = lambda *args: None
    receipt, _ = core.tick()
    assert receipt is None and not core.armed and core.reason == 'joint_model_limit_exceeded'


def test_local_worker_result_is_invalid_after_hold_and_cannot_queue_another_solve():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    backend = ModelBackend(); server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, 1, ik_mode='local')
    entered, resume = threading.Event(), threading.Event()
    def delayed(*args, cancelled, **kwargs):
        entered.set(); assert resume.wait(.5)
        assert cancelled()
        return HOME[:]
    try:
        client.connect(); client.arm(); sample = client.snapshot()['sample']
        client.local_ik.solve = delayed
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.solve, target_pose(HOME), HOME, sample['seq'])
            assert entered.wait(.2)
            with pytest.raises(ProtocolError, match='ik_busy'):
                client.solve(target_pose(HOME), HOME, sample['seq'])
            client.hold(); resume.set()
            with pytest.raises(ProtocolError, match='ik_expired'):
                pending.result(.5)
        assert server.core.submitted == 0 and backend.ik_calls == 0
    finally:
        resume.set(); client.close(); server.close()


@pytest.mark.parametrize('axis,amount', [(0,.03),(3,.1)])
def test_endpoint_model_mismatch_prevents_local_arm(axis, amount):
    backend = ModelBackend()
    original = backend.snapshot
    def wrong():
        sample = original()
        if axis == 0:
            sample['pose']['p'][0] += amount
        else:
            sample['pose']['q'] = (Rotation.from_rotvec([amount,0,0])*Rotation.from_quat(sample['pose']['q'])).as_quat().tolist()
        return sample
    backend.snapshot = wrong
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, 1, ik_mode='local')
    try:
        with pytest.raises(ProtocolError, match='feedback_model_mismatch'):
            client.connect()
        assert not server.core.armed and server.core.submitted == 0
    finally:
        client.close(); server.close()


def test_model_includes_tool_transform_and_uses_actual_j6_branch_without_wrapping():
    import xml.etree.ElementTree as ET
    urdf = ET.fromstring(URDF)
    limit = urdf.find("joint[@name='right_j6']/limit")
    limit.set('lower', '-4.7124'); limit.set('upper', '4.7124')
    tool = ET.SubElement(urdf, 'joint', name='validation_tool', type='fixed')
    ET.SubElement(tool, 'parent', link='right_hand'); ET.SubElement(tool, 'child', link='validation_tip')
    ET.SubElement(tool, 'origin', xyz='0.02 -0.03 0.10', rpy='0.1 0.2 -0.3')
    model = read_kinematics_model(ET.tostring(urdf, encoding='unicode'), 'validation_tip')
    ik = LocalIK(model)
    seed = HOME[:]; seed[6] = 3.3161
    offset = np.eye(4); offset[:3, 3] = [.02,-.03,.10]
    offset[:3,:3] = Rotation.from_euler('xyz',[.1,.2,-.3]).as_matrix()
    expected = case_module.forward([seed])[0] @ offset
    np.testing.assert_allclose(ik.forward(seed), expected, atol=2e-12)
    target = {'p':expected[:3,3].tolist(),'q':Rotation.from_matrix(expected[:3,:3]).as_quat().tolist()}
    np.testing.assert_array_equal(ik.solve(target, seed), seed)


def test_real_backend_cannot_bypass_intera_collision_precheck_with_local_model():
    clock = Clock(); backend = ModelBackend(clock); backend.name = 'intera'
    core = BridgeCore(backend, clock=clock)
    hello = core.connect()
    assert hello['kinematics']['available']
    assert not hello['local_ik_targets']
    assert 'self-collision' in hello['local_ik_unavailable_reason']
    with pytest.raises(ProtocolError, match='local_ik_not_supported'):
        core.request(dict(type='arm',session=core.session,feedback_seq=core.latest['seq'],model_sha256=MODEL['sha256']))
    assert not core.armed and core.submitted == 0
    core.request(dict(type='arm',session=core.session,feedback_seq=core.latest['seq']))
    assert core.armed  # Legacy / remote route remains usable.
