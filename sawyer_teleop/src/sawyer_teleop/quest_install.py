"""One verified Quest installer shared by the CLI and desktop launchers."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


def install_verified_quest(root, serial=None, adb=None, apk=None, config=None):
    root = Path(root)
    manifest = json.loads((root/'quest/apk.json').read_text(encoding='utf-8'))
    apk = Path(apk) if apk is not None else root/'quest'/manifest['apk']
    if (manifest['requested_hz'] != 120 or manifest['protocol'] != 3
            or manifest['version_code'] < 9
            or hashlib.sha256(apk.read_bytes()).hexdigest() != manifest['sha256']):
        raise RuntimeError('The APK is not the verified default 120 Hz build. Use the current project installer.')
    if adb is None:
        relative = 'runtime/platform-tools/adb.exe' if sys.platform == 'win32' else 'runtime-rog/platform-tools/adb'
        bundled = root/relative
        adb = str(bundled) if bundled.is_file() else shutil.which('adb')
    if not adb:
        raise RuntimeError('ADB not found. Pass --adb PATH.')
    if not serial:
        config = Path(config) if config is not None else root/'config/operator.json'
        serial = json.loads(config.read_text(encoding='utf-8')).get('quest_serial') if config.is_file() else None

    def run(*args, timeout=15):
        return subprocess.check_output([str(adb), *args], text=True, encoding='utf-8',
                                       errors='replace', timeout=timeout, stderr=subprocess.STDOUT)

    devices = run('devices')
    authorized = [row[0] for row in (line.split() for line in devices.splitlines()[1:])
                  if len(row) >= 2 and row[1] == 'device']
    if not serial and len(authorized) == 1:
        serial = authorized[0]
    if serial not in authorized:
        raise RuntimeError('Connect the selected Quest and allow USB debugging.\n'+devices)
    print('Installing default 120 Hz APK '+manifest['version_name'], flush=True)
    print(run('-s', serial, 'install', '-r', str(apk.resolve()), timeout=120), flush=True)
    package = run('-s', serial, 'shell', 'dumpsys', 'package', manifest['package'])
    code = re.search(r'\bversionCode=(\d+)\b', package)
    name = re.search(r'^\s*versionName=(\S+)\s*$', package, re.MULTILINE)
    if (not code or int(code[1]) != manifest['version_code']
            or not name or name[1] != manifest['version_name']):
        raise RuntimeError('Installed APK version does not match the verified 120 Hz build.')
    print('Verified installed version. Start the app and check actual DISPLAY / INPUT before teleop.')
