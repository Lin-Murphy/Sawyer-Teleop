"""Time and readiness policy tested without importing or enabling ROS hardware."""
import threading
from types import SimpleNamespace as Obj
import pytest
from sawyer_teleop.intera_backend import InteraBackend
from sawyer_teleop.protocol import ns


def adapter():
    backend = InteraBackend.__new__(InteraBackend)
    backend.lock = threading.RLock()
    backend.state_timeout, backend.status_timeout = .1, .5
    backend.stamps, backend.received, backend.captured, backend.invalid = {}, {}, {}, {}
    backend.ros = Obj(Time=Obj(now=lambda: Obj(to_sec=lambda: 100.0)))
    return backend


def test_ros_source_age_is_included_and_duplicate_does_not_refresh():
    backend = adapter()
    backend._accept("joint", 99.92, lambda: None)
    assert 79_000_000 < backend.received["joint"]-backend.captured["joint"] < 81_000_000
    first = backend.received["joint"]
    backend._accept("joint", 99.92, lambda: pytest.fail("duplicate source updated"))
    assert backend.received["joint"] == first
    backend._accept("joint", 99.91, lambda: pytest.fail("regressed source updated"))
    assert "regressed" in backend.invalid["joint"]


def test_old_ros_source_is_rejected_before_arrival_can_make_it_fresh():
    backend = adapter()
    backend._accept("joint", 99.8, lambda: pytest.fail("old feedback accepted"))
    assert "joint" not in backend.received
    assert "invalid" in backend.invalid["joint"]


def test_no_joint_clamping_in_publish():
    backend = adapter()
    sent = []
    class Command:
        POSITION_MODE = 1
        def __init__(self):
            self.header = Obj()
    backend.command_class = Command
    backend.publisher = Obj(publish=lambda msg: sent.append(msg))
    backend.snapshot = lambda: {"valid": True}
    backend.publish([500]*7)
    assert sent[0].position == [500]*7 and sent[0].mode == 1


def test_invalid_feedback_never_published():
    backend = adapter()
    backend.snapshot = lambda: {"valid": False}
    with pytest.raises(RuntimeError):
        backend.publish([0]*7)


def test_ik_passes_full_orientation_and_reads_all_seven_joints(monkeypatch):
    import sys
    from sawyer_teleop.protocol import JOINTS
    backend = adapter()
    backend.tip_frame = "right_hand"
    backend.request_class = lambda: Obj(pose_stamp=[], tip_names=[], seed_angles=[], SEED_USER=1)
    monkeypatch.setitem(sys.modules, "geometry_msgs.msg", Obj(PoseStamped=lambda:
        Obj(header=Obj(), pose=Obj(position=Obj(), orientation=Obj()))))
    monkeypatch.setitem(sys.modules, "sensor_msgs.msg", Obj(JointState=Obj))
    requests = []
    def solve(request):
        requests.append(request)
        return Obj(result_type=[1], joints=[Obj(name=JOINTS[::-1], position=[.7, .6, .5, .4, .3, .2, .1])])
    backend.ik = solve
    quaternion = [.1, .2, .3, .9273618495495703]
    result = backend.solve({"p": [.6, -.2, .4], "q": quaternion}, [0]*7)
    request = requests[0]
    q = request.pose_stamp[0].pose.orientation
    assert [q.x, q.y, q.z, q.w] == quaternion
    assert request.tip_names == ["right_hand"]
    assert request.seed_angles[0].name == JOINTS
    assert result == pytest.approx([.1, .2, .3, .4, .5, .6, .7])


def test_snapshot_does_not_extend_source_freshness_by_transport_age():
    backend = adapter()
    now = ns()
    backend.ros.is_shutdown = lambda: False
    backend.publisher = Obj(get_num_connections=lambda: 1)
    backend.cuff_subscriber = Obj(get_num_connections=lambda: 1)
    backend.received = dict.fromkeys(("joint", "pose", "robot", "cuff", "collision"), now)
    backend.captured = dict(backend.received, joint=now-150_000_000)
    backend.robot = dict(enabled=True, ready=True, homed=True, low_voltage=False,
                        estop=False, stopped=False, error=False)
    backend.cuff = backend.collision = False
    backend.q = backend.dq = [0]*7
    backend.endpoint = {"p": [0, 0, 0], "q": [0, 0, 0, 1]}
    backend.seq = 1
    assert backend.snapshot()["fault"] == "joint_stale"
    assert not backend.snapshot()["valid"]
