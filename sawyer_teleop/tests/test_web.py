"""Local HTTP/session acceptance with actual Operator, synthetic input and software B."""
import json
from pathlib import Path
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import h5py
import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.cameras import CameraHub, SyntheticCamera
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import SyntheticQuest, wait_until
from sawyer_teleop.operator import Operator
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.web_session import SessionError, WebSession
from sawyer_teleop.web_server import WebServer

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'web-test-owner-0001'
OBSERVER = 'web-test-observer-0002'


class NoUSB:
    def start(self): return self
    def close(self): pass
    def snapshot(self): return {'usb': 'Synthetic test; no USB access'}
    def set_serial(self, serial): pass


@pytest.fixture
def web(tmp_path):
    path = tmp_path/'operator.json'
    config = json.loads((ROOT/'config/operator.json').read_text())
    config.update(connection_backend='fake', motion_profile='standard', record_cameras_enabled=False,
                  data_dir=str(tmp_path/'data'), robot_host='old-host', robot_host_history=['old-host'])
    path.write_text(json.dumps(config))
    bridge = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    inputs = []
    def factory(settings):
        quest = SyntheticQuest()
        inputs.append(quest)
        return Operator(RobotClient('127.0.0.1', bridge.control_port, bridge.ik_port, expected_backend='fake'),
                        quest, [], tmp_path/'data')
    cameras = CameraHub([dict(name='wrist', label='Synthetic wrist', width=64, height=48, fps=30)], SyntheticCamera)
    session = WebSession(path, operator_factory=factory, display=NoUSB(), cameras=cameras).start()
    session.claim(OWNER)
    yield session, bridge, inputs, tmp_path
    session.close()
    bridge.close()


def until(session, predicate, client=OWNER, timeout=5):
    def check():
        snapshot = session.snapshot(client)
        return predicate(snapshot)
    wait_until(check, timeout)


def connect(session, seq=1):
    assert session.command(OWNER, seq, 'connect', {'backend': 'fake', 'host': '127.0.0.1'})['ok']
    until(session, lambda s: s['phase'] == 'connected' and s['state'].get('mode') == 'paused')


def calibrate(session, seq=2):
    assert session.command(OWNER, seq, 'calibrate', {})['ok']
    until(session, lambda s: s['state'].get('calibrated'))


def test_direct_pose_is_listed_in_existing_web_motion_options(web):
    session, _, _, _ = web
    profiles = {p['value']: p for p in session.snapshot(OWNER)['profiles']}
    assert 'standard' in profiles and 'intera_cartesian_50hz' in profiles
    assert profiles['intera_pose_50hz']['label'] == 'Direct pose (50 Hz, experimental)'
    assert profiles['intera_pose_50hz']['reason']  # Fake backend cannot execute this native option.


def test_ownership_and_idempotent_commands(web):
    s, bridge, inputs, root = web
    with pytest.raises(SessionError, match='Another page'):
        s.claim(OBSERVER)
    with pytest.raises(SessionError, match='does not own'):
        s.command(OBSERVER, 1, 'connect', {})
    connect(s)
    calibrate(s)
    assert not (root/'data').exists()  # No ordinary telemetry recording.
    options = dict(name='one', count=1, seconds=60, pause=0, root=str(root/'data'))
    first = s.command(OWNER, 3, 'start_recording', options)
    assert s.command(OWNER, 3, 'start_recording', options) == first
    until(s, lambda snap: snap['state'].get('recording'))
    assert s.operator.attempt_count == 1
    assert not s.command(OWNER, 4, 'disconnect', {})['ok']
    assert s.command(OWNER, 5, 'end_recording', {})['ok']
    until(s, lambda snap: snap['state'].get('recording_phase') == 'complete')
    assert s.operator.saved_count == 1
    with pytest.raises(SessionError, match='different data'):
        s.command(OWNER, 3, 'discard', {})
    assert s.command(OWNER, 6, 'disconnect', {})['ok']
    until(s, lambda snap: snap['phase'] == 'disconnected')
    assert bridge.core.submitted == 0


def test_timer_discard_save_and_batch_end_use_existing_recording_state(web):
    s, _, _, root = web
    connect(s); calibrate(s)
    options = dict(name='batch', count=3, seconds=60, pause=.1, root=str(root/'data'))
    s.command(OWNER, 3, 'start_recording', options)
    until(s, lambda snap: snap['state'].get('recording'))
    s.command(OWNER, 4, 'discard', {})
    until(s, lambda snap: snap['state'].get('discarded_count') == 1 and snap['state'].get('recording'))
    s.command(OWNER, 5, 'save', {})
    until(s, lambda snap: snap['state'].get('saved_count') == 1 and snap['state'].get('recording'))
    s.command(OWNER, 6, 'end_recording', {})
    until(s, lambda snap: snap['state'].get('recording_phase') == 'idle')
    assert s.operator.saved_count == 2 and not s.operator.auto_recording
    files = list((root/'data/batch').glob('episode_*/raw.hdf5'))
    assert len(files) == 2
    for path in files:
        with h5py.File(path) as f:
            assert f.attrs['complete'] and len(f['states/ns']) > 0


