"""Persistent remote IK connection, tested with a ROS double (no ROS import)."""
from types import SimpleNamespace as Obj
import pytest
from sawyer_teleop.ros_service import PersistentService


class Proxy:
    def __init__(self, fail=False):
        self.calls, self.closed, self.fail = [], False, fail

    def __call__(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("connection reset")
        return ("ok", request)

    def close(self):
        self.closed = True


def ros(*proxies):
    created, queue = [], list(proxies)
    def make(name, kind, persistent=False):
        created.append((name, kind, persistent))
        return queue.pop(0)
    return Obj(ServiceProxy=make), created


def test_one_persistent_connection_is_reused():
    proxy = Proxy()
    rospy, created = ros(proxy)
    service = PersistentService(rospy, "/ik", "Kind")
    assert service.connections == 0  # Lazy: construction never connects.
    assert service(1) == ("ok", 1) and service(2) == ("ok", 2)
    assert created == [("/ik", "Kind", True)] and proxy.calls == [1, 2]


def test_failed_call_is_reported_once_and_next_call_reconnects():
    broken, fresh = Proxy(fail=True), Proxy()
    rospy, created = ros(broken, fresh)
    service = PersistentService(rospy, "/ik", "Kind")
    with pytest.raises(RuntimeError, match="connection reset"):
        service(1)
    assert broken.calls == [1] and broken.closed  # Not retried within the same call.
    assert service(2) == ("ok", 2)
    assert len(created) == 2 and service.connections == 2


def test_close_releases_connection_and_tolerates_idle_service():
    proxy = Proxy()
    rospy, _ = ros(proxy)
    service = PersistentService(rospy, "/ik", "Kind")
    service.close()
    service(1)
    service.close()
    assert proxy.closed and service.proxy is None
