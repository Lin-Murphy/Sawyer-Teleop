"""Export the lab Ubuntu B-Lab-Ubuntu folder into a new directory; never overwrite a release.

Since 2026-10-29 ../deploy/ keeps only B-Lab-Ubuntu. Windows A and Windows simulation
run from the development source (scripts/windows) and are no longer exported.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROLE = 'B-Lab-Ubuntu'
B_FILES = (
    'src/sawyer_teleop', 'pyproject.toml', 'runtime-wheels',
    'config/real-b.env.example', 'config/lab-neutral-pose.yaml',
    'scripts/ubuntu/Start-B-Real.sh', 'scripts/prepare_trajectory_runtime.py', 'scripts/probe_intera_ik.py',
)
IGNORE = shutil.ignore_patterns(
    '__pycache__', '*.pyc', '*.egg-info', '__editable__.sawyer_teleop*',
    '__editable___sawyer_teleop*', 'sawyer_teleop-*.dist-info', 'sawyer-teleop.exe',
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_resources(root):
    """B needs only the pinned Python 3.8 Ruckig; no Windows runtime, APK or Docker image."""
    manifest = json.loads((root / 'runtime-wheels/manifest.json').read_text(encoding='utf-8'))
    wheel = root / 'runtime-wheels' / manifest['filename']
    if manifest['version'] != '0.12.2' or sha256(wheel) != manifest['sha256']:
        raise ValueError('Ruckig wheel verification failed')
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith('/'):
                continue
            installed = root / 'runtime-wheels/python38' / name
            if not installed.is_file() or sha256(installed) != hashlib.sha256(archive.read(name)).hexdigest():
                raise ValueError('Ruckig installed file verification failed: ' + name)


def copy(root, relative, destination):
    source, target = root / relative, destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        # Materialize real files: exported folders never depend on source symlinks.
        shutil.copytree(source, target, ignore=IGNORE)
    else:
        shutil.copy2(source, target)


def write_manifest(directory, created_at):
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Deployment contains a symlink: ' + str(path))
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = {
                'bytes': path.stat().st_size, 'sha256': sha256(path)}
    manifest = {
        'role': directory.name, 'created_at_utc': created_at,
        'release_accepted': False, 'hardware_validation': 'pending',
        'configuration': 'Initial defaults only; edit addresses locally.',
        'files': files,
    }
    (directory / 'DEPLOYMENT-MANIFEST.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'files': len(files), 'bytes': sum(item['bytes'] for item in files.values())}


def destination(output, root):
    output = Path(output).absolute()
    root = Path(root).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Keep the existing deployment; choose a new output directory.')
    if output.resolve() == root or root in output.resolve().parents:
        raise ValueError('Place deployments outside the development project.')
    return output, root


def build(output, root=ROOT):
    """Write <output>/B-Lab-Ubuntu; output must not exist yet."""
    output, root = destination(output, root)
    verify_resources(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sawyer-export-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'deployment'
        b = stage / ROLE
        shutil.copytree(root / 'packaging' / ROLE, b)
        for name in B_FILES:
            copy(root, name, b)
        shutil.copy2(b / 'config/real-b.env.example', b / 'config/real-b.env')
        # The launcher's dependency error points to this document.
        (b / 'docs').mkdir()
        shutil.copy2(b / 'README.md', b / 'docs/DEPLOYMENT_B.md')
        result = {ROLE: write_manifest(b, datetime.now(timezone.utc).isoformat())}
        if output.exists() or output.is_symlink():
            raise FileExistsError('Output appeared during export; it was not overwritten.')
        stage.rename(output)
    return {'directory': str(output), 'release_accepted': False, 'folders': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='New directory outside the source project; B-Lab-Ubuntu is written inside it')
    print(json.dumps(build(parser.parse_args().output), indent=2))
