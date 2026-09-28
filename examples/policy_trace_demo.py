"""Synthetic, stdlib-only trace -> review -> action-SFT demonstration."""
import argparse
import json
from pathlib import Path

from search_policy.dataset import candidates, export_dataset, review_template, write_new_jsonl
from search_policy.events import bind, emit
from search_policy.intent import SearchIntent
from search_policy.store import TraceStore


class FixtureModel:
    def complete_messages(self, messages):
        return json.dumps({'search': messages[-1]['content']}, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    store = TraceStore(args.output / 'traces.sqlite3')
    policy = SearchIntent(FixtureModel(), model_id='synthetic-fixture', model_revision='v1')
    for i in range(12):
        question = f'虚构灯塔 {i} 的闪光周期是多少？'
        run_id = store.start({'question': question, 'conversation_id': f'fixture-session-{i // 2}',
                              'synthetic': True})
        with bind(lambda kind, payload: store.append(run_id, kind, payload)):
            decision = policy.decide(question)
            emit('retrieval.request', {'search_id': f'search-{i}', 'query': decision.query,
                'policy_call_id': decision.call_id, 'origin': 'policy'})
            emit('retrieval.result', {'search_id': f'search-{i}',
                'chunks': [{'chunk_id': f'fixture-{i}', 'text': '虚构灯塔每隔十二秒闪光。',
                            'source_path': 'fiction.pdf', 'page_number': 1}]})
        store.finish(run_id)
    runs = store.read_runs()
    rows = candidates(runs)
    write_new_jsonl(args.output / 'raw.jsonl', runs)
    write_new_jsonl(args.output / 'candidates.jsonl', rows)
    write_new_jsonl(args.output / 'review-template.jsonl', review_template(rows))
    # Only this explicitly synthetic demo auto-approves its own software fixtures.
    reviews = [{**r, 'decision': 'approve', 'reviewer': 'synthetic-demo',
                'note': 'Software fixture only; not real training supervision.'} for r in review_template(rows)]
    write_new_jsonl(args.output / 'reviews.jsonl', reviews)
    report = export_dataset(runs, reviews, args.output / 'dataset')
    print(json.dumps({'synthetic': True, 'counts': report['counts'], 'output': str(args.output)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
