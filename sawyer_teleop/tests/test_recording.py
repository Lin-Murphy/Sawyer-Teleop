import json
import time
import h5py
import numpy as np
import pytest
from sawyer_teleop.dataset import causal_indices
from sawyer_teleop.recording import Episode


def test_discard_removes_every_episode_file(tmp_path):
    episode = Episode(tmp_path, {}, ["front"])
    episode.event("private_episode_event")
    episode.frame("front", {"ns": 1, "read_start_ns": 0, "seq": 1,
                             "rgb": np.zeros((48, 64, 3), dtype="uint8")})
    episode.finish(False, {"holding": True}).wait(5)
    assert episode.done.is_set() and not episode.error
    assert not list(tmp_path.iterdir())


def test_saved_incomplete_episode_marked(tmp_path):
    episode = Episode(tmp_path, {})
    episode.finish(True, {"holding": False}).wait(5)
    with h5py.File(episode.result/"raw.hdf5") as file:
        assert not file.attrs["complete"]


@pytest.mark.parametrize("stop, expected", [
    ({"capture_closed": True, "operator_decision": True}, True),
    ({"capture_closed": True}, None),  # time limit: the operator did not judge it
    ({"capture_closed": True, "camera_lost": "front"}, None),
    ({"holding": False, "reason": "operator_shutdown"}, None),
])
def test_operator_save_labels_task_success(tmp_path, stop, expected):
    # Lab convention: failed demonstrations are discarded with B, so an
    # explicit operator save is a success label; other saves stay unknown.
    episode = Episode(tmp_path, {})
    assert episode.finish(True, stop).wait(5)
    manifest = json.loads((episode.result/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_success"] is expected
    with h5py.File(episode.result/"raw.hdf5") as data:
        assert json.loads(data.attrs["metadata"])["task_success"] is expected


def test_no_future_observations():
    indices, valid = causal_indices([100, 200, 300], np.array([99, 100, 250, 500]), 100)
    assert list(indices) == [-1, 0, 1, 2]
    assert list(valid) == [False, True, True, False]


def test_writer_failure_can_still_discard(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk_full")
    monkeypatch.setattr(h5py, "File", fail)
    episode = Episode(tmp_path, {})
    time.sleep(.1)
    assert episode.error == "disk_full"
    episode.finish(False).wait(5)
    assert not list(tmp_path.iterdir())


def test_queue_overflow_is_explicit_and_excludes_training(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    original = Episode._write
    def delayed(self):
        gate.wait(2)
        original(self)
    monkeypatch.setattr(Episode, "_write", delayed)
    episode = Episode(tmp_path, {}, queue_size=1)
    assert episode.put("event", {"test": 1})
    assert not episode.put("event", {"test": 2})
    assert episode.error == "recording_queue_full"
    episode.finish(True, {"holding": True})
    gate.set()
    assert episode.done.wait(5)
    with h5py.File(episode.result/"raw.hdf5") as file:
        assert not file.attrs["complete"]


def test_queued_images_have_a_byte_limit(tmp_path, monkeypatch):
    import threading
    from sawyer_teleop import recording

    gate = threading.Event()
    original = Episode._write

    def delayed(self):
        gate.wait(3)
        original(self)

    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(Episode, "_write", delayed)
    monkeypatch.setattr(recording, "RECORDING_QUEUE_IMAGE_BYTES", 2 * rgb.nbytes)
    episode = Episode(tmp_path, {}, ["front"])
    try:
        for seq in (1, 2):
            assert episode.put("frame", ("front", dict(seq=seq, ns=seq, read_start_ns=0, rgb=rgb)))
        assert not episode.put("frame", ("front", dict(seq=3, ns=3, read_start_ns=0, rgb=rgb)))
        assert episode.error == "recording_queue_full"
    finally:
        episode.finish(True, {"capture_closed": True})
        gate.set()
        assert episode.done.wait(5)
    with h5py.File(episode.result/"raw.hdf5") as data:
        assert not data.attrs["complete"]
        assert data["frames/front/seq"][:].tolist() == [1, 2]
        assert data["frames/front/rgb"].shape == (2, 48, 64, 3)
        assert data["frames/front/rgb"].chunks == (1, 48, 64, 3)


def test_saved_telemetry_preserves_all_rows_precision_and_partial_tail(tmp_path):
    episode = Episode(tmp_path, {}, queue_size=1024)
    expected = []
    events = []
    # Includes session changes, repeated samples and a short final write.
    for index in range(75):
        session, seq = ("first", index) if index < 37 else ("second", index-37)
        stamp = 1_000_000_000_000_000+index*10_000_000+123
        q = [float(index)+axis/123456789 for axis in range(7)]
        ticks = 1024+index*30 if index % 2 else None
        state = {"session": session, "clock_offset_ns": 13, "clock_uncertainty_ns": 17,
                 "sample": {"valid": True, "seq": seq, "captured_ns": stamp,
                            "available_ns": stamp+19, "source_stamp": index+.123456789012345,
                            "pose_source_stamp": index+.234567890123456,
                            "q": q, "dq": [x/3 for x in q],
                            "pose": {"p": q[:3], "q": [0., 0., 0., 1.]},
                            "gripper": {"valid": ticks is not None, "position_ticks": ticks,
                                        "open_ticks": 3766, "closed_ticks": 1024}}}
        assert episode.put("state", state)
        assert episode.put("state", state)  # Duplicate feedback must not add a row.
        assert episode.put("state", dict(state, sample=dict(state["sample"], valid=False)))
        episode.command({"b_ns": stamp, "q": q, "seq": index, "feedback_seq": seq,
                         "generation": 3, "gripper": -1 if index % 2 else None}, 2, 23, 29)
        event = {"reason": "precision", "a_ns": stamp, "index": index, "q": q}
        assert episode.put("event", event)
        events.append(event)
        expected.append((stamp, seq, q, ticks))
    assert episode.finish(True, {"capture_closed": True}).wait(5)
    assert episode.error is None
    with h5py.File(episode.result/"raw.hdf5", "r") as data:
        assert data.attrs["complete"]
        assert all(len(ds) == 75 for name in ("states", "commands") for ds in data[name].values())
        assert data["states/ns"].dtype == np.int64
        assert data["states/q"].dtype == np.float64
        assert data["states/gripper_ticks_valid"].dtype == np.bool_
        # Uncalibrated metric placeholders are no longer written.
        assert "gripper_position_m" not in data["states"] and "gripper_valid" not in data["states"]
        np.testing.assert_array_equal(data["states/captured_ns"][:], [x[0]+13 for x in expected])
        np.testing.assert_array_equal(data["states/ns"][:], [x[0]+32 for x in expected])
        np.testing.assert_array_equal(data["states/source_seq"][:], [x[1] for x in expected])
        np.testing.assert_array_equal(data["states/q"][:], [x[2] for x in expected])
        np.testing.assert_array_equal(data["states/source_stamp"][:], [i+.123456789012345 for i in range(75)])
        np.testing.assert_array_equal(data["states/pose_source_stamp"][:], [i+.234567890123456 for i in range(75)])
        np.testing.assert_array_equal(data["states/gripper_position_ticks"][:], [x[3] or 0 for x in expected])
        np.testing.assert_array_equal(data["states/gripper_ticks_valid"][:], [x[3] is not None for x in expected])
        np.testing.assert_allclose(data["states/gripper_open_fraction"][:],
                                   [(x[3]-1024)/2742 if x[3] is not None else np.nan for x in expected])
        np.testing.assert_array_equal(data["commands/ns"][:], [x[0]+23 for x in expected])
        np.testing.assert_array_equal(data["commands/seq"][:], list(range(75)))
        np.testing.assert_array_equal(data["commands/q"][:], [x[2] for x in expected])
        np.testing.assert_array_equal(data["commands/gripper"][:], [-1 if i % 2 else 0 for i in range(75)])
    assert [json.loads(line) for line in (episode.result/"events.jsonl").read_text(encoding="utf-8").splitlines()] == events


def test_dynamixel_ticks_are_recorded_without_claiming_metric_finger_distance(tmp_path):
    episode = Episode(tmp_path, {})
    episode.state({"session": "ticks", "clock_offset_ns": 0, "clock_uncertainty_ns": 0,
                   "sample": {"valid": True, "seq": 1, "captured_ns": 123,
                              "q": [0.]*7, "dq": [0.]*7,
                              "pose": {"p": [0.]*3, "q": [0., 0., 0., 1.]},
                              "gripper": {"valid": True, "position_ticks": -23,
                                          "target_ticks": 1024, "age_ms": 12.5}}})
    assert episode.finish(True).wait(5)
    assert episode.error is None
    with h5py.File(episode.result/"raw.hdf5") as data:
        assert data['states/gripper_position_ticks'][0] == -23
        assert data['states/gripper_ticks_valid'][0]
        assert data['states/gripper_target_ticks'][0] == 1024
        assert data['states/gripper_feedback_age_ms'][0] == 12.5
        assert 'gripper_valid' not in data['states']
        # No encoder endpoints: no normalized value is invented.
        assert np.isnan(data['states/gripper_open_fraction'][0])


def test_telemetry_write_failure_never_marks_saved_capture_complete(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk_full_during_rows")
    monkeypatch.setattr(h5py.Dataset, "resize", fail)
    episode = Episode(tmp_path, {})
    episode.command({"b_ns": 123, "q": [0.]*7, "seq": 1, "feedback_seq": 1,
                     "generation": 1}, 1, 0, 0)
    assert episode.finish(True, {"capture_closed": True}).wait(5)
    assert episode.error == "disk_full_during_rows"
    with h5py.File(episode.result/"raw.hdf5", "r") as data:
        assert not data.attrs["complete"]
    manifest = json.loads((episode.result/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["recording_error"] == episode.error


def test_end_to_end_export_and_resume(tmp_path):
    from sawyer_teleop.demo import run_demo
    result = run_demo(tmp_path, seconds=.6)
    assert result["episodes"] == 2 and result["discarded_pending_dirs"] == 0
    assert len(list((tmp_path/"demo").glob("episode_*"))) == 2
    for episode in (tmp_path/"demo").glob("episode_*"):
        with h5py.File(episode/"raw.hdf5") as raw:
            assert len(raw["commands/gripper"]) == len(raw["commands/q"])
            assert np.all(raw["commands/gripper"][:] == -1)
            assert np.all(raw["commands/gripper_valid"][:])
            assert len(raw["states/gripper_ticks_valid"]) == len(raw["states/q"])
            # Fake has no gripper encoder feedback; do not label placeholders valid.
            assert not np.any(raw["states/gripper_ticks_valid"][:])
            assert "gripper_position_m" not in raw["states"]
    with h5py.File(tmp_path/"dataset.hdf5") as file:
        normal = json.loads(file["data"].attrs["action_normalization"])
        for demo in file["data"].values():
            actions = demo["actions"][:]
            assert np.isfinite(actions).all() and np.abs(actions).max() <= 1.00001
            assert np.allclose(actions*np.array(normal["scale"])+normal["center"],
                               demo["actions_raw"][:], atol=1e-7)
            assert demo["obs/front_image"].dtype == np.uint8
            assert demo["obs/front_image"].shape[-1] == 3
            assert np.all(np.diff(demo["timestamps_ns"][:]) == 20_000_000)
        train, valid = file["mask/train"][:], file["mask/valid"][:]
        assert len(train) and len(valid) and not set(train) & set(valid)


@pytest.mark.parametrize('save', [True, False], ids=['retain', 'discard'])
def test_close_failure_finishes_and_never_promotes_capture(tmp_path, monkeypatch, save):
    original = h5py.File
    class CloseFailure:
        def __init__(self, *args, **kwargs):
            self.file = original(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.file, name)
        def close(self):
            self.file.close()
            raise OSError('close_failed')
    monkeypatch.setattr(h5py, 'File', CloseFailure)
    episode = Episode(tmp_path, {})
    assert episode.finish(save, {'capture_closed': True}).wait(1)
    assert 'close_failed' in episode.error
    assert not list(tmp_path.glob('episode_*'))
    if save:
        assert episode.result == episode.path and episode.path.name.startswith('.pending_')
        assert 'close_failed' in json.loads((episode.path/'manifest.json').read_text())['recording_error']
    else:
        assert episode.result == 'discarded' and not list(tmp_path.iterdir())
