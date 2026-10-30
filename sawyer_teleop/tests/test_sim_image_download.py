"""Start-B image archive: download once when missing, verify SHA256, never keep a bad file."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_sim_launcher', ROOT / 'scripts/windows_sim_launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

PAYLOAD = b'Small image-archive test substitute; never loaded into Docker.'


@pytest.fixture
def cache(tmp_path, monkeypatch):
    directory = tmp_path / 'gazebo image'
    directory.mkdir()
    (directory / 'manifest.json').write_text(json.dumps(
        {'archive_sha256': hashlib.sha256(PAYLOAD).hexdigest()}))
    source = tmp_path / 'release asset.tar.gz'
    source.write_bytes(PAYLOAD)
    monkeypatch.setenv('SAWYER_SIM_IMAGE_URL', source.as_uri())
    return directory, source


def test_missing_archive_is_downloaded_and_verified(cache):
    directory, _ = cache
    archive = launcher.ensure_archive(directory)
    assert archive == directory / launcher.ARCHIVE
    assert archive.read_bytes() == PAYLOAD
    assert not list(directory.glob('*.part'))


def test_existing_archive_is_not_downloaded_again(cache, monkeypatch):
    directory, _ = cache
    (directory / launcher.ARCHIVE).write_bytes(PAYLOAD)
    monkeypatch.setenv('SAWYER_SIM_IMAGE_URL', 'file:///nonexistent/never-fetched.tar.gz')
    assert launcher.ensure_archive(directory).read_bytes() == PAYLOAD


def test_bad_download_is_deleted(cache):
    directory, source = cache
    source.write_bytes(b'tampered')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        launcher.ensure_archive(directory)
    assert list(directory.iterdir()) == [directory / 'manifest.json']


def test_unreachable_url_leaves_no_partial_file(cache, monkeypatch):
    directory, _ = cache
    monkeypatch.setenv('SAWYER_SIM_IMAGE_URL', (directory / 'missing.tar.gz').as_uri())
    with pytest.raises(RuntimeError, match='download failed'):
        launcher.ensure_archive(directory)
    assert list(directory.iterdir()) == [directory / 'manifest.json']


def test_corrupt_existing_archive_is_reported_not_deleted(cache):
    directory, _ = cache
    (directory / launcher.ARCHIVE).write_bytes(b'corrupt')
    with pytest.raises(RuntimeError, match='delete .* and retry'):
        launcher.ensure_archive(directory)
    assert (directory / launcher.ARCHIVE).exists()


def test_default_url_points_at_the_pinned_release_asset():
    assert launcher.ARCHIVE_URL.startswith('https://github.com/')
    assert launcher.ARCHIVE_URL.endswith('/' + launcher.ARCHIVE)
