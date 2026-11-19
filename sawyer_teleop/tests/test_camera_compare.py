"""Offline checks: no ROS master, USB camera, SSH or robot commands."""
import hashlib
import json

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('cv2')
pytest.importorskip('PIL')
from sawyer_teleop import camera_compare as compare


@pytest.fixture
def references(tmp_path, monkeypatch):
    dataset = compare.DATASET
    # Segment numbers are deliberately out of recording order; demo 2 is absent.
    rows = [dict(episode_index=0, frame_index=0, source_episode_index=0,
                 state=[.3, 0, 0, 0, 0, 0]),
            dict(episode_index=4, frame_index=9, source_episode_index=0,
                 state=[.1, 0, 0, 0, 0, 0])]
    documents = {
        'pose_index.json': dict(dataset=dataset, rows=rows),
        'info.json': dict(features={
            'image': dict(shape=[224, 224, 3]), 'wrist_image': dict(shape=[224, 224, 3]),
            'state': dict(names=[f'right_hand_{axis}_m' for axis in 'xyz'] +
                               [f'right_hand_rotvec_{axis}_rad' for axis in 'xyz'])}),
        'contract.json': dict(pose_frame='base/right_hand',
                              image_processing='openpi_client.image_tools.resize_with_pad',
                              members=[dict(episode='recording_a'), dict(episode='recording_b')]),
        'source_mapping.json': dict(segments=[
            dict(episode_index=0, source_index=0, source_episode='recording_a', numeric_start=100),
            dict(episode_index=4, source_index=0, source_episode='recording_a', numeric_start=0)]),
    }
    for name, value in documents.items():
        (tmp_path / name).write_text(json.dumps(value))
    source = b'def resize_with_pad(*args): pass\n'
    (tmp_path / 'pinned_image_tools.py').write_bytes(source)
    monkeypatch.setattr(compare, 'RESIZE_SHA', hashlib.sha256(source).hexdigest())
    def no_ssh(*_):
        pytest.fail('Offline checks must not fetch remote references')
    monkeypatch.setattr(compare.References, 'fetch', no_ssh)
    refs = compare.References(tmp_path, 'unused', 'unused', dataset)
    refs.load_index()
    return refs


def test_demo_endpoints_follow_source_recording_order(references):
    assert references.select('first', 1, None) == (4, 9)
    assert references.select('last', 1, None) == (0, 0)
    assert references.select('first', 2, None) is None


def test_pose_matching_uses_measured_pose_and_selected_demo(references):
    pose = dict(position=dict(x=.29, y=0, z=0), orientation=dict(x=0, y=0, z=0, w=-1))
    assert references.select('pose', None, pose) == (0, 0)
    assert references.select('pose', 2, pose) is None
    distance, angle, _ = compare.pose_distance(references.states, pose)
    assert distance[0] == pytest.approx(.01)
    assert angle[0] == pytest.approx(0)


def test_pixel_difference_excludes_padding_and_keeps_black_objects():
    reference = np.zeros((224, 224, 3), np.uint8)
    current = np.full_like(reference, 255)
    current[28:196] = 10
    metrics, heat = compare.pixel_metrics(reference, current)
    assert metrics['mae'] == 10
    assert metrics['rmse'] == 10
    assert heat.shape == (168, 224)
    assert np.all(heat == 10)


def test_blank_images_do_not_report_reliable_feature_displacement():
    rgb = np.zeros((224, 224, 3), np.uint8)
    features = compare.feature_displacement(rgb, rgb)
    assert features['confidence'] == 'insufficient'
    assert features.get('displacement_xy_px') is None


@pytest.mark.parametrize('args', [
    ['--headless'], ['--hz', '0'], ['--hz', 'nan'],
    ['--seconds', '-1'], ['--seconds', 'nan'], ['--seconds', 'inf'], ['--demo', '0'],
])
def test_invalid_arguments_fail_before_opening_devices(args, monkeypatch):
    monkeypatch.setattr(compare, 'run', lambda _: pytest.fail('Invalid input reached acquisition'))
    with pytest.raises(SystemExit) as exc:
        compare.main(args)
    assert exc.value.code == 2


def test_ros_startup_failure_closes_reference_readers(tmp_path, references, monkeypatch):
    readers = []
    class Reader:
        def __init__(self, *args):
            self.demos = references.demos
            self.resize = references.resize
            self.closed = False
            readers.append(self)

        def load_index(self):
            pass

        def close(self):
            self.closed = True

    def unavailable_ros(*_):
        raise RuntimeError('ROS unavailable')
    monkeypatch.setattr(compare, 'References', Reader)
    monkeypatch.setattr(compare, 'RosReadOnly', unavailable_ros)
    monkeypatch.setattr(compare, 'verify_resize', lambda _: None)
    with pytest.raises(RuntimeError, match='ROS unavailable'):
        compare.main(['--headless', '--seconds', '1', '--output', str(tmp_path / 'output')])
    assert readers and all(reader.closed for reader in readers)
