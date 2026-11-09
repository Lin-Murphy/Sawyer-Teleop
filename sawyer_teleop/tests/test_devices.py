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


from sawyer_teleop.devices import enable_wireless, quest_wifi_ip


IP_OUT = '27: wlan0: <BROADCAST,UP> mtu 1500\n    inet 192.168.1.42/24 brd 192.168.1.255 scope global wlan0\n'


def scripted_adb(monkeypatch, connect_replies, wireless_listed=True):
    calls = []
    replies = list(connect_replies)
    def run(cmd, **kw):
        args = cmd[1:]
        calls.append(args)
        out = ''
        if args[:2] == ['devices', '-l']:
            out = 'List of devices attached\nUSBQ device model:Quest_3\n'
        elif args == ['devices']:
            out = 'List of devices attached\nUSBQ device\n' + ('192.168.1.42:5555 device\n' if wireless_listed else '')
        elif 'addr' in args:
            out = IP_OUT
        elif args[0] == 'connect':
            out = replies.pop(0) if replies else 'failed to connect'
        return subprocess.CompletedProcess(cmd, 0, out, '')
    monkeypatch.setattr('sawyer_teleop.devices.shutil.which', lambda _: 'adb')
    monkeypatch.setattr('sawyer_teleop.devices.subprocess.run', run)
    return calls


def test_wifi_ip_parsing_skips_loopback():
    assert quest_wifi_ip(IP_OUT) == '192.168.1.42'
    assert quest_wifi_ip('inet 127.0.0.1/8 scope host lo') is None
    assert quest_wifi_ip('') is None


def test_enable_wireless_tcpip_then_connect_with_retry(monkeypatch):
    calls = scripted_adb(monkeypatch, ['failed to connect', 'connected to 192.168.1.42:5555'])
    assert enable_wireless(sleep=lambda _: None) == '192.168.1.42:5555'
    assert ['-s', 'USBQ', 'tcpip', '5555'] in calls
    assert calls.index(['-s', 'USBQ', 'tcpip', '5555']) < calls.index(['connect', '192.168.1.42:5555'])


def test_enable_wireless_fails_without_listed_device(monkeypatch):
    scripted_adb(monkeypatch, [], wireless_listed=False)
    with pytest.raises(RuntimeError, match='same network'):
        enable_wireless(wait_s=0, sleep=lambda _: None)


def test_enable_wireless_needs_wifi_address(monkeypatch):
    scripted_adb(monkeypatch, [])
    monkeypatch.setattr('sawyer_teleop.devices.quest_wifi_ip', lambda _: None)
    with pytest.raises(RuntimeError, match='Wi-Fi address'):
        enable_wireless(sleep=lambda _: None)
