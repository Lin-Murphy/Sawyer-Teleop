"""OS-owned advisory lock; released automatically on process exit."""
import hashlib
from pathlib import Path
import tempfile


class ProcessLock:
    def __init__(self, identity):
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.path = Path(tempfile.gettempdir())/("sawyer-teleop-"+digest+".lock")
        self.file = None

    def acquire(self):
        import os
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("An A process is already using this device or data directory")
        return self

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
