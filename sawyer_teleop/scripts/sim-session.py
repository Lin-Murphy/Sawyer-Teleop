#!/usr/bin/env python3
"""Keep one Docker simulation tied to its launch terminal, including SIGHUP."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading


CID = re.compile(r'[0-9a-f]{64}')


def owner_identity(pid):
    """A PID plus its start time prevents a recycled PID extending the session."""
    try:
        fields = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def stop_container(container):
    # Never stop by the shared name: a failed/repeated launch must not stop another instance.
    if not container or not CID.fullmatch(container):
        return
    try:
        subprocess.run(['docker', 'stop', '--time', '10', container],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Still attempt to remove our own instance if graceful stopping failed.
    try:
        removed = subprocess.run(['docker', 'rm', '--force', container],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=10, start_new_session=True)
        if removed.returncode and 'No such container' not in removed.stderr:
            raise RuntimeError(removed.stderr.strip())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print('Simulation cleanup failed for {}: {}'.format(container, exc), file=sys.stderr)
        return False
    return True


def finish_child(child):
    if child is None:
        return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-file')
    parser.add_argument('--watch-owner', type=int,
                        help='Bind an already running simulation to its existing launch process')
    parser.add_argument('--container')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.watch_owner:
        if command or not args.container or not CID.fullmatch(args.container):
            parser.error('--watch-owner requires one full container ID and no create command')
    elif command[:2] != ['docker', 'create'] or args.container:
        parser.error('provide a docker create command after --')

    stopping = threading.Event()
    def request_stop(signum, _frame):
        stopping.set()
        if signum == signal.SIGHUP:
            # The terminal is gone; flushing Python's streams must not fail after cleanup.
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(fd, 1)
                os.dup2(fd, 2)
            finally:
                os.close(fd)
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    container = args.container
    attached = created = None
    result = 0
    with tempfile.TemporaryDirectory(prefix='sawyer-sim-session-') as directory:
        cidfile = Path(directory)/'container.id'
        try:
            if args.watch_owner:
                identity = owner_identity(args.watch_owner)
                while identity is not None and owner_identity(args.watch_owner) == identity:
                    if stopping.wait(.25):
                        break
            else:
                # Creation cannot start the service. Its own process group survives terminal
                # hangup, letting us capture its ID and remove it even if the user closes early.
                created = subprocess.Popen(command[:2]+['--cidfile', str(cidfile)]+command[2:],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, start_new_session=True)
                created.communicate(timeout=90)
                result = created.returncode
                if cidfile.exists():
                    container = cidfile.read_text().strip()
                if result == 0 and not stopping.is_set():
                    if not container or not CID.fullmatch(container):
                        raise RuntimeError('Docker did not return a valid simulation container ID')
                    print('Close this launch-log window or press Ctrl+C to stop Gazebo and simulation B.', flush=True)
                    attached = subprocess.Popen(['docker', 'start', '--attach', container],
                        stdin=subprocess.DEVNULL, start_new_session=True)
                    while attached.poll() is None and not stopping.wait(.2):
                        pass
                    result = attached.returncode or 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print('Simulation launch failed: {}'.format(exc), file=sys.stderr)
            result = 1
        finally:
            # More terminal signals must not interrupt cleanup halfway through.
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            finish_child(created)
            if not container and cidfile.exists():
                container = cidfile.read_text().strip()
            if stop_container(container) is False:
                result = 1
            finish_child(attached)
            if args.auth_file:
                Path(args.auth_file).unlink(missing_ok=True)
    return result


if __name__ == '__main__':
    sys.exit(main())
