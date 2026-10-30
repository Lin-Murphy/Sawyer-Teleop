import time
import pytest
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import SyntheticQuest, wait_until
from sawyer_teleop.operator import Operator
from sawyer_teleop.server import BridgeServer


@pytest.fixture
def running(tmp_path):
    backend = FakeBackend()
    bridge = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = SyntheticQuest()
    operator = Operator(RobotClient("127.0.0.1", bridge.control_port, bridge.ik_port),
                        quest, [], tmp_path).start()
    wait_until(lambda: operator.epoch is not None)
    operator.request("calibrate")
    wait_until(lambda: operator.mapping is not None)
    yield backend, bridge, quest, operator
    operator.close()
    bridge.close()


def engage(quest, operator):
    quest.grip = 0
    wait_until(lambda: operator.release_seen)
    quest.grip = 1
    wait_until(lambda: operator.following)


@pytest.mark.parametrize('fail_handshake', [False, True])
def test_connected_host_requires_completed_b_handshake_and_survives_quest_failure(tmp_path, monkeypatch, fail_handshake):
    bridge = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    robot = RobotClient('127.0.0.1', bridge.control_port, bridge.ik_port)
    quest = SyntheticQuest()
    def fail_quest():
        raise RuntimeError('Quest unavailable')
    monkeypatch.setattr(quest, 'start', fail_quest)
    if fail_handshake:
        connect = robot.connect
        def incomplete_connect():
            connect()
            raise RuntimeError('B setup incomplete')
        monkeypatch.setattr(robot, 'connect', incomplete_connect)
    operator = Operator(robot, quest, [], tmp_path).start()
    try:
        wait_until(lambda: not operator.thread.is_alive())
        status = operator.view()
        assert status['mode'] == 'error'
        assert status['connected_host'] == (None if fail_handshake else '127.0.0.1')
    finally:
        operator.close()
        bridge.close()


def test_tracking_fault_requires_release_and_buttons_still_work(running, tmp_path):
    _, _, quest, operator = running
    engage(quest, operator)
    time.sleep(.1)
    quest.valid = False
    wait_until(lambda: not operator.following)
    quest.valid = True
    time.sleep(.08)
    assert not operator.following
    engage(quest, operator)
    quest.valid = False
    quest.events.append("discard")
    wait_until(lambda: operator.episode is None and operator.finishing is None)
    assert not list(tmp_path.iterdir())


def test_release_resume_uses_new_segment_with_same_episode(running):
    _, _, quest, operator = running
    engage(quest, operator)
    episode, segment = operator.episode, operator.segment
    quest.grip = 0
    wait_until(lambda: not operator.following)
    assert operator.episode is episode
    engage(quest, operator)
    assert operator.episode is episode and operator.segment == segment+1


def test_feedback_failure_pauses_before_new_targets(running):
    backend, bridge, quest, operator = running
    engage(quest, operator)
    backend.frozen = True
    wait_until(lambda: not operator.following)
    count = bridge.core.submitted
    time.sleep(.1)
    assert bridge.core.submitted == count
    assert not operator.release_seen


def test_calibration_result_persists_and_failure_is_visible(running):
    _, _, quest, operator = running
    assert operator.view()['calibration_result'].startswith('Calibrated')
    quest.valid = False
    wait_until(lambda: not operator.view()['calibrated'])
    operator.request('calibrate')
    wait_until(lambda: operator.view()['calibration_result'].startswith('Calibration failed'))
    assert not operator.view()['calibrated']
    assert operator.view()['calibration_exists']


def test_teleop_does_not_create_recording(running, tmp_path):
    _, _, quest, operator = running
    engage(quest, operator)
    time.sleep(.15)
    assert operator.following
    assert operator.episode is None
    assert not list(tmp_path.iterdir())


