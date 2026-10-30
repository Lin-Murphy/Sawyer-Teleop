"""Simulation timing/ordering tests; these mocks are not physical validation."""
import threading
from types import SimpleNamespace as Obj
import pytest
from sawyer_teleop.gazebo_backend import SimulationClock, GazeboBackend
from sawyer_teleop.gripper import SimulationGripper
from sawyer_teleop.protocol import JOINTS, ns


class Wall:
    value = 1_000_000_000
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += int(seconds*1e9)


def test_paused_or_duplicate_clock_cannot_refresh_liveness():
    wall = Wall()
    clock = SimulationClock(clock=wall)
    assert clock.status() == "simulation_clock_paused_or_stale"
    clock.update(10.)
    wall.advance(.11)
    clock.update(10.)
    assert clock.status() == "simulation_clock_paused_or_stale"
    clock.update(10.001)
    assert clock.status() is None


def test_clock_regression_is_latched_until_new_backend():
    clock = SimulationClock()
    clock.update(20.)
    clock.update(0.)
    clock.update(30.)
    assert "regressed_restart_bridge" in clock.status()
    with pytest.raises(ValueError):
        clock.captured(30.)


def test_non_realtime_simulation_maps_to_observed_wall_time():
    wall = Wall()
    clock = SimulationClock(clock=wall)
    clock.update(100.)
    first = wall()
    wall.advance(.05)
    clock.update(100.01)  # simulation runs five times slower than wall time
    assert clock.captured(100.) == first
    assert wall()-clock.captured(100.) == 50_000_000
    with pytest.raises(ValueError):
        clock.captured(99.)
    with pytest.raises(ValueError):
        clock.captured(101.)


def adapter():
    backend = GazeboBackend.__new__(GazeboBackend)
    backend.lock = threading.RLock()
    backend.clock = SimulationClock()
    backend.clock.update(100.)
    backend.state_timeout = .1
    backend.stamps, backend.received, backend.captured, backend.invalid = {}, {}, {}, {}
    backend.pending_sources = {}
    backend.seq = 0
    backend.q = backend.dq = backend.endpoint = None
    backend.ros = Obj(is_shutdown=lambda: False, Time=Obj(now=lambda: 100.))
    backend.publisher = Obj(get_num_connections=lambda: 1)
    backend.gripper = SimulationGripper(
        [Obj(get_num_connections=lambda: 1)] * 2, Obj)
    return backend


def test_duplicate_sources_and_regression_do_not_become_new_samples():
    b = adapter()
    b._accept("joint", 100., lambda: None)
    first = b.captured["joint"]
    b._accept("joint", 100., lambda: pytest.fail("duplicate refreshed"))
    assert b.seq == 1 and b.captured["joint"] == first
    b._accept("joint", 99., lambda: pytest.fail("regression accepted"))
    assert "regressed" in b.clock.status()


def clock_message(stamp):
    return Obj(clock=Obj(to_sec=lambda: stamp))


@pytest.fixture
def ordered_backend(monkeypatch):
    import sawyer_teleop.gazebo_backend as module
    wall = Wall()
    monkeypatch.setattr(module, 'ns', wall)
    b = adapter()
    b.clock = SimulationClock(clock=wall)
    b.clock.update(100.)
    return b, wall


def test_overtaking_source_waits_for_clock_and_keeps_original_age(ordered_backend):
    b, wall = ordered_backend;accepted=[]
    b._accept('pose', 100., lambda: accepted.append('old'))
    wall.advance(.01)
    arrival = wall()
    b._accept('pose', 100.05, lambda: accepted.append('new'))
    assert accepted == ['old'] and b.seq == 1 and not b.invalid
    wall.advance(.03)
    b._clock(clock_message(100.04))
    assert accepted == ['old']  # Even within 20 ms, a deferred sample waits for matching time.
    wall.advance(.01)
    b._clock(clock_message(100.05))
    assert accepted == ['old', 'new'] and b.seq == 2
    assert b.captured['pose'] == arrival and b.received['pose'] == arrival
    assert wall()-b.captured['pose'] == 40_000_000 and not b.pending_sources


def test_pending_sources_keep_only_latest_and_duplicates_cannot_renew(ordered_backend):
    b, wall = ordered_backend;accepted=[]
    b._accept('pose',100.05,lambda:accepted.append('superseded'))
    wall.advance(.01)
    b._accept('pose',100.06,lambda:accepted.append('latest'))
    arrival=wall()
    wall.advance(.02)
    b._accept('pose',100.06,lambda:accepted.append('duplicate'))
    b._clock(clock_message(100.06))
    assert accepted == ['latest'] and b.captured['pose'] == arrival


def test_pending_source_expires_in_wall_time_without_acceptance(ordered_backend):
    b, wall = ordered_backend
    b._accept('joint',100.05,lambda:pytest.fail('expired source accepted'))
    wall.advance(.100000001)
    b._clock(clock_message(100.05))
    assert b.seq == 0 and not b.pending_sources and b.invalid['joint'] == 'joint_stale'


