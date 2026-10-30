"""Independent path, latency and state continuity gates for the new planner."""
import json
import math
from pathlib import Path
import random
import sys

import numpy as np
import pytest

from sawyer_teleop.trajectory import CoordinatedJointTrajectory, make_trajectory, trajectory_options
from test_trajectory import make_core
from test_bridge import target


def options():
    return json.loads((Path(__file__).resolve().parents[1]/'config/operator.json').read_text())['trajectory']


@pytest.fixture(autouse=True, params=[1, 4], ids=['standard', 'gazebo-fast'])
def acceleration_budget(request, monkeypatch):
    """Run the same path, continuity, reversal and cancellation gates at both budgets."""
    value = options()
    value['max_acceleration_rad_s2'] = [a * request.param for a in value['max_acceleration_rad_s2']]
    monkeypatch.setattr(sys.modules[__name__], 'options', lambda: value)


def test_shipping_profile_and_recorded_cartesian_regression():
    from test_recorded_path_case import case_module as check
    case, data = check.load_case()
    opts = options()
    assert opts['profile'] == 'coordinated'
    assert isinstance(make_trajectory(opts), CoordinatedJointTrajectory)
    report = check.evaluate(case, data, *check.replay(data, opts), opts)
    assert report['offline_case_passed'], report['checks']


def test_common_progress_for_rest_to_rest_vector():
    stream = make_trajectory(options());stream.reset([0.]*7,0)
    goal=np.array([.2,-.5,.8,-.9,.1,1.2,-1.3]);stream.retarget(goal.tolist(),0)
    for stamp in np.linspace(0,stream.duration*1e9,401).astype(np.int64):
        q,v,a=map(np.array,stream.sample(int(stamp)))
        assert np.ptp(q/goal)<1e-8
        assert np.all(q/goal>=-1e-9) and np.all(q/goal<=1+1e-9)
        assert np.all(abs(v)<=np.array(stream.velocity)+1e-9)
        assert np.all(abs(a)<=np.array(stream.acceleration)+1e-9)


@pytest.mark.parametrize('joint',[0,3,6])
@pytest.mark.parametrize('amplitude,hz',[(.03,.5),(.12,.5),(.3,.25)])
def test_feasible_tracking_does_not_add_smoothing_lag(joint,amplitude,hz):
    stream=make_trajectory(options());stream.reset([0.]*7,0);rows=[]
    for i in range(1000):
        goal=[0.]*7;goal[joint]=amplitude*math.sin(2*math.pi*hz*i*.01)
        stream.retarget(goal,i*10_000_000,i*10_000_000)
        q,v,a=stream.sample(i*10_000_000);rows.append(q[joint])
    t=np.arange(200,1000)*.01;y=np.array(rows)[200:]
    design=np.column_stack([np.sin(2*math.pi*hz*t),np.cos(2*math.pi*hz*t),np.ones(len(t))])
    fitted=np.linalg.lstsq(design,y,rcond=None)[0]
    lag=-math.atan2(fitted[1],fitted[0])/(2*math.pi*hz)
    assert -.005<=lag<=.020
    assert max(abs(y))<=amplitude+.005


def test_retarget_continuity_and_dense_whole_curve_limits():
    stream=make_trajectory(options());stream.reset([0.]*7,0)
    rng=random.Random(20261021);stamp=0
    for _ in range(400):
        stamp+=rng.randint(5_000_000,65_000_000)
        before=stream.sample(stamp)
        stream.retarget([rng.uniform(-2,2)for _ in range(7)],stamp,stamp)
        after=stream.sample(stamp)
        assert after[0]==pytest.approx(before[0],abs=1e-9)
        assert after[1]==pytest.approx(before[1],abs=1e-9)
        points=np.array([stream.curve.at_time(t)for t in np.linspace(0,stream.duration,401)])
        assert np.max(abs(points[:,1])/stream.velocity)<=1+1e-9
        assert np.max(abs(points[:,2])/stream.acceleration)<=1+1e-9
        assert points[-1,0]==pytest.approx(stream.goal,abs=1e-8)
        assert points[-1,1]==pytest.approx(stream.end_velocity,abs=1e-8)


def test_nonperiodic_multi_axis_reversal_and_final_hold():
    stream = make_trajectory(options());stream.reset([0.]*7, 0)
    knots = np.array([0., .6, .9, 1.5, 2.1, 2.6, 3.0, 3.8, 5.8])
    progress = np.array([0., .7, .7, -.4, .2, .2, -.8, 0., 0.])
    vector = np.array([.2, -.3, .1, .25, -.15, .3, -.2])
    rows = []
    for tick in range(581):
        t = tick*.01;stamp = tick*10_000_000
        stream.retarget((np.interp(t, knots, progress)*vector).tolist(), stamp, stamp)
        rows.append(stream.sample(stamp))
    result = np.array(rows)
    assert np.all(np.isfinite(result))
    assert np.max(abs(result[:, 1])/stream.velocity) <= 1+1e-9
    assert np.max(abs(result[:, 2])/stream.acceleration) <= 1+1e-9
    assert np.max(abs(result[-30:, :2])) < 1e-6


@pytest.mark.parametrize('fault',['hold','disconnect','feedback','lease','loop_gap','collision'])
def test_shipping_coordinated_cancel_has_no_late_points(fault):
    clock,backend,core=make_core(options())
    for seq in range(1,71):
        clock.advance(.01);core._refresh();target(core,seq,[2.]*7,source_sample_ns=clock())
        receipt,_=core.tick();assert core.armed,core.reason
    assert max(abs(v)for v in receipt['trajectory']['dq'])>.4
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


def test_duplicate_sample_and_frozen_goal_do_not_restart_motion():
    stream=make_trajectory(options());stream.reset([0.]*7,0);stream.retarget([.2]*7,0,100)
    curve=stream.curve;stream.retarget([.2]*7,10_000_000,100)
    assert stream.curve is curve
    stream.retarget([.2]*7,20_000_000,20_000_100)
    assert stream.curve is curve
    with pytest.raises(ValueError):trajectory_options(dict(options(),min_duration_ms=20))
    with pytest.raises(RuntimeError,match='clock_regressed'):stream.sample(0)
