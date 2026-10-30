"""Headless A session for the web UI. Operator alone owns control and recording."""
from collections import deque, OrderedDict
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from .cameras import Camera
from .client import RobotClient
from .connection_profiles import BACKENDS, DEFAULT_BACKEND, connection_ports
from .devices import discover_quests
from .display_feedback import Posture, feedback_message
from .input_status import expire_status
from .motion_profiles import CARTESIAN_RATES, MOTION_PROFILES, selected_motion_profile, motion_profile_reason


def profile_reason(profile, backend, capability):
    """Selection reason for the web page.

    Before a handshake (no capability yet) native options stay selectable for
    backends that can provide them; Operator checks B's reported capability at
    connect and refuses to arm if it is missing. Greying them out made Gazebo
    users unable to pick them before or, with remote IK, after connecting.
    """
    if capability is None and profile in CARTESIAN_RATES and backend in ('intera', 'gazebo'):
        return None
    return motion_profile_reason(profile, backend, capability)
from .operator import Operator
from .quest import QuestInput
from .quest_connection import QuestConnection


class SessionError(ValueError):
    pass


def recording_busy(state):
    return bool(state.get('auto_recording') or state.get('recording_phase') in
                ('recording', 'review', 'finishing', 'countdown'))


def json_safe(value):
    """No NaN or numpy scalars cross the JSON boundary."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, 'tolist'):
        return json_safe(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class WebSession:
    LEASE_SECONDS = 3.0  # UI supervision only; existing motion deadlines are unchanged.

    def __init__(self, config_path, operator_factory=None, display=None):
        self.path = Path(config_path).resolve()
        self.config = json.loads(self.path.read_text(encoding='utf-8'))
        self.lock = threading.RLock()
        self.operator_factory = operator_factory or self._make_operator
        self.display = display or QuestConnection(self.config.get('quest_serial'), self.config.get('adb', 'adb'))
        self.operator = None
        self.phase = 'disconnected'
        self.message = 'Ready. Connect B and Quest to begin.'
        self.events = deque(maxlen=600)
        self.event_seq = 0
        self.owner = None
        self.owner_seen = 0.0
        self.request_seq = 0
        self.replies = OrderedDict()
        self.job = 0
        self.worker = None
        self.pending_host = None
        self.profile_request = 0
        self.pending_profile = None
        self.devices = []
        self.scanning = False
        self.scan_worker = None
        self.posture = Posture()
        self.last_recording = None
        self.last_motion = 0.0
        self.last_motion_value = None
        self.closed = threading.Event()
        self.thread = None
        self.log('Web operator ready. Recording starts only with Start Recording.')

    def start(self):
        self.display.start()
        self.thread = threading.Thread(target=self._watch, name='web-session', daemon=True)
        self.thread.start()
        return self

    def log(self, text, kind='event', timestamp=None):
        with self.lock:
            self.event_seq += 1
            self.events.append(dict(seq=self.event_seq, time=timestamp or time.strftime('%H:%M:%S'),
                                    kind=kind, text=str(text)))

    def _save(self, **changes):
        # Merge into the current file so unrelated local settings survive.
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        saved.update(changes)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                         prefix='.operator-web-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(saved, stream, ensure_ascii=False, indent=2)
        try:
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self.config.update(changes)

    def defaults(self):
        history = self.config.get('robot_host_history', [])
        history = list(dict.fromkeys(x.strip() for x in history if isinstance(x, str) and x.strip()))[:10] if isinstance(history, list) else []
        motion = self.config.get('motion', {})
        output = Path(self.config.get('data_dir', '../data')).expanduser()
        if not output.is_absolute():
            output = self.path.parent / output
        return dict(backend=self.config.get('connection_backend', DEFAULT_BACKEND),
                    host=history[0] if history else self.config.get('robot_host', ''), history=history,
                    serial=self.config.get('quest_serial') or '',
                    smoothing=motion.get('enabled', False), smoothing_ms=motion.get('time_constant_ms', 40.0),
                    profile=selected_motion_profile(self.config.get('motion_profile'),
                        self.config.get('gazebo_max_acceleration_rad_s2')),
                    joint_protection=self.config.get('joint_protection_enabled', True),
                    cameras=self.config.get('record_cameras_enabled', False), output=str(output.resolve()),
                    fast_available=self.config.get('gazebo_max_acceleration_rad_s2') is not None)

    def claim(self, client):
        with self.lock:
            if self.closed.is_set():
                raise SessionError('Service is shutting down.')
            self._expire_owner()
            if self.owner not in (None, client):
                raise SessionError('Another page owns the controls. This page is read-only.')
            if self.owner is None:
                self.owner = client
                self.request_seq = 0
                self.replies.clear()
                self.log('This browser page acquired the controls.')
            self.owner_seen = time.monotonic()

    def release(self, client):
        with self.lock:
            if self.owner == client:
                self.owner = None
                self._disconnect(force=True, reason='Browser page closed or refreshed')

    def _expire_owner(self):
        if self.owner and time.monotonic()-self.owner_seen > self.LEASE_SECONDS:
            self.owner = None
            self._disconnect(force=True, reason='Browser connection lost for 3 seconds')

    def _watch(self):
        while not self.closed.wait(.1):
            with self.lock:
                try:
                    self._expire_owner()
                    self._refresh()
                except Exception as exc:
                    self.log('Session monitor failed: '+str(exc), 'error')
                    self.owner = None
                    self._disconnect(force=True, reason='Session monitor failed')

    def _refresh(self):
        op = self.operator
        if op is None or self.phase == 'disconnecting':
            return {}
        state = op.view()
        for entry in op.drain_ui_events():
            kind = 'motion' if entry['text'].startswith('Buttons · ') else entry.get('kind', 'event')
            self.log(entry['text'], kind, entry.get('time'))
        if self.pending_host and state.get('connected_host') == self.pending_host:
            host, self.pending_host = self.pending_host, None
            history = [host]+[x for x in self.defaults()['history'] if x != host]
            try:
                self._save(robot_host=host, robot_host_history=history[:10])
            except OSError as exc:
                self.log('Connected; could not save address history: '+str(exc), 'warning')
        if self.phase == 'connecting' and (state.get('robot_connected') or state.get('mode') != 'starting'):
            self.phase = 'connected' if state.get('robot_connected') else 'error'
        if state.get('mode') == 'error':
            self.phase = 'error'
        result = state.get('motion_profile_result') or {}
        if self.pending_profile is not None and result.get('request_id') == self.pending_profile:
            self.pending_profile = None
            if result.get('accepted'):
                try:
                    self._save(motion_profile=result['profile'],
                               joint_protection_enabled=result.get('joint_protection', True))
                except OSError as exc:
                    self.log('Applied for this session; configuration save failed: '+str(exc), 'warning')
            else:
                self.log('Motion option unchanged: '+result.get('error', 'Rejected'), 'warning')
        self.message = state.get('message', self.message)
        if time.monotonic()-self.last_motion >= .5:
            quest = expire_status(state.get('quest', {}))
            hand = quest.get('hands', {}).get('r', {})
            motion = dict(joints=state.get('joints'), position=hand.get('position'),
                          grip=hand.get('grip'), trigger=hand.get('trigger'))
            value = json.dumps(json_safe(motion), sort_keys=True)
            if value != self.last_motion_value:
                self.log('Motion · '+value, 'motion')
                self.last_motion_value = value
            self.last_motion = time.monotonic()
        return state

    def snapshot(self, client, after=0, show_robot=False):
        with self.lock:
            self._expire_owner()  # A late poll cannot renew an already expired lease.
            if self.owner == client:
                self.owner_seen = time.monotonic()
            state = self._refresh()
            state['quest'] = expire_status(state.get('quest', {}))
            defaults = self.defaults()
            backend = state.get('backend', defaults['backend'])
            profiles = []
            for key, (label, description) in MOTION_PROFILES.items():
                reason = profile_reason(key, backend, state.get('intera_cartesian'))
                if key == 'fast_follow' and backend == 'gazebo' and self.config.get('gazebo_max_acceleration_rad_s2') is None:
                    reason = 'Fast follow requires configured Gazebo acceleration limits.'
                profiles.append(dict(value=key, label=label, description=description, reason=reason))
            posture = None
            if show_robot:
                self.posture.accept(feedback_message(self.operator if self.phase != 'disconnecting' else None))
                posture = dict(session=self.posture.session, status=self.posture.status,
                    backend=self.posture.backend, fresh=self.posture.fresh(),
                    remaining_ms=max(0, (self.posture.expires_ns-time.monotonic_ns())/1e6),
                    frames=self.posture.frames.tolist() if self.posture.frames is not None else None,
                    tip=self.posture.chain.tip_frame if self.posture.chain else '')
            return json_safe(dict(phase=self.phase, message=self.message, state=state, defaults=defaults,
                owner=self.owner == client, occupied=self.owner is not None, lease_seconds=self.LEASE_SECONDS,
                has_session=self.operator is not None,
                pending_profile=self.pending_profile, usb=self.display.snapshot().get('usb'),
                devices=self.devices, scanning=self.scanning, backends=BACKENDS, profiles=profiles,
                events=[x for x in self.events if x['seq'] > after], event_seq=self.event_seq,
                posture=posture, last_recording=self.last_recording))

    def command(self, client, sequence, action, options):
        with self.lock:
            self._expire_owner()
            if self.closed.is_set() or self.owner != client:
                raise SessionError('This page does not own the controls. Acquire control and reconnect if needed.')
            if (type(sequence) is not int or sequence < 1 or not isinstance(action, str)
                    or not isinstance(options, dict)):
                raise SessionError('Invalid request sequence or options.')
            fingerprint = json.dumps([action, options], sort_keys=True, allow_nan=False)
            if sequence in self.replies:
                previous, result = self.replies[sequence]
                if previous != fingerprint:
                    raise SessionError('Request sequence reused with different data.')
                return result
            if sequence <= self.request_seq:
                raise SessionError('Request expired; it will not be replayed.')
            self.request_seq = sequence
            self.owner_seen = time.monotonic()
            try:
                result = {'ok': True, 'result': self._command(action, options)}
            except (ValueError, RuntimeError, OSError) as exc:
                self.log(str(exc), 'warning')
                result = {'ok': False, 'error': str(exc)}
            self.replies[sequence] = (fingerprint, result)
            while len(self.replies) > 128:
                self.replies.popitem(last=False)
            return result

    def _settings(self, options):
        settings = dict(self.defaults(), **options)
        if not isinstance(settings['backend'], str) or settings['backend'] not in dict(BACKENDS):
            raise SessionError('Unknown backend.')
        for key in ('host', 'serial'):
            if not isinstance(settings[key], str) or len(settings[key]) > 255:
                raise SessionError('Invalid '+key)
            settings[key] = settings[key].strip()
        if not settings['host'] or any(c.isspace() for c in settings['host']):
            raise SessionError('Enter a B hostname or IP address without spaces.')
        for key in ('smoothing', 'joint_protection', 'cameras'):
            if type(settings[key]) is not bool:
                raise SessionError('Invalid '+key)
        tau = settings['smoothing_ms']
        original = self.config.get('motion', {}).get('time_constant_ms', 40.)
        if type(tau) not in (int, float) or not math.isfinite(tau) or tau not in (20., 40., 150., original):
            raise SessionError('Select Light, Heavy, DROID-like, or the existing custom smoothing value.')
        if not isinstance(settings['profile'], str) or settings['profile'] not in MOTION_PROFILES:
            raise SessionError('Unknown motion option.')
        # Normalize backend choices. Real fast limits are selected independently in Operator.
        if settings['backend'] != 'gazebo' and settings['profile'] not in CARTESIAN_RATES and not (settings['backend'] == 'intera' and settings['profile'] == 'fast_follow'):
            settings['profile'] = 'standard'
        if settings['backend'] not in ('intera', 'gazebo') and settings['profile'] in CARTESIAN_RATES:
            settings['profile'] = 'standard'
        return settings

    def _make_operator(self, settings):
        backend = settings['backend']
        control_port, ik_port = connection_ports(backend)
        return Operator(RobotClient(settings['host'], control_port, ik_port,
            self.config.get('state_timeout_s', .1), expected_backend=backend,
            ik_mode=self.config.get('ik_mode', 'local') if backend == 'gazebo' else 'remote',
            feedback_profile=self.config.get('feedback_profile', 'standard') if backend == 'gazebo' else 'standard',
            defer_remote_ik=(settings['profile'] == 'intera_pose_50hz')),
            QuestInput(settings['serial'] or None, self.config.get('adb', 'adb')),
            [Camera(**x) for x in self.config.get('cameras', [])] if settings['cameras'] else [],
            self.defaults()['output'], self.config.get('operator_hz', 50),
            self.config.get('position_scale', 1.),
            dict(self.config.get('motion', {}), enabled=settings['smoothing'], time_constant_ms=settings['smoothing_ms']),
            self.config.get('trajectory'), self.config.get('gazebo_max_acceleration_rad_s2'),
            settings['profile'], joint_protection=settings['joint_protection'])

    def _connect(self, options):
        if self.operator is not None or self.phase in ('connecting', 'disconnecting'):
            raise SessionError('Disconnect the existing session before connecting again.')
        settings = self._settings(options)
        control, ik = connection_ports(settings['backend'])
        self._save(connection_backend=settings['backend'], quest_serial=settings['serial'] or None,
            control_port=control, ik_port=ik, record_cameras_enabled=settings['cameras'],
            motion=dict(self.config.get('motion', {}), enabled=settings['smoothing'], time_constant_ms=settings['smoothing_ms']),
            motion_profile=settings['profile'], joint_protection_enabled=settings['joint_protection'])
        self.display.set_serial(settings['serial'] or None)
        self.phase, self.message, self.pending_host = 'connecting', 'Connecting B and Quest…', settings['host']
        self.job += 1
        job = self.job
        def connect():
            try:
                op = self.operator_factory(settings)
                with self.lock:
                    if job != self.job or self.closed.is_set() or self.owner is None:
                        op.close()
                        return
                    self.operator = op
                    op.start()
            except Exception as exc:
                with self.lock:
                    if job == self.job:
                        self.phase, self.message = 'error', 'Connection failed: '+str(exc)
                        self.pending_host = None
                        self.log(self.message, 'error')
        self.worker = threading.Thread(target=connect, name='web-connect', daemon=True)
        self.worker.start()
        self.log('Connecting to '+settings['backend']+' · '+settings['host'])

    def _disconnect(self, force=False, reason='Disconnected by operator'):
        if self.phase == 'disconnecting':
            return
        op = self.operator
        if op and not force and recording_busy(op.view()):
            raise SessionError('Use Save & End and wait for processing before disconnecting.')
        self.job += 1  # Invalidate any constructor still running.
        self.pending_host = self.pending_profile = None
        if op:
            # Signal immediately, independently of the HTTP request or writer cleanup.
            op.stop.set()
            op.wake.set()
        previous = self.worker
        self.phase, self.message = 'disconnecting', reason+'; finishing session…'
        self.log(reason, 'warning' if force else 'event')
        def cleanup():
            if previous and previous is not threading.current_thread():
                previous.join()
            try:
                if op:
                    op.close()
                    if op.thread:
                        op.thread.join()  # Do not permit another owner to overlap USB/writers.
                    finishing = op.finishing
                    if finishing:
                        finishing.done.wait()
                        self.last_recording = dict(path=str(finishing.result or ''), error=finishing.error,
                            note='Session ended unexpectedly; inspect completeness before using this recording.')
            except Exception as exc:
                self.log('Session cleanup failed: '+str(exc), 'error')
                return  # Remain disconnecting; fail closed rather than overlap resources.
            with self.lock:
                self.operator = None
                self.posture = Posture()
                self.phase, self.message = 'disconnected', reason+'. Connect and calibrate to resume.'
        self.worker = threading.Thread(target=cleanup, name='web-disconnect', daemon=True)
        self.worker.start()

    def _command(self, action, options):
        if action == 'connect':
            self._connect(options)
        elif action in ('disconnect', 'stop_session'):
            self._disconnect(force=action == 'stop_session', reason='Stopped by operator' if action == 'stop_session' else 'Disconnected by operator')
        elif action == 'scan':
            if self.scanning:
                raise SessionError('Device discovery is already running.')
            serial = options.get('serial') or None
            if serial is not None and (not isinstance(serial, str) or len(serial) > 255):
                raise SessionError('Invalid Quest serial.')
            self.scanning = True
            def scan():
                try:
                    devices = discover_quests(self.config.get('adb', 'adb'), serial)
                    with self.lock:
                        self.devices = devices
                        self.log('Authorized Quest devices: '+(', '.join(devices) or 'None'))
                except Exception as exc:
                    self.log('Device discovery failed: '+str(exc), 'warning')
                finally:
                    with self.lock:
                        self.scanning = False
            self.scan_worker = threading.Thread(target=scan, daemon=True)
            self.scan_worker.start()
        elif action == 'apply_motion':
            profile = options.get('profile')
            guard = options.get('joint_protection')
            if not isinstance(profile, str) or profile not in MOTION_PROFILES or type(guard) is not bool:
                raise SessionError('Invalid motion option or joint protection.')
            if self.pending_profile is not None or self.phase in ('connecting', 'disconnecting'):
                raise SessionError('Wait for the current operation.')
            state = self.operator.view() if self.operator else {}
            backend = state.get('backend', options.get('backend', self.defaults()['backend']))
            reason = profile_reason(profile, backend, state.get('intera_cartesian'))
            if profile == 'fast_follow' and backend == 'gazebo' and self.config.get('gazebo_max_acceleration_rad_s2') is None:
                reason = 'Fast follow requires configured Gazebo acceleration limits.'
            if reason:
                raise SessionError(reason)
            if self.operator:
                self.profile_request += 1
                self.operator.request('set_motion_profile', profile=profile, joint_protection=guard,
                                      request_id=self.profile_request)
                self.pending_profile = self.profile_request
            else:
                self._save(motion_profile=profile, joint_protection_enabled=guard)
                self.log('Motion option saved for the next connection: '+MOTION_PROFILES[profile][0])
        elif action in ('calibrate', 'reset_arm', 'stop_reset', 'start_recording', 'save', 'discard', 'end_recording', 'stop_batch'):
            if self.operator is None or self.phase != 'connected':
                raise SessionError('Connect B and Quest first.')
            parameters = {}
            if action == 'start_recording':
                allowed = {'name', 'task', 'root', 'count', 'seconds', 'pause'}
                if set(options)-allowed:
                    raise SessionError('Unknown recording option.')
                parameters = dict(options)
                root = parameters.get('root', self.defaults()['output'])
                if not isinstance(root, str) or not Path(root).expanduser().is_absolute():
                    raise SessionError('Choose an absolute output folder on the A computer.')
                parameters['root'] = root
            self.operator.request(action, **parameters)
            self.log('Requested: '+action.replace('_', ' '))
        else:
            raise SessionError('Unknown action.')
        return {'accepted': True}  # Operator snapshots, not HTTP success, confirm completion.

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            self.owner = None
            self._disconnect(force=True, reason='Web service stopped')
            worker = self.worker
        if worker:
            worker.join(timeout=20)
        if self.thread:
            self.thread.join(timeout=2)
        self.display.close()
