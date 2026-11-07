"""Single-instance startup; only loopback software sessions and temporary configs."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from sawyer_teleop.demo import wait_until
from sawyer_teleop.locking import ProcessLock
from sawyer_teleop.web_instance import config_key, existing_url, instance_file, publish_instance, remove_instance
from sawyer_teleop.web_server import WebServer, run_web
from test_web import web, OWNER

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux TCP restart behavior')
def test_web_can_restart_on_same_port_after_serving_a_request():
    server = WebServer(object(), 0)
    port = server.server_port
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
            client.sendall(f'GET / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
            # Let the server close first, leaving its port in TCP TIME_WAIT.
            response = bytearray()
            while chunk := client.recv(65536):
                response.extend(chunk)
            assert response.startswith(b'HTTP/1.0 200')
    finally:
        server.server_close()
        thread.join(3)
    replacement = WebServer(object(), port)
    replacement.server_close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux TCP listener exclusivity')
def test_web_cannot_bind_a_port_with_an_active_listener():
    server = WebServer(object(), 0)
    try:
        with pytest.raises(OSError):
            WebServer(object(), server.server_port)
    finally:
        server.server_close()


def test_authenticated_reopen_does_not_change_control_owner(web):
    session, _, _, _ = web
    server = WebServer(session, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        publish_instance(session.path, server)
        assert existing_url(session.path) == server.launch_url
        assert session.owner == OWNER and session.operator is None
        remove_instance(session.path, 'other-instance')
        assert instance_file(session.path).exists()
        remove_instance(session.path, server.instance_id)
        assert not instance_file(session.path).exists()
    finally:
        server.shutdown();server.server_close();thread.join(3)


@pytest.mark.parametrize('url', ['https://remote.invalid/', 'http://remote.invalid/#token=secret',
    'http://user:password@127.0.0.1:8780/#token=secret', 'http://127.0.0.1:8780/other#token=secret'])
def test_registry_never_opens_nonlocal_or_unexpected_urls(tmp_path, url):
    config = tmp_path/'operator.json';config.write_text('{}')
    instance_file(config).write_text(json.dumps(dict(config=config_key(config), url=url)))
    assert existing_url(config) is None


@pytest.mark.parametrize('content', ['[]', '{broken', '{}'])
def test_bad_or_stale_registry_is_not_a_running_instance(tmp_path, content):
    config = tmp_path/'operator.json';config.write_text('{}')
    instance_file(config).write_text(content)
    assert existing_url(config) is None


def test_repeat_start_reuses_url_without_constructing_a_session(monkeypatch, capsys, tmp_path):
    class Busy:
        def __init__(self, *args): pass
        def acquire(self): raise RuntimeError('busy')
    calls = []
    monkeypatch.setattr('sawyer_teleop.web_server.ProcessLock', Busy)
    monkeypatch.setattr('sawyer_teleop.web_server.existing_url', lambda path:'http://127.0.0.1:8780/#token=test')
    monkeypatch.setattr('sawyer_teleop.web_server.webbrowser.open', calls.append)
    def forbidden(*args):raise AssertionError('Must not construct a second Operator service')
    monkeypatch.setattr('sawyer_teleop.web_server.WebSession', forbidden)
    assert run_web(tmp_path/'operator.json')
    assert calls == ['http://127.0.0.1:8780/#token=test']
    assert 'Reusing' in capsys.readouterr().out


def test_legacy_running_instance_has_actionable_message_without_traceback(tmp_path, capsys):
    config = tmp_path/'operator.json';config.write_text('{}')
    lock = ProcessLock('web-config:'+str(config.resolve())).acquire()
    try:
        assert run_web(config, open_browser=False) is False
        text = capsys.readouterr().out
        assert 'Ctrl+C' in text and 'Save & End' in text and 'Traceback' not in text
    finally:
        lock.close()


def test_two_actual_processes_reuse_service_and_recover_after_first_exits(tmp_path):
    config = tmp_path/'operator.json'
    config.write_text(json.dumps(dict(adb='missing-adb-for-software-test', data_dir=str(tmp_path/'data'))))
    code = 'import sys;sys.path.insert(0,sys.argv.pop(1));from sawyer_teleop.cli import main;main()'
    command = [sys.executable, '-I', '-c', code, str(ROOT/'src'), 'web', '--config', str(config), '--port', '0', '--no-browser']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        wait_until(lambda: existing_url(config) is not None, timeout=8)
        url = existing_url(config)
        duplicate = subprocess.run(command, capture_output=True, text=True, timeout=8, creationflags=flags)
        assert duplicate.returncode == 0 and 'Reusing' in duplicate.stdout and url in duplicate.stdout
        assert not duplicate.stderr and first.poll() is None
    finally:
        first.terminate();first.communicate(timeout=5)
    assert existing_url(config) is None  # A stale JSON file cannot revive a dead process.
    third = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        wait_until(lambda: existing_url(config) is not None, timeout=8)
        assert existing_url(config) != url
    finally:
        third.terminate();third.communicate(timeout=5)
