"""Backend and planner dependencies are loaded only when selected."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys


def _run_bridge(args):
    journal = None
    path = args.feedback_log
    if not args.no_feedback_log and path is None and args.backend == 'intera':
        path = str(Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')
                   / 'sawyer-teleop/feedback.jsonl')
    if path and not args.no_feedback_log:
        try:
            from .feedback_log import FeedbackLog
            journal = FeedbackLog(path)
            journal.start({key: getattr(args, key) for key in (
                'backend', 'host', 'control_port', 'ik_port', 'state_timeout',
                'reference_timeout', 'command_timeout', 'hz', 'tip')})
            print('B feedback log: ' + str(journal.path.resolve()), flush=True)
        except Exception as exc:
            if journal is not None:
                journal.close()
            journal = None
            print('feedback_log_open_failed: ' + str(exc), file=sys.stderr, flush=True)
    try:
        _serve_bridge(args, journal)
    except Exception as exc:
        if journal is not None:
            try:
                journal.write('service_error', error=str(exc))
            except Exception:
                pass
        raise
    finally:
        if journal is not None:
            journal.close()


def _serve_bridge(args, journal):
    from .server import BridgeServer
    if args.backend in ("intera", "gazebo"):
        # This node's ROS files live only in RAM; do not alter any existing lab ROS node.
        ram = Path("/dev/shm")
        if not ram.is_dir():
            raise RuntimeError("B ROS logs require /dev/shm")
        import tempfile
        logdir = tempfile.TemporaryDirectory(prefix="sawyer-bridge-", dir=str(ram))
        os.environ["ROS_LOG_DIR"] = logdir.name
        if args.backend == "intera":
            from .intera_backend import InteraBackend
            backend = InteraBackend(args.tip, args.state_timeout)
        else:
            from .gazebo_backend import GazeboBackend
            backend = GazeboBackend(args.tip, args.state_timeout)
    else:
        from .backends import FakeBackend
        backend = FakeBackend()
        logdir = None
    server = BridgeServer(backend, args.host, args.control_port, args.ik_port,
                          args.state_timeout, args.command_timeout, args.hz, args.reference_timeout).start()
    def stop(*_):
        server.stop.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(json.dumps({"backend": args.backend, "host": args.host,
                      "control": server.control_port, "ik": server.ik_port}), flush=True)
    monitor_failed = False
    log_failed = False
    def update_status(running=True):
        nonlocal monitor_failed, log_failed
        if args.status_file:
            try:
                from .monitor import write_status
                write_status(args.status_file, server.core, running, server.error)
                monitor_failed = False
            except Exception as exc:
                # Monitoring must not stop execution or its feedback stream.
                if not monitor_failed:
                    print("monitor_write_failed: " + str(exc), file=sys.stderr, flush=True)
                monitor_failed = True
        if journal is not None:
            try:
                state = server.core.diagnostics()
                state.update(running=running, error=server.error)
                journal.snapshot(state)
                log_failed = False
            except Exception as exc:
                if not log_failed:
                    print("feedback_log_write_failed: " + str(exc), file=sys.stderr, flush=True)
                log_failed = True
    try:
        update_status()
        while not server.stop.wait(.5):
            update_status()
            if server.error:
                print(server.error, file=sys.stderr)
                server.stop.set()
    finally:
        server.close()
        update_status(running=False)
        if logdir:
            logdir.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Sawyer A/B teleoperation")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("operator", "web"):
        web = sub.add_parser(command, help="Local Web operator station")
        web.add_argument("--config", required=True)
        web.add_argument("--port", type=int, default=8780)
        web.add_argument("--no-browser", action="store_true", help="Print the local URL without opening a browser")
    bridge = sub.add_parser("bridge", help="B command execution and feedback service")
    bridge.add_argument("--backend", choices=["fake", "intera", "gazebo"], default="fake")
    bridge.add_argument("--host", default="127.0.0.1")
    bridge.add_argument("--control-port", type=int, default=8765)
    bridge.add_argument("--ik-port", type=int, default=8766)
    bridge.add_argument("--state-timeout", type=float, default=.1)
    bridge.add_argument("--command-timeout", type=float, default=.2)
    bridge.add_argument("--reference-timeout", type=float, default=.12,
                        help="Maximum source feedback reference age, independent of --state-timeout")
    bridge.add_argument("--hz", type=float, default=100)
    bridge.add_argument("--tip", default="right_hand")
    bridge.add_argument("--status-file", help="Optional read-only status snapshot; use /dev/shm on B")
    logs = bridge.add_mutually_exclusive_group()
    logs.add_argument("--feedback-log", help="Rotating JSONL diagnostics path; enabled by default for real Intera B")
    logs.add_argument("--no-feedback-log", action="store_true", help="Disable persistent B diagnostics")
    monitor = sub.add_parser("monitor", help="Read the local B status snapshot without using the A control connection")
    monitor.add_argument("--status-file", required=True)
    monitor.add_argument("--once", action="store_true")
    export = sub.add_parser("export", help="Export robomimic data on A")
    export.add_argument("episodes")
    export.add_argument("output")
    export.add_argument("--hz", type=int, default=50)
    export.add_argument("--valid-fraction", type=float, default=.2)
    motion = sub.add_parser("analyze-motion", help="Read-only analysis of recorded target changes and latency")
    motion.add_argument("episodes", help="An episode, raw.hdf5 file or batch directory")
    motion.add_argument("--output", help="Optional JSON output path; existing files are not overwritten")
    demo = sub.add_parser("demo", help="Full self-check with a fake backend and synthetic camera; no real robot")
    demo.add_argument("--output", required=True)
    demo.add_argument("--seconds", type=float, default=3)
    apk = sub.add_parser("install-quest", help="Install the verified default 120 Hz Quest APK")
    apk.add_argument("apk", nargs="?", help="Optional copy of the verified APK; its SHA256 must match the project manifest")
    apk.add_argument("--serial")
    apk.add_argument("--adb")
    args = parser.parse_args()
    if args.command in ("operator", "web"):
        from .web_server import run_web
        if run_web(args.config, args.port, not args.no_browser) is False:
            raise SystemExit(1)
    elif args.command == "bridge":
        _run_bridge(args)
    elif args.command == "monitor":
        from .monitor import watch
        watch(args.status_file, args.once)
    elif args.command == "export":
        from .dataset import export_dataset
        print(json.dumps(export_dataset(args.episodes, args.output, args.hz,
                                         args.valid_fraction), ensure_ascii=False, indent=2))
    elif args.command == "demo":
        from .demo import run_demo
        print(json.dumps(run_demo(args.output, args.seconds), indent=2))
    elif args.command == "analyze-motion":
        from .motion_report import analyze_motion
        text = json.dumps(analyze_motion(args.episodes), ensure_ascii=False, indent=2)
        if args.output:
            with Path(args.output).open("x", encoding="utf-8") as stream:
                stream.write(text+"\n")
            print(str(Path(args.output).resolve()))
        else:
            print(text)
    elif args.command == "install-quest":
        from .quest_install import install_verified_quest
        install_verified_quest(Path(__file__).resolve().parents[2], args.serial, args.adb, args.apk)


if __name__ == "__main__":
    main()
