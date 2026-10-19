"""Inspect the Git index before publishing, without printing potential secrets."""
import json
from pathlib import PurePosixPath
import re
import subprocess


SECRET_PATTERNS = {
    'provider_token': re.compile(r'\bsk-[A-Za-z0-9_-]{24,}'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential_url': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
}
PRIVATE_DIRS = {'data', 'tmp', '.venv', 'node_modules', 'volumes', 'backups',
                'artifacts', 'model_cache', 'output', '__pycache__'}


def inspect(path, content):
    issues = []
    parts = PurePosixPath(path).parts
    if any(part in PRIVATE_DIRS or part.endswith('.working-memory') for part in parts):
        issues.append({'path': path, 'rule': 'runtime_or_private_directory'})
    if any(part.startswith('.env') and not part.endswith('.example') for part in parts):
        issues.append({'path': path, 'rule': 'private_environment'})
    if len(content) > 5_000_000:
        issues.append({'path': path, 'rule': 'large_artifact'})
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return issues
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            # Exact localhost rejection fixture only; never exempt a whole test file.
            if (rule == 'credential_url' and path == 'tests/test_mcp_runtime.py'
                    and match.group() == 'http://' + 'user:secret@'
                    and text[match.end():].startswith('localhost/mcp"')):
                continue
            if (rule == 'credential_url' and path == 'tests/test_multihop_upstream.py'
                    and match.group() == 'http://' + 'user:secret@'
                    and text[match.end():].startswith("localhost:7897'")):
                continue
            issues.append({'path': path, 'line': text.count('\n', 0, match.start()) + 1, 'rule': rule})
    return issues


def main():
    names = subprocess.check_output(['git', 'ls-files', '-z']).decode('utf-8').split('\0')
    issues = []
    for name in filter(None, names):
        content = subprocess.check_output(['git', 'show', ':' + name])
        issues.extend(inspect(name, content))
    print(json.dumps({'scope': 'git_index_not_history', 'files': len(list(filter(None, names))),
                      'findings': issues}, ensure_ascii=True, indent=2))
    return bool(issues)


if __name__ == '__main__':
    raise SystemExit(main())
