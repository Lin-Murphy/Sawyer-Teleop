import subprocess
import pytest
from sawyer_teleop.devices import discover_quests


def mock_adb(monkeypatch, output):
    monkeypatch.setattr('sawyer_teleop.devices.shutil.which', lambda _: 'adb')
    monkeypatch.setattr('sawyer_teleop.devices.subprocess.run',
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, output, ''))


def test_discovery_ignores_phone_and_reports_all_quests(monkeypatch):
    mock_adb(monkeypatch, 'List of devices attached\nphone device model:Pixel\nq1 device model:Quest_3\nq2 device model:Quest_2\n')
    assert discover_quests()==['q1','q2']


def test_unauthorized_device_is_not_auto_selected(monkeypatch):
    mock_adb(monkeypatch, 'List of devices attached\nq1 unauthorized\n')
    with pytest.raises(RuntimeError, match='unauthorized'):
        discover_quests()


def test_known_serial_retained_when_model_unusual(monkeypatch):
    mock_adb(monkeypatch, 'List of devices attached\nknown device model:Unknown\n')
    assert discover_quests(preferred='known')==['known']
