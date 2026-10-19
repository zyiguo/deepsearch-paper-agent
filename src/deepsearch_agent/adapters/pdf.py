"""Read PDFs through the bounded, sanitized worker used by uploads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class SubprocessPdfReader:
    def read(self, path: str) -> list[str]:
        with tempfile.TemporaryDirectory(prefix='deepsearch-pdf-') as root:
            target = Path(root) / 'pages.json'
            result = subprocess.run([sys.executable, '-B', '-m', 'deepsearch_agent.pdf_worker',
                'extract', str(path), str(target)], capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                try:
                    message = json.loads(result.stdout)['error']
                except (ValueError, KeyError, TypeError):
                    message = 'PDF 处理失败'
                raise ValueError(message)
            return json.loads(target.read_text(encoding='utf-8'))
