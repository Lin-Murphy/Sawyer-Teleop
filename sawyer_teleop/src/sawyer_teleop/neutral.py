"""Official named-pose lookup, performed at B startup rather than in its loop."""
from .protocol import JOINTS, vector

NEUTRAL_PARAM = '/named_poses/right/poses/neutral'
NEUTRAL_TOLERANCE_RAD = 0.008726646  # Intera settings.JOINT_ANGLE_TOLERANCE.
NEUTRAL_TIMEOUT_S = 15.0  # Intera Limb.move_to_neutral default.


def read_neutral_pose(get_param):
    result = {'available': False, 'source': NEUTRAL_PARAM}
    try:
        angles = get_param(NEUTRAL_PARAM, None)
        if angles is None:
            raise ValueError('Official neutral pose is missing on B; load named_poses and restart B.')
        angles = vector(angles, 7, 'official neutral joint angles')
        names = get_param('/named_poses/right/joint_names', JOINTS)
        if not isinstance(names, (list, tuple)) or len(names) != 7 or set(names) != set(JOINTS):
            raise ValueError('Official neutral pose has invalid joint names on B.')
        ordered = dict(zip(names, angles))
        result.update(available=True, joint_names=list(JOINTS), q=[ordered[n] for n in JOINTS])
    except Exception as exc:
        result['reason'] = str(exc)
    return result


def neutral_target(capability):
    if not isinstance(capability, dict) or capability.get('available') is not True:
        reason = capability.get('reason') if isinstance(capability, dict) else None
        raise ValueError(reason or 'B does not provide an official neutral pose; update B and reconnect.')
    if capability.get('joint_names') != JOINTS:
        raise ValueError('B neutral pose joint order does not match the robot.')
    return vector(capability.get('q'), 7, 'official neutral joint angles')


def validate_neutral_model(capability, urdf):
    """Reject an unavailable model/pose combination; never wrap or clamp joints."""
    if not capability.get('available'):
        return capability
    import math
    import xml.etree.ElementTree as ET
    try:
        joints = {item.get('name'): item for item in ET.fromstring(urdf).findall('joint')}
        for name, angle in zip(JOINTS, neutral_target(capability)):
            limit = joints[name].find('limit')
            lower, upper = float(limit.get('lower')), float(limit.get('upper'))
            if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
                raise ValueError('Invalid model limits for '+name)
            if not lower <= angle <= upper:
                raise ValueError('Official neutral '+name+'='+format(angle,'.4f')+
                                 ' rad is outside Gazebo model limits ['+format(lower,'.4f')+', '+
                                 format(upper,'.4f')+']; Reset arm is unavailable. Align the model and named pose.')
    except Exception as exc:
        return {'available':False, 'source':capability.get('source'),
                'reason':str(exc) or 'Gazebo neutral model limits are unavailable.'}
    return capability


def gazebo_neutral_pose(capability, urdf):
    """Adapt only the SDK's known J6 neutral/model mismatch in simulation.

    sawyer_robot d1ef3d0: gazebo=true gives J6 [-3.14, 3.14], while the
    shared named pose uses SDK 3.3161 or lab-confirmed 3.3439736328125
    (both valid for the non-Gazebo +/-4.7124 model).
    A single full turn gives the identical link/tool transform inside the
    original simulation limits. This is a fixed reset destination, NOT a
    shortest-path rule or angle wrapping for streamed teleoperation.
    """
    checked = validate_neutral_model(capability, urdf)
    if checked.get('available') or not capability.get('available'):
        return checked
    import math
    import xml.etree.ElementTree as ET
    try:
        q = neutral_target(capability)
        joint = ET.fromstring(urdf).find("joint[@name='right_j6']")
        limit = joint.find('limit')
        # Fail closed for unknown poses/models. Do not silently repair other
        # out-of-range joints, clamp a pose, or change the official ROS params.
        if (capability.get('source') != NEUTRAL_PARAM
                or joint.get('type') != 'revolute'
                or not any(abs(q[6] - known) <= 1e-9
                           for known in (3.3161, 3.3439736328125))
                or abs(float(limit.get('lower')) + 3.14) > 1e-9
                or abs(float(limit.get('upper')) - 3.14) > 1e-9):
            return checked
        adapted = dict(capability, q=q[:], source_q=q[:],
                       adaptation='gazebo_j6_equivalent_minus_one_turn')
        adapted['q'][6] -= 2 * math.pi
        return validate_neutral_model(adapted, urdf)
    except (ValueError, TypeError, AttributeError, ET.ParseError):
        return checked
