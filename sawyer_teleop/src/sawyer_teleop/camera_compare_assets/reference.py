"""HPC read-only helper, sent over the existing SSH connection; writes only a tar stream."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pyarrow.parquet as pq
from PIL import Image


def export(action, dataset, episode=None, frame=None):
    root = Path(dataset).resolve()
    artifacts = {}
    if action == 'index':
        rows = []
        columns = ['state', 'episode_index', 'frame_index', 'source_time_ns', 'source_episode_index', 'index']
        for path in sorted((root / 'data').rglob('*.parquet')):
            rows.extend(pq.read_table(path, columns=columns).to_pylist())
        artifacts['pose_index.json'] = json.dumps({'dataset': dataset, 'resolved_dataset': str(root), 'rows': rows}).encode()
        for name, path in [('info.json', root / 'meta/info.json'),
                           ('contract.json', root.parent / 'contract.json'),
                           ('source_mapping.json', root / 'source_mapping.json')]:
            artifacts[name] = path.read_bytes()
        source = Path('/scratch/prj/eng_demt_robot_learning/openpi_official_2026-11-11/source/packages/openpi-client/src/openpi_client/image_tools.py')
        data = source.read_bytes()
        assert hashlib.sha256(data).hexdigest() == 'd48b4bd7f44e79fe6db8a8e07c9161144fa250be686e1245014a8b47e6171977'
        artifacts['pinned_image_tools.py'] = data
    elif action == 'frame':
        episode, frame = int(episode), int(frame)
        info = json.loads((root / 'meta/info.json').read_text())
        path = root / info['data_path'].format(episode_chunk=episode // info['chunks_size'], episode_index=episode)
        columns = ['image', 'wrist_image', 'state', 'frame_index', 'episode_index', 'source_time_ns', 'source_episode_index', 'index']
        row = next(r for r in pq.read_table(path, columns=columns).to_pylist() if r['frame_index'] == frame)
        segment = next(s for s in json.loads((root / 'source_mapping.json').read_text())['segments'] if s['episode_index'] == episode)
        upstream = None
        if segment.get('source_parquet'):
            upstream = next(r for r in pq.read_table(segment['source_parquet'], columns=['image', 'wrist_image', 'frame_index']).to_pylist() if r['frame_index'] == frame)
        prefix = 'ep%03d_frame%04d' % (episode, frame)
        metadata = {k: v for k, v in row.items() if k not in ('image', 'wrist_image')}
        metadata.update(dataset=str(root), parquet_path=str(path), source_mapping=segment,
                        parquet_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), images={})
        for key in ('image', 'wrist_image'):
            value = row[key]
            data = value['bytes'] if value['bytes'] is not None else Path(value['path']).read_bytes()
            if upstream is not None:
                prior = upstream[key]
                prior_data = prior['bytes'] if prior['bytes'] is not None else Path(prior['path']).read_bytes()
                assert hashlib.sha256(data).digest() == hashlib.sha256(prior_data).digest()
            image = Image.open(io.BytesIO(data))
            assert image.mode == 'RGB' and image.size == (224, 224)
            metadata['images'][key] = {'sha256': hashlib.sha256(data).hexdigest(),
                                       'upstream_png_equal': True if upstream is not None else None}
            artifacts[prefix + '_' + key + '.png'] = data
        artifacts[prefix + '.json'] = json.dumps(metadata, indent=2).encode()
    else:
        raise ValueError('unknown action')
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for name, data in artifacts.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


if __name__ == '__main__':
    export(*sys.argv[1:])
