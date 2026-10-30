"""USB authorization monitoring, independent of controller input."""
import shutil
import subprocess
import threading



class QuestConnection:
    def __init__(self, serial=None, adb="adb"):
        self.serial, self.adb = serial, shutil.which(adb)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.generation = 0
        self.state = {"usb": "Checking USB"}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def set_serial(self, serial):
        with self.lock:
            if serial != self.serial:
                self.serial = serial
                self.generation += 1
                self.state.update(usb="Checking selected Quest")

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _command(self, args):
        if not self.adb:
            raise RuntimeError("ADB not found")
        return subprocess.run([self.adb]+args, capture_output=True, timeout=4,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _run(self):
        while not self.stop.is_set():
            with self.lock:
                serial, generation = self.serial, self.generation
            try:
                devices = self._command(["devices"])
                if devices.returncode:
                    raise RuntimeError(devices.stderr.decode("utf-8", "replace")[:180])
                entries = [line.split() for line in devices.stdout.decode("utf-8", "replace").splitlines()[1:] if line.strip()]
                if serial:
                    row = next((row for row in entries if row[0] == serial), None)
                else:
                    authorized = [row for row in entries if len(row)>1 and row[1] == "device"]
                    row = authorized[0] if len(authorized) == 1 else None
                authorized = bool(row and len(row)>1 and row[1] == "device")
                usb = "Authorized · " + row[0] if authorized else ("Selected Quest not detected" if row is None else "Not ready · " + " ".join(row[1:]))
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.state["usb"] = usb
            except Exception as exc:
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.state["usb"] = ("USB check timed out. Check the connection." if isinstance(exc, subprocess.TimeoutExpired)
                                         else "Check failed: "+str(exc)[:100])
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
