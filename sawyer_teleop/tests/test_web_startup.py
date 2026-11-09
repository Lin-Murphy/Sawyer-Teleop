"""Single-instance startup; only loopback software sessions and temporary configs."""
import json
import os
from pathlib import Path
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


def test_repeat_start_refused_by_running_instance_does_not_construct_a_session(monkeypatch, capsys, tmp_path):
    class Busy:
        def __init__(self, *args): pass
        def acquire(self): raise RuntimeError('busy')
    monkeypatch.setattr('sawyer_teleop.web_server.ProcessLock', Busy)
    monkeypatch.setattr('sawyer_teleop.web_server.request_shutdown', lambda path: (False, 'Recording in progress.'))
    def forbidden(*args):raise AssertionError('Must not construct a second Operator service')
    monkeypatch.setattr('sawyer_teleop.web_server.WebSession', forbidden)
    assert run_web(tmp_path/'operator.json') is False
    assert 'refused to restart: Recording in progress.' in capsys.readouterr().out


def test_shutdown_endpoint_refuses_while_recording_and_stops_server_otherwise(web, monkeypatch):
    from sawyer_teleop.web_instance import request_shutdown
    session, _, _, _ = web
    server = WebServer(session, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        publish_instance(session.path, server)
        monkeypatch.setattr('sawyer_teleop.web_server.recording_busy', lambda state: True)
        class Op:
            def view(self): return {}
        session.operator = Op()
        accepted, error = request_shutdown(session.path)
        assert not accepted and 'Recording' in error and thread.is_alive()
        session.operator = None
        assert request_shutdown(session.path) == (True, None)
        thread.join(3)
        assert not thread.is_alive()
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_legacy_running_instance_has_actionable_message_without_traceback(tmp_path, capsys):
    config = tmp_path/'operator.json';config.write_text('{}')
    lock = ProcessLock('web-config:'+str(config.resolve())).acquire()
    try:
        assert run_web(config, open_browser=False) is False
        text = capsys.readouterr().out
        assert 'Ctrl+C' in text and 'did not answer' in text and 'Traceback' not in text
    finally:
        lock.close()


def test_second_process_restarts_the_first_and_takes_over(tmp_path):
    config = tmp_path/'operator.json'
    config.write_text(json.dumps(dict(adb='missing-adb-for-software-test', data_dir=str(tmp_path/'data'))))
    code = 'import sys;sys.path.insert(0,sys.argv.pop(1));from sawyer_teleop.cli import main;main()'
    command = [sys.executable, '-I', '-c', code, str(ROOT/'src'), 'web', '--config', str(config), '--port', '0', '--no-browser']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    second = None
    try:
        wait_until(lambda: existing_url(config) is not None, timeout=8)
        url = existing_url(config)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        assert first.wait(timeout=30) == 0  # Old instance exits cleanly on request.
        wait_until(lambda: existing_url(config) not in (None, url), timeout=15)
        assert second.poll() is None
    finally:
        for process in (first, second):
            if process and process.poll() is None:
                process.terminate()
            if process:
                process.communicate(timeout=10)
    assert existing_url(config) is None
