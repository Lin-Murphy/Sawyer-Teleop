"""Every motion change must retain and pass the user's full laboratory capture."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lab_scene_case', ROOT/'scripts/validate_lab_scene.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture(scope='module')
def capture():
    return check.load_case()


def test_complete_raw_vr_and_recording_are_frozen(capture):
    case, data, _ = capture
    assert case['required_for_every_motion_change']
    assert case['counts'] == dict(events=12098, vr_inputs=7177, submitted=4889, states=2917, unique_goals=1779)
    assert len(data['vr_right_p']) == 7177
    assert set(data['segment']) == {3, 4, 5, 6, 9}
    # Missing or unsuccessful engagements remain in the original event archive.
    assert case['reasons']['engaged'] == 8 and case['reasons']['hold'] == 8


def test_recorded_reference_can_be_reproduced_without_ik(capture):
    case, data, _ = capture
    values = check.replay(data, case['recorded_trajectory'])
    np.testing.assert_array_equal(values[:, 0], data['recorded_q'])


@pytest.mark.parametrize('fast', [False, True], ids=['standard', 'gazebo-fast'])
def test_current_controller_must_pass_lab_scene_reference_gate(capture, fast):
    case, data, meta = capture
    config = json.loads((ROOT/'config/operator.json').read_text())
    options = check.trajectory_options(config['trajectory'])
    if fast:
        options['max_acceleration_rad_s2'] = config['gazebo_max_acceleration_rad_s2']
    values = check.replay(data, options)
    from sawyer_teleop.joint_protection import check_jump, protection_options
    previous, segment = None, None
    for i, row in enumerate(values[:, 0]):
        if data['segment'][i] != segment:
            previous = data['recorded_q'][i].tolist()
            segment = data['segment'][i]
        check_jump(row.tolist(), previous, protection_options(), 'lab_execution_reference')
        previous = row.tolist()
    report = check.evaluate(case, data, meta, values, options)
    assert report['offline_case_passed'], report
    assert report['release_accepted'] is False  # Physical + full VR A/B still required.


def test_cannot_skip_retime_or_invent_samples(capture):
    case, data, meta = capture
    values = np.stack([data['recorded_q'],data['recorded_dq'],data['recorded_ddq']], axis=1)
    with pytest.raises(ValueError, match='Every original sample'):
        check.evaluate(case, data, meta, values[:-1], case['recorded_trajectory'])
    with pytest.raises(ValueError, match='timing'):
        check.evaluate(case, data, meta, values, case['recorded_trajectory'], data['b_ns']+1)
    values[20, 0, 0] = np.nan
    with pytest.raises(ValueError, match='finite'):
        check.evaluate(case, data, meta, values, case['recorded_trajectory'])
