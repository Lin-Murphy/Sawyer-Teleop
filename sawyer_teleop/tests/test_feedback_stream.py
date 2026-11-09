"""Recording-only feedback stream and raw-data quality (schema 2)."""
import json
import time

import h5py
import numpy as np
import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.feedback_stream import MAX_ROWS_PER_MESSAGE, FeedbackStream, merge_stream
from sawyer_teleop.protocol import MAX_FRAME, encode
from sawyer_teleop.recording import (CAMERA_LOST_S, Episode, camera_quality, latest_index,
                                     stream_quality)
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.episode_store import open_episode


def test_stream_overflow_is_counted_and_visible_as_sequence_gap():
    stream = FeedbackStream(capacity=3)
    for index in range(5):
        stream.append("joint", {"index": index})
    rows, dropped = stream.drain()
    assert dropped == 2
    assert [r["stream_seq"] for r in rows] == [3, 4, 5]
    assert stream.drain() == ([], 0)


def test_merge_keeps_unsent_rows_and_caps_frame_size():
    first = {"rows": [{"stream_seq": i} for i in range(60)], "dropped": 1}
    second = {"rows": [{"stream_seq": i} for i in range(60, 120)], "dropped": 0}
    merged = merge_stream(first, second)
    assert len(merged["rows"]) == MAX_ROWS_PER_MESSAGE
    assert merged["rows"][-1]["stream_seq"] == 119
    assert merged["dropped"] == 1+120-MAX_ROWS_PER_MESSAGE
    # A full merged batch of realistic rows still fits one protocol frame.
    row = {"kind": "joint", "stream_seq": 10**9, "source_stamp": 1.7e9+.123456789,
           "b_received_ns": 10**18, "b_captured_ns": 10**18, "head_pan": -0.123456789012345,
           "q": [-1.2345678901234567]*7, "dq": [-1.2345678901234567]*7}
    message = {"type": "state", "feedback_stream": {"rows": [row]*MAX_ROWS_PER_MESSAGE, "dropped": 0},
               "padding": "x"*8000}
    assert len(encode(message)) < MAX_FRAME


def test_transport_latest_slot_merges_stream_rows():
    from sawyer_teleop.transport import Connection

    class Sock:
        def setsockopt(self, *args):
            pass

        def settimeout(self, *args):
            pass
    connection = Connection(Sock(), lambda msg: None)
    connection.send({"type": "state", "n": 1, "feedback_stream": {"rows": [{"stream_seq": 1}], "dropped": 0}},
                    latest="state", merge="feedback_stream")
    connection.send({"type": "state", "n": 2}, latest="state", merge="feedback_stream")
    connection.send({"type": "state", "n": 3, "feedback_stream": {"rows": [{"stream_seq": 2}], "dropped": 0}},
                    latest="state", merge="feedback_stream")
    state = connection.latest["state"]
    assert state["n"] == 3
    assert [r["stream_seq"] for r in state["feedback_stream"]["rows"]] == [1, 2]


def test_bridge_offers_stream_only_after_opt_in():
    core = BridgeCore(FakeBackend())
    hello = core.connect()
    assert hello["feedback_stream"] == 1
    core.tick()
    assert core.drain_feedback_stream() is None  # Not requested: nothing is sent.
    core.request({"type": "feedback_stream", "session": hello["session"], "enabled": True})
    time.sleep(.006)
    core.tick()
    stream = core.drain_feedback_stream()
    assert {r["kind"] for r in stream["rows"]} == {"joint", "pose"}
    joint = next(r for r in stream["rows"] if r["kind"] == "joint")
    assert len(joint["q"]) == 7 and len(joint["dq"]) == 7 and "b_received_ns" in joint


def test_client_receives_every_accepted_sample_end_to_end():
    server = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    client = RobotClient("127.0.0.1", server.control_port, server.ik_port).connect()
    try:
        assert client.feedback_stream_status["enabled"]
        time.sleep(.3)
        rows, counters = client.drain_feedback_stream()
        joint = [r["stream_seq"] for r in rows if r["kind"] == "joint"]
        assert len(joint) > 10
        assert joint == list(range(joint[0], joint[0]+len(joint)))  # no gaps
        assert counters["dropped_a"] == 0 and counters["dropped_b"] == 0
        assert all(r["clock_offset_ns"] is not None for r in rows)
        assert "feedback_stream" not in client.snapshot()  # control state stays unchanged
    finally:
        client.close()
        server.close()


def test_client_opt_out_keeps_latest_value_path():
    server = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    client = RobotClient("127.0.0.1", server.control_port, server.ik_port, feedback_stream=False).connect()
    try:
        time.sleep(.1)
        assert client.feedback_stream_status == {"enabled": False, "reason": "disabled_by_config"}
        assert client.drain_feedback_stream()[0] == []
    finally:
        client.close()
        server.close()


