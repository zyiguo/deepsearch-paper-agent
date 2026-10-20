"""Atomic local writes shared by document and attachment storage."""
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            name = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
        # Only repeat the atomic rename, never repeat the operation that produced
        # the bytes. Persistent permission failures still propagate.
        for attempt in range(6):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if os.name != 'nt' or attempt == 5:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        if name is not None:
            name.unlink(missing_ok=True)
