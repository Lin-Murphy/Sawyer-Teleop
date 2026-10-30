"""Run shell entry points with fake ROS/Docker; never contact hardware or Docker."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Ubuntu shell launcher tests')

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT/'scripts/ubuntu/Start-B-Real.sh'
SIM = ROOT/'scripts/ubuntu/Start-B-Sim.sh'
IMAGE_ID = 'sha256:f5e1e474226438833d57352952d9c347b952a206366b02bb8311477bde665046'


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


def run(script, *args, env=None):
    return subprocess.run(['bash', str(script), *map(str, args)], cwd='/tmp',
                          env=env, capture_output=True, text=True, timeout=15)


def test_ubuntu_a_uses_configured_python_local_config_and_current_source(tmp_path):
    home = tmp_path / 'local home'
    python = executable(tmp_path / 'a python', '#!' + sys.executable + '\n' + '''
import json, os, sys
print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'source': os.environ['PYTHONPATH']}))
''')
    result = run(ROOT / 'scripts/ubuntu/Start-A.sh', '--help',
                 env=dict(os.environ, SAWYER_PYTHON=str(python), SAWYER_LOCAL_HOME=str(home)))
    assert result.returncode == 0, result.stderr
    dispatched = json.loads(result.stdout)
    assert dispatched['args'] == ['-m', 'sawyer_teleop.cli', 'web', '--config',
                                  str(home / 'config/operator.json'), '--help']
    assert (home / 'config/operator.json').read_bytes() == (ROOT / 'config/operator.json').read_bytes()
    assert dispatched['cwd'] == str(ROOT)
    assert dispatched['source'].split(os.pathsep)[0] == str(ROOT / 'src')


@pytest.fixture
def lab(tmp_path):
    # Environment hooks intentionally live at paths containing spaces.
    space = tmp_path/'lab space'
    space.mkdir()
    modules = space/'modules'
    modules.mkdir()
    (modules/'rospy.py').write_text('''import os
def get_param(name, default=None):
    with open(os.environ['TRACE'], 'a') as out: out.write('ros-param\\n')
    return os.environ.get('SIM_CLOCK') == '1'
def wait_for_service(name, timeout=None):
    with open(os.environ['TRACE'], 'a') as out: out.write('ik-check\\n')
    if os.environ.get('IK_MISSING') == '1': raise RuntimeError('IK unavailable')
''')
    (modules/'intera_core_msgs').mkdir()
    for name in ['__init__.py', 'msg.py', 'srv.py']:
        (modules/'intera_core_msgs'/name).write_text('')
    setup = space/'ros setup.bash'
    setup.write_text('export PYTHONPATH='+shlex.quote(str(modules))+'\n')
    workspace = space/'workspace'
    (workspace/'devel').mkdir(parents=True)
    (workspace/'devel/setup.bash').write_text('export ROS_HOSTNAME=old-simulation\n')
    python = executable(space/'b python', '#!'+sys.executable+'\n'+'''
import json, os, sys
if sys.argv[1:4] == ['-m', 'sawyer_teleop.cli', 'bridge']:
    with open(os.environ['BRIDGE'], 'w') as out:
        json.dump({'args':sys.argv[1:], 'master':os.environ['ROS_MASTER_URI'],
                   'ros_ip':os.environ['ROS_IP'], 'hostname':os.environ.get('ROS_HOSTNAME')}, out)
    raise SystemExit(0)
os.execv('''+repr(sys.executable)+''', ['''+repr(sys.executable)+''', *sys.argv[1:]])
''')
    values = dict(SAWYER_ROBOT_HOST='robot.test', SAWYER_ROS_IP='192.0.2.10',
                  SAWYER_BIND_IP='192.0.2.20', SAWYER_ROS_SETUP=str(setup),
                  SAWYER_WORKSPACE=str(workspace), SAWYER_B_PYTHON=str(python))
    config = space/'real-b.env'
    def write(**updates):
        settings = dict(values, **updates)
        config.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in settings.items()))
    write()
    # Fake ROS CLI: only records the parameter file that would be loaded.
    bin_dir = space/'bin'
    bin_dir.mkdir()
    executable(bin_dir/'rosparam', '#!'+sys.executable+'''
import os, sys
with open(os.environ['TRACE'], 'a') as out: out.write('rosparam ' + ' '.join(sys.argv[1:2]) + '\\n')
with open(os.environ['NEUTRAL'], 'w') as out: out.write(sys.argv[2])
''')
    env = dict(os.environ, TRACE=str(space/'trace'), BRIDGE=str(space/'bridge.json'),
               NEUTRAL=str(space/'neutral-path'), PATH=str(bin_dir)+os.pathsep+os.environ['PATH'])
    return config, env, write


def test_real_check_never_connects_to_ros_or_starts_bridge(lab):
    config, env, _ = lab
    result = run(REAL, '--config', config, '--check', env=env)
    assert result.returncode == 0, result.stderr
    assert 'NOT been checked' in result.stdout
    assert not Path(env['TRACE']).exists()
    assert not Path(env['BRIDGE']).exists()


def test_real_dispatch_has_lab_network_and_standard_limits(lab):
    config, env, _ = lab
    result = run(REAL, '--config', config, env=env)
    assert result.returncode == 0, result.stderr
    recorded = json.loads(Path(env['BRIDGE']).read_text())
    assert recorded['master'] == 'http://robot.test:11311'
    assert recorded['ros_ip'] == '192.0.2.10'
    assert recorded['hostname'] is None
    args = recorded['args']
    flags = dict(zip(args[3::2], args[4::2]))
    assert flags == {
        '--backend':'intera', '--host':'192.0.2.20', '--control-port':'8765', '--ik-port':'8766',
        '--state-timeout':'0.1', '--reference-timeout':'0.12', '--command-timeout':'0.2',
        '--hz':'100', '--tip':'right_hand',
        '--status-file':f'/dev/shm/sawyer-teleop-{os.getuid()}/intera-b.json'}
    # Graph checks first, then the lab Reset pose; loading a parameter sends no motion.
    assert Path(env['TRACE']).read_text() == 'ros-param\nik-check\nrosparam load\n'
    assert Path(env['NEUTRAL']).read_text() == str(ROOT/'config/lab-neutral-pose.yaml')


@pytest.mark.parametrize('field,value', [
    ('SAWYER_ROBOT_HOST', ''), ('SAWYER_ROBOT_HOST', '127.0.0.1'),
    ('SAWYER_ROBOT_HOST', 'localhost'), ('SAWYER_ROBOT_HOST', 'http://robot.test:11311'),
    ('SAWYER_ROS_IP', '0.0.0.0'), ('SAWYER_BIND_IP', '127.0.0.1'),
    ('SAWYER_BIND_IP', 'not-an-ip')])
def test_real_rejects_missing_or_simulation_addresses_before_ros(lab, field, value):
    config, env, write = lab
    write(**{field:value})
    result = run(REAL, '--config', config, env=env)
    assert result.returncode != 0
    assert not Path(env['TRACE']).exists()
    assert not Path(env['BRIDGE']).exists()


@pytest.mark.parametrize('fault', ['SIM_CLOCK', 'IK_MISSING'])
def test_real_graph_failure_never_starts_bridge(lab, fault):
    config, env, _ = lab
    result = run(REAL, '--config', config, env=dict(env, **{fault:'1'}))
    assert result.returncode != 0
    assert not Path(env['BRIDGE']).exists()


def test_real_missing_workspace_is_actionable(lab):
    config, env, write = lab
    write(SAWYER_WORKSPACE='/missing/lab/workspace')
    result = run(REAL, '--config', config, '--check', env=env)
    assert result.returncode != 0
    assert 'Missing built workspace' in result.stderr


def test_real_requires_explicit_config(tmp_path):
    result = run(REAL, '--config', tmp_path/'missing.env')
    assert result.returncode != 0
    assert 'real-b.env.example' in result.stderr


@pytest.fixture
def docker_stub(tmp_path):
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    executable(bin_dir/'docker', '#!'+sys.executable+'\n'+'''
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ['DOCKER_TRACE'],'a') as out: out.write(json.dumps(args)+'\\n')
if args[0]=='info': print('linux')
elif args[:2]==['image','inspect']: print(os.environ['IMAGE_ID'])
elif args[0]=='create':
    pathlib.Path(args[args.index('--cidfile')+1]).write_text('a'*64)
    print('a'*64)
elif args[0] not in ('start','stop','rm'): raise SystemExit(12)
''')
    env = dict(os.environ, PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],
               DOCKER_TRACE=str(tmp_path/'docker.jsonl'), IMAGE_ID=IMAGE_ID)
    env.pop('SAWYER_SIM_BIND', None)
    return env


def test_sim_check_is_read_only(docker_stub):
    result = run(SIM, '--headless', '--bind', '192.0.2.20', '--check', env=docker_stub)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(docker_stub['DOCKER_TRACE']).read_text().splitlines()]
    assert [call[0] for call in calls] == ['info', 'image']


def test_sim_wrong_image_does_not_create_container(docker_stub):
    result = run(SIM, '--headless', env=dict(docker_stub, IMAGE_ID='sha256:wrong'))
    assert result.returncode != 0
    assert 'differs from the verified image' in result.stderr
    assert 'create' not in Path(docker_stub['DOCKER_TRACE']).read_text()


def test_sim_remote_ports_and_cleanup_keep_ownership(docker_stub):
    result = run(SIM, '--headless', '--bind', '192.0.2.20', env=docker_stub)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(docker_stub['DOCKER_TRACE']).read_text().splitlines()]
    create = next(call for call in calls if call[0] == 'create')
    for value in ['127.0.0.1:8875:8875', '127.0.0.1:8876:8876',
                  '192.0.2.20:8875:8875', '192.0.2.20:8876:8876', IMAGE_ID]:
        assert value in create
    assert [call[-1] for call in calls if call[0] in ('stop', 'rm')] == ['a'*64, 'a'*64]


@pytest.mark.parametrize('args', [('--bind',), ('--bind','not-an-ip'), ('--unexpected',)])
def test_sim_bad_arguments_fail_before_docker(docker_stub, args):
    result = run(SIM, *args, env=docker_stub)
    assert result.returncode != 0
    assert not Path(docker_stub['DOCKER_TRACE']).exists()
