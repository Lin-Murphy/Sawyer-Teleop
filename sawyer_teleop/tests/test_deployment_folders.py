"""B-Lab-Ubuntu export and development launcher relocation; no Windows process or robot is started."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_launchers import lab, run

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_deployment', ROOT / 'scripts/export_deployment.py')
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)
sys.path.insert(0, str(ROOT / 'scripts'))
import project_resources as resources  # noqa: E402

SIM_IMAGE = 'sawyer/simulation:20261017'
SIM_IMAGE_ID = 'sha256:f5e1e474226438833d57352952d9c347b952a206366b02bb8311477bde665046'


@pytest.fixture(params=['split', 'directory', 'symlink'] if sys.platform == 'linux' else ['split', 'directory'])
def source(tmp_path, request):
    root = tmp_path / 'development source'
    # Use real code, launchers, APK and wheel; stub only the large Windows runtime and image.
    for name in ('src/sawyer_teleop', 'scripts', 'config', 'runtime-wheels', 'packaging/B-Lab-Ubuntu'):
        shutil.copytree(ROOT / name, root / name, ignore=export.IGNORE)
    for name in ('pyproject.toml', 'quest/apk.json', 'quest/sawyer-quest-input-120hz.apk', 'docs/DEPLOYMENT.md'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, path)
    # Normal Windows users need no symlink privilege. Keep Linux coverage for
    # older source trees that used a shared runtime link as well.
    shared = (tmp_path / 'windows-runtime' if request.param == 'split' else
              tmp_path / 'shared runtime' if request.param == 'symlink' else root / 'runtime')
    shared.mkdir()
    if request.param == 'symlink':
        (root / 'runtime').symlink_to(shared, target_is_directory=True)
    for name in ('python/python.exe', 'platform-tools/adb.exe',
                 'python/Lib/site-packages/__editable__.sawyer_teleop-1.3.0.dev0.pth',
                 'python/Scripts/sawyer-teleop.exe'):
        p = shared / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'old absolute development path')
    # Site addresses and experiment evidence must not leak into deployment defaults.
    (root / 'config/real-b.env').write_text('SAWYER_ROBOT_HOST=old.site\n')
    config_path = root / 'config/operator.json'
    config = json.loads(config_path.read_text())
    config['robot_host_history'] = ['private-lab.example', '192.0.2.10']
    config_path.write_text(json.dumps(config))
    (root / 'artifacts').mkdir()
    (root / 'artifacts/private-recording').write_text('keep on developer machine')
    cache = tmp_path / 'gazebo-image' if request.param == 'split' else shared / 'docker'
    cache.mkdir()
    archive = cache / 'sawyer-simulation-20261017.tar.gz'
    archive.write_bytes(b'Small image-cache test substitute; never loaded into Docker.')
    (cache / 'manifest.json').write_text(json.dumps({
        'image': SIM_IMAGE, 'image_id': SIM_IMAGE_ID,
        'archive_sha256': export.sha256(archive)}))
    return root


def test_export_contains_only_lab_b_and_survives_relocation(source, tmp_path):
    destination = tmp_path / 'export'
    result = export.build(destination, root=source)
    assert sorted(result['folders']) == ['B-Lab-Ubuntu']
    # deploy/ keeps only B-Lab-Ubuntu: no A, Windows runtime, simulation or top-level extras.
    assert [p.name for p in destination.iterdir()] == ['B-Lab-Ubuntu']
    relocated = tmp_path / 'USB copy with spaces'
    destination.rename(relocated)
    source.rename(tmp_path / 'source no longer available')
    for name in ('windows-runtime', 'gazebo-image', 'shared runtime'):
        if (tmp_path / name).exists():
            (tmp_path / name).rename(tmp_path / (name + ' unavailable'))
    b = relocated / 'B-Lab-Ubuntu'
    assert not any(p.is_symlink() for p in relocated.rglob('*'))
    assert not (b / 'runtime').exists() and not (b / 'artifacts').exists() and not (b / 'quest').exists()
    assert (b / 'config/real-b.env').read_bytes() == (ROOT / 'config/real-b.env.example').read_bytes()
    assert (b / 'config/lab-neutral-pose.yaml').read_bytes() == (ROOT / 'config/lab-neutral-pose.yaml').read_bytes()
    assert (b / 'scripts/probe_intera_ik.py').is_file()
    assert (b / 'docs/DEPLOYMENT_B.md').read_bytes() == (b / 'README.md').read_bytes()
    assert (b / 'docs/DEPLOYMENT.md').read_bytes() == (ROOT / 'docs/DEPLOYMENT.md').read_bytes()
    manifest = json.loads((b / 'DEPLOYMENT-MANIFEST.json').read_text())
    assert manifest['role'] == 'B-Lab-Ubuntu' and manifest['release_accepted'] is False
    for name, item in manifest['files'].items():
        assert export.sha256(b / name) == item['sha256']
    for p in (ROOT / 'src/sawyer_teleop').glob('*.py'):
        assert (b / 'src/sawyer_teleop' / p.name).read_bytes() == p.read_bytes()
    if sys.platform == 'linux':
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        assert run(b / 'Start-B.sh', '--help', env=env).returncode == 0


def test_export_needs_no_windows_runtime_apk_or_image(source, tmp_path):
    # B is exported from any machine with the source alone.
    for name in ('windows-runtime', 'gazebo-image', 'shared runtime'):
        if (tmp_path / name).exists():
            shutil.rmtree(tmp_path / name)
    if (source / 'runtime').exists() or (source / 'runtime').is_symlink():
        (source / 'runtime').unlink() if (source / 'runtime').is_symlink() else shutil.rmtree(source / 'runtime')
    shutil.rmtree(source / 'quest')
    export.build(tmp_path / 'export', root=source)
    assert (tmp_path / 'export/B-Lab-Ubuntu/Start-B.sh').is_file()


@pytest.mark.skipif(sys.platform != 'linux', reason='Ubuntu launcher integration')
def test_relocated_b_uses_own_source_and_preserves_lab_limits(source, tmp_path, lab):
    target = tmp_path / 'lab copy with spaces'
    export.build(target, root=source)
    source.rename(tmp_path / 'offline source')
    config, env, _ = lab
    result = run(target / 'B-Lab-Ubuntu/Start-B.sh', '--config', config, '--check', env=env)
    assert result.returncode == 0, result.stderr
    assert not Path(env['TRACE']).exists()
    result = run(target / 'B-Lab-Ubuntu/Start-B.sh', '--config', config, env=env)
    assert result.returncode == 0, result.stderr
    dispatched = json.loads(Path(env['BRIDGE']).read_text())
    args = dispatched['args']
    assert args[args.index('--backend') + 1] == 'intera'
    assert args[args.index('--reference-timeout') + 1] == '0.12'
    assert args[args.index('--command-timeout') + 1] == '0.2'
    assert Path(env['NEUTRAL']).read_text() == str(target / 'B-Lab-Ubuntu/config/lab-neutral-pose.yaml')


def test_export_never_overwrites_an_existing_deployment(source, tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    sentinel = target / 'config-with-lab-addresses'
    sentinel.write_text('preserve')
    with pytest.raises(FileExistsError):
        export.build(target, root=source)
    assert sentinel.read_text() == 'preserve'
    assert list(target.iterdir()) == [sentinel]


def test_export_preserves_persistent_lab_config_and_allows_explicit_override(source, tmp_path):
    saved = source.parent / 'windows-local/config/real-b.env'
    saved.parent.mkdir(parents=True)
    contents = b'SAWYER_ROBOT_HOST="lab.local"\nSAWYER_WORKSPACE="/home/lab/sawyer_ws"\nSAWYER_GRIPPER_PORT=""\n'
    saved.write_bytes(contents)
    export.build(tmp_path / 'lab export', root=source)
    b = tmp_path / 'lab export/B-Lab-Ubuntu'
    assert (b / 'config/real-b.env').read_bytes() == contents
    manifest = json.loads((b / 'DEPLOYMENT-MANIFEST.json').read_text())
    assert manifest['files']['config/real-b.env']['sha256'] == export.sha256(saved)
    assert (b / 'config/real-b.env.example').read_bytes() == (source / 'config/real-b.env.example').read_bytes()
    assert saved.read_bytes() == contents
    # Explicit template selection can still produce a generic package.
    export.build(tmp_path / 'generic export', root=source, config=source / 'config/real-b.env.example')
    assert (tmp_path / 'generic export/B-Lab-Ubuntu/config/real-b.env').read_bytes() == (source / 'config/real-b.env.example').read_bytes()
    with pytest.raises(FileNotFoundError):
        export.build(tmp_path / 'missing config export', root=source, config=tmp_path / 'missing.env')
    assert not (tmp_path / 'missing config export').exists()


@pytest.mark.parametrize('resource', ['runtime-wheels/python38/ruckig.cpython-38-x86_64-linux-gnu.so',
                                      'runtime-wheels/ruckig-0.12.2-cp38-cp38-manylinux_2_17_x86_64.manylinux2014_x86_64.whl'])
def test_corrupt_resources_do_not_publish_a_deployment(source, tmp_path, resource):
    p = source / resource
    assert p.is_file()
    p.write_bytes(b'corrupt')
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        export.build(target, root=source)
    assert not target.exists()


def test_development_uses_its_own_adb_and_pinned_cache_after_workspace_move(source, tmp_path, tmp_path_factory, monkeypatch):
    # Relocate the entire small fixture workspace, including any sibling resources.
    moved = tmp_path_factory.mktemp('relocated') / 'workspace moved with spaces'
    tmp_path.rename(moved)
    source = moved / source.name
    expected_runtime = source / 'runtime' if (source / 'runtime').exists() else moved / 'windows-runtime'
    expected_cache = source / 'runtime/docker' if (source / 'runtime').exists() else moved / 'gazebo-image'
    # Repair only the obsolete absolute symlink fixture after its parent move.
    if (source / 'runtime').is_symlink():
        (source / 'runtime').unlink()
        (source / 'runtime').symlink_to(moved / 'shared runtime', target_is_directory=True)
        expected_runtime, expected_cache = source / 'runtime', source / 'runtime/docker'
    spec = importlib.util.spec_from_file_location('development_a', source / 'scripts/windows_launcher.py')
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    monkeypatch.chdir(moved)
    monkeypatch.setenv('PATH', os.environ['PATH'])
    a.configure()
    assert Path(os.environ['PATH'].split(os.pathsep)[0]) == expected_runtime / 'platform-tools'
    spec = importlib.util.spec_from_file_location('development_sim', source / 'scripts/windows_sim_launcher.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'info':
            return SimpleNamespace(returncode=0, stdout='linux', stderr='')
        if args[:2] == ('image', 'inspect'):
            loaded = any(call[0] == 'load' for call in calls)
            return SimpleNamespace(returncode=0 if loaded else 1, stdout=sim.IMAGE_ID if loaded else '', stderr='')
        assert args == ('load', '--input', str(expected_cache / 'sawyer-simulation-20261017.tar.gz'))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    sim.docker = docker
    assert sim.prepare(headless=True) is False
    assert [call[0] for call in calls] == ['info', 'image', 'load', 'image']


def test_incomplete_bundled_runtime_does_not_borrow_sibling_resources(tmp_path):
    root = tmp_path / 'deployment'
    (root / 'runtime').mkdir(parents=True)
    (tmp_path / 'windows-runtime/python').mkdir(parents=True)
    assert resources.windows_runtime(root) == root / 'runtime'
    assert resources.gazebo_cache(root) == root / 'runtime/docker'


def test_source_replacement_keeps_local_operator_settings_and_recordings(tmp_path):
    root = tmp_path / 'sawyer_teleop'
    (root / 'config').mkdir(parents=True)
    template = root / 'config/operator.json'
    template.write_text('{"robot_host":"first-template","data_dir":"../data"}')
    spec = importlib.util.spec_from_file_location('state_isolation_a', ROOT / 'scripts/windows_launcher.py')
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    a.ROOT = root
    local = a.operator_config()
    assert local == tmp_path / 'windows-local/config/operator.json'
    assert local.read_bytes() == template.read_bytes()
    local.write_text('{"robot_host":"my-lab-address","data_dir":"../data"}')
    capture = a.local_home() / 'data/my-recording'
    capture.parent.mkdir(); capture.write_bytes(b'keep this recording')
    shutil.rmtree(root)
    (root / 'config').mkdir(parents=True)
    template.write_text('{"robot_host":"replacement-template"}')
    assert json.loads(a.operator_config().read_text())['robot_host'] == 'my-lab-address'
    assert capture.read_bytes() == b'keep this recording'
    # A self-contained deployment still uses its existing local config.
    (root / 'runtime').mkdir()
    assert a.operator_config() == template and a.local_home() == root
