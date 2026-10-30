"""Verify the bundled planner for Linux x86_64 Python 3.8 (Gazebo or lab B)."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root/'runtime-wheels/manifest.json').read_text())
wheel = root/'runtime-wheels'/manifest['filename']
if sys.version_info[:2] != (3, 8) or platform.machine() != 'x86_64':
    raise SystemExit('Bundled planner requires Linux x86_64 Python 3.8')
if hashlib.sha256(wheel.read_bytes()).hexdigest() != manifest['sha256']:
    raise SystemExit('Planner wheel checksum mismatch')
destination = root/'runtime-wheels/python38'
with zipfile.ZipFile(wheel) as archive:
    for name in archive.namelist():
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise SystemExit('Invalid planner wheel member')
        if not name.endswith('/'):
            installed = destination/name
            if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(archive.read(name)).digest():
                raise SystemExit('Planner runtime checksum mismatch: '+name)
print(destination)
