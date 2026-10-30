"""Deterministic A/B deadline injection, entirely offline; NOT Gazebo validation."""
import argparse
import copy
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.feedback_diagnostics import reference_from_snapshot, reference_remaining_ms


class Clock:
    value = 1_000_000_000

    def __call__(self):
        return self.value


class CaptureConnection:
    def __init__(self):
        self.closed = threading.Event()
        self.sent = []

    def send(self, message, **kwargs):
        self.sent.append(message)


# (source age, B->A delivery, A wait, IK delay, A->B delay, B wait), in ms.
# Clock uncertainty is added separately. Inputs are identical for both profiles.
CASES = {
    'nominal': (10, 15, 2, 2, 5, 5),
    'brief_103ms_feedback': (10, 90, 3, 1, 2, 1),
    'feedback_111ms': (10, 100, 1, 1, 1, 1),
    'return_path_exceeds_reference': (10, 90, 5, 3, 15, 1),
    'b_execution_exceeds_reference': (10, 90, 5, 3, 2, 12),
    'slow_ik_original_reference': (10, 55, 5, 60, 1, 1),
    'a_wait_exceeds_feedback': (10, 5, 97, 1, 1, 1),
    'clock_uncertainty_boundary': (10, 87, 2, 1, 1, 1),
}


def run_case(name, profile, *, reference_ms=120, injected_ms=None, uncertainty_ms=None):
    delays = CASES[name] if injected_ms is None else injected_ms
    source, delivery, a_wait, ik_delay, returning, b_wait = delays
    clock = Clock()
    backend = FakeBackend(clock)
    core = BridgeCore(backend, clock=clock, reference_timeout=reference_ms / 1000)
    hello = core.connect()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
    client = RobotClient('offline', expected_backend='gazebo', feedback_profile=profile)
    # Explicit test-only handshake fixture. There is no socket, Gazebo, or hardware.
    client.hello = dict(hello, backend='gazebo')
    client.session, client.generation = core.session, core.generation
    client.connection = CaptureConnection()
    client.offset_ns = 0
    if uncertainty_ms is None:
        uncertainty_ms = 2 if name == 'clock_uncertainty_boundary' else 0
    client.uncertainty_ns = round(uncertainty_ms * 1e6)
    client.sync_ns = clock()

    def advance(ms):
        end = clock() + round(ms * 1e6)
        while clock() < end:
            clock.value = min(end, clock()+10_000_000)
            core.tick()  # B stays fresh and its scheduler keeps running.

    frame = copy.deepcopy(core.telemetry())
    advance(source)
    frame['b_ns'] = clock()  # Construct a frame carrying the older source sample.
    result = dict(case=name, profile=profile, b_reference_ms=reference_ms, injected_ms=dict(zip(
        ('source', 'delivery', 'a_wait', 'ik', 'return', 'b_wait'), delays)))
    with patch('sawyer_teleop.client.ns', clock):
        advance(delivery)
        client._receive(frame)
        advance(a_wait)
        result['diagnostics'] = client.feedback_diagnostics()
        try:
            state = client.snapshot()
        except ConnectionError as exc:
            return dict(result, outcome='A_feedback', reason=str(exc))
        reference = reference_from_snapshot(state)
        advance(ik_delay)
        # New telemetry can arrive during IK, but the old solution keeps its seq.
        client._receive(copy.deepcopy(core.telemetry()))
        remaining = reference_remaining_ms(reference, clock(), hello['reference_timeout_s'])
        result['reference_remaining_before_send_ms'] = remaining
        if remaining < 0:
            return dict(result, outcome='A_reference', reason='ik_reference_expired')
        client.target([0.] * 7, reference['seq'])
        advance(returning)
        try:
            core.request(client.connection.sent[-1])
        except ValueError as exc:
            return dict(result, outcome='B_receive', reason=str(exc))
        # No B execution until the injected bounded scheduling wait has elapsed.
        clock.value += round(b_wait * 1e6)
        receipt, state = core.tick()
        return dict(result, outcome='submitted' if receipt else 'B_execute',
                    reason=None if receipt else state['reason'])


def run_matrix():
    rows = [run_case(name, profile) for name in CASES for profile in ('standard', 'gazebo_110ms')]
    expected = {
        'nominal': ('submitted', 'submitted'),
        'brief_103ms_feedback': ('A_feedback', 'submitted'),
        'feedback_111ms': ('A_feedback', 'A_feedback'),
        'return_path_exceeds_reference': ('A_feedback', 'B_receive'),
        'b_execution_exceeds_reference': ('A_feedback', 'B_execute'),
        'slow_ik_original_reference': ('A_reference', 'A_reference'),
        'a_wait_exceeds_feedback': ('A_feedback', 'A_feedback'),
        'clock_uncertainty_boundary': ('A_feedback', 'submitted'),
    }
    for name, outcomes in expected.items():
        actual = tuple(row['outcome'] for row in rows if row['case'] == name)
        if actual != outcomes:
            raise AssertionError((name, actual, outcomes))
    return rows


