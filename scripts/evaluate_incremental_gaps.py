"""Same original cases and budgets, same-build v4 versus v5; exclusive outputs."""
import argparse
import json
from pathlib import Path
from evaluate_multicandidate_paths import prepare, run, finalize, digest, write


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'finalize'])
    parser.add_argument('--output', type=Path, default=Path('output/incremental-gaps-20261019'))
    args = parser.parse_args()
    root = args.output
    if args.command == 'prepare':
        previous = Path('output/candidate-reuse-20261019')
        manifest = json.loads((previous / 'delivery-manifest.json').read_text(encoding='utf-8'))
        # Preserve all previous evidence, not only the older source canary.
        if isinstance(manifest, dict):
            manifest = manifest['files']
        hashes = {str(previous / item['path']): item['sha256'] for item in manifest}
        older = json.loads((previous / 'previous-artifact-hashes.json').read_text(encoding='utf-8'))
        hashes.update(older)
        assert all(digest(Path(p)) == h for p, h in hashes.items())
        write(root / 'previous-artifact-hashes.json', hashes)
        prepare(root, ['mainline-evidence-controlled-v4', 'mainline-evidence-controlled-v5'])
    else:
        plan = json.loads((root / 'plan.json').read_text(encoding='utf-8'))
        hashes = json.loads((root / 'previous-artifact-hashes.json').read_text(encoding='utf-8'))
        assert all(digest(Path(p)) == h for p, h in hashes.items())
        (run if args.command == 'run' else finalize)(root, plan)