def test_trigger_controls_gripper_only_with_valid_active_teleop(running, tmp_path):
    backend, _, quest, operator = running
    quest.trigger = 1.
    time.sleep(.1)
    assert backend.gripper_publish_count == 0
    engage(quest, operator)
    wait_until(lambda: backend.gripper_target == 1)
    quest.trigger = .299
    wait_until(lambda: backend.gripper_target == -1)
    quest.trigger = .3
    wait_until(lambda: backend.gripper_target == 1)
    quest.grip = 0
    wait_until(lambda: not operator.following)
    # A drops following before its asynchronous hold acknowledgement arrives.
    wait_until(lambda: not backend.gripper_active, timeout=.5)
    count = backend.gripper_publish_count
    quest.trigger = 0.
    time.sleep(.1)
    assert backend.gripper_publish_count == count
    engage(quest, operator)
    wait_until(lambda: backend.gripper_target == -1)
    quest.valid = False
    wait_until(lambda: not operator.following)
    wait_until(lambda: not backend.gripper_active, timeout=.5)
    count = backend.gripper_publish_count
    quest.trigger = 1.
    time.sleep(.1)
    assert backend.gripper_publish_count == count
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('stop_action', ['end_recording', 'stop_batch'])
@pytest.mark.parametrize('pause', [0, .4])
def test_explicit_end_saves_and_stops_all_continuation(running, tmp_path, stop_action, pause):
    import h5py
    _, _, quest, operator = running
    engage(quest, operator)
    operator.request('start_recording', name='explicit', count=2, seconds=60, pause=pause)
    wait_until(lambda: operator.episode is not None)
    time.sleep(.2)
    episode = operator.episode
    operator.request(stop_action)
    wait_until(lambda: operator.view().get('recording_phase') == 'idle')
    assert not episode.put('event', {'late':True})
    assert operator.following and operator.saved_count == 1
    assert operator.episode is None and operator.review is None
    assert not operator.auto_recording and operator.next_record_at == 0
    with h5py.File(episode.result/'raw.hdf5') as data:
        assert data.attrs['complete']
        assert len(data['states/ns']) > 0
        assert len(data['commands/ns']) > 0
    time.sleep(pause+.15)
    assert operator.episode is None and operator.attempt_count == 1
    assert len(list((tmp_path/'explicit').glob('episode_*'))) == 1
    assert not list((tmp_path/'explicit').glob('.pending_*'))


def test_end_recording_takes_priority_over_queued_start_or_buttons(running):
    _, _, _, operator = running
    options = dict(name='stop_priority', count=3, seconds=60, pause=0)
    operator.request('start_recording', **options)
    wait_until(lambda: operator.episode is not None)
    with operator.lock:
        operator.requests.extend(['end_recording', 'discard', ('start_recording', options)])
    wait_until(lambda: operator.view().get('recording_phase') == 'idle')
    time.sleep(.15)
    assert operator.saved_count == 1 and operator.discarded_count == 0
    assert operator.attempt_count == 1 and not operator.auto_recording


def test_timer_saves_counts_down_and_stops_at_batch_goal(running, tmp_path):
    _, bridge, _, operator = running
    operator.request('start_recording', name='timer', count=2, seconds=.15, pause=.4)
    wait_until(lambda: operator.view().get('recording_phase') == 'countdown')
    assert operator.saved_count == 1
    assert 0 < operator.view()['ready_in_s'] <= .4
    assert operator.review is None
    assert operator.episode is None
    assert operator.attempt_count == 1
    wait_until(lambda: operator.attempt_count == 2)
    wait_until(lambda: operator.view().get('recording_phase') == 'complete')
    assert operator.saved_count == 2 and not operator.auto_recording
    time.sleep(.45)
    assert operator.attempt_count == 2 and operator.episode is None
    assert bridge.core.submitted == 0  # Automatic capture never takes control of the arm.
    assert len(list((tmp_path/'timer').glob('episode_*'))) == 2
    operator.request('start_recording', name='timer', count=2, seconds=.15, pause=.4)
    wait_until(lambda: 'Batch target reached' in operator.view()['message'])
    assert operator.episode is None


