"""Known SDK model mismatch: equivalent pose, original bounds, no generic wrap."""
import copy
import json
import math
import runpy
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from sawyer_teleop.neutral import NEUTRAL_PARAM, gazebo_neutral_pose, read_neutral_pose
from sawyer_teleop.trajectory import JointTrajectory
from test_recorded_path_case import case_module

ROOT = Path(__file__).resolve().parents[1]
URDF = (ROOT / 'tests/fixtures/sideways_lift_1_2_11/robot.urdf').read_text()
OFFICIAL = [0.55823046875, -0.38636328125, -1.474220703125, 1.7777822265625, 1.219943359375, 1.4652578125, 3.3439736328125]


def capability(q=None):
    return read_neutral_pose({NEUTRAL_PARAM: OFFICIAL if q is None else q}.get)


def model(**attributes):
    root = ET.fromstring(URDF)
    joint = root.find("joint[@name='right_j6']")
    if 'type' in attributes:
        joint.set('type', attributes.pop('type'))
    for name, value in attributes.items():
        joint.find('limit').set(name, value)
    return ET.tostring(root, encoding='unicode')


@pytest.mark.parametrize('q', [[0., -1.18, 0., 2.18, 0., .57, 3.3161], OFFICIAL])
def test_scene_initialization_and_reset_share_validated_destination(q):
    initialize = runpy.run_path(str(ROOT/'scripts/initialize_gazebo_scene.py'))['initial_arm_pose']
    params = {NEUTRAL_PARAM: q[:], '/robot_description': URDF}
    before = copy.deepcopy(params)
    home = initialize(params.get)
    reset = gazebo_neutral_pose(read_neutral_pose(params.get), URDF)
    assert list(home.values()) == reset['q']
    assert home['right_j6'] == pytest.approx(q[6] - 2*math.pi)
    assert params == before
    params['/robot_description'] = ''
    with pytest.raises(ValueError):
        initialize(params.get)


def test_scene_initialization_waits_for_late_spawner_pose():
    # 2026-10-29: configuring before spawn_model's delayed -J call left J6 at +3.207.
    module = runpy.run_path(str(ROOT/'scripts/initialize_gazebo_scene.py'))
    now, calls = [0.], []
    names = iter([['/gazebo', '/urdf_spawner'], ['/urdf_spawner'], ['/gazebo']])
    def node_names():
        calls.append(now[0])
        return next(names)
    module['wait_for_spawner'](node_names, sleep=lambda s: now.__setitem__(0, now[0]+s), clock=lambda: now[0])
    assert len(calls) == 3
    with pytest.raises(RuntimeError, match='spawner did not finish'):
        module['wait_for_spawner'](lambda: ['/urdf_spawner'], timeout=1,
                                   sleep=lambda s: now.__setitem__(0, now[0]+s), clock=lambda: now[0])


@pytest.mark.parametrize('j6', [3.3161, 3.3439736328125])
def test_equivalent_gazebo_neutral_preserves_full_tool_transform_and_inputs(j6):
    original = capability([*OFFICIAL[:6], j6])
    before = copy.deepcopy(original)
    result = gazebo_neutral_pose(original, URDF)
    assert result['available'] and result['q'][6] == pytest.approx(j6-2*math.pi)
    assert result['q'][:6] == OFFICIAL[:6]
    assert original == before and result['source_q'] == before['q']
    assert result['source'] == NEUTRAL_PARAM
    transforms = case_module.forward([before['q'], result['q']])
    np.testing.assert_allclose(transforms[0], transforms[1], atol=1e-12, rtol=0)


def test_non_gazebo_model_and_already_valid_pose_never_wrap():
    original = capability()
    assert gazebo_neutral_pose(original, model(lower='-4.7124', upper='4.7124')) == original
    valid = capability([*OFFICIAL[:6], 2.9])
    assert gazebo_neutral_pose(valid, URDF) == valid


@pytest.mark.parametrize('urdf', ['', '<robot>', model(type='continuous'),
    model(type='prismatic'), model(lower='-3.0'), model(upper='3.0'),
    model(upper='nan'), model(lower='inf'), '<robot/>'])
def test_unknown_or_invalid_models_fail_closed(urdf):
    result = gazebo_neutral_pose(capability(), urdf)
    assert not result['available'] and 'q' not in result


@pytest.mark.parametrize('change', ['other_joint', 'other_pose', 'other_source', 'unavailable'])
def test_no_silent_repair_of_other_mismatches(change):
    original = capability()
    if change == 'other_joint': original['q'][0] = 10.
    if change == 'other_pose': original['q'][6] = 3.4
    if change == 'other_source': original['source'] = '/custom/neutral'
    if change == 'unavailable': original = {'available': False, 'reason': 'missing'}
    result = gazebo_neutral_pose(original, URDF)
    assert not result['available'] and 'q' not in result


@pytest.mark.parametrize('j6', [-3.13, -2., 0., 2., 3.13])
@pytest.mark.parametrize('neutral_j6', [3.3161, 3.3439736328125])
def test_reset_curve_stays_inside_original_limits_without_boundary_shortcut(j6, neutral_j6):
    options = json.loads((ROOT/'config/operator.json').read_text())['trajectory']
    stream = JointTrajectory(dict(options, profile='cubic'))
    goal = gazebo_neutral_pose(capability([*OFFICIAL[:6], neutral_j6]), URDF)['q']
    start = [.2, -1., -.2, 2., .15, .7, j6]
    stream.reset(start, 0)
    stream.retarget(goal, 0)
    samples = np.array([stream.sample(int(t*1e9)) for t in np.linspace(0, stream.duration+.01, 1001)])
    q, dq, ddq = samples.transpose(1, 0, 2)
    assert np.all(q >= np.minimum(start, goal)-1e-10)
    assert np.all(q <= np.maximum(start, goal)+1e-10)
    assert np.all(abs(dq) <= np.array(options['max_velocity_rad_s'])+1e-9)
    assert np.all(abs(ddq) <= np.array(options['max_acceleration_rad_s2'])+1e-9)
    assert stream.duration < 15
    np.testing.assert_allclose(q[-1], goal, atol=1e-12)
    if j6 > 3:
        assert np.all(np.diff(q[:, 6]) <= 1e-10)  # Full bounded return, never through +pi.
