"""Legacy responsive profile regression, using the configured joint limits."""
import json
import math
from pathlib import Path
import random

import numpy as np
import pytest

from sawyer_teleop.trajectory import ResponsiveJointTrajectory
from test_trajectory import make_core
from test_bridge import target


def options():
    return dict(json.loads((Path(__file__).resolve().parents[1]/'config/operator.json').read_text())['trajectory'], profile='responsive')


def test_shipping_limits_stationary_steps_reach_without_reference_overshoot():
    opts=options();v=np.array(opts['max_velocity_rad_s']);a=np.array(opts['max_acceleration_rad_s2'])
    for direction in (1,-1):
        stream=ResponsiveJointTrajectory(opts);stream.reset([0.]*7,0)
        goal=np.array([.7,.6,.8,.9,1.1,1.2,1.3])*direction
        stream.retarget(goal.tolist(),0)
        for stamp in range(0,4_000_000_001,2_000_000):
            q,dq,ddq=map(np.array,stream.sample(stamp))
            assert np.all(direction*q>=-1e-10)
            assert np.all(direction*(q-goal)<=1e-10)
            assert np.all(abs(dq)<=v+1e-10) and np.all(abs(ddq)<=a+1e-10)
        assert q==pytest.approx(goal) and dq==pytest.approx([0.]*7)


def test_shipping_full_speed_reversal_has_bounded_braking_distance():
    opts=options();v=np.array(opts['max_velocity_rad_s']);a=np.array(opts['max_acceleration_rad_s2'])
    stream=ResponsiveJointTrajectory(opts);stream.reset([0.]*7,0);stream.retarget([2.]*7,0)
    stamp=700_000_000;q0,dq0,_=map(np.array,stream.sample(stamp))
    assert dq0==pytest.approx(v)
    stream.retarget([-2.]*7,stamp)
    q,dq,ddq=map(np.array,stream.sample(stamp))
    assert q==pytest.approx(q0) and dq==pytest.approx(dq0) and ddq==pytest.approx(-a)
    peak=q0.copy()
    for delta in range(0,math.ceil(stream.duration*1000)*1_000_000+1,1_000_000):
        q,dq,ddq=map(np.array,stream.sample(stamp+delta));peak=np.maximum(peak,q)
        assert np.all(abs(dq)<=v+1e-10) and np.all(abs(ddq)<=a+1e-10)
    assert peak-q0==pytest.approx(v*v/(2*a),abs=2e-6)
    assert q==pytest.approx([-2.]*7) and dq==pytest.approx([0.]*7)


def test_shipping_irregular_retargets_have_continuous_bounded_phases():
    opts=options();stream=ResponsiveJointTrajectory(opts);stream.reset([0.]*7,0)
    rng=random.Random(1212);stamp=0
    for _ in range(400):
        stamp+=rng.randint(5_000_000,65_000_000)
        before=stream.sample(stamp);stream.retarget([rng.uniform(-2,2) for _ in range(7)],stamp,stamp)
        after=stream.sample(stamp)
        assert after[0]==pytest.approx(before[0],abs=1e-10)
        assert after[1]==pytest.approx(before[1],abs=1e-10)
        for j,phases in enumerate(stream.plans):
            q,v=stream.origin[j],stream.initial_velocity[j]
            for duration,a in phases:
                assert duration>=0 and abs(a)<=stream.acceleration[j]
                assert max(abs(v),abs(v+a*duration))<=stream.velocity[j]+1e-9
                q+=v*duration+.5*a*duration**2;v+=a*duration
            assert q==pytest.approx(stream.goal[j],abs=1e-8)
            assert v==pytest.approx(stream.end_velocity[j],abs=1e-8)


@pytest.mark.parametrize('fault',['hold','disconnect','feedback','lease','loop_gap','collision'])
def test_shipping_limits_cancel_from_full_speed_without_late_points(fault):
    opts=options();clock,backend,core=make_core(opts)
    for seq in range(1,71):
        clock.advance(.01);core._refresh();target(core,seq,[2.]*7,source_sample_ns=clock())
        receipt,_=core.tick();assert core.armed,core.reason
    assert receipt['trajectory']['dq']==pytest.approx(opts['max_velocity_rad_s'])
    generation=core.generation
    if fault=='hold':core.request(dict(type='hold',session=core.session,reason='released'))
    elif fault=='disconnect':core.disconnect(core.session)
    elif fault=='feedback':
        backend.frozen=True
        for _ in range(11):clock.advance(.01);core.tick()
        assert core.reason=='feedback_latest_expired'
    elif fault=='lease':
        for _ in range(13):clock.advance(.01);core.tick()
        assert core.reason=='feedback_reference_expired'
    elif fault=='loop_gap':
        clock.advance(.11);core._refresh();target(core,71,[2.]*7);core.tick()
        assert core.reason=='trajectory_control_gap'
    else:
        backend.fault='robot_collision';clock.advance(.01);core.tick()
    assert not core.armed and core.trajectory is None and core.pending is None and core.active_target is None
    count,held=backend.publish_count,list(backend.target)
    for _ in range(40):clock.advance(.01);core.tick()
    assert backend.publish_count==count and backend.target==held
    if core.session:assert not target(core,99,[2.]*7,generation=generation)['accepted']
