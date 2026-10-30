"""Direct native Cartesian goals: no application IK/FK, one latest slot, 50 Hz.

Called under BridgeCore's lock; its existing STOP acknowledgement and feedback
watchdogs apply. Joint bias is captured by B at arm and is never an action label.
"""
from .protocol import ProtocolError, integer, pose
from .joint_protection import check_jump
from .a_timing import INPUT_MAX_AGE_S

DIRECT_POSE_MODE = 'intera_pose_50hz'
PERIOD_NS = 20_000_000
# Same lifetime A uses to build input_deadline; a smaller B value rejects valid deadlines.
INPUT_MAX_AGE_NS = int(INPUT_MAX_AGE_S*1e9)


def valid_deadline(core, msg):
    deadline = integer(msg.get('input_deadline_b_ns'), 'input deadline')
    if not 0 < deadline-core.clock() <= INPUT_MAX_AGE_NS:
        raise ProtocolError('direct_pose_input_expired_or_invalid_deadline')


def accept_pose(core, msg):
    if core.execution_mode != DIRECT_POSE_MODE:
        raise ProtocolError('pose_target_requires_direct_pose_session')
    seq = integer(msg.get('seq'), 'command seq')
    if msg.get('generation') != core.generation or not core.armed:
        core.rejected += 1
        return dict(accepted=False, reason='not_armed', seq=seq)
    if seq <= core.last_seq:
        core.rejected += 1
        return dict(accepted=False, reason='old_command', seq=seq)
    try:
        core._refresh()
        core._reference(msg.get('feedback_seq'))
        valid_deadline(core, msg)
        target = pose(msg.get('pose'))
        if msg.get('frame_id') != 'base' or msg.get('tip_frame') != core.backend.tip_frame:
            raise ProtocolError('direct_pose_frame_mismatch')
        # This SDK mistakes component-sum zero for an absent pose and runs FK.
        # Reject that documented SDK limitation instead of silently changing path.
        if not sum(target['p']) or not sum(target['q']):
            raise ProtocolError('direct_pose_sdk_zero_sum_pose')
        integer(msg.get('source_sample_ns'), 'source sample')
        gripper = msg.get('gripper')
        if gripper is not None:
            if type(gripper) is not int or gripper not in (-1, 1):
                raise ProtocolError('invalid_gripper_command')
            if not callable(getattr(core.backend, 'publish_gripper', None)):
                raise ProtocolError('gripper_unsupported')
    except (ValueError, RuntimeError) as exc:
        core.rejected += 1
        core._hold('direct_pose_rejected', target=msg, details={'native_error': str(exc)})
        raise
    core.last_seq = seq
    core.pending = dict(msg, pose=target, received_ns=core.clock())
    core.last_target = dict(seq=seq, pose=target, received_ns=core.clock(), command_kind='direct_cartesian_pose')
    core.accepted += 1
    return None


def pose_tick(core, now):
    msg = core.pending or core.active_target
    try:
        status = core.cartesian.status() if core.active_target is not None else 'idle'
        check_jump(core.latest['q'], core.direct_feedback_anchor, core.joint_protection, 'B_direct_feedback')
        core.direct_feedback_anchor = list(core.latest['q'])
        if core.active_target is not None:
            core._reference(core.active_target['feedback_seq'])
            valid_deadline(core, core.active_target)
        # Minimum separation, never round early or catch up after delays.
        if core.pending is None or now < core.next_native_ns:
            return None
        msg, core.pending = core.pending, None
        core._reference(msg['feedback_seq'])
        valid_deadline(core, msg)
        if core.clock()-msg['received_ns'] > core.command_timeout_ns:
            raise ProtocolError('command_timeout')
        waypoint = core.cartesian.prepare_pose(msg['pose'], core.direct_bias)
        # Preparation is local message construction; still revalidate at use.
        core._refresh()
        core._reference(msg['feedback_seq'])
        valid_deadline(core, msg)
        if not core._submit_gripper(msg):
            return None
        reference = core._reference(msg['feedback_seq'])
        valid_deadline(core, msg)
        core.cartesian.send(waypoint)
        submitted = core.clock()
        interval = ((submitted-core.last_native_submit_ns)/1e6
                    if core.last_native_submit_ns is not None else None)
        core.last_native_submit_ns = submitted
        core.next_native_ns = submitted + PERIOD_NS
        core.active_target = msg
        core.last_command_ns = msg['received_ns']
        core.submitted_seq = msg['seq']
        core.submitted += 1
        core.submission_rate.mark()
        core.last_submission = dict(seq=msg['seq'], pose=msg['pose'],
            command_kind='direct_cartesian_pose', submitted_ns=submitted)
        return dict(type='submitted', session=core.session, seq=msg['seq'],
            generation=core.generation, feedback_seq=msg['feedback_seq'], b_ns=submitted,
            command_kind='direct_cartesian_pose', pose=msg['pose'],
            joint_bias=list(core.direct_bias), gripper=msg.get('gripper'),
            native_cartesian=dict(send_hz=50, submit_interval_ms=interval,
                reference_age_ms=(submitted-reference['captured_ns'])/1e6,
                input_remaining_ms=(msg['input_deadline_b_ns']-submitted)/1e6,
                status_before_send=status, ik_called=False, fk_called=False))
    except Exception as exc:
        core._hold('direct_pose_failed', target=msg,
                   details=dict(getattr(exc, 'details', {}), native_error=str(exc)))
        return None
