"""Reused ROS service connection for B's per-target calls. Imports no ROS itself."""
import threading


class PersistentService:
    """One persistent proxy, created lazily and discarded after any failed call.

    A per-call proxy repeats the master lookup and TCP handshake for every IK
    request. The failed call is still reported to the caller; it is not retried
    here, so timing budgets and failure semantics stay with the caller.
    """

    def __init__(self, rospy, name, service_class):
        self.ros, self.name, self.service_class = rospy, name, service_class
        self.lock = threading.Lock()
        self.proxy = None
        self.connections = 0

    def __call__(self, request):
        with self.lock:
            if self.proxy is None:
                self.proxy = self.ros.ServiceProxy(self.name, self.service_class, persistent=True)
                self.connections += 1
            try:
                return self.proxy(request)
            except Exception:
                self._drop()
                raise

    def _drop(self):
        proxy, self.proxy = self.proxy, None
        try:
            proxy.close()
        except Exception:
            pass

    def close(self):
        with self.lock:
            if self.proxy is not None:
                self._drop()
