"""Record true input ingress and consumed anchors without changing control semantics."""
import io
import json
import threading
from types import SimpleNamespace

import h5py
import numpy as np

from sawyer_teleop.quest import QuestInput, QuestValidator, input_evidence
from sawyer_teleop.operator import Operator
from sawyer_teleop.recording import Episode
from test_quest import packet
from test_ik_schedule import Robot


def reader(monkeypatch):
    monkeypatch.setattr('sawyer_teleop.quest.shutil.which', lambda _: 'unused-adb')
    monkeypatch.setattr('sawyer_teleop.quest.ns', lambda: 1_010_000_000)
    quest = QuestInput('test')
    quest.validator = QuestValidator(13, 17)
    quest.sync_at = 1_010_000_000
    quest.process = SimpleNamespace(poll=lambda: None)
    return quest


def feed(quest, messages):
    quest.process.stdout = io.BytesIO(b''.join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b'\n' for m in messages))
    quest._read()


def test_ingress_is_every_received_frame_not_repeated_polls_and_detaches(monkeypatch):
    q = reader(monkeypatch)
    feed(q, [packet(1)])  # Ordinary teleop has no sink or disk destination.
    events = []
    q.set_recording_sink(lambda reason, **kw: events.append(dict(reason=reason, **kw)))
    p = packet(3); p['hands']['r']['tracking_ok'] = False
    feed(q, [packet(2), packet(2), p, b'{broken'])
    for _ in range(30):
        q.poll()
    q.set_recording_sink(None)
    closed = list(events)
    feed(q, [packet(4)])
    assert events == closed
    assert events[0]['reason'] == 'quest_capture_started'
    assert events[0]['ingress_seq'] == 1
    assert events[0]['cached_input']['packet']['seq'] == 1
    got = [r['input'] for r in events if r['reason'] == 'quest_ingress']
    assert [v['ingress_seq'] for v in got] == [2, 3, 4]
    assert [v['packet']['seq'] for v in got] == [2, 2, 3]
    assert [v['packet_fresh'] for v in got] == [True, False, True]
    assert [v['valid'] for v in got] == [True, False, False]
    assert got[0]['mapped_ns'] == packet(2)['sample_ns'] + 13
    assert got[0]['received_ns'] == got[0]['read_completed_ns'] == 1_010_000_000
    assert got[0]['validation_offset_ns'] == 13 and got[0]['validation_uncertainty_ns'] == 17
    error = next(r for r in events if r['reason'] == 'quest_ingress_error')
    assert bytes.fromhex(error['line_hex']) == b'{broken\n'
    assert error['input']['ingress_seq'] == 5
    assert events[-1] == {'reason': 'quest_capture_ended', 'ingress_seq': 5}
    json.dumps(events)  # No numpy transform leaked into the writer queue.


def test_clock_revision_is_per_ingress_and_cached_input_does_not_change(monkeypatch):
    q = reader(monkeypatch); events=[]
    q.set_recording_sink(lambda reason, **kw: events.append(dict(reason=reason, **kw)))
    feed(q, [packet(1)])
    original = input_evidence(q.latest)
    q.command = lambda *a, **kw: '1.00 0.00'
    q.sync()
    assert q.clock_revision == 1
    assert input_evidence(q.latest) == original
    feed(q, [packet(2)])
    sync = next(r for r in events if r['reason'] == 'quest_clock_sync')
    assert sync['clock']['offset_ns'] == 10_000_000
    assert q.latest['clock_revision'] == 1
    assert q.latest['mapped_ns'] == packet(2)['sample_ns']+10_000_000
    assert q.latest['validation_uncertainty_ns'] == 10_000_000
    q.set_recording_sink(None)


