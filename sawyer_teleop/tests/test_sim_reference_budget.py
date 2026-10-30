"""Optional Gazebo startup deadline; shared defaults and other stops stay intact."""
import importlib.util
from pathlib import Path

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from test_bridge import Clock, target

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_experiment():
    clock = Clock()
    backend = FakeBackend(clock)
    core = BridgeCore(backend, clock=clock, reference_timeout=.18)
    hello = core.connect()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                      trajectory={'profile': 'coordinated'}))
    return clock, backend, core, hello


def test_deterministic_120_180_comparison():
    script = load_script('validate_feedback_delay')
    rows = script.run_reference_matrix()
    assert len(rows) == 12
    assert rows == script.run_reference_matrix()


def test_old_reference_stops_after_180ms_even_with_fresh_feedback():
    clock, backend, core, hello = make_experiment()
    assert hello['reference_timeout_s'] == .18
    assert core.state_timeout_ns == 100_000_000
    assert core.command_timeout_ns == 200_000_000
    original = core.latest['seq']
    target(core, q=[.05] * 7)
    for _ in range(18):
        clock.advance(.01)
        receipt, _ = core.tick()
        assert receipt is not None
        assert receipt['feedback_seq'] == original
    clock.value += 1
    receipt, state = core.tick()
    assert receipt is None and not state['armed']
    assert state['reason'] == 'feedback_reference_expired'
    assert state['pause_details']['reference_feedback_age_ms'] == pytest.approx(180.000001)
    assert state['pause_details']['latest_feedback_age_ms'] < .001


@pytest.mark.parametrize('stop', ['release', 'frozen', 'control_gap', 'command'])
def test_other_stops_are_not_relaxed(stop):
    clock, backend, core, _ = make_experiment()
    old_generation = core.generation
    target(core, q=[.05] * 7)
    core.tick()
    if stop == 'release':
        for _ in range(15):
            clock.advance(.01)
            core.tick()
        core.request(dict(type='hold', session=core.session, reason='released'))
        count = core.submitted
        clock.advance(.01)
        receipt, state = core.tick()
        assert core.submitted == count
        assert not target(core, seq=2, generation=old_generation)['accepted']
        expected = 'released'
    elif stop == 'frozen':
        backend.frozen = True
        for _ in range(10):
            clock.advance(.01)
            core.tick()
        count = backend.publish_count
        clock.value += 1
        receipt, state = core.tick()
        assert not state['holding'] and backend.publish_count == count
        expected = 'feedback_latest_expired'
    else:
        clock.advance(.100000001 if stop == 'control_gap' else .200000001)
        receipt, state = core.tick()
        expected = 'trajectory_control_gap' if stop == 'control_gap' else 'command_timeout'
    assert receipt is None and not state['armed'] and state['reason'] == expected


def test_launcher_refuses_to_silently_reuse_wrong_deadline(monkeypatch):
    script = load_script('windows_sim_launcher')
    old = {'Config': {'Labels': {script.LABEL: script.PROJECT}, 'Env': []}}
    monkeypatch.setattr(script, 'inspect_container', lambda _: old)
    monkeypatch.setattr(script, 'docker', lambda *a, **k: pytest.fail('must not mutate existing container'))
    with pytest.raises(RuntimeError, match='NOT applied'):
        script.launch(False, True, 180)
    assert script.launch(False, True) == 0
    old['Config']['Env'] = ['SAWYER_SIM_REFERENCE_TIMEOUT=0.18']
    assert script.launch(False, True, 180) == 0
    with pytest.raises(RuntimeError, match='NOT applied'):
        script.launch(False, True)


@pytest.mark.parametrize('value', [0, 100, 200, 1000])
def test_launcher_rejects_unbounded_experiment(value):
    script = load_script('windows_sim_launcher')
    with pytest.raises(ValueError):
        script.reference_timeout_ms(value)


def test_default_bridge_deadline_stays_120ms():
    core = BridgeCore(FakeBackend())
    assert core.reference_timeout_ns == 120_000_000
