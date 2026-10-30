"""Start the existing Gazebo B locally through Docker Desktop and WSLg."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from project_resources import gazebo_cache

IMAGE = "sawyer/simulation:20261017"
IMAGE_ID = "sha256:f5e1e474226438833d57352952d9c347b952a206366b02bb8311477bde665046"
ARCHIVE = "sawyer-simulation-20261017.tar.gz"
# GitHub Release asset; SAWYER_SIM_IMAGE_URL overrides it (mirror or local copy).
ARCHIVE_URL = "https://github.com/Lin-Murphy/sawyer-teleop/releases/download/sim-20261017/" + ARCHIVE
NAME = "sawyer-windows-sim"
CONTROL_PORT, IK_PORT = 8875, 8876
LABEL = "io.sawyer.windows-project"
PROJECT = hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()
DOCKER = [shutil.which("docker") or str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) /
          "Docker/Docker/resources/bin/docker.exe"), "--context", "desktop-linux"]
CID = re.compile(r"[0-9a-f]{64}")
DISPLAY_ARGS = ["--env", "DISPLAY=:0", "--env", "QT_X11_NO_MITSHM=1",
                "--mount", "type=bind,source=/mnt/host/wslg/.X11-unix,target=/tmp/.X11-unix,readonly"]
GPU_ARGS = ["--device", "/dev/dxg",
            "--mount", "type=bind,source=/usr/lib/wsl,target=/usr/lib/wsl,readonly",
            "--env", "LD_LIBRARY_PATH=/usr/lib/wsl/lib", "--env", "LIBGL_ALWAYS_SOFTWARE=0",
            "--env", "GALLIUM_DRIVER=d3d12"]


def docker(*args, timeout=30, check=True):
    result = subprocess.run([*DOCKER, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def inspect_container(identity):
    result = docker("inspect", "--type", "container", identity, check=False)
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)[0]


def stop_owned(identity):
    if not CID.fullmatch(identity):
        raise RuntimeError("Refusing cleanup without the full container ID.")
    current = inspect_container(identity)
    if current is None:
        return
    if current["Config"].get("Labels", {}).get(LABEL) != PROJECT:
        raise RuntimeError("Container belongs to a different project; left unchanged.")
    docker("stop", "--time", "10", identity, timeout=20, check=False)
    current = inspect_container(identity)
    if current is not None:
        docker("rm", "--force", identity)


def discard_session(directory):
    directory = Path(directory).resolve()
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith("sawyer-windows-sim-"):
        raise RuntimeError("Unexpected temporary session directory; left unchanged.")
    shutil.rmtree(directory)


def guard(owner_pid, directory):
    """Hold the actual Windows process handle, so recycled PIDs cannot own B."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, owner_pid)  # SYNCHRONIZE
    if not handle:
        return 1
    directory = Path(directory)
    (directory / "ready").touch()
    try:
        while kernel.WaitForSingleObject(handle, 200) == 258:  # WAIT_TIMEOUT
            if (directory / "done").exists():
                return 0
        # docker create can finish after its parent console has been closed.
        deadline = time.monotonic() + 95
        cidfile = directory / "container.id"
        while True:
            identity = cidfile.read_text().strip() if cidfile.exists() else ""
            if CID.fullmatch(identity):
                stop_owned(identity)
                break
            if not (directory / "creating").exists():
                break
            if time.monotonic() >= deadline:
                return 1
            time.sleep(.2)
        discard_session(directory)
        return 0
    finally:
        kernel.CloseHandle(handle)


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_archive(cache):
    """Return the verified image archive, downloading it once when it is missing."""
    archive, manifest = cache / ARCHIVE, cache / "manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"Simulation image manifest missing: {manifest}")
    expected = json.loads(manifest.read_text())["archive_sha256"]
    if not archive.is_file():
        url = os.environ.get("SAWYER_SIM_IMAGE_URL") or ARCHIVE_URL
        partial = archive.with_name(archive.name + ".part")
        print(f"Simulation image missing; downloading (about 1 GB, first launch only):\n  {url}", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
                total, done, shown = int(response.headers.get("Content-Length") or 0), 0, -1
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    out.write(chunk)
                    done += len(chunk)
                    percent = done * 100 // total if total else -1
                    if percent != shown and (percent % 5 == 0 or percent < 0):
                        shown = percent
                        print(f"  {done >> 20} MiB" + (f" ({percent}%)" if total else ""), flush=True)
        except (OSError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"Simulation image download failed ({exc}). Download {ARCHIVE} from the "
                               f"GitHub Release into {cache} manually; see docs/SETUP.md.") from None
        if file_sha256(partial) != expected:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Downloaded simulation archive checksum mismatch; the file was deleted. Retry Start-B.cmd.")
        partial.replace(archive)
        return archive
    if file_sha256(archive) != expected:
        raise RuntimeError(f"Cached simulation archive checksum mismatch: delete {archive} and retry.")
    return archive


def prepare(headless, rendering="auto"):
    result = docker("info", "--format", "{{.OSType}}", timeout=10, check=False)
    if result.returncode:
        desktop = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/Docker Desktop.exe"
        if not desktop.is_file():
            raise RuntimeError("Install Docker Desktop with the WSL 2 Linux backend first.")
        print("Starting Docker Desktop; waiting for the Linux engine...", flush=True)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        subprocess.Popen([str(desktop)], startupinfo=startup,
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 120
        while result.returncode and time.monotonic() < deadline:
            time.sleep(2)
            result = docker("info", "--format", "{{.OSType}}", timeout=10, check=False)
    if result.returncode or result.stdout.strip() != "linux":
        raise RuntimeError("Docker Desktop Linux engine is unavailable. Open Docker Desktop and retry.")
    found = docker("image", "inspect", IMAGE, "--format", "{{.Id}}", check=False)
    if found.returncode:
        archive = ensure_archive(gazebo_cache(ROOT))
        print("Importing the cached simulation image (first launch only)...", flush=True)
        docker("load", "--input", str(archive), timeout=300)
        found = docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
    if found.stdout.strip() != IMAGE_ID:
        raise RuntimeError("Simulation image differs from the verified image. No container was started.")
    for relative in ("scripts/gazebo-windows-entrypoint.sh", "scripts/initialize_windows_sim.py", "scripts/prepare_trajectory_runtime.py",
                     "runtime-wheels/python38/ruckig.cpython-38-x86_64-linux-gnu.so"):
        if not (ROOT / relative).is_file():
            raise RuntimeError("Missing simulation resource: " + relative)
    if not headless:
        # docker-desktop owns this WSLg socket. No WSL distribution integration is needed.
        probe = subprocess.run(["wsl", "-d", "docker-desktop", "--exec", "test", "-S",
                                "/mnt/host/wslg/.X11-unix/X0"], capture_output=True, timeout=20)
        if probe.returncode:
            raise RuntimeError("WSLg display is unavailable. See docs/TROUBLESHOOTING.md.")
    if not headless and rendering != "software":
        probe = docker("run", "--rm", "--user", "1000:1000", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", *DISPLAY_ARGS, *GPU_ARGS,
                       "--entrypoint", "glxinfo", IMAGE_ID, "-B", timeout=30, check=False)
        accelerated = probe.returncode == 0 and "Accelerated: yes" in probe.stdout
        if accelerated:
            renderer = next((line.strip() for line in probe.stdout.splitlines()
                             if "OpenGL renderer string:" in line), "D3D12 hardware rendering")
            print("Available for new simulation: " + renderer, flush=True)
            return True
        if rendering == "gpu":
            raise RuntimeError("GPU rendering probe failed: " + probe.stderr.strip())
        print("GPU acceleration unavailable; using CPU rendering. Try a smaller Gazebo window.", flush=True)
    return False


def reference_timeout_ms(value):
    """Keep the optional simulation experiment separate from bridge defaults."""
    if value not in (120, 180, 250):
        raise ValueError("Simulation reference timeout must be 120, 180 or 250 ms.")
    return f"{value / 1000:.2f}"


def launch(headless, use_gpu=False, reference_ms=120):
    reference_seconds = reference_timeout_ms(reference_ms)
    existing = inspect_container(NAME)
    if existing is not None:
        if existing["Config"].get("Labels", {}).get(LABEL) != PROJECT:
            raise RuntimeError("The simulation name is occupied by another project; left unchanged.")
        environment = dict(item.split("=", 1) for item in existing["Config"].get("Env", []) if "=" in item)
        current_seconds = environment.get("SAWYER_SIM_REFERENCE_TIMEOUT", "0.12")
        if current_seconds != reference_seconds:
            raise RuntimeError(
                f"Existing simulation keeps its reference timeout ({current_seconds} s); "
                f"requested {reference_seconds} s was NOT applied. "
                "Release grip and disconnect A, then Stop-B.cmd and start again. "
                "Restarting resets the simulation scene.")
        print("Simulation already exists; its scene has been kept. Use Stop-B.cmd before restarting.")
        print("Rendering changes apply only after restarting B.")
        print(f"A: Gazebo Simulation | 127.0.0.1 | Local on A | ports {CONTROL_PORT} / {IK_PORT}")
        return 0
    print("Starting local Gazebo physics simulation (no real robot).", flush=True)
    print("Rendering: " + ("GPU / D3D12" if use_gpu else "CPU / software"), flush=True)
    command_ms = 250 if reference_ms == 250 else 200
    print(f"B deadlines: latest feedback 100 ms | source reference {reference_ms} ms | command {command_ms} ms", flush=True)
    if reference_ms != 120:
        print(f"Simulation experiment: old references may remain valid {reference_ms - 120} ms longer.", flush=True)
    print(f"A: Gazebo Simulation | 127.0.0.1 | Local on A | ports {CONTROL_PORT} / {IK_PORT}", flush=True)
    print("Wait for the scene and the backend=gazebo startup line before connecting A.", flush=True)
    print("Keep this window open. Ctrl+C or closing it stops this simulation. Stop-B.cmd also stops it.", flush=True)
    child = watcher = None
    directory = Path(tempfile.mkdtemp(prefix="sawyer-windows-sim-"))
    cidfile = directory / "container.id"
    try:
        watcher = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).resolve()),
                                    "--guard", str(os.getpid()), str(directory)],
                                   creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (directory / "ready").exists():
            if watcher.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Could not attach simulation lifetime to the launch window.")
            time.sleep(.05)
        (directory / "creating").touch()
        args = ["create", "--rm", "--init", "--name", NAME, "--cidfile", str(cidfile),
                "--label", LABEL + "=" + PROJECT,
                "--user", "1000:1000", "--env", "HOME=/tmp/sawyer-home",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--shm-size", "512m", "--tmpfs", "/logs:rw,uid=1000,gid=1000",
                "--tmpfs", "/tmp:rw", "-p", f"127.0.0.1:{CONTROL_PORT}:8875", "-p", f"127.0.0.1:{IK_PORT}:8876",
                "--mount", "type=bind,source=" + str(ROOT) + ",target=/phase3,readonly",
                "--env", "LP_NUM_THREADS=2",
                "--env", "SAWYER_SIM_REFERENCE_TIMEOUT=" + reference_seconds,
                "--env", "SAWYER_SIM_GUI=" + ("false" if headless else "true")]
        if not headless:
            args += DISPLAY_ARGS
        if use_gpu:
            args += GPU_ARGS
        docker(*args, IMAGE_ID, "bash", "/phase3/scripts/gazebo-windows-entrypoint.sh", timeout=90)
        (directory / "creating").unlink()
        identity = cidfile.read_text().strip()
        if not CID.fullmatch(identity):
            raise RuntimeError("Docker returned an invalid container ID.")
        child = subprocess.Popen([*DOCKER, "start", "--attach", identity])
        while child.poll() is None:
            time.sleep(.2)
        return 0 if child.returncode in (0, 137, 143) else child.returncode
    except KeyboardInterrupt:
        print("\nStopping local Gazebo and B...", flush=True)
        return 0
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        if cidfile.exists():
            stop_owned(cidfile.read_text().strip())
        if child is not None:
            child.wait(timeout=20)
        (directory / "done").touch()
        if watcher is not None:
            watcher.wait(timeout=5)
        discard_session(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Check local prerequisites without starting B")
    action.add_argument("--stop", action="store_true", help="Stop only this project's local simulation")
    parser.add_argument("--headless", action="store_true", help="Run physical simulation without its 3D window")
    parser.add_argument("--rendering", choices=("auto", "gpu", "software"), default="auto",
                        help="Prefer WSLg GPU acceleration, with a software fallback (default: auto)")
    parser.add_argument("--reference-timeout-ms", type=int, choices=(120, 180, 250),
                        help="Gazebo reference deadline; defaults to config/gazebo-reference-timeout-ms. "
                             "250 also sets the command watchdog to 250 ms; requires a new B instance")
    parser.add_argument("--guard", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This entry point is for Windows; use start-sim.sh on Ubuntu.")
    if args.guard:
        return guard(int(args.guard[0]), args.guard[1])
    os.chdir(ROOT)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    if args.stop:
        current = inspect_container(NAME)
        if current is not None:
            stop_owned(current["Id"])
        print("Local simulation stopped.")
        return 0
    use_gpu = prepare(args.headless, args.rendering)
    if args.check:
        print("PASS: Docker Desktop, pinned image, simulation resources and WSLg are ready.")
        return 0
    reference_ms = args.reference_timeout_ms
    if reference_ms is None:
        deadline_file = ROOT / "config/gazebo-reference-timeout-ms"
        reference_ms = int(deadline_file.read_text().strip()) if deadline_file.is_file() else 120
    return launch(args.headless, use_gpu, reference_ms)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Simulation launch failed: " + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)
