"""Read-only threshold screening of complete recordings, never robot execution."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np

THRESHOLDS = (.05, .10, .15, .20, .25, .35, .50)


def summary(values):
    values = np.asarray(values, dtype=float)
    if not values.size:
        return {"count": 0}
    return dict(count=len(values), median=float(np.median(values)),
                p95=float(np.quantile(values, .95)), p99=float(np.quantile(values, .99)),
                maximum=float(np.max(values)))


def evaluate(directory):
    directory = Path(directory)
    events_path = directory/'events.jsonl'
    if not events_path.exists():
        events_path = directory/'events.jsonl.gz'
    opener = gzip.open if events_path.suffix == '.gz' else open
    with opener(events_path, 'rt', encoding='utf-8') as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    counts = Counter(e.get('reason') for e in events)
    anchors, goals, seen, segment_order = {}, defaultdict(list), set(), []
    for e in events:
        segment = e.get('segment')
        if e.get('reason') == 'engaged':
            segment_order.append(segment)
            anchors[segment] = e.get('feedback', {}).get('sample', {}).get('q')
        if e.get('reason') != 'submitted':
            continue
        seq = e.get('source_command_seq', e['command_seq'])
        key = segment, seq
        if key in seen:
            continue
        seen.add(key)
        trajectory = e.get('trajectory') or e.get('native_cartesian') or {}
        q = trajectory.get('goal_q', e['submitted_q'])
        goals[segment].append(dict(seq=seq, q=q, stamp=e['timing']['target_sent_ns']))
    steps, intervals, first, rows, errors = [], [], [], [], []
    with h5py.File(directory/'raw.hdf5', 'r') as raw:
        state_ns, state_q = raw['states/ns'][:], raw['states/q'][:]
    assert np.all(np.diff(state_ns) >= 0), 'feedback observation timestamps must be ordered'
    for segment, items in goals.items():
        previous = None
        for item in items:
            q = np.asarray(item['q'], dtype=float)
            assert q.shape == (7,) and np.all(np.isfinite(q))
            anchor = np.asarray(previous['q']) if previous else anchors.get(segment)
            if anchor is not None:
                delta = np.abs(q-anchor)
                maximum = float(np.max(delta))
                (steps if previous else first).append(maximum)
                gap_ms = (item['stamp']-previous['stamp'])/1e6 if previous else None
                if previous:
                    assert gap_ms >= 0
                    intervals.append(gap_ms)
                rows.append(dict(segment=segment, source_command_seq=item['seq'],
                    first=previous is None, max_delta_rad=maximum, joint_index=int(np.argmax(delta)),
                    delta_rad=delta.tolist(), interval_ms=gap_ms,
                    rejected_at=[t for t in THRESHOLDS if maximum > t+1e-12]))
            previous = item
            index = int(np.searchsorted(state_ns, item['stamp'], side='right'))-1
            if index >= 0:
                errors.append(float(np.max(np.abs(q-state_q[index]))))
    cadence = {}
    for hz in (10, 20, 50, 100):
        deltas = []
        # Hypothetical latest-slot selection at fixed cadence; original samples
        # are untouched. No FK latency, feedback lease or robot model is simulated.
        for items in goals.values():
            stamps = np.array([item['stamp'] for item in items], dtype=np.int64)
            ticks = np.arange(stamps[0], stamps[-1]+1, int(1e9/hz), dtype=np.int64)
            indices = np.unique(np.searchsorted(stamps, ticks, side='right')-1)
            selected = np.array([items[i]['q'] for i in indices])
            if len(selected) > 1:
                deltas.extend(np.max(np.abs(np.diff(selected, axis=0)), axis=1).tolist())
        cadence[str(hz)] = dict(summary(deltas), over_035=sum(x > .35+1e-12 for x in deltas))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
              (events_path, directory/'raw.hdf5', directory/'manifest.json') if p.exists()}
    return dict(directory=str(directory.resolve()), sha256=hashes, event_counts=dict(counts),
        total_events=len(events), unique_goals=sum(map(len, goals.values())),
        engaged_segments=segment_order, submitted_segments=list(goals),
        no_submit_segments=[s for s in segment_order if s not in goals],
        first_goal_checks=summary(first), missing_first_anchor=sum(anchors.get(s) is None for s in goals),
        adjacent_steps_rad=summary(steps), original_intervals_ms=summary(intervals),
        candidate_rejections={str(t):sum(x > t+1e-12 for x in steps) for t in THRESHOLDS},
        feedback_distance_proxy_rad=dict(summary(errors), over_035=sum(x > .35 for x in errors)),
        hypothetical_cadence=cadence, comparisons=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recording', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = dict(threshold_rad=.35, offline_only=True, physical_acceptance=False,
        limitations=[
            'Submitted goals only: unsubmitted/rejected IK solutions and exact historical IK seeds are unavailable.',
            'All input/event rows and failed/no-submit segments counted; adjacent comparisons do not cross engagements.',
            'Feedback distance uses latest recorded observation before target dispatch, not exact IK seed or reference.',
            'Cadence screening is hypothetical latest-slot selection, not a native controller replay or continuity proof.',
            'Native Cartesian goal joints are a bias; input checks do not bound the internal physical joint path.'],
        recordings=[evaluate(p) for p in args.recording])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps([{k:r[k] for k in ('directory','unique_goals','adjacent_steps_rad',
                     'candidate_rejections','hypothetical_cadence')} for r in result['recordings']], indent=2))


if __name__ == '__main__':
    main()
