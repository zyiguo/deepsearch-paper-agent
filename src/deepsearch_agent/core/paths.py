"""One location for default project paths, independent of feature module depth."""
from pathlib import Path
import os
PROJECT_ROOT = Path(os.environ.get('DEEPSEARCH_ROOT', Path(__file__).resolve().parents[3])).resolve()
DEFAULT_INDEX_PATH = PROJECT_ROOT / 'data' / 'indexes' / 'demo.json'