@pytest.mark.parametrize('loss', ['release', 'expired_poll', 'watchdog', 'stop_session'])
def test_ui_loss_stops_submissions_retains_incomplete_and_never_auto_resumes(web, loss):
    s, bridge, inputs, root = web
    connect(s); calibrate(s)
    until(s, lambda snap: s.operator.release_seen)
    inputs[0].grip = 1
    until(s, lambda snap: snap['state'].get('following'))
    s.command(OWNER, 3, 'start_recording', dict(name='interrupted', count=5, seconds=60, pause=0, root=str(root/'data')))
    until(s, lambda snap: snap['state'].get('recording'))
    time.sleep(.08)
    old = s.operator
    if loss == 'release':
        s.release(OWNER)
    elif loss == 'stop_session':
        s.command(OWNER, 4, 'stop_session', {})
    else:
        with s.lock:
            s.owner_seen = time.monotonic()-s.LEASE_SECONDS-.01
            if loss == 'expired_poll':
                snap = s.snapshot(OWNER)
                assert not snap['owner'] and old.stop.is_set()
    until(s, lambda snap: snap['phase'] == 'disconnected', client=OBSERVER)
    count = bridge.core.submitted
    time.sleep(.08)
    assert bridge.core.submitted == count and not bridge.core.armed
    assert old.stop.is_set() and not old.thread.is_alive()
    files = list((root/'data/interrupted').glob('episode_*/raw.hdf5'))
    assert len(files) == 1
    with h5py.File(files[0]) as f:
        assert not f.attrs['complete']
    s.claim(OWNER)
    snap = s.snapshot(OWNER)
    assert s.operator is None and not snap['state'].get('calibrated')
    assert snap['last_recording']['path']


def test_cancel_during_construction_never_starts_an_orphan_session(web):
    s, _, _, _ = web
    gate = threading.Event()
    started = threading.Event()
    factory = s.operator_factory
    made = []
    def slow(settings):
        started.set(); gate.wait(3)
        op = factory(settings); made.append(op); return op
    s.operator_factory = slow
    s.command(OWNER, 1, 'connect', {'backend': 'fake', 'host': '127.0.0.1'})
    assert started.wait(2)
    s.release(OWNER)
    gate.set()
    until(s, lambda snap: snap['phase'] == 'disconnected', client=OBSERVER)
    assert made and made[0].thread is None and made[0].stop.is_set()


def test_successful_handshake_history_and_motion_ack(web):
    s, _, _, _ = web
    connect(s)
    until(s, lambda snap: snap['defaults']['history'][0] == '127.0.0.1')
    assert s.command(OWNER, 2, 'apply_motion', {'profile': 'standard', 'joint_protection': False})['ok']
    until(s, lambda snap: snap['pending_profile'] is None and snap['defaults']['joint_protection'] is False)
    assert s.operator.joint_protection['enabled'] is False
    inputs = json.loads(s.path.read_text())
    assert inputs['ik_mode'] == 'local' and inputs['feedback_profile'] == 'gazebo_250ms'
    assert not s.command(OWNER, 3, 'apply_motion', {'profile': 'fast_follow', 'joint_protection': True})['ok']


def test_unknown_actions_and_replay_after_eviction_are_rejected(web):
    s, _, _, _ = web
    for seq in range(1, 131):
        assert not s.command(OWNER, seq, 'unknown', {})['ok']
    with pytest.raises(SessionError, match='expired'):
        s.command(OWNER, 1, 'unknown', {})
    assert len(s.events) <= 600 and len(s.replies) == 128


@pytest.mark.parametrize('action,options', [('connect', {'backend': []}),
    ('connect', {'profile': {}}), ('apply_motion', {'profile': [], 'joint_protection': True})])
def test_malformed_options_return_errors_without_ending_service(web, action, options):
    s, _, _, _ = web
    assert not s.command(OWNER, 1, action, options)['ok']
    assert s.snapshot(OWNER)['owner'] and s.operator is None