def test_pending_source_cannot_survive_clock_regression(ordered_backend):
    b, wall = ordered_backend
    b._accept('joint',100.05,lambda:pytest.fail('regressed clock accepted'))
    b._clock(clock_message(99.))
    b._clock(clock_message(101.))
    assert b.seq == 0 and 'regressed' in b.clock.status()


@pytest.mark.parametrize('stamp',[100.101,99.89,float('nan'),float('inf')])
def test_out_of_window_or_invalid_sources_still_rejected(ordered_backend,stamp):
    b, wall = ordered_backend
    b._accept('joint',stamp,lambda:pytest.fail('invalid source accepted'))
    assert b.seq == 0 and not b.pending_sources
    assert b.invalid or b.clock.status()


def test_new_valid_source_supersedes_unvalidated_future_sample(ordered_backend):
    b, wall = ordered_backend;accepted=[]
    b._accept('pose',100.08,lambda:accepted.append('future'))
    b._accept('pose',100.001,lambda:accepted.append('valid'))
    b._clock(clock_message(100.08))
    assert accepted == ['valid'] and not b.pending_sources


def test_invalid_new_source_cannot_be_cleared_by_older_pending_update(ordered_backend):
    b, wall = ordered_backend
    b._accept('pose',100.05,lambda:pytest.fail('superseded source accepted'))
    b._endpoint(Obj(valid=False,header=Obj(frame_id='base')))
    b._clock(clock_message(100.05))
    assert b.invalid['pose'] == 'endpoint_invalid' and not b.pending_sources


def test_joint_order_missing_velocity_and_duplicate_names():
    b = adapter()
    def message(names, positions, velocities):
        return Obj(header=Obj(stamp=Obj(to_sec=lambda:100.)),
                   name=names, position=positions, velocity=velocities)
    b._joint(message(JOINTS[::-1], list(range(7)), [0]*7))
    assert b.q == list(reversed(range(7)))
    b._joint(message(JOINTS, [0]*7, []))
    assert b.invalid["joint"] == "joint_invalid"
    b._joint(message([JOINTS[0]]*7, [0]*7, [0]*7))
    assert b.invalid["joint"] == "joint_invalid"


def test_stale_joint_rejected_even_when_clock_keeps_ticking():
    b = adapter()
    b.q = b.dq = [0]*7
    b.endpoint = {"p":[0,0,0],"q":[0,0,0,1]}
    b.stamps = {"joint":100., "pose":100.}
    b.received = {"joint":ns(), "pose":ns()}
    b.captured = dict(b.received, joint=ns()-110_000_000)
    assert b.snapshot()["fault"] == "joint_stale"
    with pytest.raises(RuntimeError):
        b.publish([0]*7)


def test_base_frame_and_validity_are_required():
    b = adapter()
    m = Obj(valid=True,header=Obj(frame_id="world"))
    b._endpoint(m)
    assert b.invalid["pose"] == "endpoint_invalid"


def test_real_and_simulated_ros_entrypoints_are_mutually_exclusive(monkeypatch):
    import sys
    def install(use_sim_time):
        ros = Obj(core=Obj(is_initialized=lambda: True),get_param=lambda key, default=None:
                  use_sim_time if key == "/use_sim_time" else default)
        monkeypatch.setitem(sys.modules,"rospy",ros)
        monkeypatch.setitem(sys.modules,"rosgraph_msgs.msg",Obj(Clock=Obj))
        monkeypatch.setitem(sys.modules,"sensor_msgs.msg",Obj(JointState=Obj))
        monkeypatch.setitem(sys.modules,"intera_core_msgs.msg",Obj(EndpointState=Obj,
            RobotAssemblyState=Obj,CollisionDetectionState=Obj,IODeviceStatus=Obj,JointCommand=Obj))
        monkeypatch.setitem(sys.modules,"intera_core_msgs.srv",Obj(SolvePositionIK=Obj,SolvePositionIKRequest=Obj))
    install(False)
    with pytest.raises(RuntimeError,match="use_sim_time"):
        GazeboBackend()
    install(True)
    monkeypatch.setenv("ROS_MASTER_URI","http://127.0.0.1:11322")
    from sawyer_teleop.intera_backend import InteraBackend
    with pytest.raises(RuntimeError,match="Real robot backend"):
        InteraBackend()


def test_planned_velocity_and_zero_velocity_hold_replace_implicit_position_feedforward():
    backend = adapter();messages=[]
    backend.snapshot = lambda: {'valid':True}
    backend.command_class = lambda: Obj(header=Obj(),POSITION_MODE=1,TRAJECTORY_MODE=4)
    backend.publisher.publish = messages.append
    q=[.1]*7;v=[.2,-.3,.1,-.1,.4,-.4,.5];a=[1.]*7
    backend.publish_reference(q,v,a)
    assert messages[-1].mode==4 and messages[-1].position==q
    assert messages[-1].velocity==v and messages[-1].acceleration==a
    backend.publish(q)
    assert messages[-1].mode==4 and messages[-1].velocity==[0.]*7
    assert messages[-1].acceleration==[0.]*7
    backend.snapshot=lambda:{'valid':False}
    with pytest.raises(RuntimeError):backend.publish_reference(q,v,a)
    assert len(messages)==2