@pytest.mark.parametrize('action', ['save', 'discard'])
def test_controller_buttons_count_down_and_next_capture_is_automatic(running, tmp_path, action):
    _, _, quest, operator = running
    operator.request('start_recording', name='buttons', count=2, seconds=60, pause=.4)
    wait_until(lambda: operator.episode is not None)
    first = operator.episode
    quest.events.append(action)
    wait_until(lambda: operator.view().get('recording_phase') == 'countdown')
    assert operator.saved_count == (action == 'save')
    assert operator.discarded_count == (action == 'discard')
    assert not list((tmp_path/'buttons').glob('.pending_*'))
    wait_until(lambda: operator.episode is not None)
    assert operator.episode is not first and operator.attempt_count == 2
    operator.request('end_recording')
    wait_until(lambda: operator.view().get('recording_phase') in ('idle','complete'))
    assert not operator.auto_recording
    assert operator.saved_count == (action == 'save')+1
    time.sleep(.45)
    assert operator.episode is None


def test_stopping_countdown_does_not_start_another_capture(running):
    _, _, quest, operator = running
    operator.request('start_recording', name='cancel', count=2, seconds=60, pause=.4)
    wait_until(lambda: operator.episode is not None)
    quest.events.append('save')
    wait_until(lambda: operator.view().get('recording_phase') == 'countdown')
    operator.request('stop_batch')
    wait_until(lambda: operator.view().get('recording_phase') == 'idle')
    time.sleep(.45)
    assert not operator.auto_recording and operator.next_record_at == 0
    assert operator.attempt_count == 1 and operator.episode is None


@pytest.mark.parametrize('cancel', [False, True])
def test_next_capture_waits_for_writer_even_with_zero_interval(running, monkeypatch, cancel):
    import threading
    from sawyer_teleop.recording import Episode
    _, _, quest, operator = running
    release = threading.Event()
    original = Episode.finish
    def delayed_finish(episode, *args):
        def finish():
            release.wait(3)
            original(episode, *args)
        threading.Thread(target=finish, daemon=True).start()
        return episode.done
    monkeypatch.setattr(Episode, 'finish', delayed_finish)
    try:
        operator.request('start_recording', name='slow_writer', count=2, seconds=60, pause=0)
        wait_until(lambda: operator.episode is not None)
        quest.events.append('save')
        wait_until(lambda: operator.finishing is not None)
        time.sleep(.15)
        assert operator.attempt_count == 1 and operator.episode is None
        if cancel:
            operator.request('stop_batch')
            wait_until(lambda: not operator.auto_recording)
        release.set()
        if cancel:
            wait_until(lambda: operator.view().get('recording_phase') == 'idle')
            time.sleep(.15)
            assert operator.saved_count == 1 and operator.attempt_count == 1
        else:
            wait_until(lambda: operator.attempt_count == 2)
            operator.request('end_recording')
            wait_until(lambda: operator.view().get('recording_phase') == 'complete')
            assert operator.saved_count == 2 and not operator.auto_recording
    finally:
        release.set()


def test_invalid_input_at_countdown_end_cancels_batch(running):
    _, _, quest, operator = running
    operator.request('start_recording', name='tracking', count=2, seconds=.15, pause=.3)
    wait_until(lambda: operator.view().get('recording_phase') == 'countdown')
    quest.valid = False
    wait_until(lambda: not operator.auto_recording)
    quest.valid = True
    time.sleep(.4)
    assert operator.attempt_count == 1 and operator.episode is None


@pytest.mark.parametrize('action', ['save', 'discard', 'end_recording'])
def test_failed_capture_does_not_advance_batch(running, action):
    _, _, quest, operator = running
    operator.request('start_recording', name='write_failure', count=2, seconds=60, pause=0)
    wait_until(lambda: operator.episode is not None)
    operator.episode.error = 'simulated_writer_failure'
    quest.events.append(action)
    wait_until(lambda: operator.finishing is None and operator.episode is None)
    time.sleep(.15)
    assert not operator.auto_recording and operator.saved_count == 0
    assert operator.attempt_count == 1
    assert operator.discarded_count == (action == 'discard')
    if action != 'discard':
        assert 'Incomplete data retained but not counted' in operator.view()['recording_error']
    else:
        assert operator.view()['recording_error'] == ''


