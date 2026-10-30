"""Offline release gate for a recorded multi-joint path defect; never opens a device.

A successful unit-test suite does not waive this gate. This command exits 1
while the selected trajectory still fails the recorded Cartesian path case.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sawyer_teleop.trajectory import make_trajectory

CASE = ROOT / 'tests/fixtures/sideways_lift_1_2_11'
JOINTS = ['right_j' + str(j) for j in range(7)]


def load_case(directory=CASE):
    directory = Path(directory)
    case = json.loads((directory / 'case.json').read_text())
    for filename, digest in case['fixture_sha256'].items():
        if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != digest:
            raise ValueError('Evidence checksum mismatch: ' + filename)
    with np.load(directory / 'samples.npz', allow_pickle=False) as saved:
        data = {name: saved[name] for name in saved.files}
    count = case['points']
    if (data['goal_q'].shape != (count, 7)
            or len(np.unique(data['source_seq'])) != case['source_goals']
            or np.any(np.diff(data['b_ns']) <= 0)
            or np.any(np.diff(data['t_s']) <= 0)):
        raise ValueError('Invalid recorded timeline')
    return case, data


def forward(q, urdf=CASE / 'robot.urdf'):
    """Batch FK from the captured model, base -> right_hand, in metres."""
    q = np.asarray(q, dtype=float)
    if q.ndim != 2 or q.shape[1] != 7 or not np.all(np.isfinite(q)):
        raise ValueError('FK requires finite N x 7 joints')
    root = ET.parse(urdf).getroot()
    joints = {j.find('child').get('link'): j for j in root.findall('joint')}
    chain, link = [], 'right_hand'
    while link != 'base':
        joint = joints[link]
        chain.insert(0, joint)
        link = joint.find('parent').get('link')
    transform = np.broadcast_to(np.eye(4), (len(q), 4, 4)).copy()
    for joint in chain:
        origin = np.eye(4)
        item = joint.find('origin')
        if item is not None:
            origin[:3, 3] = np.fromstring(item.get('xyz', '0 0 0'), sep=' ')
            origin[:3, :3] = Rotation.from_euler(
                'xyz', np.fromstring(item.get('rpy', '0 0 0'), sep=' ')).as_matrix()
        transform = transform @ origin
        name = joint.get('name')
        if name in JOINTS:
            axis = np.fromstring(joint.find('axis').get('xyz'), sep=' ')
            motion = np.broadcast_to(np.eye(4), transform.shape).copy()
            motion[:, :3, :3] = Rotation.from_rotvec(q[:, JOINTS.index(name), None] * axis).as_matrix()
            transform = transform @ motion
        elif joint.get('type') != 'fixed':
            raise ValueError('Unexpected moving joint in captured chain: ' + name)
    return transform


def replay(data, options):
    stream = make_trajectory(options)
    stream.reset(data['recorded_q'][0].tolist(), int(data['b_ns'][0]))
    previous = None
    rows = []
    for index, stamp in enumerate(data['b_ns']):
        if data['source_seq'][index] != previous:
            stream.retarget(data['goal_q'][index].tolist(), int(stamp), int(data['source_ns'][index]))
            previous = data['source_seq'][index]
        rows.append(stream.sample(int(stamp)))
    result = np.asarray(rows)
    return result[:, 0], result[:, 1], result[:, 2]


def evaluate(case, data, q, dq, ddq, options, timestamps=None):
    """Evaluate all original samples without aligning, slowing, or dropping them."""
    arrays = [np.asarray(x, dtype=float) for x in (q, dq, ddq)]
    if any(x.shape != (case['points'], 7) or not np.all(np.isfinite(x)) for x in arrays):
        raise ValueError('Every original sample needs finite q/dq/ddq with seven joints')
    if timestamps is not None and not np.array_equal(timestamps, data['b_ns']):
        raise ValueError('Candidate timing must match the unmodified recorded timeline')
    q, dq, ddq = arrays
    xyz = forward(q)[:, :3, 3]
    limits = case['minimum_regression_criteria']
    tolerance = limits['derivative_tolerance']
    velocity = np.array(options['max_velocity_rad_s'])
    acceleration = np.array(options['max_acceleration_rad_s2'])
    dt = np.diff(data['b_ns']) / 1e9
    secant_velocity = np.diff(q, axis=0) / dt[:, None]
    secant_acceleration = 2 * np.diff(secant_velocity, axis=0) / (dt[1:] + dt[:-1])[:, None]
    checks = []
    for label, observed, maximum in [
            ('reported_velocity', np.max(abs(dq), axis=0), velocity),
            ('reported_acceleration', np.max(abs(ddq), axis=0), acceleration),
            ('position_sample_velocity', np.max(abs(secant_velocity), axis=0), velocity),
            ('position_sample_acceleration', np.max(abs(secant_acceleration), axis=0), acceleration),
            ('velocity_sample_acceleration', np.max(abs(np.diff(dq, axis=0) / dt[:, None]), axis=0), acceleration)]:
        checks.append(dict(name=label, passed=bool(np.all(observed <= maximum + tolerance)),
                           observed=observed.tolist(), limit=maximum.tolist()))
    height_cases = []
    for window in case['height_cases']:
        lo, hi = window['range_s']
        mask = (data['t_s'] >= lo) & (data['t_s'] < hi)
        if not np.any(mask):
            raise ValueError('Missing regression window')
        low, high = np.min(data['target_p'][mask, 2]), np.max(data['target_p'][mask, 2])
        zlow, zhigh = np.min(xyz[mask, 2]), np.max(xyz[mask, 2])
        excursion = float(max(0, low - zlow, zhigh - high))
        bound = limits['height_outside_target_band_m']
        height_cases.append(dict(id=window['id'], range_s=[lo, hi],
                                 target_z_range_m=[float(low), float(high)],
                                 reference_z_range_m=[float(zlow), float(zhigh)],
                                 excursion_m=excursion))
        checks.append(dict(name=window['id'], passed=excursion <= bound,
                           observed=excursion, limit=bound))
    error_mm = np.linalg.norm(xyz - data['target_p'], axis=1) * 1000
    for name, value in zip(('median', 'p95', 'max'), np.percentile(error_mm, [50, 95, 100])):
        bound = limits['whole_record_position_error_mm'][name]
        checks.append(dict(name='unshifted_position_error_' + name + '_mm',
                           passed=bool(value <= bound), observed=float(value), limit=bound))
    return dict(case=case['id'], offline_case_passed=all(x['passed'] for x in checks),
                release_accepted=False, points=len(q), options=options, checks=checks,
                height_cases=height_cases,
                recorded_reference_max_difference_rad=float(np.max(abs(q - data['recorded_q']))),
                required_followup=case['required_followup'], limitations=case['limitations'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--config', type=Path, help='Candidate operator config; defaults to config/operator.json')
    mode.add_argument('--recorded-config', action='store_true', help='Replay the frozen 1.2.11 configuration')
    parser.add_argument('--reference', type=Path, help='Optional external NPZ with b_ns/q/dq/ddq at every original time')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    case, data = load_case()
    options = (case['recorded_trajectory'] if args.recorded_config else
               json.loads((args.config or ROOT / 'config/operator.json').read_text())['trajectory'])
    if args.reference:
        with np.load(args.reference, allow_pickle=False) as ref:
            report = evaluate(case, data, ref['q'], ref['dq'], ref['ddq'], options, ref['b_ns'])
    else:
        report = evaluate(case, data, *replay(data, options), options)
    report['fixture_sha256'] = case['fixture_sha256']
    report['trajectory_source_sha256'] = hashlib.sha256(
        (ROOT / 'src/sawyer_teleop/trajectory.py').read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    failed = [x['name'] for x in report['checks'] if not x['passed']]
    print(json.dumps(dict(offline_case_passed=report['offline_case_passed'],
                          failed_checks=failed, report=str(args.output)), ensure_ascii=False))
    return 0 if report['offline_case_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
