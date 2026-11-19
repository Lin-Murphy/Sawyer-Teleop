"""Offline checks: no ROS master, USB camera, SSH or robot commands."""
import hashlib
import json
from threading import Event
import time

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('cv2')
pytest.importorskip('PIL')
from sawyer_teleop import camera_compare as compare, cameras


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
    source = b'''import numpy as np
from PIL import Image

def resize_with_pad(rgb, height, width):
    resized = Image.fromarray(rgb).resize((width, 168), Image.Resampling.BILINEAR)
    result = np.zeros((height, width, 3), np.uint8)
    result[28:196] = np.asarray(resized)
    return result
'''
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
    monkeypatch.setattr(compare, 'WINDOWS', False)
    monkeypatch.setattr(compare, 'RosReadOnly', unavailable_ros)
    with pytest.raises(RuntimeError, match='ROS unavailable'):
        compare.main(['--headless', '--seconds', '1', '--output', str(tmp_path / 'output')])
    assert readers and all(reader.closed for reader in readers)


def test_windows_compares_both_streams_without_ros_and_saves_unicode_paths(tmp_path, references, monkeypatch):
    devices = []
    monkeypatch.setattr(compare, 'WINDOWS', True)

    class Capture:
        def __init__(self, **config):
            self.config = config
            self.closed = False
            devices.append(self)

        def start(self):
            return self

        def latest(self):
            return dict(rgb=np.full((480, 640, 3), 42, np.uint8), seq=1, ns=time.perf_counter_ns())

        def close(self):
            self.closed = True

    monkeypatch.setattr(cameras, 'Camera', Capture)
    monkeypatch.setattr(compare, 'RosReadOnly', lambda *_: pytest.fail('Windows must not start ROS'))
    # Cache the last frame from original demo 1. The fixture's local Pillow resize
    # still has to match the committed training probes byte-for-byte.
    prefix = 'ep000_frame0000'
    rgb = references.resize(np.full((480, 640, 3), 42, np.uint8), 224, 224)
    meta = dict(dataset=compare.DATASET, episode_index=0, frame_index=0, images={})
    for key in ('image', 'wrist_image'):
        png = references.cache / f'{prefix}_{key}.png'
        compare.Image.fromarray(rgb).save(png)
        meta['images'][key] = dict(sha256=hashlib.sha256(png.read_bytes()).hexdigest())
    (references.cache / f'{prefix}.json').write_text(json.dumps(meta), encoding='utf-8')

    output = tmp_path / '相机对比'
    compare.main(['--headless', '--seconds', '1', '--main-device', '2', '--frame-mode', 'last',
                  '--wrist-device', 'sn9c20x', '--dataset', compare.DATASET, '--demo', '1',
                  '--reference-cache', str(references.cache), '--output', str(output)])

    assert [device.config for device in devices] == [
        dict(name='main', device=2, backend='dshow', reconnect=True),
        dict(name='wrist', device='sn9c20x', backend='sn9c20x', reconnect=True),
    ]
    assert all(device.closed for device in devices)
    final = next(output.glob('*/final'))
    observation = json.loads((final / 'observation.json').read_text(encoding='utf-8'))
    assert observation['raw_ros_observations'] == {}
    assert observation['reference']['demo_number'] == 1
    assert observation['comparison_valid']
    with compare.Image.open(final / 'comparison.png') as screenshot:
        assert screenshot.size == (1240, 790)
    for role in ('main', 'wrist'):
        assert observation['cameras'][role]['metrics']['mae'] == 0
        assert (final / f'{role}_native_rgb.png').is_file()


def test_windows_window_keeps_live_preview_after_ssh_timeout(tmp_path, monkeypatch):
    preview_shown = Event()
    monkeypatch.setattr(compare, 'WINDOWS', True)

    class Capture:
        def __init__(self, **settings):
            pass

        def latest(self):
            return np.full((480, 640, 3), 42, np.uint8), dict(received_monotonic_ns=time.perf_counter_ns())

        def close(self):
            pass

    class Window:
        closed = freeze_requested = save_requested = False

        def __init__(self, *args):
            self.root = self

        def update(self):
            pass

        def destroy(self):
            pass

        def choices(self):
            return compare.DATASET, 'first', None

        def set_demos(self, refs, mode):
            assert not refs.loaded

        def show(self, canvas):
            preview_shown.set()

    def fail_ssh(self, action, *args):
        assert action == 'index'
        assert preview_shown.wait(2), 'Camera preview must render while SSH is pending'
        raise RuntimeError('ssh: connect to host create port 22: Connection timed out')

    monkeypatch.setattr(compare, 'WindowsCamera', Capture)
    monkeypatch.setattr(compare, 'CompareWindow', Window)
    monkeypatch.setattr(compare.References, 'fetch', fail_ssh)
    output = tmp_path / 'output'
    compare.main(['--seconds', '1', '--main-device', '2', '--wrist-device', 'sn9c20x',
                  '--dataset', compare.DATASET,
                  '--reference-cache', str(tmp_path / 'empty-cache'), '--output', str(output)])

    final = next(output.glob('*/final'))
    observation = json.loads((final / 'observation.json').read_text(encoding='utf-8'))
    assert observation['reference_error'].endswith('Connection timed out')
    assert not observation['comparison_valid']
    assert observation['reference'] is None
    for role in ('main', 'wrist'):
        assert (final / f'{role}_native_rgb.png').is_file()
        assert not (final / f'{role}_diff_heatmap.png').exists()


