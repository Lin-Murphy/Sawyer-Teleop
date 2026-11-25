"""Verify the Quest-to-OSC boundary and the native recording contract."""
import http.client
import json
import os
import threading
import time
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from sawyer_teleop.libero_teleop import (
    MAX_AGE_NS, PoseMapping, Station, StationServer, station_loop,
)


def packet(seq, grip, position=(0., 0., 0.), trigger=0., quaternion=(0., 0., 0., 1.)):
    return dict(
        protocol=3, session="quest-smoke", seq=seq, sample_ns=seq * 1_000_000,
        focused=True, mounted=True, space=1, head_status=15, recenter=0,
        head=dict(p=[0., 0., 0.], q=[0., 0., 0., 1.]),
        hands={"r": dict(input_ok=True, tracking_ok=True, status=15, buttons=0,
                         pose=dict(p=list(map(float, position)), q=list(map(float, quaternion))),
                         grip=grip, trigger=trigger, recenter=0, device=1)},
    )


def receive(station, now, sample):
    station.receive(dict(packet=sample, bound_ns=now - 10, source_age_ns=20), now)


def test_native_pose_mapping_clutch_and_gripper():
    station = Station()
    mapper = PoseMapping(position_scale=1., action_cap=.35)
    frame = dict(eef_pos=np.zeros(3), eef_rotation=np.eye(3))
    limit = np.array([.05] * 3 + [.5] * 3)
    now = MAX_AGE_NS
    receive(station, now, packet(1, 0.))
    value, interrupted, _ = station.take_input(now)
    mapper.calibrate(value)
    np.testing.assert_array_equal(mapper.action(value, interrupted, frame, limit),
                                  [0., 0., 0., 0., 0., 0., -1.])

    receive(station, now, packet(2, .9))
    value, interrupted, _ = station.take_input(now)
    np.testing.assert_array_equal(mapper.action(value, interrupted, frame, limit),
                                  [0., 0., 0., 0., 0., 0., -1.])
    rotation = Rotation.from_rotvec([0., .1, 0.]).as_quat()
    receive(station, now, packet(3, .9, (0., 0., -.1), .9, rotation))
    value, interrupted, _ = station.take_input(now)
    np.testing.assert_allclose(mapper.action(value, interrupted, frame, limit),
                               [.35, 0., 0., 0., 0., .1, 1.], atol=1e-12)

    # Release must still cancel if another held input arrives in the same tick.
    receive(station, now, packet(4, 0.))
    receive(station, now, packet(5, .9, (0., 0., -.1)))
    value, interrupted, _ = station.take_input(now)
    np.testing.assert_array_equal(mapper.action(value, interrupted, frame, limit),
                                  [0., 0., 0., 0., 0., 0., 1.])
    assert mapper.anchor is None


def test_input_budget_and_replayed_input_cannot_renew():
    station = Station()
    now = 3 * MAX_AGE_NS
    sample = packet(1, .9)
    bound = now - MAX_AGE_NS // 2
    age = MAX_AGE_NS // 4
    with pytest.raises(ValueError, match="time budget"):
        station.receive(dict(packet=sample, bound_ns=bound, source_age_ns=2 * age), now)
    station.receive(dict(packet=sample, bound_ns=bound, source_age_ns=age), now)
    value, _, _ = station.take_input(now)
    assert value["deadline"] == now + age
    value, interrupted, _ = station.take_input(now + age)
    assert value is None and interrupted
    receive(station, now + age, sample)
    value, interrupted, reason = station.take_input(now + age)
    assert value is None and interrupted and reason == "quest_replayed"


def test_tracking_epoch_requires_new_heading():
    station = Station()
    mapper = PoseMapping(1., .35)
    frame = dict(eef_pos=np.zeros(3), eef_rotation=np.eye(3))
    limit = np.ones(6)
    now = MAX_AGE_NS
    receive(station, now, packet(1, 0.))
    value, interrupted, _ = station.take_input(now)
    mapper.calibrate(value)
    mapper.action(value, interrupted, frame, limit)
    sample = packet(2, .9, (0., 0., -.3), .9)
    sample["recenter"] = 1
    receive(station, now, sample)
    value, interrupted, _ = station.take_input(now)
    np.testing.assert_array_equal(mapper.action(value, interrupted, frame, limit),
                                  [0., 0., 0., 0., 0., 0., -1.])
    assert mapper.heading is None


def request(conn, method, path, body=None, authorized=True):
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = "Bearer " + "test" * 4
    conn.request(method, path, None if body is None else json.dumps(body), headers)
    response = conn.getresponse()
    return response.status, response.read()


@pytest.fixture
def http_station():
    station = Station()
    with StationServer(0, station, "test" * 4) as server:
        service = threading.Thread(target=server.serve_forever, daemon=True)
        service.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            yield station, conn
        finally:
            conn.close()
            server.shutdown()
            service.join()


