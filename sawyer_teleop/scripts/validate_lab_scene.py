"""Mandatory offline reference gate for the complete user-selected lab capture.

Preserves raw VR packets separately for full A/B replay. Never opens a device.
Passing this check alone never grants release acceptance.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from sawyer_teleop.local_ik import LocalIK
from sawyer_teleop.trajectory import make_trajectory, trajectory_options

CASE = ROOT/'tests/fixtures/lab_scene_20261022'


def load_case(directory=CASE):
    directory = Path(directory)
    case = json.loads((directory/'case.json').read_text(encoding='utf8'))
    for name, digest in case['fixture_sha256'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != digest:
            raise ValueError('Evidence checksum mismatch: '+name)
    raw = gzip.decompress((directory/'events.jsonl.gz').read_bytes())
    if hashlib.sha256(raw).hexdigest() != case['original_sha256']['events.jsonl']:
        raise ValueError('Original event bytes changed')
    events = [json.loads(line) for line in raw.splitlines()]
    inputs = [e for e in events if e['reason'] == 'quest_input']
    submitted = [e for e in events if e['reason'] == 'submitted']
    if (len(events) != case['counts']['events'] or len(inputs) != case['counts']['vr_inputs']
            or len(submitted) != case['counts']['submitted']):
        raise ValueError('Missing original input or submitted samples')
    with np.load(directory/'samples.npz', allow_pickle=False) as saved:
        data = {name:saved[name] for name in saved.files}
    if (np.any(np.diff(data['b_ns']) <= 0) or np.any(np.diff(data['vr_received_ns']) < 0)
            or not np.array_equal(data['vr_sample_ns'], [e['packet']['sample_ns'] for e in inputs])
            or not np.array_equal(data['vr_received_ns'], [e['a_ns'] for e in inputs])
            or not np.array_equal(data['goal_q'], [e['trajectory']['goal_q'] for e in submitted])):
        raise ValueError('Original timeline or goals changed')
    with h5py.File(directory/'raw.hdf5', 'r') as saved:
        if (not saved.attrs['complete'] or not np.array_equal(saved['commands/q'][:], data['recorded_q'])
                or not np.array_equal(saved['commands/ns'][:], data['submitted_a_ns'])
                or len(saved['states/q']) != case['counts']['states']):
            raise ValueError('Incomplete or inconsistent recorded reference/feedback')
    meta = json.loads((directory/'manifest.json').read_text(encoding='utf8'))
    return case, data, meta


def replay(data, options):
    rows, previous_segment, previous_source = [], None, None
    for i, stamp in enumerate(data['b_ns']):
        segment = int(data['segment'][i])
        if segment != previous_segment:
            stream = make_trajectory(options)
            stream.reset(data['recorded_q'][i].tolist(), int(stamp))
            previous_segment, previous_source = segment, None
        if data['source_seq'][i] != previous_source:
            stream.retarget(data['goal_q'][i].tolist(), int(stamp), int(data['source_ns'][i]))
            previous_source = data['source_seq'][i]
        rows.append(stream.sample(int(stamp)))
    return np.asarray(rows)


def evaluate(case, data, meta, values, options, timestamps=None):
    values = np.asarray(values, dtype=float)
    if values.shape != (case['counts']['submitted'], 3, 7) or not np.all(np.isfinite(values)):
        raise ValueError('Every original sample requires finite q/dq/ddq')
    if timestamps is not None and not np.array_equal(timestamps, data['b_ns']):
        raise ValueError('Original timing must remain unchanged')
    fk = LocalIK(meta['robot']['kinematics'])  # FK only; no IK solve.
    xyz = lambda q: np.array([fk.forward(row.tolist())[:3, 3] for row in q])
    p95 = lambda q: float(np.percentile(np.linalg.norm(xyz(q)-data['target_p'],axis=1)*1000,95))
    old, new = p95(data['recorded_q']), p95(values[:, 0])
    tolerance = case['offline_gate']['derivative_tolerance']
    velocity, acceleration = np.array(options['max_velocity_rad_s']), np.array(options['max_acceleration_rad_s2'])
    checks = dict(
        full_original_timeline=True,
        unshifted_reference_error=new <= old+case['offline_gate']['unshifted_reference_p95_tolerance_mm'],
        reported_velocity=bool(np.all(abs(values[:, 1]) <= velocity+tolerance)),
        reported_acceleration=bool(np.all(abs(values[:, 2]) <= acceleration+tolerance)))
    # Check each engagement separately: an operator hold can reset the reference.
    speed, accel, dv = [], [], []
    for segment in np.unique(data['segment']):
        mask = data['segment'] == segment
        q, v = values[mask, 0], values[mask, 1]
        dt = np.diff(data['b_ns'][mask])/1e9
        if len(dt):
            secant = np.diff(q, axis=0)/dt[:, None]
            speed.extend(secant)
            dv.extend(np.diff(v, axis=0)/dt[:, None])
            if len(dt)>1:
                accel.extend(2*np.diff(secant,axis=0)/(dt[1:]+dt[:-1])[:, None])
    for name, rows, bound in [('position_sample_velocity',speed,velocity),
                              ('position_sample_acceleration',accel,acceleration),
                              ('velocity_sample_acceleration',dv,acceleration)]:
        checks[name] = bool(rows and np.all(abs(np.array(rows)) <= bound+tolerance))
    return dict(case=case['id'], offline_case_passed=all(checks.values()), release_accepted=False,
                checks=checks, counts=case['counts'], recorded_reference_error_p95_mm=old,
                candidate_reference_error_p95_mm=new, options=options,
                evidence='Offline joint reference only; all raw VR input frozen for required ROG full-loop replay',
                required_release_layers=case['required_release_layers'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config/operator.json')
    parser.add_argument('--gazebo-fast', action='store_true', help='Explicitly evaluate the experimental Gazebo override')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    case, data, meta = load_case()
    config = json.loads(args.config.read_text(encoding='utf8'))
    options = trajectory_options(config['trajectory'])
    if args.gazebo_fast:
        options['max_acceleration_rad_s2'] = config['gazebo_max_acceleration_rad_s2']
        options = trajectory_options(options)
    report = evaluate(case, data, meta, replay(data, options), options)
    report['fixture_sha256'] = case['fixture_sha256']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['offline_case_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
