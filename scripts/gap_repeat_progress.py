"""Read-only progress from persisted attempts, never scheduled manifest counts."""
from collections import Counter
import json
from pathlib import Path

root=Path('output/gap-repeats-20261019')
rows=[]
for source in (root/'runs').glob('*/attempts.jsonl'):
    for line in source.read_text(encoding='utf-8').splitlines():
        rows.append((source.parent.name,json.loads(line)))
print(json.dumps({'completed':len(rows),'expected':120,
    'versions':dict(Counter(name.rsplit('-',1)[-1] for name,_ in rows)),
    'statuses':dict(Counter(a['prediction']['status'] for _,a in rows)),
    'deferred_paths':sum(len(e['paths']) for _,a in rows for e in a['events'] if e['kind']=='mainline.candidates.gap_review_deferred'),
    'completed_jobs':len(list(root.glob('*.done.json'))),
    'model_calls':sum(a['usage']['model_calls'] for _,a in rows),
    'known_tokens':sum(a['usage']['known_reported_tokens'] for _,a in rows),
    'missing_usage_calls':sum(a['usage']['usage_missing_calls'] for _,a in rows)},ensure_ascii=False))
