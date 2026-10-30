"""Fixed A/B TCP endpoints; ROS/Gazebo master ports remain inside the simulator."""
DEFAULT_BACKEND = "gazebo"
BACKENDS = (("gazebo", "Gazebo Simulation"), ("fake", "Software Test"), ("intera", "Real Sawyer"))


def connection_ports(backend):
    if backend == "gazebo":
        return 8875, 8876
    if backend in ("fake", "intera"):
        return 8765, 8766
    raise ValueError("Unknown backend: " + str(backend))


def backend_label(backend):
    return dict(BACKENDS).get(backend, str(backend or "unknown"))
