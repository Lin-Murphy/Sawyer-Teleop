"""The real-B Reset target loaded by Start-B-Real.sh is the lab-measured pose, byte-for-value."""
import math
from pathlib import Path

from sawyer_teleop.neutral import NEUTRAL_PARAM, neutral_target, read_neutral_pose
from sawyer_teleop.protocol import JOINTS

ROOT = Path(__file__).resolve().parents[1]
LAB_POSE = ROOT / 'config/lab-neutral-pose.yaml'
# Lab-measured Sawyer pose confirmed 2026-11-07 (rad, right_j0..right_j6).
EXPECTED = ['1.2704287109375', '-1.1166728515625', '-1.3760634765625', '1.06116015625',
            '0.5706650390625', '1.9465263671875', '3.327021484375']
# Non-Gazebo sawyer_base.urdf.xacro limits (sawyer_robot, gazebo=false; J6 +/-4.7124).
REAL_LIMITS = [(-3.0503, 3.0503), (-3.8095, 2.2736), (-3.0426, 3.0426), (-3.0439, 3.0439),
               (-2.9761, 2.9761), (-2.9761, 2.9761), (-4.7124, 4.7124)]


def load_rosparam_lists(path):
    """Minimal reader for this file's fixed `key:` + `  - item` layout (no PyYAML on A)."""
    params, key = {}, None
    for line in path.read_text(encoding='ascii').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.startswith('  - '):
            params[key].append(line[4:].strip())
        else:
            assert line.endswith(':'), line
            key = line[:-1]
            params[key] = []
    return params


def test_lab_reset_pose_matches_measured_angles_exactly():
    params = load_rosparam_lists(LAB_POSE)
    assert params['/named_poses/right/joint_names'] == JOINTS
    assert params[NEUTRAL_PARAM] == EXPECTED  # Original text precision, no rounding.
    angles = [float(value) for value in params[NEUTRAL_PARAM]]
    assert all(math.isfinite(value) for value in angles)
    capability = read_neutral_pose({NEUTRAL_PARAM: angles,
                                    '/named_poses/right/joint_names': JOINTS}.get)
    assert neutral_target(capability) == angles  # Not wrapped, clamped or reordered.


def test_lab_reset_pose_is_inside_real_sawyer_model_limits():
    angles = [float(value) for value in load_rosparam_lists(LAB_POSE)[NEUTRAL_PARAM]]
    for name, angle, (lower, upper) in zip(JOINTS, angles, REAL_LIMITS):
        assert lower <= angle <= upper, name
    assert angles[6] > math.pi  # Valid only for the real +/-4.7124 J6; never wrapped here.
