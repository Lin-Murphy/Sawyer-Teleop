"""Offline exports must never overwrite or remove another writer's files."""
import json
from pathlib import Path
import shutil

import h5py
import numpy as np
import pytest
from sawyer_teleop import dataset


@pytest.fixture
def batch(tmp_path):
    episode = tmp_path/'batch'/'episode_test'
    episode.mkdir(parents=True)
    times = np.arange(1, 12, dtype=np.int64)*20_000_000
    with h5py.File(episode/'raw.hdf5', 'w') as data:
        data.attrs['complete'] = True
        data.attrs['metadata'] = json.dumps({'cameras': [], 'backend': 'fake'})
        data.create_group('frames')
        for group in ('commands', 'states'):
            data.create_dataset(group+'/ns', data=times)
            data.create_dataset(group+'/q', data=np.zeros((len(times), 7)))
        data.create_dataset('commands/seq', data=np.arange(len(times)))
        data.create_dataset('commands/segment', data=np.ones(len(times)))
        data.create_dataset('states/uncertainty_ns', data=np.zeros(len(times), dtype=np.int64))
        data.create_dataset('states/dq', data=np.zeros((len(times), 7)))
        data.create_dataset('states/eef_pos', data=np.zeros((len(times), 3)))
        data.create_dataset('states/eef_quat', data=np.tile([0., 0., 0., 1.], (len(times), 1)))
    return episode.parent


def test_previous_partial_is_preserved_and_export_uses_its_own_file(batch, tmp_path):
    output = tmp_path/'dataset.hdf5'
    partial = output.with_name(output.name+'.partial')
    partial.write_bytes(b'recover previous export')
    result = dataset.export_dataset(batch, output)
    assert partial.read_bytes() == b'recover previous export'
    assert result['demos'] == 1 and result['samples'] == 10
    with h5py.File(output) as data:
        assert data['data'].attrs['total'] == 10
        np.testing.assert_array_equal(data['data/demo_0/timestamps_ns'][:], np.arange(1,11)*20_000_000)
    assert list(tmp_path.glob('*.partial')) == [partial]


def test_export_excludes_quality_incomplete_even_when_capture_is_complete(batch, tmp_path):
    incomplete = batch/'episode_camera_lost'/'raw.hdf5'
    incomplete.parent.mkdir()
    shutil.copyfile(batch/'episode_test'/'raw.hdf5', incomplete)
    reasons = ['wrist:camera_lost']
    with h5py.File(incomplete, 'r+') as data:
        data.attrs['quality'] = json.dumps({'status': 'incomplete', 'reasons': reasons})
        data['commands/q'][:] = 10.  # Excluded rows must stay out of normalization, too.

    output = tmp_path/'dataset.hdf5'
    result = dataset.export_dataset(batch, output)

    assert result['episodes'] == result['demos'] == 1
    assert result['excluded'] == [{'path': str(incomplete), 'reason': 'quality_incomplete',
                                   'details': reasons}]
    with h5py.File(output) as data:
        assert data['data/demo_0'].attrs['source_episode'] == 'episode_test'
        assert json.loads(data['data'].attrs['action_normalization'])['center'] == [0.]*7
    assert incomplete.is_file()


def test_concurrent_output_wins_without_being_overwritten(batch, tmp_path, monkeypatch):
    output = tmp_path/'dataset.hdf5'
    original = dataset.os.fsync
    def race(fd):
        original(fd)
        output.write_bytes(b'another export already published')
    monkeypatch.setattr(dataset.os, 'fsync', race)
    with pytest.raises(FileExistsError):
        dataset.export_dataset(batch, output)
    assert output.read_bytes() == b'another export already published'
    assert not list(tmp_path.glob('*.partial'))


def test_failed_export_only_cleans_its_own_temporary_file(batch, tmp_path):
    output = tmp_path/'dataset.hdf5'
    partial = output.with_name(output.name+'.partial')
    partial.write_bytes(b'prior data')
    with h5py.File(batch/'episode_test/raw.hdf5', 'r+') as data:
        data['states/ns'][3] = data['states/ns'][2]
    with pytest.raises(ValueError, match='timestamps'):
        dataset.export_dataset(batch, output)
    assert partial.read_bytes() == b'prior data'
    assert not output.exists()
    assert list(tmp_path.glob('*.partial')) == [partial]
