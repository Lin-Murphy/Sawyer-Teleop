"""Relocatable Windows entry point: use the bundled interpreter with -I."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from project_resources import windows_runtime


def local_home():
    # Standalone deployments keep their own state; source updates leave local
    # addresses, recordings and logs in a separate sibling directory.
    return ROOT if (ROOT / 'runtime').exists() else ROOT.parent / 'windows-local'


def operator_config():
    path = local_home() / 'config/operator.json'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'config/operator.json', path)
    return path


def configure():
    os.chdir(ROOT)
    adb_dir = windows_runtime(ROOT) / "platform-tools"
    if adb_dir.is_dir():
        os.environ["PATH"] = str(adb_dir) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def check_environment():
    import cv2
    import h5py
    import numpy as np
    import scipy
    from scipy.linalg import solve
    from scipy.spatial.transform import Rotation
    from sawyer_teleop.locking import ProcessLock

    versions = {"python": sys.version.split()[0], "numpy": np.__version__,
                "scipy": scipy.__version__, "h5py": h5py.__version__,
                "opencv": cv2.__version__}
    print("Checking bundled Python, numerical libraries, video and HDF5...")
    assert np.allclose(solve(np.eye(3), np.ones(3)), np.ones(3))
    assert np.allclose(np.linalg.svd(np.eye(3))[1], np.ones(3))
    assert np.allclose(Rotation.identity().as_matrix(), np.eye(3))
    with tempfile.TemporaryDirectory(prefix="sawyer-check-") as temp:
        folder = Path(temp)
        with h5py.File(folder / "check.hdf5", "w") as f:
            f.create_dataset("sample", data=np.arange(12), compression="gzip")
        with h5py.File(folder / "check.hdf5", "r") as f:
            assert f["sample"][:].tolist() == list(range(12))
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        assert cv2.imencode(".jpg", frame)[0]
        video = cv2.VideoWriter(str(folder / "check.mp4"),
                                cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48))
        assert video.isOpened(), "MP4 encoder unavailable"
        for _ in range(5):
            video.write(frame)
        video.release()
        capture = cv2.VideoCapture(str(folder / "check.mp4"))
        try:
            assert capture.read()[0], "MP4 readback failed"
        finally:
            capture.release()
        lock = ProcessLock("portable-check:" + temp)
        lock.acquire()
        lock.close()
    adb = windows_runtime(ROOT) / "platform-tools/adb.exe"
    result = subprocess.run([str(adb), "version"], check=True, capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    versions["adb"] = result.stdout.strip()
    print(json.dumps(versions, indent=2, ensure_ascii=False))
    print("Checking software-only control, save/discard and dataset export...")
    from sawyer_teleop.demo import run_demo
    destination = local_home() / "data" / ("self-check-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "executable": sys.executable,
        "versions": versions,
        "synthetic_demo": run_demo(destination, seconds=1),
    }
    (destination / "environment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS: portable runtime and software self-check.")
    print("Synthetic test data:", destination)
    print("Quest USB, camera hardware and real robot are not part of this self-check.")


def install_quest(serial):
    from sawyer_teleop.quest_install import install_verified_quest
    install_verified_quest(ROOT, serial=serial, config=operator_config())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["web", "check", "install-quest"], nargs="?", default="web")
    parser.add_argument("--serial")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    configure()
    try:
        if args.action == "check":
            check_environment()
        elif args.action == "install-quest":
            install_quest(args.serial)
        elif args.action == "web":
            from sawyer_teleop.web_server import run_web
            if run_web(operator_config(), args.port, not args.no_browser) is False:
                return 1
    except Exception:
        folder = local_home() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        detail = traceback.format_exc()
        with (folder / "windows-launch.log").open("a", encoding="utf-8") as log:
            log.write(datetime.now(timezone.utc).isoformat() + "\n" + detail + "\n")
        print(detail, file=sys.stderr)
        print("Details saved to:", folder / "windows-launch.log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
