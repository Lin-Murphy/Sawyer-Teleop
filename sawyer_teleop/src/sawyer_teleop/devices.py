"""ADB Quest discovery, separate from the control connection."""
import shutil
import subprocess


def discover_quests(adb='adb', preferred=None):
    executable = shutil.which(adb)
    if not executable:
        raise RuntimeError('ADB not found. Use the complete runtime package.')
    def run(args):
        result = subprocess.run([executable]+args, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=5,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[:300])
        return result.stdout
    candidates = []
    blocked = []
    for line in run(['devices', '-l']).splitlines():
        parts = line.split()
        if len(parts)<2 or line.startswith('List of devices') or line.startswith('*'):
            continue
        serial, state = parts[:2]
        if state != 'device':
            blocked.append(serial+' '+ ' '.join(parts[1:]))
            continue
        model = next((item[6:] for item in parts[2:] if item.startswith('model:')), '')
        if not model:
            model = run(['-s', serial, 'shell', 'getprop', 'ro.product.model']).strip()
        if 'quest' in model.lower() or 'oculus' in model.lower() or serial == preferred:
            candidates.append(serial)
    if not candidates:
        raise RuntimeError('No authorized Quest found. Connect USB and allow debugging in the headset.'+
                           (' Device status: '+'; '.join(blocked) if blocked else ''))
    return list(dict.fromkeys(candidates))
