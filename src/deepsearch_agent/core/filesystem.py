"""OS-native absolute paths for deeply nested workspace-owned resources."""
import os
from pathlib import Path


def native_path(path):
    path = Path(path).resolve()
    if os.name != 'nt' or str(path).startswith('\\\\?\\'):
        return path
    value = str(path)
    if value.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + value[2:])
    return Path('\\\\?\\' + value)