def test_http_station_restricts_recording_and_input_endpoints(http_station):
    station, conn = http_station
    assert request(conn, "GET", "/", authorized=False)[0] == 200
    assert request(conn, "POST", "/command", {"command": "start"}, authorized=False)[0] == 401
    code, body = request(conn, "GET", "/status")
    assert code == 200
    bound = json.loads(body)["bound_ns"]
    assert request(conn, "POST", "/quest", dict(packet=packet(1, 0.), bound_ns=bound,
                                               source_age_ns=0))[0] == 200
    assert station.take_input(time.monotonic_ns())[0]["grip"] == 0.
    assert request(conn, "POST", "/command", {"command": "start"})[0] == 202
    assert request(conn, "POST", "/command", {"command": "stop"})[0] == 409
    assert station.commands.get_nowait() == "start"


@pytest.mark.skipif(not os.getenv("SAWYER_LIBERO_TEST_ATOMIC"), reason="Requires offscreen native LIBERO")
@pytest.mark.parametrize("skill", ["grasp", "pull", "place"])
def test_native_libero_http_recording_and_saved_action_replay(http_station, tmp_path, skill):
    from sawyer_teleop.libero_sim import atomic_bddl, replay_episode

    station, conn = http_station
    stop = threading.Event()
    args = SimpleNamespace(bddl=atomic_bddl(skill), output=tmp_path,
                           seed=0, action_cap=.35, position_scale=1.)
    failures = []

    def run():
        try:
            station_loop(args, station, stop)
        except BaseException as exc:
            failures.append(exc)

    def status():
        try:
            code, body = request(conn, "GET", "/status")
        except http.client.RemoteDisconnected:
            conn.close()
            code, body = request(conn, "GET", "/status")
        assert code == 200
        return json.loads(body)

    sequence = 0

    def stream(grip, position=(0., 0., 0.), trigger=0.):
        nonlocal sequence
        sequence += 1
        bound = status()["bound_ns"]
        code, body = request(conn, "POST", "/quest",
                             dict(packet=packet(sequence, grip, position, trigger), bound_ns=bound,
                                  source_age_ns=0))
        assert code == 200, body

    def wait_for(predicate, *, sample=None, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if failures:
                raise failures[0]
            current = status()
            if current.get("scene_ready") is False:
                pytest.fail(current["message"])
            if predicate(current):
                return current
            if sample is not None:
                stream(*sample)
            time.sleep(.025)
        pytest.fail(f"Station did not reach requested state: {status()}")

    simulator = threading.Thread(target=run, daemon=True)
    simulator.start()
    try:
        wait_for(lambda s: s.get("scene_ready"), timeout=150)
        print(f"{skill}: native scene ready", flush=True)
        assert list(tmp_path.iterdir()) == []
        assert request(conn, "GET", "/agentview_rgb")[1].startswith(b"\xff\xd8")
        assert request(conn, "GET", "/eye_in_hand_rgb")[1].startswith(b"\xff\xd8")
        assert request(conn, "POST", "/command", {"command": "start"})[0] == 202
        wait_for(lambda s: s.get("recording") and s["steps"] >= 1, timeout=80)

        # Use real lease turnover and protocol v3 packets with synthetic poses.
        stream(0.)
        assert request(conn, "POST", "/command", {"command": "calibrate"})[0] == 202
        ready = wait_for(lambda s: s["calibrated"], sample=(0.,))
        wait_for(lambda s: s["steps"] > ready["steps"] + 1, sample=(0.,))
        engaged = wait_for(lambda s: s["moving"], sample=(.9,))
        closed = wait_for(lambda s: s["steps"] > engaged["steps"] + 2,
                          sample=(.9, (0., 0., -.05), .9))
        wait_for(lambda s: s["steps"] > closed["steps"] + 2, sample=(.9, (0., 0., -.05)))
        wait_for(lambda s: not s["moving"], sample=(0.,))
        assert request(conn, "POST", "/command", {"command": "stop"})[0] == 202
        final = wait_for(lambda s: bool(s.get("last_saved")))
        folder = tmp_path / final["last_saved"]
        start = json.loads((folder / "start.json").read_text())
        if skill == "place":
            preparation = start["start"]["preparation"]
            assert preparation["preparation_passed"]
            assert preparation["final_measurement"]["bilateral_contact"]
            assert not preparation["final_measurement"]["native_in"]
        with h5py.File(folder / "trajectory.hdf5") as raw:
            actions = raw["actions/native_osc_pose"][:]
            assert np.any(actions[:, 0] > .01)
            assert np.any(actions[:, -1] == 1.)
            assert np.any(actions[:, -1] == -1.)
            assert actions[0, -1] == (1. if skill == "place" else -1.)
            assert not raw["observations/success"][0]
            assert len(raw["observations/agentview_rgb"]) == len(actions) + 1
            assert not raw["actions/controller_active"][-1]
    finally:
        stop.set()
        simulator.join(150)
    assert not simulator.is_alive()
    if failures:
        raise failures[0]
    replay = replay_episode(folder)
    print(json.dumps(dict(skill=skill, **replay), sort_keys=True), flush=True)
    assert replay["passed"], replay