def test_http_auth_origin_assets_and_live_snapshot(web):
    session, _, _, root = web
    server = WebServer(session, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def get(path, auth=True, body=None, **headers):
        h = {'X-Sawyer-Token': server.token, 'X-Sawyer-Client': OWNER} if auth else {}
        h.update(headers)
        data = None if body is None else json.dumps(body).encode()
        if data: h['Content-Type'] = 'application/json'
        return urlopen(Request(server.origin+path, data=data, headers=h), timeout=3)
    try:
        with get('/', auth=False) as response:
            html = response.read().decode('utf-8')
            assert 'Batch recording' in html
            for label in ('A 接收 · Quest→A', 'A 发送给 B · A→B', 'B 执行提交', 'B→A 反馈接收'):
                assert label in html
            for metric in ('inputHz','targetHz','submissionHz','feedbackHz'):
                assert 'id="'+metric+'"' in html
            assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        for asset in ('/app.js', '/robot.js', '/style.css', '/favicon.svg'):
            assert get(asset, auth=False).status == 200
        for headers in ({'Origin': 'https://other.invalid'}, {'Host': 'rebind.invalid'}, {'X-Sawyer-Token': 'wrong'}):
            with pytest.raises(HTTPError) as exc:
                get('/api/state', **headers)
            assert exc.value.code == 403
        with pytest.raises(HTTPError) as exc:
            get('/api/state', auth=False)
        assert exc.value.code == 403
        assert json.load(get('/api/state'))['owner']
        until(session, lambda snap: snap['cameras'][0]['live'])
        with get('/api/camera?name=wrist') as response:
            assert response.headers['Content-Type'] == 'image/jpeg' and response.read(2) == bytes([0xff, 0xd8])
        with pytest.raises(HTTPError) as exc:
            get('/api/camera?name=missing')
        assert exc.value.code == 404
        with pytest.raises(HTTPError) as exc:
            get('/api/camera?name=wrist', auth=False)
        assert exc.value.code == 403
        assert json.load(get('/api/action', body=dict(seq=1, action='connect', options={'host':'127.0.0.1','backend':'fake'})))['ok']
        until(session, lambda snap: snap['phase'] == 'connected')
        assert json.load(get('/api/state?view=1'))['posture']['session']
        folders = json.load(get('/api/folders?path='+str(root).replace('\\','/')))
        assert Path(folders['path']) == root
        # A beacon can only release its own tab; another token/client has no ownership.
        assert json.load(get('/api/release', auth=False, body={'token':server.token,'client':OBSERVER}))['ok']
        assert session.owner == OWNER
        with pytest.raises(HTTPError):
            get('/../operator.json', auth=False)
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_camera_sliders_apply_save_and_lock_while_recording(web):
    s, _, _, root = web
    spec = s.snapshot(OWNER)['cameras'][0]['controls']
    assert spec['exposure']['min'] < spec['exposure']['max'] and 'auto_exposure' in spec
    assert s.command(OWNER, 1, 'camera_controls', {'name': 'wrist', 'controls': {'exposure': -7, 'gain': 20}})['ok']
    spec = s.snapshot(OWNER)['cameras'][0]['controls']
    assert spec['exposure']['value'] == -7 and spec['gain']['value'] == 20 and spec['auto_exposure'] is False
    saved = json.loads((root/'operator.json').read_text())
    wrist = next(c for c in saved['cameras'] if c['name'] == 'wrist')
    assert wrist['controls']['exposure'] == -7 and wrist['controls']['auto_exposure'] is False
    assert not s.command(OWNER, 2, 'camera_controls', {'name': 'wrist', 'controls': {'exposure': 999}})['ok']
    assert not s.command(OWNER, 3, 'camera_controls', {'name': 'nope', 'controls': {'gain': 1}})['ok']
    assert not s.command(OWNER, 4, 'camera_controls', {'name': 'wrist', 'controls': {'hue': 1}})['ok']
    with pytest.raises(SessionError):
        s.command(OBSERVER, 5, 'camera_controls', {'name': 'wrist', 'controls': {'gain': 1}})
    connect(s, 6); calibrate(s, 7)
    s.command(OWNER, 8, 'start_recording', dict(name='lock', count=1, seconds=60, pause=.1, root=str(root/'data')))
    until(s, lambda snap: snap['state'].get('recording'))
    result = s.command(OWNER, 9, 'camera_controls', {'name': 'wrist', 'controls': {'gain': 1}})
    assert not result['ok'] and 'locked while recording' in result['error']
    s.command(OWNER, 10, 'end_recording', {})
    until(s, lambda snap: snap['state'].get('recording_phase') in ('idle', 'complete'))
    assert s.command(OWNER, 11, 'camera_controls', {'name': 'wrist', 'controls': {'gain': 1}})['ok']
