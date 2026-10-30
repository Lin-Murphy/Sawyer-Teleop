"""Offline local-IK regression using the immutable real recording; no device access."""
import argparse
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'scripts')]
from sawyer_teleop.kinematics_model import read_kinematics_model
from sawyer_teleop.local_ik import LocalIK
from validate_recorded_path import load_case, forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    _, data = load_case()
    model = read_kinematics_model((ROOT/'tests/fixtures/sideways_lift_1_2_11/robot.urdf').read_text())
    solver = LocalIK(model)
    indices = np.r_[0, np.flatnonzero(np.diff(data['source_seq']))+1]
    seed = data['recorded_q'][0].tolist()
    rows = []
    for i in indices:
        target = {'p': data['target_p'][i].tolist(), 'q': data['target_quat'][i].tolist()}
        start = time.perf_counter_ns()
        q = solver.solve(target, seed)  # Same 20 ms budget as the control client.
        elapsed = (time.perf_counter_ns()-start)/1e6
        if q is None:
            rows.append(dict(index=int(i), solved=False, ms=elapsed))
            break  # No reseeding, skipping or retries to conceal a failure.
        fk = forward([q])[0]
        rows.append(dict(index=int(i), solved=True, ms=elapsed,
                         position_m=float(np.linalg.norm(fk[:3, 3]-target['p'])),
                         angle_rad=float((Rotation.from_matrix(fk[:3, :3]).inv()*Rotation.from_quat(target['q'])).magnitude()),
                         max_joint_step_rad=float(np.max(abs(np.array(q)-seed))), q=q))
        seed = q
    solved = [row for row in rows if row['solved']]
    passed = (len(solved) == len(indices) and all(
        row['position_m'] <= solver.position_tolerance*1.01
        and row['angle_rad'] <= solver.rotation_tolerance*1.01
        and row['max_joint_step_rad'] <= solver.max_seed_distance
        and np.all(np.array(row['q']) >= solver.lower)
        and np.all(np.array(row['q']) <= solver.upper) for row in solved))
    report = dict(passed=passed, platform=platform.platform(), python=platform.python_version(),
                  kind='offline IK only; not hand-to-robot latency', model=model,
                  requested=len(indices), solved=len(solved),
                  ik_ms=dict(zip(('median','p95','p99','max'),np.percentile([row['ms'] for row in rows],[50,95,99,100]).tolist())),
                  rows=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','model')}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
