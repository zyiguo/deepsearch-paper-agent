"""A local process lock shared by API lifetimes and offline maintenance tools."""
import os
from pathlib import Path


class DeploymentBusy(RuntimeError):
    pass


class DeploymentLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise DeploymentBusy('Deployment is active; stop this API and other writers before offline maintenance') from None
        self.stream = stream
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            # Closing releases the OS lock even after a failed backup. Keep the inode.
            self.stream.close()
            self.stream = None
