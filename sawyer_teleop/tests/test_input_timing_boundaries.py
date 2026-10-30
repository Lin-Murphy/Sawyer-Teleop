"""The same original input must expire at receive, cache reuse, and IK dispatch."""
from types import SimpleNamespace

import pytest

from sawyer_teleop.quest import QuestInput, QuestValidator
from test_quest import packet
from test_ik_schedule import rig, finish


# Explicit old boundaries, including custom input timeouts and a one-ns crossing.
CASES = [
    (.15, 148_000_000, 2_000_000, True),
    (.15, 148_000_001, 2_000_000, False),
    (.15, -12_000_000, 2_000_000, True),
    (.15, -12_000_001, 2_000_000, False),
    (.1, 98_000_000, 2_000_000, True),
    (.1, 98_000_001, 2_000_000, False),
]


def cached_quest(monkeypatch, now, timeout=.15):
    monkeypatch.setattr('sawyer_teleop.quest.shutil.which', lambda _: 'unused-test-adb')
    monkeypatch.setattr('sawyer_teleop.quest.ns', lambda: now)
    quest = QuestInput(timeout=timeout)
    quest.process = SimpleNamespace(poll=lambda: None)
    quest.sync_at = now
    quest.latest = dict(valid=True, reason='ready', received_ns=now,
                        mapped_ns=now, uncertainty_ns=2_000_000)
    return quest


@pytest.mark.parametrize('timeout,age,uncertainty,valid', CASES)
def test_receive_and_cached_input_have_identical_source_age_boundaries(
        monkeypatch, timeout, age, uncertainty, valid):
    now = 2_000_000_000
    value = packet()
    value['sample_ns'] = now-age
    parsed = QuestValidator(0, uncertainty, timeout).parse(value, now)
    assert parsed['valid'] is valid
    if not valid:
        assert parsed['reason'] == 'quest_stale' and not parsed['events']

    # Test cache validation independently, without inheriting parse's invalid flag.
    quest = cached_quest(monkeypatch, now, timeout)
    quest.latest.update(mapped_ns=now-age, uncertainty_ns=uncertainty)
    cached = quest.poll()
    assert cached['valid'] is valid
    if not valid:
        assert cached['reason'] == 'quest_stale'


@pytest.mark.parametrize('timeout,age,uncertainty,valid', CASES)
def test_fresh_new_input_does_not_renew_the_input_used_by_ik(
        rig, timeout, age, uncertainty, valid):
    clock, robot, quest, operator = rig()
    quest.timeout = timeout
    clock[0] += 10_000_000
    quest.stamp += 10_000_000
    operator.future_info['timing'].update(input_mapped_ns=clock[0]-age,
                                         input_uncertainty_ns=uncertainty)
    finish(operator, clock)
    operator.tick()
    assert bool(robot.targets) is valid
    if not valid:
        assert not operator.following
        assert operator.view()['message'] == 'ik_input_expired'


@pytest.mark.parametrize('fault', ['arrival', 'sync', 'process'])
def test_cached_source_age_does_not_replace_other_input_guards(monkeypatch, fault):
    now = 100_000_000_000
    quest = cached_quest(monkeypatch, now)
    quest.latest['mapped_ns'] = now-140_000_000  # Source age is still within budget.
    if fault == 'arrival':
        quest.latest['received_ns'] = now-150_000_001
    elif fault == 'sync':
        quest.sync_at = now-30_000_000_001
    else:
        quest.process = SimpleNamespace(poll=lambda: 1)
    assert not quest.poll()['valid']
