"""Atomic local writes shared by document and attachment storage."""
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            name = Path(handle.name)
            handle.write(content)
        os.replace(name, path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)
