import subprocess
import time
import pytest
from sawyer_teleop.quest_connection import QuestConnection

@pytest.mark.parametrize("state", ["device", "unauthorized"])
def test_usb_monitor_only_lists_devices(monkeypatch, state):
    monitor = QuestConnection("test-quest")
    calls = []
    def command(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0,
            ("List of devices attached\ntest-quest\t"+state+"\n").encode(), b"")
    monkeypatch.setattr(monitor, "_command", command)
    monitor.start()
    try:
        deadline = time.monotonic()+2
        while monitor.snapshot()["usb"] == "Checking USB" and time.monotonic()<deadline:
            time.sleep(.01)
        expected = "Authorized" if state == "device" else "unauthorized"
        assert expected in monitor.snapshot()["usb"]
        assert calls and all(args == ["devices"] for args in calls)
    finally:
        monitor.close()


@pytest.mark.parametrize("serial,link", [("test-quest", "USB"), ("192.168.1.42:5555", "Wi-Fi")])
def test_monitor_reports_link_type(monkeypatch, serial, link):
    monitor = QuestConnection(serial)
    monkeypatch.setattr(monitor, "_command", lambda args: subprocess.CompletedProcess(args, 0,
        ("List of devices attached"+chr(10)+serial+chr(9)+"device"+chr(10)).encode(), b""))
    monitor.start()
    try:
        deadline = time.monotonic()+2
        while "Authorized" not in monitor.snapshot()["usb"] and time.monotonic()<deadline:
            time.sleep(.01)
        assert monitor.snapshot()["link"] == link
    finally:
        monitor.close()
