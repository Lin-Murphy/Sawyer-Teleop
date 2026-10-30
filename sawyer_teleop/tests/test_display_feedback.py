"""Read-only Web posture: actual feedback, model FK and expiry; no desktop UI."""
import copy
from pathlib import Path
from types import SimpleNamespace
import h5py
import numpy as np
import pytest
from sawyer_teleop.display_feedback import DisplayChain, Posture, feedback_message
from sawyer_teleop.kinematics_model import read_kinematics_model, model_digest
from test_recorded_path_case import case_module

ROOT = Path(__file__).resolve().parents[1]
MODEL = read_kinematics_model((ROOT/'tests/fixtures/sideways_lift_1_2_11/robot.urdf').read_text())
Q = [-.3, -1., .1, 1.7, -.1, .8, -1.6]


def packet(**changes):
    return dict(dict(session='session-one', model=MODEL, q=Q,
                     expires_ns=1000, backend='gazebo', source_seq=12), **changes)


def test_display_matches_independent_urdf_fk_for_all_recorded_actual_joints():
    chain = DisplayChain(MODEL)
    with h5py.File(ROOT/'tests/fixtures/lab_scene_20261022/raw.hdf5') as f:
        lab = f['states/q'][:]
    with np.load(ROOT/'tests/fixtures/sideways_lift_1_2_11/samples.npz') as f:
        old = f['feedback_q'].copy()
    assert lab.shape == (2917, 7) and old.shape == (2331, 7)
    samples = np.concatenate((lab, old))
    expected = case_module.forward(samples)
    actual = np.array([chain.frames(q.tolist())[-1] for q in samples])
    np.testing.assert_allclose(actual, expected, atol=3e-12)


def test_display_does_not_use_targets_or_compute_ik():
    class Robot:
        hello = dict(session='real-session', backend='intera', kinematics=MODEL)
        feedback_timeout = .1
        def snapshot(self):
            return dict(sample={'q': Q, 'seq': 2}, target={'q': [2.]*7},
                        age_s=.04, a_received_ns=1_000_000_000)
    message = feedback_message(SimpleNamespace(robot=Robot()))
    assert message['q'] == Q and message['expires_ns'] == 1_060_000_000
    view = Posture()
    view.accept(message, now=1_010_000_000)
    assert view.fresh(1_059_999_999) and not view.fresh(1_060_000_000)
    assert view.q == Q


def test_stale_or_delayed_packets_freeze_last_valid_pose_without_renewal():
    view = Posture()
    view.accept(packet(), now=900)
    original = view.frames.copy()
    view.accept(packet(q=[1.]*7, expires_ns=999), now=1000)
    assert not view.fresh(1000)
    np.testing.assert_array_equal(view.frames, original)
    view.accept(packet(q=None, status='feedback_stale'), now=1100)
    np.testing.assert_array_equal(view.frames, original)
    assert not view.fresh(1100)


def test_disconnection_and_new_session_clear_old_robot_pose():
    view = Posture()
    view.accept(packet(), now=900)
    view.accept(packet(session='new-session', q=None), now=950)
    assert view.frames is None
    view.accept(packet(), now=950)
    view.accept({'session': None, 'status': 'Not connected'}, now=960)
    assert view.frames is None and view.chain is None


@pytest.mark.parametrize('damage', ['missing', 'hash', 'axis', 'order', 'nonfinite'])
def test_missing_or_mismatched_model_never_uses_a_default_robot(damage):
    model = copy.deepcopy(MODEL)
    if damage == 'missing':
        model = {'available': False}
    elif damage == 'hash':
        model['chain'][0]['xyz'][0] += .1
    else:
        moving = [j for j in model['chain'] if j['type'] == 'revolute']
        if damage == 'axis': moving[0]['axis'] = [0., 0., 0.]
        if damage == 'order': moving[0]['name'] = 'other'
        if damage == 'nonfinite': model['chain'][0]['xyz'] = [None, 0., 0.]
        model['sha256'] = model_digest(model)
    view = Posture()
    view.accept(packet(model=model), now=900)
    assert view.frames is None and not view.fresh(900)


def test_model_change_replaces_geometry_and_invalid_feedback_cannot_animate():
    view = Posture()
    view.accept(packet(), now=900)
    modified = copy.deepcopy(MODEL)
    modified['chain'][0]['xyz'][0] += .1
    modified['sha256'] = model_digest(modified)
    original = view.frames.copy()
    view.accept(packet(model=modified), now=950)
    assert view.frames[-1, 0, 3] == pytest.approx(original[-1, 0, 3]+.1)
    view.accept(packet(q=[float('nan')]*7), now=950)
    assert view.frames is None


def test_snapshot_expiry_does_not_expose_targets():
    class Robot:
        hello = dict(session='one', kinematics=MODEL)
        def snapshot(self): raise ConnectionError('feedback_stale')
    message = feedback_message(SimpleNamespace(robot=Robot()))
    assert 'q' not in message and 'feedback_stale' in message['status']
    assert feedback_message(None)['session'] is None


