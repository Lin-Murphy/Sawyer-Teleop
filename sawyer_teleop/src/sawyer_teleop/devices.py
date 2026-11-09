"""ADB Quest discovery, separate from the control connection."""
import re
import shutil
import subprocess
import time

WIRELESS_PORT = 5555
_IPV4 = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')


def _runner(adb):
    executable = shutil.which(adb)
    if not executable:
        raise RuntimeError('ADB not found. Use the complete runtime package.')
    def run(args, timeout=5):
        result = subprocess.run([executable]+args, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[:300])
        return result.stdout
    return run


def is_wireless(serial):
    return bool(serial) and ':' in serial


def quest_wifi_ip(text):
    """IPv4 of wlan0 from `ip -f inet addr show wlan0` output, else None."""
    for match in re.finditer(r'inet (\d{1,3}(?:\.\d{1,3}){3})/', text or ''):
        if not match.group(1).startswith('127.'):
            return match.group(1)
    return None


def enable_wireless(adb='adb', serial=None, port=WIRELESS_PORT, wait_s=10., sleep=time.sleep):
    """Switch a USB-attached, authorized Quest to ADB over Wi-Fi; return the `ip:port` serial.

    Restarts adbd on the headset, so it must not run while a session reads Quest input.
    """
    run = _runner(adb)
    if is_wireless(serial):
        serial = None  # Already wireless: re-derive from the USB device.
    usb = [s for s in discover_quests(adb, serial) if not is_wireless(s)]
    if serial:
        usb = [s for s in usb if s == serial]
    if len(usb) != 1:
        raise RuntimeError('Connect exactly one Quest by USB (authorized) before switching to wireless.'
                           + (' Found: '+', '.join(usb) if usb else ''))
    serial = usb[0]
    ip = quest_wifi_ip(run(['-s', serial, 'shell', 'ip', '-f', 'inet', 'addr', 'show', 'wlan0']))
    if not ip or not _IPV4.match(ip):
        raise RuntimeError('Quest has no Wi-Fi address. Join the headset to the same network as this computer.')
    target = '%s:%d' % (ip, port)
    run(['-s', serial, 'tcpip', str(port)])
    deadline = time.monotonic()+wait_s
    last = ''
    while True:
        sleep(1.)  # adbd restarts on the headset after tcpip.
        try:
            last = run(['connect', target], timeout=6).strip()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        if last.startswith(('connected to', 'already connected')):
            for line in run(['devices']).splitlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[0] == target and parts[1] == 'device':
                    return target
        if time.monotonic() >= deadline:
            raise RuntimeError('Wireless connect to %s failed: %s. Check that the Quest and this computer are on the same network.'
                               % (target, last[:200] or 'no response'))


def discover_quests(adb='adb', preferred=None):
    run = _runner(adb)
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
