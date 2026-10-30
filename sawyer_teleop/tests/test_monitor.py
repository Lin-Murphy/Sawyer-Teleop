import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop import monitor


def test_monitor_tracks_coalescing_and_submission_without_backend_reads(monkeypatch):
    stamp = [1_000_000_000]
    backend = FakeBackend(lambda: stamp[0])
    core = BridgeCore(backend, clock=lambda: stamp[0])
    core.connect()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
    for seq in (1, 2):
        core.request(dict(type='target', session=core.session, generation=core.generation,
                          seq=seq, feedback_seq=core.latest['seq'], q=[float(seq)]*7))
    stamp[0] += 3_000_000
    receipt, _ = core.tick()
    assert receipt['seq'] == 2
    before = backend.publish_count
    def forbidden():
        raise AssertionError('Monitor must not query backend')
    monkeypatch.setattr(backend, 'snapshot', forbidden)
    state = core.diagnostics()
    assert state['accepted'] == 2
    assert state['counts']['submitted'] == 1
    assert state['last_target']['q'] == [2.]*7
    assert state['last_submission']['receive_to_submit_ms'] == pytest.approx(3.)
    assert backend.publish_count == before
    assert core.armed


def test_atomic_snapshot_marks_stale_and_stopped(tmp_path, monkeypatch):
    core = BridgeCore(FakeBackend())
    path = tmp_path/'status.json'
    state = monitor.write_status(path, core)
    assert json.loads(path.read_text()) == state
    assert list(tmp_path.glob('.status-*')) == []
    monkeypatch.setattr(monitor, 'ns', lambda: state['written_ns']+4_000_000_000)
    assert 'stale status' in monitor.render(state)
    state = monitor.write_status(path, core, running=False)
    assert 'Stopped' in monitor.render(state)


@pytest.mark.skipif(os.name == "nt", reason="Linux B subprocess signal lifecycle")
def test_snapshot_available_with_owner_and_shutdown_marked(tmp_path):
    status = tmp_path/'status.json'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    proc = subprocess.Popen([sys.executable, '-m', 'sawyer_teleop.cli', 'bridge',
        '--control-port', '0', '--ik-port', '0', '--status-file', str(status)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    client = None
    try:
        info = json.loads(proc.stdout.readline())
        client = RobotClient('127.0.0.1', info['control'], info['ik']).connect()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if status.exists():
                state = json.loads(status.read_text())
                if state['connected']:
                    break
            time.sleep(.02)
        else:
            pytest.fail('No connected snapshot')
        # Reading a dashboard neither takes control ownership nor closes A.
        assert 'A connected: True' in monitor.render(state)
        assert client.snapshot()['sample']['valid']
    finally:
        if client:
            client.close()
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert json.loads(status.read_text())['running'] is False
