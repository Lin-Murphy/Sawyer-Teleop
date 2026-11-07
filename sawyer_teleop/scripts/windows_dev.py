"""Windows development helpers behind Setup.cmd and Test.cmd."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT.parent / 'windows-local/artifacts'
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
TESTS = ('all', 'ik', 'lab-scene', 'feedback-delay', 'env')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'test'))
    parser.add_argument('kind', nargs='?', default='all', choices=TESTS)
    args, extra = parser.parse_known_args()
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 11) or sys.maxsize <= 2**32:
        raise RuntimeError('Use the Windows Python 3.11 x64 interpreter installed by Setup.cmd.')
    if args.action == 'setup':
        if extra:
            parser.error('setup takes no additional arguments')
        # Pinned versions come from pyproject.toml; already satisfied packages are kept.
        # The editable link makes editors / python -m import this source tree.
        pip = [sys.executable, '-I', '-m', 'pip', '--disable-pip-version-check', 'install',
               '--no-warn-script-location']
        subprocess.run([*pip, 'setuptools>=68', 'wheel'], cwd=ROOT, check=True)
        subprocess.run([*pip, '--no-build-isolation', '-e', str(ROOT) + '[operator,test]'],
                       cwd=ROOT, check=True)
        # Test the installed link outside the project directory, without our
        # explicit sys.path above. This catches accidental imports of an old A.
        version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
        check = (
            'import tempfile,os;from pathlib import Path;'
            'os.chdir(tempfile.gettempdir());import sawyer_teleop;'
            'assert Path(sawyer_teleop.__file__).resolve()==Path('
            + repr(str(ROOT / 'src/sawyer_teleop/__init__.py'))
            + ').resolve();assert sawyer_teleop.__version__==' + repr(version) + ';'
            'import numpy,scipy,h5py,cv2,pytest,ruckig,usb1;'
            'from scipy.spatial.transform import Rotation;'
            'print("Editable source:",sawyer_teleop.__file__);'
            'import sawyer_teleop.web_server;print("Python / Web:",__import__("sys").version.split()[0])'
        )
        subprocess.run([sys.executable, '-I', '-c', check], cwd=ROOT, check=True)
        print('Setup complete. Run Start-A.cmd, or Test.cmd for the offline tests.')
        print('If you move this folder later, run Setup.cmd again.')
        return 0
    if args.kind == 'all':
        os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
        report = ARTIFACTS / 'software-tests' / (
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.xml')
        report.parent.mkdir(parents=True, exist_ok=True)
        print('Software test report:', report, flush=True)
        import pytest
        return pytest.main([str(ROOT / 'tests'), '--junitxml', str(report), *extra])
    if args.kind == 'env':
        import windows_launcher
        windows_launcher.configure()
        windows_launcher.check_environment()
        return 0
    module, output = {'ik': ('validate_local_ik', 'local-ik-check.json'),
                      'lab-scene': ('validate_lab_scene', 'lab-scene-check.json'),
                      'feedback-delay': ('validate_feedback_delay', 'feedback-delay-check.json')}[args.kind]
    validator = __import__(module)
    sys.argv = [str(ROOT / 'scripts' / (module + '.py')), '--output', str(ARTIFACTS / output), *extra]
    result = validator.main()
    if args.kind == 'lab-scene':
        print('Offline reference check only. Physical and full VR gates remain separate.')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
