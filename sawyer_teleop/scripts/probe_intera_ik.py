#!/usr/bin/env python3
"""Read-only Intera IK timing probe; never arms or publishes a command."""
import argparse
import json
import statistics
import time

from sawyer_teleop.intera_backend import InteraBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--pause-ms', type=float, default=20.0)
    parser.add_argument('--connection', choices=('persistent', 'per-call'), default='persistent',
                        help='per-call reproduces the pre-2026-10-29 IK service connection for comparison')
    args = parser.parse_args()
    if args.samples < 1 or args.samples > 500 or args.pause_ms < 0:
        raise SystemExit('invalid samples or pause-ms')
    backend = InteraBackend()
    if args.connection == 'per-call':
        from intera_core_msgs.srv import SolvePositionIK
        backend.ik.close()
        backend.ik = backend.ros.ServiceProxy('/ExternalTools/right/PositionKinematicsNode/IKService',
                                              SolvePositionIK, persistent=False)
        backend.ik.close = lambda: None
    rows = []
    try:
        for index in range(args.samples):
            state = backend.snapshot()
            if not state['valid']:
                rows.append(dict(index=index, ok=False, error='feedback_invalid',
                                 fault=state.get('fault'), feedback_age_ms=(time.monotonic_ns()-state['captured_ns'])/1e6))
            else:
                started = time.monotonic_ns()
                try:
                    result = backend.solve(state['pose'], state['q'])
                    error = None if result is not None else 'ik_no_solution'
                    rows.append(dict(index=index, ok=result is not None, error=error,
                                     elapsed_ms=(time.monotonic_ns()-started)/1e6,
                                     feedback_age_ms=(time.monotonic_ns()-state['captured_ns'])/1e6,
                                     feedback_seq=state['seq']))
                except Exception as exc:
                    rows.append(dict(index=index, ok=False, error=type(exc).__name__+': '+str(exc),
                                     elapsed_ms=(time.monotonic_ns()-started)/1e6,
                                     feedback_age_ms=(time.monotonic_ns()-state['captured_ns'])/1e6,
                                     feedback_seq=state['seq']))
            time.sleep(args.pause_ms/1000.0)
    finally:
        backend.close()
    durations = [r['elapsed_ms'] for r in rows if r.get('elapsed_ms') is not None]
    summary = dict(connection=args.connection, samples=len(rows), ok=sum(r['ok'] for r in rows), failed=sum(not r['ok'] for r in rows),
                   over_80_ms=sum(x > 80 for x in durations), over_100_ms=sum(x > 100 for x in durations),
                   p50_ms=statistics.median(durations) if durations else None,
                   p95_ms=sorted(durations)[max(0, int(len(durations)*.95)-1)] if durations else None,
                   max_ms=max(durations) if durations else None)
    print(json.dumps(dict(summary=summary, rows=rows), ensure_ascii=False, indent=2))
    return 0 if summary['failed'] == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
