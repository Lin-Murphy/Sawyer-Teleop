"""Validate the evidence and gate; these tests do not mark the controller fixed."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/validate_recorded_path.py'
spec = importlib.util.spec_from_file_location('recorded_path_case', PATH)
case_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case_module)


@pytest.fixture(scope='module')
def recorded():
    return case_module.load_case()


def test_captured_model_agrees_with_recorded_ik_and_feedback(recorded):
    case, data = recorded
    goals = case_module.forward(data['goal_q'])
    feedback = case_module.forward(data['feedback_q'])
    assert np.max(np.linalg.norm(goals[:, :3, 3] - data['target_p'], axis=1)) < .00002
    rotation = case_module.Rotation.from_matrix(goals[:, :3, :3]).inv() * case_module.Rotation.from_quat(data['target_quat'])
    assert np.max(rotation.magnitude()) < .00002
    # Joint/endpoint feedback are asynchronous; this is the measured consistency bound.
    assert np.max(np.linalg.norm(feedback[:, :3, 3] - data['feedback_p'], axis=1)) < .008


def test_frozen_failure_rejected_despite_valid_joint_limits(recorded):
    case, data = recorded
    report = case_module.evaluate(case, data, data['recorded_q'], data['recorded_dq'],
                                  data['recorded_ddq'], case['recorded_trajectory'])
    checks = {x['name']: x for x in report['checks']}
    assert checks['reported_velocity']['passed'] and checks['reported_acceleration']['passed']
    assert not checks['unrequested_rise']['passed'] and not checks['unrequested_dip']['passed']
    assert checks['unrequested_rise']['observed'] > .14
    assert not report['offline_case_passed'] and not report['release_accepted']


def test_skipping_timing_changes_or_nonfinite_values_cannot_pass(recorded):
    case, data = recorded
    q, dq, ddq = data['recorded_q'], data['recorded_dq'], data['recorded_ddq']
    with pytest.raises(ValueError, match='Every original sample'):
        case_module.evaluate(case, data, q[:-1], dq[:-1], ddq[:-1], case['recorded_trajectory'])
    with pytest.raises(ValueError, match='timing'):
        case_module.evaluate(case, data, q, dq, ddq, case['recorded_trajectory'], data['b_ns'] + 1)
    invalid = q.copy(); invalid[100, 0] = np.nan
    with pytest.raises(ValueError, match='finite'):
        case_module.evaluate(case, data, invalid, dq, ddq, case['recorded_trajectory'])


def test_direct_goal_jumps_cannot_hide_behind_zero_derivative_labels(recorded):
    case, data = recorded
    zeros = np.zeros_like(data['goal_q'])
    report = case_module.evaluate(case, data, data['goal_q'], zeros, zeros, case['recorded_trajectory'])
    checks = {x['name']: x for x in report['checks']}
    assert checks['unrequested_rise']['passed'] and checks['unrequested_dip']['passed']
    assert not checks['position_sample_velocity']['passed']
    assert not checks['position_sample_acceleration']['passed']
    assert not report['offline_case_passed']