def test_windows_default_uses_web_camera_settings_without_loading_training(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, 'WINDOWS', True)
    web_cameras = [
        dict(name='wrist', device='usb:0c45:627b', backend='sn9c20x', fps=20,
             controls=dict(exposure=99, gain=3)),
        dict(name='realsense', device=2, backend='dshow', fps=20, source_fps=30,
             controls=dict(auto_exposure=True, gain=0)),
    ]
    config = tmp_path / 'operator.json'
    config.write_text(json.dumps({'cameras': web_cameras}), encoding='utf-8')
    opened = []

    class Capture:
        def __init__(self, **settings):
            opened.append(settings)

        def latest(self):
            return np.full((480, 640, 3), 42, np.uint8), dict(received_monotonic_ns=time.perf_counter_ns())

        def close(self):
            pass

    def no_training(*args):
        pytest.fail('Local camera preview must never request training data')

    monkeypatch.setattr(compare, 'WindowsCamera', Capture)
    monkeypatch.setattr(compare.References, 'load_index', no_training)
    output = tmp_path / 'output'
    cache = tmp_path / 'unused-cache'
    compare.main(['--headless', '--seconds', '.25', '--output', str(output), '--reference-cache', str(cache)],
                 default_operator_config=lambda: config)

    assert opened == list(reversed(web_cameras))
    assert not cache.exists()
    final = next(output.glob('*/final'))
    observation = json.loads((final / 'observation.json').read_text(encoding='utf-8'))
    assert observation['reference_error'] is None
    assert observation['reference_selection']['dataset'] is None
    assert all((final / f'{role}_native_rgb.png').is_file() for role in ('main', 'wrist'))


def test_windows_uses_saved_local_episode_as_offline_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, 'WINDOWS', True)
    batch = tmp_path / 'data' / 'batch_local'
    episode = batch / 'episode_20261110T173719_demo'
    episode.mkdir(parents=True)
    (episode / 'manifest.json').write_text(json.dumps(
        dict(complete=True, cameras=['realsense', 'wrist'])), encoding='utf-8')
    (episode / 'states.jsonl').touch()
    for name, timestamps, levels in (
        ('realsense', (100, 130, 170), (10, 30, 50)),
        ('wrist', (110, 140, 160), (15, 35, 55)),
    ):
        folder = episode / 'cameras' / name
        folder.mkdir(parents=True)
        (folder / 'frames.jsonl').write_text(''.join(f'{{"ns":{stamp}}}\n' for stamp in timestamps), encoding='utf-8')
        (folder / 'info.json').write_text(json.dumps(dict(shape=[480, 640, 3], dtype='uint8')), encoding='utf-8')
        (folder / 'frames.rgb').write_bytes(b''.join(bytes([level]) * (480 * 640 * 3) for level in levels))
    config = tmp_path / 'config' / 'operator.json'
    config.parent.mkdir()
    config.write_text(json.dumps(dict(data_dir='../data', cameras=[])), encoding='utf-8')

    class Capture:
        def __init__(self, **settings):
            self.level = 50 if settings['name'] == 'main' else 55

        def latest(self):
            return np.full((480, 640, 3), self.level, np.uint8), dict(received_monotonic_ns=time.perf_counter_ns())

        def close(self):
            pass

    def no_ssh(*args):
        pytest.fail('The local batch must not use SSH')

    monkeypatch.setattr(compare, 'WindowsCamera', Capture)
    monkeypatch.setattr(compare.References, 'fetch', no_ssh)
    output = tmp_path / 'output'
    compare.main(['--headless', '--seconds', '1', '--frame-mode', 'last', '--demo', '1',
                  '--main-device', '2', '--wrist-device', 'sn9c20x', '--output', str(output)],
                 default_operator_config=lambda: config)

    final = next(output.glob('*/final'))
    observation = json.loads((final / 'observation.json').read_text(encoding='utf-8'))
    reference = observation['reference']
    assert observation['comparison_valid']
    assert reference['source'] == 'local_recording'
    assert reference['recording_path'] == str(episode)
    assert reference['target_ns'] == 160
    assert reference['images']['main']['timestamp_ns'] == 170
    assert reference['images']['wrist']['timestamp_ns'] == 160
    for role in ('main', 'wrist'):
        assert observation['cameras'][role]['metrics']['mae'] == 0
        assert (final / f'{role}_recorded.png').is_file()