def test_existing_batch_is_not_overwritten(running, tmp_path):
    _, _, quest, operator = running
    (tmp_path/'existing').mkdir()
    marker = tmp_path/'existing'/'previous.hdf5'
    marker.write_bytes(b'existing dataset')
    operator.request('start_recording', name='existing')
    wait_until(lambda: 'Batch folder already exists' in operator.view().get('recording_error',''))
    error = operator.view()['recording_error']
    assert str(tmp_path/'existing') in error
    assert 'different batch name or output folder' in error
    assert operator.episode is None and not operator.auto_recording
    assert marker.read_bytes() == b'existing dataset'
    events = operator.drain_ui_events()
    assert any(event.get('kind') == 'error' and 'Batch folder already exists' in event['text'] for event in events)
    # Live control status can change without hiding the actionable recording error.
    engage(quest, operator)
    wait_until(lambda: operator.view()['message'] == 'Teleop active')
    assert operator.view()['recording_error'] == error
    operator.request('start_recording', name='fresh')
    wait_until(lambda: operator.episode is not None)
    assert operator.view()['recording_error'] == ''
    operator.request('end_recording')
    wait_until(lambda: operator.view().get('recording_phase') == 'complete')
    assert marker.read_bytes() == b'existing dataset'


@pytest.mark.parametrize('name_options', [{}, {'name': ''}, {'name': '   '}])
def test_blank_name_uses_start_time_and_countdown_keeps_same_batch(running, tmp_path, monkeypatch, name_options):
    import json
    _, _, _, operator = running
    original = time.strftime
    monkeypatch.setattr(time, 'strftime', lambda fmt, *args:
        'batch_20261020_173012' if fmt == 'batch_%Y%m%d_%H%M%S' else original(fmt, *args))
    assert not list(tmp_path.iterdir())
    operator.request('start_recording', count=2, seconds=.15, pause=.1, **name_options)
    wait_until(lambda: operator.view().get('recording_phase') == 'complete')
    destination = tmp_path/'batch_20261020_173012'
    assert operator.batch['name'] == destination.name
    assert list(tmp_path.iterdir()) == [destination]
    assert len(list(destination.glob('episode_*'))) == 2
    assert json.loads((destination/'batch.json').read_text(encoding='utf-8'))['name'] == destination.name


def test_automatic_names_avoid_existing_data_and_same_second_restarts(running, tmp_path, monkeypatch):
    _, _, _, operator = running
    name = 'batch_20261020_173012'
    original = time.strftime
    monkeypatch.setattr(time, 'strftime', lambda fmt, *args:
        name if fmt == 'batch_%Y%m%d_%H%M%S' else original(fmt, *args))
    (tmp_path/name).mkdir()
    marker = tmp_path/name/'previous.hdf5'
    marker.write_bytes(b'keep original data')
    (tmp_path/(name+'_01')).write_bytes(b'keep existing file')
    for suffix in ('_02', '_03'):
        operator.request('start_recording', name='', count=1, seconds=.15, pause=0)
        wait_until(lambda: operator.batch and operator.batch['name'] == name+suffix)
        # A prior batch may still be visible while this request is being handled.
        # Match the batch and completion in one published status snapshot.
        def this_batch_complete():
            status = operator.view()
            return ((status.get('batch') or {}).get('name') == name+suffix
                    and status.get('recording_phase') == 'complete')
        wait_until(this_batch_complete)
        assert len(list((tmp_path/(name+suffix)).glob('episode_*'))) == 1
    assert marker.read_bytes() == b'keep original data'
    assert (tmp_path/(name+'_01')).read_bytes() == b'keep existing file'