def test_camera_quality_thresholds():
    start, fps = 0, 20
    period = int(1e9/fps)
    ok = camera_quality(np.arange(1, 21)*period, fps, start, 21*period)
    assert ok["status"] == "ok" and ok["expected_frames"] == 21
    gap = np.r_[np.arange(1, 10), np.arange(16, 21)]*period  # one 350 ms hole
    degraded = camera_quality(gap, fps, start, 21*period)
    assert degraded["status"] == "degraded" and degraded["max_gap_ms"] == pytest.approx(350)
    lost = camera_quality(np.arange(1, 5)*period, fps, start, int(4*period+CAMERA_LOST_S*1e9))
    assert lost["status"] == "incomplete"
    assert camera_quality([], fps, 0, 10**9)["status"] == "incomplete"


def test_stream_quality_counts_missing_rows():
    result = stream_quality([1, 2, 3, 6, 7], np.arange(5)*10_000_000)
    assert result["missing_rows"] == 2 and result["status"] == "degraded"
    assert stream_quality([1, 2, 3], [0, 10_000_000, 20_000_000])["status"] == "ok"


def test_latest_index_is_causal():
    assert list(latest_index([10, 20, 30], [5, 10, 25, 40])) == [-1, 0, 1, 2]


def _row(kind, seq, stamp):
    base = {"kind": kind, "stream_seq": seq, "source_stamp": stamp/1e9, "b_received_ns": stamp,
            "b_captured_ns": stamp-1, "a_received_ns": stamp+5, "session": "s",
            "clock_offset_ns": 100, "clock_uncertainty_ns": 7}
    if kind == "joint":
        return dict(base, q=[seq/10]*7, dq=[0.]*7, head_pan=None)
    return dict(base, p=[.5, 0, .3], q=[0, 0, 0, 1], twist=[.1, 0, 0, 0, 0, .2])


def test_episode_writes_stream_alignment_quality_and_gap_event(tmp_path):
    episode = Episode(tmp_path, {"initial_state": {"q": [0.]*7}}, ["front"], fps={"front": 20})
    start = episode.started_ns
    rows = []
    for index in range(1, 41):
        rows += [_row("joint", index, start+index*10_000_000), _row("pose", index, start+index*10_000_000)]
    episode.stream(rows)
    frame_times = [start+i*50_000_000 for i in range(1, 5)] + [start+700_000_000]  # 450 ms hole
    for seq, stamp in enumerate(frame_times):
        episode.frame("front", {"ns": stamp, "read_start_ns": stamp-1, "seq": seq, "source_seq": seq*3,
                                "open_count": 1, "rgb": np.zeros((48, 64, 3), dtype="uint8")})
    time.sleep(.05)
    episode.seal()
    episode.sealed_ns = start+750_000_000  # deterministic recording window
    assert episode.finish(True, {"capture_closed": True}).wait(5)
    assert episode.error is None
    with open_episode(episode.result) as data:
        metadata = json.loads(data.attrs["metadata"])
        assert metadata["schema_version"] == 3 and metadata["initial_state"] == {"q": [0.]*7}
        joint, pose = data["feedback/joint"], data["feedback/pose"]
        assert len(joint["q"]) == 40 and not joint["head_pan_valid"][:].any()
        np.testing.assert_array_equal(joint["ns"][:], [start+i*10_000_000+100 for i in range(1, 41)])
        assert pose["eef_twist_valid"][:].all() and pose["eef_twist"][0][5] == pytest.approx(.2)
        assert list(data["frames/front/source_seq"][:]) == [0, 3, 6, 9, 12]
        index = data["alignment/front/joint_index"][:]
        assert np.all(joint["ns"][:][index] <= data["frames/front/ns"][:])
        quality = json.loads(data.attrs["quality"])
    assert quality["cameras"]["front"]["status"] == "degraded"
    assert quality["feedback"]["joint"]["missing_rows"] == 0
    assert quality["status"] == "degraded"
    manifest = json.loads((episode.result/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["status"] == "degraded"
    events = [json.loads(line) for line in (episode.result/"events.jsonl").read_text(encoding="utf-8").splitlines()]
    gaps = [e for e in events if e["reason"] == "camera_frame_gap"]
    assert len(gaps) == 1 and gaps[0]["gap_ms"] == pytest.approx(500)


def test_camera_lost_marks_episode_incomplete(tmp_path):
    episode = Episode(tmp_path, {}, ["front"], fps={"front": 20})
    assert episode.finish(True, {"capture_closed": True, "camera_lost": "front"}).wait(5)
    manifest = json.loads((episode.result/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["status"] == "incomplete"


def test_demo_records_raw_schema_two(tmp_path):
    from sawyer_teleop.demo import run_demo
    run_demo(tmp_path, seconds=.6)
    episodes = sorted((tmp_path/"demo").glob("episode_*"))
    assert episodes
    with open_episode(episodes[0]) as data:
        metadata = json.loads(data.attrs["metadata"])
        initial = metadata["initial_state"]
        assert len(initial["q"]) == 7 and len(initial["eef_pos"]) == 3 and len(initial["eef_quat"]) == 4
        assert initial["feedback_age_ms"] <= 100
        assert metadata["feedback_stream"]["enabled"]
        assert len(data["feedback/joint/q"]) > 0 and len(data["feedback/pose/eef_pos"]) > 0
        assert "state_index" in data["alignment/front"]
        assert data["states/eef_twist_valid"][:].all()
        assert "command_kind" in data["commands"] and "target_pos" in data["commands"]
