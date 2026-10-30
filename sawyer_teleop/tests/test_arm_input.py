"""Arm confirmation is a blocking boundary, never a new input lifetime."""
import copy

import pytest

from test_ik_schedule import rig, finish


def prepare(rig):
    clock, robot, quest, op = rig()
    finish(op, clock)
    quest.grip = 0
    op.tick()
    clock[0] += 10_000_000
    quest.stamp += 10_000_000
    quest.grip = 1
    return clock, robot, quest, op


def test_new_input_after_arm_is_origin_and_first_ik_input(rig):
    clock, robot, quest, op = prepare(rig)
    old_poll, old_arm = quest.poll, robot.arm

    def arm(**kw):
        assert op.arming and not op.following
        clock[0] += 80_000_000
        quest.stamp += 80_000_000
        old_arm(**kw)

    def poll():
        value = old_poll()
        value['uncertainty_ns'] = 2_000_000
        value['transform'] = value['transform'].copy()
        value['transform'][0, 3] = clock[0]/1e9
        return value

    quest.poll, robot.arm = poll, arm
    op.tick()
    assert op.following
    assert op.future_info['timing']['input_mapped_ns'] == clock[0]
    assert op.engagement['input']['mapped_ns'] == clock[0]
    assert op.vr_origin[0, 3] == clock[0]/1e9
    assert op.future_info['target']['p'] == robot.snapshot()['sample']['pose']['p']
    clock[0] += 80_000_000
    finish(op, clock)
    op.tick()
    assert len(robot.targets) == 1 and op.following


@pytest.mark.parametrize('fault', ['no_new_input', 'released', 'tracking', 'epoch', 'future', 'stop', 'arm_error'])
def test_post_arm_validation_holds_without_any_ik_or_target(rig, fault):
    clock, robot, quest, op = prepare(rig)
    jobs = len(op.executor.jobs)
    before = copy.deepcopy(quest.poll())
    old_arm = robot.arm

    def arm(**kw):
        assert op.arming and not op.following
        old_arm(**kw)
        clock[0] += 210_000_000  # Past the 200 ms input limit.
        value = copy.deepcopy(before)
        value.update(mapped_ns=clock[0], received_ns=clock[0], uncertainty_ns=0)
        if fault == 'no_new_input':
            value = before
        elif fault == 'released':
            value['grip'] = 0
        elif fault == 'tracking':
            value.update(valid=False, reason='controller_tracking_lost')
        elif fault == 'epoch':
            value['epoch'] = ('changed', 1, 1, 1)
        elif fault == 'future':
            value['mapped_ns'] += 10_000_001
        elif fault == 'stop':
            op.stop.set()
        quest.poll = lambda: value
        if fault == 'arm_error':
            raise TimeoutError('arm_feedback_timeout')

    robot.arm = arm
    op.tick()
    assert not op.following and not robot.armed and robot.holds
    assert not robot.targets and len(op.executor.jobs) == jobs
    diagnostics = op.view()['last_pause_diagnostics']
    assert diagnostics['arm_wait_ms'] == 210
    if fault == 'no_new_input':
        assert diagnostics['input']['expired'] is True
    if fault == 'future':
        assert diagnostics['input']['future'] is True
    if fault == 'epoch':
        assert op.mapping is None and not op.view()['calibrated']


def test_old_ik_is_rejected_with_structured_timings_even_with_new_input(rig):
    clock, robot, quest, op = rig()
    op.future_info['timing'].update(input_mapped_ns=clock[0]-195_000_000,
                                  input_uncertainty_ns=2_000_000)
    clock[0] += 10_000_000
    finish(op, clock)
    clock[0] += 5_000_000
    op.tick()
    pause = op.view()['last_pause_diagnostics']
    assert not robot.targets and not op.following
    assert pause['reason'] == 'ik_input_expired'
    assert pause['ik_input']['age_ms'] == 210
    assert pause['ik_input']['uncertainty_ms'] == 2
    assert pause['ik_input']['expired'] and not pause['ik_input']['future']
    assert pause['input']['age_ms'] == 0
    assert pause['ik_queue_wait_ms'] == 0
    assert pause['ik_call_ms'] == 10
    assert pause['ik_result_wait_ms'] == 5
    quest.valid = False
    op.tick()
    assert op.view()['last_pause_diagnostics'] == pause
