from types import SimpleNamespace as Obj

import pytest

from sawyer_teleop.gripper import SimulationGripper, trigger_command
from sawyer_teleop.protocol import ProtocolError
from test_bridge import Clock, setup_core, target


@pytest.mark.parametrize("trigger,expected", [(0., -1), (.2999, -1), (.3, 1), (1., 1)])
def test_trigger_threshold(trigger, expected):
    assert trigger_command(trigger) == expected


@pytest.mark.parametrize("trigger", [None, True, "1", -.01, 1.01, float("nan"), float("inf")])
def test_invalid_trigger_never_becomes_close(trigger):
    with pytest.raises(ValueError):
        trigger_command(trigger)


def test_observation_does_not_actuate_and_latest_target_wins():
    _, backend, core = setup_core()
    core.request(dict(type="gripper_observation", session=core.session, value=1))
    core.tick()
    assert backend.gripper_publish_count == 0
    target(core, 1, gripper=1)
    target(core, 2, gripper=-1)
    receipt, _ = core.tick()
    assert receipt["gripper"] == -1 and backend.gripper_target == -1
    assert backend.gripper_publish_count == 1
    assert target(core, 1, gripper=1)["accepted"] is False
    assert backend.gripper_target == -1


def test_observation_reaches_b_over_real_tcp_without_actuation():
    from sawyer_teleop.backends import FakeBackend
    from sawyer_teleop.server import BridgeServer
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.demo import wait_until
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient("127.0.0.1", server.control_port, server.ik_port)
    try:
        client.connect()
        for trigger, expected in [(0., -1), (.3, 1)]:
            client.gripper_observation(trigger)
            wait_until(lambda: (server.core.diagnostics()["gripper_observation"] or {}).get("value") == expected)
        assert backend.gripper_publish_count == 0
        assert not server.core.armed
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("trajectory", [False, True])
def test_gripper_uses_target_lease_and_release_cancels(trajectory):
    clock, backend, core = setup_core()
    if trajectory:
        core.request(dict(type="hold", session=core.session))
        core.request(dict(type="arm", session=core.session,
                          feedback_seq=core.latest["seq"], trajectory={"enabled": True}))
    target(core, 1, gripper=1)
    receipt, _ = core.tick()
    assert receipt["gripper"] == 1 and backend.gripper_active
    target(core, 2, gripper=1)
    core.tick()
    assert backend.gripper_publish_count == 1  # no repeated controller restarts
    target(core, 3, gripper=-1)
    clock.advance(.121)
    core.tick()
    assert not core.armed and not backend.gripper_active
    assert backend.gripper_target == 1  # expired open command was not submitted
    assert target(core, 4, gripper=-1)["accepted"] is False


def test_disconnect_drops_pending_gripper_and_new_session_does_not_replay():
    _, backend, core = setup_core()
    target(core, gripper=1)
    core.disconnect(core.session)
    hello = core.connect()
    assert hello["gripper_control"]
    core.tick()
    assert backend.gripper_publish_count == 0


@pytest.mark.parametrize("value", [0, 2, True, "1", 1.0])
def test_invalid_gripper_target_pauses_without_submission(value):
    _, backend, core = setup_core()
    with pytest.raises(ProtocolError, match="invalid_gripper"):
        target(core, gripper=value)
    assert not core.armed and backend.gripper_publish_count == 0


def test_gripper_submit_error_pauses_instead_of_crashing_bridge():
    _, backend, core = setup_core()
    def fail(value):
        raise OSError("controller disconnected")
    backend.publish_gripper = fail
    target(core, gripper=1)
    receipt, _ = core.tick()
    assert receipt is None and not core.armed
    assert core.reason == "gripper_submit_failed"


def test_old_backend_does_not_advertise_or_accept_gripper():
    _, backend, core = setup_core()
    backend.publish_gripper = None
    with pytest.raises(ProtocolError, match="gripper_unsupported"):
        target(core, gripper=1)
    assert backend.gripper_publish_count == 0


def test_simulation_two_finger_commands_feedback_and_hold():
    wall = Clock()
    commands = [[], []]
    connected = [True]
    pubs = [Obj(get_num_connections=lambda: connected[0], publish=items.append)
            for items in commands]
    gripper = SimulationGripper(pubs, Obj, clock=wall)
    with pytest.raises(RuntimeError, match="feedback_stale"):
        gripper.command(1)
    gripper.observe(dict(zip(gripper.names, [.03, .025])), wall())
    gripper.command(1)
    gripper.command(1)
    assert [[m.data for m in values] for values in commands] == [[0.], [0.]]
    gripper.hold()
    assert commands[0][-1].data == .03 and commands[1][-1].data == .025
    gripper.command(-1)
    assert commands[0][-1].data == commands[1][-1].data == .04
    connected[0] = False
    with pytest.raises(RuntimeError, match="controller_missing"):
        gripper.command(1)
    connected[0] = True
    wall.advance(.11)
    with pytest.raises(RuntimeError, match="feedback_stale"):
        gripper.command(1)
    before = [len(items) for items in commands]
    gripper.hold()
    assert [len(items) for items in commands] == before