def run_reference_matrix():
    # Match observed total ages, not an inferred decomposition of the live run.
    cases = {
        'receive_at_159_027ms': ((10, 70, 5, 2, 72.027, 0), ('B_receive', 'submitted')),
        'execute_at_123_435ms': ((14, 60, 12, 2.131, 0, 35.304), ('B_execute', 'submitted')),
        'execute_at_126_958ms': ((10, 55, 6, 1.568, 0, 54.390), ('B_execute', 'submitted')),
        'reference_exact_180ms': ((10, 90, 8, 2, 70, 0), ('B_receive', 'submitted')),
        'reference_over_180ms': ((10, 90, 8, 2, 70.001, 0), ('B_receive', 'B_receive')),
        'a_feedback_still_111ms': ((10, 100, 1, 1, 1, 1), ('A_feedback', 'A_feedback')),
    }
    rows = []
    for name, (delays, expected) in cases.items():
        pair = [run_case(name, 'gazebo_110ms', reference_ms=limit, injected_ms=delays)
                for limit in (120, 180)]
        actual = tuple(row['outcome'] for row in pair)
        if actual != expected:
            raise AssertionError((name, actual, expected))
        rows.extend(pair)
    return rows


def replay_age_checks(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    ages = [row['age_ms'] for row in data['polls'] if isinstance(row.get('age_ms'), (float, int))]
    return dict(file=str(path), checks=len(ages), expired_checks={
        str(limit): sum(age > limit for age in ages) for limit in (100, 110, 120, 150, 250)},
        note='Offline age-threshold comparison only; not pause counts or a closed-loop rerun. B rejection is not estimated.')


def run_a150_matrix():
    # Same B=180 ms, compare only A's observation-age limit. Never renew a reference.
    cases = {
        'feedback_130ms': ((10, 115, 5, 2, 10, 5), 0, ('A_feedback', 'submitted')),
        'feedback_exact_150ms': ((10, 135, 5, 2, 10, 5), 0, ('A_feedback', 'submitted')),
        'feedback_over_150ms': ((10, 135, 5.000001, 2, 10, 5), 0, ('A_feedback', 'A_feedback')),
        'uncertainty_over_150ms': ((10, 137, 2, 2, 10, 5), 2, ('A_feedback', 'A_feedback')),
        'return_still_exceeds_180ms': ((10, 125, 5, 2, 39, 0), 0, ('A_feedback', 'B_receive')),
        'execution_still_exceeds_180ms': ((10, 125, 5, 2, 10, 29), 0, ('A_feedback', 'B_execute')),
        'old_ik_reference_still_expires': ((10, 115, 5, 51, 0, 0), 0, ('A_feedback', 'A_reference')),
    }
    rows = []
    for name, (delays, uncertainty, expected) in cases.items():
        pair = [run_case(name, profile, reference_ms=180, injected_ms=delays,
                         uncertainty_ms=uncertainty)
                for profile in ('gazebo_110ms', 'gazebo_150ms')]
        actual = tuple(row['outcome'] for row in pair)
        if actual != expected:
            raise AssertionError((name, actual, expected))
        rows.extend(pair)
    return rows


def run_a250_matrix():
    # B remains 180 ms: accepting an older observation is not permission to send an old target.
    cases = {
        'feedback_170ms': ((10, 155, 5, 2, 3, 3), 0, ('A_feedback', 'submitted')),
        'reference_exact_180ms': ((10, 165, 5, 0, 0, 0), 0, ('A_feedback', 'submitted')),
        'feedback_exact_250ms': ((10, 235, 5, 0, 0, 0), 0, ('A_feedback', 'A_reference')),
        'feedback_over_250ms': ((10, 235, 5.000001, 0, 0, 0), 0, ('A_feedback', 'A_feedback')),
        'uncertainty_over_250ms': ((10, 237, 2, 0, 0, 0), 2, ('A_feedback', 'A_feedback')),
        'return_still_exceeds_180ms': ((10, 160, 5, 2, 5, 0), 0, ('A_feedback', 'B_receive')),
        'execution_still_exceeds_180ms': ((10, 160, 5, 2, 1, 4), 0, ('A_feedback', 'B_execute')),
        'old_ik_reference_still_expires': ((10, 155, 5, 11, 0, 0), 0, ('A_feedback', 'A_reference')),
    }
    rows = []
    for name, (delays, uncertainty, expected) in cases.items():
        pair = [run_case(name, profile, reference_ms=180, injected_ms=delays,
                         uncertainty_ms=uncertainty)
                for profile in ('gazebo_150ms', 'gazebo_250ms')]
        actual = tuple(row['outcome'] for row in pair)
        if actual != expected:
            raise AssertionError((name, actual, expected))
        rows.extend(pair)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/feedback-delay-check.json')
    parser.add_argument('--trace', type=Path, action='append', default=[])
    args = parser.parse_args()
    result = dict(evidence='deterministic software fixtures, no devices or sockets',
                  b_protection_ms=dict(latest=100, reference=120, command=200),
                  cases=run_matrix(), reference_experiment=run_reference_matrix(),
                  a150_experiment=run_a150_matrix(),
                  a250_experiment=run_a250_matrix(),
                  archived_age_comparisons=[replay_age_checks(p) for p in args.trace])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(f"PASS: {len(result['cases'])} baseline + {len(result['reference_experiment'])} B reference "
          f"+ {len(result['a150_experiment'])} A 150 ms + {len(result['a250_experiment'])} A 250 ms cases. Report: {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
