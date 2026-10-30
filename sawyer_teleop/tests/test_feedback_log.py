"""Persistent diagnostics are bounded, survive restarts and never read/control a backend."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from sawyer_teleop.feedback_log import FeedbackLog
from sawyer_teleop import cli


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_raw_feedback_fault_events_and_restart_survive(tmp_path):
    path = tmp_path/'feedback.jsonl'
    log = FeedbackLog(path)
    log.start({'backend': 'intera'})
    state = dict(b_ns=10, sample=dict(q=[.2]*7, dq=[.1]*7, pose={'p': [.1,.2,.3]},
                 robot=dict(stopped=True, error=True, estop_button=2, estop_source=3)),
                 feedback_valid=False, feedback_age_ms=150, native_stop_failed=True,
                 events=[dict(seq=7, reason='trajectory_following_error')])
    log.snapshot(state)
    log.snapshot(state)
    log.close()
    other = FeedbackLog(path)
    other.start({'backend': 'intera'})
    other.snapshot(state)
    other.close()
    result = rows(path)
    assert result[1]['state']['sample'] == state['sample']
    assert result[1]['state']['native_stop_failed']
    assert result[1]['events'] == state['events'] and result[2]['events'] == []
    assert result[4]['events'] == state['events']
    assert result[0]['run_id'] != result[3]['run_id']
    assert len(result[0]['source_sha256']['intera_backend.py']) == 64
    assert result[0]['utc'].endswith('+00:00')


def test_rotates_without_unbounded_files_or_partial_json(tmp_path):
    path = tmp_path/'feedback.jsonl'
    log = FeedbackLog(path, max_bytes=1500, backups=3)
    for n in range(50):
        log.write('feedback', number=n, sample='x'*400)
    log.close()
    files = list(tmp_path.iterdir())
    assert len(files) == 4
    assert all(p.stat().st_size <= 1500 for p in files)
    assert all(rows(p) for p in files)
    assert rows(path)[-1]['number'] == 49


def test_oversize_nan_and_disk_failure_are_observable(tmp_path, monkeypatch):
    log = FeedbackLog(tmp_path/'feedback.jsonl')
    with pytest.raises(ValueError):
        log.write('feedback', value=float('nan'))
    with pytest.raises(ValueError, match='too_large'):
        log.write('feedback', value='x'*(256*1024))
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(log.handler, '_open', fail)
    with pytest.raises(OSError, match='disk full'):
        log.snapshot({'events': [{'seq': 1, 'reason': 'fault'}]})
    assert log.last_event == -1  # Failed event write must not count as persisted.
    log.close()


def arguments(**values):
    return NS(**dict(dict(backend='fake', host='127.0.0.1', control_port=0, ik_port=0,
        state_timeout=.1, reference_timeout=.12, command_timeout=.2, hz=100,
        tip='right_hand', feedback_log=None, no_feedback_log=False, status_file=None), **values))


def test_default_intera_path_opt_out_and_startup_errors(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    seen = []
    def serve(args, log):
        seen.append(log.path if log else None)
        if log:
            raise RuntimeError('ROS master unavailable')
    monkeypatch.setattr(cli, '_serve_bridge', serve)
    with pytest.raises(RuntimeError, match='ROS master'):
        cli._run_bridge(arguments(backend='intera'))
    path = tmp_path/'sawyer-teleop/feedback.jsonl'
    assert rows(path)[-1]['kind'] == 'service_error'
    cli._run_bridge(arguments(backend='intera', no_feedback_log=True))
    cli._run_bridge(arguments())
    assert seen == [path, None, None]


def test_logging_error_never_stops_service_and_never_queries_backend(tmp_path, monkeypatch, capsys):
    from sawyer_teleop.backends import FakeBackend
    from sawyer_teleop.bridge import BridgeCore
    from sawyer_teleop import server
    backend = FakeBackend()
    core = BridgeCore(backend)
    def forbidden():
        raise AssertionError('Diagnostics queried backend')
    monkeypatch.setattr(backend, 'snapshot', forbidden)
    class Stop:
        calls = 0
        def wait(self, interval):
            self.calls += 1
            return self.calls > 2
        def set(self):
            pytest.fail('Logging failure stopped the service')
    service = NS(core=core, stop=Stop(), error=None, control_port=0, ik_port=0)
    closed = []
    service.close = lambda: closed.append(True)
    service.start = lambda: service
    monkeypatch.setattr(server, 'BridgeServer', lambda *a: service)
    monkeypatch.setattr(cli.signal, 'signal', lambda *a: None)
    class BrokenLog:
        def snapshot(self, state):
            raise OSError('disk full')
    cli._serve_bridge(arguments(), BrokenLog())
    assert closed == [True] and service.stop.calls == 3
    assert capsys.readouterr().err.count('feedback_log_write_failed') == 1


def test_effective_arm_configuration_survives_stop_and_is_persisted(tmp_path):
    import hashlib
    from test_joint_protection import rig
    for mode in ('coordinated', 50):
        clock, backend, core = rig(mode)
        config = core.telemetry()['last_arm_configuration']
        options = config['configuration']
        expected = hashlib.sha256(json.dumps(options, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        assert config['sha256'] == expected
        assert config['session'] == core.session and config['generation'] == core.generation
        if mode == 50:
            assert options['execution_mode'] == 'intera_cartesian'
            assert options['cartesian_options']['send_hz'] == 50
            assert options['trajectory'] is None
        else:
            assert options['trajectory']['profile'] == 'coordinated'
            assert options['trajectory_algorithm'] == core.trajectory.algorithm
            assert options['cartesian_options'] is None
        core.request(dict(type='hold', session=core.session))
        assert not core.armed and core.last_arm_configuration == config
        log = FeedbackLog(tmp_path/str(mode)/'feedback.jsonl')
        log.snapshot(core.diagnostics())
        log.close()
        result = rows(log.path)[0]
        assert result['state']['last_arm_configuration'] == config
        event = next(e for e in result['events'] if e['reason'] == 'control_configuration')
        assert event['details'] == config
