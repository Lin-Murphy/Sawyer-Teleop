"""Bounded B diagnostics, written by the CLI/status thread, never the control loop."""
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import uuid


class _Handler(RotatingFileHandler):
    def handleError(self, record):
        # Let the CLI report write errors without stopping robot feedback.
        raise


class FeedbackLog:
    def __init__(self, path, max_bytes=5*1024*1024, backups=3):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler = _Handler(str(self.path), maxBytes=max_bytes,
                                backupCount=backups, encoding='utf-8', delay=True)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.run_id = uuid.uuid4().hex
        self.last_event = -1

    def write(self, kind, **values):
        record = dict(schema=1, kind=kind, run_id=self.run_id, pid=os.getpid(),
                      utc=datetime.now(timezone.utc).isoformat(), **values)
        message = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len((message+'\n').encode('utf-8')) > min(self.handler.maxBytes-2, 256*1024):
            raise ValueError('feedback_log_record_too_large')
        self.handler.handle(logging.LogRecord('sawyer.feedback', logging.INFO, '', 0,
                                             message, (), None))

    def start(self, configuration):
        root = Path(__file__).parent
        hashes = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                  for name in ('intera_backend.py', 'bridge.py', 'intera_cartesian.py',
                               'feedback_log.py', 'dynamixel_gripper.py', 'robot_clock.py')}
        self.write('startup', configuration=configuration, source_sha256=hashes,
                   max_bytes=self.handler.maxBytes, backups=self.handler.backupCount)

    def snapshot(self, state):
        # Only cached diagnostics: never call the backend or hold core.lock during I/O.
        events = [event for event in state.get('events', []) if event['seq'] > self.last_event]
        value = {key: state.get(key) for key in (
            'b_ns', 'backend', 'running', 'error', 'session', 'generation', 'armed',
            'reason', 'holding', 'sample', 'feedback_valid', 'feedback_age_ms',
            'execution_mode', 'native_stopping', 'native_stop_failed', 'pause_details',
            'counts', 'last_target', 'last_submission', 'last_arm_configuration')}
        self.write('feedback', state=value, events=events)
        if events:
            self.last_event = max(event['seq'] for event in events)

    def close(self):
        self.handler.close()
