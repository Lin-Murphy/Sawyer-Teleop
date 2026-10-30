"""Every installer uses the same verified artifact and host-native adb."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from sawyer_teleop import cli, quest_install


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    (tmp_path/'quest').mkdir()
    apk = tmp_path/'quest/default.apk'
    apk.write_bytes(b'verified fixture, not an installable APK')
    manifest = {'apk':apk.name, 'requested_hz':120, 'protocol':3, 'version_code':9,
                'version_name':'4.3-input120', 'package':'org.sawyer.questinput',
                'sha256':hashlib.sha256(apk.read_bytes()).hexdigest()}
    (tmp_path/'quest/apk.json').write_text(json.dumps(manifest))
    for name in ('runtime/platform-tools/adb.exe', 'runtime-rog/platform-tools/adb'):
        path = tmp_path/name
        path.parent.mkdir(parents=True)
        path.touch()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1:] == ['devices']:
            return 'List of devices attached\nTEST\tdevice\n'
        if args[3:5] == ['install', '-r']:
            return 'Success'
        return '  versionCode=9 minSdk=29\n  versionName=4.3-input120\n'
    monkeypatch.setattr(quest_install.subprocess, 'check_output', run)
    return tmp_path, calls


@pytest.mark.parametrize('platform,relative', [('linux','runtime-rog/platform-tools/adb'),
                                               ('win32','runtime/platform-tools/adb.exe')])
def test_selects_host_adb_with_both_runtimes_present(bundle, monkeypatch, platform, relative):
    root, calls = bundle
    monkeypatch.setattr(quest_install.sys, 'platform', platform)
    quest_install.install_verified_quest(root)
    assert len(calls) == 3 and all(call[0] == str(root/relative) for call in calls)
    assert calls[1][1:5] == ['-s', 'TEST', 'install', '-r']
    assert all('uninstall' not in call for call in calls)


def test_explicit_adb_is_respected(bundle):
    root, calls = bundle
    quest_install.install_verified_quest(root, adb='/custom/adb')
    assert all(call[0] == '/custom/adb' for call in calls)


def test_external_operator_config_selects_local_device_instead_of_source_default(bundle):
    root, calls = bundle
    (root / 'config').mkdir()
    (root / 'config/operator.json').write_text('{"quest_serial":"OLD"}')
    local = root / 'local-operator.json'
    local.write_text('{"quest_serial":"TEST"}')
    quest_install.install_verified_quest(root, config=local)
    assert calls[1][1:5] == ['-s', 'TEST', 'install', '-r']


def test_unverified_apk_never_calls_adb(bundle):
    root, calls = bundle
    old = root/'old.apk'
    old.write_bytes(b'legacy build')
    with pytest.raises(RuntimeError, match='verified default'):
        quest_install.install_verified_quest(root, apk=old)
    assert not calls


def test_unavailable_selected_device_never_installs(bundle):
    root, calls = bundle
    with pytest.raises(RuntimeError, match='selected Quest'):
        quest_install.install_verified_quest(root, serial='ABSENT')
    assert len(calls) == 1


def test_installed_version_name_must_match_exactly(bundle, monkeypatch):
    root, calls = bundle
    original = quest_install.subprocess.check_output
    def wrong_version(*args, **kwargs):
        return original(*args, **kwargs).replace('4.3-input120', '4.3-input120-obsolete')
    monkeypatch.setattr(quest_install.subprocess, 'check_output', wrong_version)
    with pytest.raises(RuntimeError, match='Installed APK version'):
        quest_install.install_verified_quest(root)


def test_cli_legacy_apk_argument_cannot_bypass_verification(tmp_path, monkeypatch):
    old = tmp_path/'old.apk'
    old.write_bytes(b'legacy build')
    calls = []
    monkeypatch.setattr(quest_install.subprocess, 'check_output', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, 'argv', ['sawyer-teleop', 'install-quest', str(old)])
    with pytest.raises(RuntimeError, match='verified default'):
        cli.main()
    assert not calls