def test_capture_detach_orders_final_ingress_before_sealing(monkeypatch):
    q = reader(monkeypatch); entered=threading.Event(); release=threading.Event(); detached=threading.Event(); events=[]
    def sink(reason, **kw):
        if reason == 'quest_ingress':
            entered.set()
            assert release.wait(2)
        events.append(reason)
    q.set_recording_sink(sink)
    producer=threading.Thread(target=lambda: feed(q,[packet(1)])); producer.start()
    assert entered.wait(2)
    closer=threading.Thread(target=lambda: (q.set_recording_sink(None),detached.set())); closer.start()
    assert not detached.wait(.02)
    release.set();producer.join(2);closer.join(2)
    assert not producer.is_alive() and detached.is_set()
    assert events.index('quest_ingress') < events.index('quest_capture_ended')
    assert events[-1] == 'quest_capture_ended'


def test_engage_records_post_arm_feedback_and_exact_input_then_start_mid_segment(tmp_path, monkeypatch):
    # Source-age validation and the reader must use the same synthetic clock.
    monkeypatch.setattr('sawyer_teleop.operator.ns', lambda: 1_010_000_000)
    q = reader(monkeypatch); feed(q,[packet(1)]);value=q.poll()
    class ArmRobot(Robot):
        sample_seq=5
        def arm(self, **kwargs):
            super().arm(**kwargs);self.sample_seq=99
        def snapshot(self, **kw):
            s=super().snapshot(**kw);s['sample']['seq']=self.sample_seq
            s['sample']['pose']['p']=[.51, .02, .31] if self.armed else [.5,0.,.3]
            return s
    robot=ArmRobot();op=Operator(robot,q,[],tmp_path)
    try:
        op.mapping=np.eye(3);op.calibration_epoch=value['epoch']
        op._engage(value,robot.snapshot())
        evidence=op.engagement
        assert evidence['feedback']['sample']['seq']==99
        assert evidence['input']['ingress_seq']==1
        assert evidence['input']['received_ns']==value['received_ns']
        assert evidence['robot_origin']==op.robot_origin.tolist()
        op._start_recording({'name':'anchors','seconds':1},value)
        ep=op.episode
        op._engage(value,robot.snapshot())  # Same consumed identity, new actual engagement.
        op._end_recording()
        assert q.recording_sink is None
        assert ep.finish(True,{'capture_closed':True}).wait(5)
        rows=[json.loads(line) for line in (ep.result/'events.jsonl').read_text().splitlines()]
        start=next(r for r in rows if r['reason']=='recording_started')
        assert start['active_engagement']['feedback']['sample']['seq']==99
        assert start['active_engagement']['segment']==1
        engaged=next(r for r in rows if r['reason']=='engaged')
        assert engaged['segment']==2 and engaged['feedback']['sample']['seq']==99
        assert engaged['input']['packet']['seq']==1
        assert engaged['mapping']==np.eye(3).tolist()
        reasons=[r['reason'] for r in rows]
        assert reasons.index('recording_started')<reasons.index('quest_capture_started')
        assert reasons.index('quest_capture_ended')<reasons.index('recording_ended')
        with h5py.File(ep.result/'raw.hdf5') as data:assert data.attrs['complete']
        assert ep.metadata['quest_ingress_schema']==1
    finally:
        q.set_recording_sink(None)
        op.executor.shutdown(wait=True)


def test_ingress_queue_overflow_remains_explicit_without_blocking_reader(tmp_path, monkeypatch):
    q=reader(monkeypatch); gate=threading.Event();original=Episode._write
    def delayed(self):
        assert gate.wait(3)
        original(self)
    monkeypatch.setattr(Episode,'_write',delayed)
    ep=Episode(tmp_path,{},queue_size=2)
    try:
        q.set_recording_sink(ep.event)
        feed(q,[packet(1),packet(2),packet(3)])
        assert q.latest['packet']['seq']==3 and q.latest['valid']
        assert ep.error=='recording_queue_full'
        q.set_recording_sink(None)
        ep.finish(True,{'capture_closed':True});gate.set()
        assert ep.done.wait(5)
        with h5py.File(ep.result/'raw.hdf5') as data:assert not data.attrs['complete']
    finally:
        gate.set()
