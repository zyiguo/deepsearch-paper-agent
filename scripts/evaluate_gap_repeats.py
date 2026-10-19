"""Frozen 20-case, three-repeat v4/v5 comparison. No automatic failed-attempt retries."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
from statistics import median
import subprocess
import sys

from evaluate_multicandidate_paths import digest, write, role_cost
from deepsearch_agent.adapters.llm_config import LLMSettings
from deepsearch_agent.core.evidence_paths import fingerprint
from deepsearch_experiments.benchmarks.multihop.datasets import load_manifest
from deepsearch_experiments.benchmarks.multihop.runner import implementation_identity, read_attempts, score_run


ROOT = Path('output/gap-repeats-20261019')


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    prior = Path('output/incremental-gaps-20261019')
    old = json.loads((prior / 'plan.json').read_text(encoding='utf-8'))
    assert implementation_identity() == old['implementation'], 'Production implementation changed'
    settings = LLMSettings.load(Path('.env'))
    assert all(getattr(settings, k) == old['model'][k] for k in ('backend', 'model', 'base_url', 'timeout_seconds'))
    hashes = {str(prior / r['path']): r['sha256'] for r in json.loads((prior / 'delivery-manifest.json').read_text())}
    hashes.update(json.loads((prior / 'previous-artifact-hashes.json').read_text()))
    hashes.update(old['original_hashes'])
    assert all(digest(Path(p)) == h for p, h in hashes.items())
    full_root = Path('output/expanded-evaluation-20261018-100x2')
    old_full = json.loads((full_root / 'plan.json').read_text())
    shards, selection = {}, {}
    for dataset in old['datasets']:
        source = full_root / old_full['datasets'][dataset]['manifest']
        full = json.loads(source.read_text(encoding='utf-8'))
        canary = json.loads(Path(old['datasets'][dataset]['manifest']).read_text(encoding='utf-8'))
        assert full['documents'] == canary['documents']
        anchors = {c['id'] for c in canary['cases']}
        extras = sorted((c['id'] for c in full['cases'] if c['id'] not in anchors),
                        key=lambda s: hashlib.sha256(('gap-repeats-20261019:' + s).encode()).hexdigest())[:8]
        ids = sorted(anchors) + extras
        selection[dataset] = {'ids': ids, 'anchors': sorted(anchors), 'source': str(source),
                              'source_sha256': digest(source), 'corpus_documents': len(full['documents'])}
        for i in range(5):
            selected = set(ids[2*i:2*i+2])
            value = {**full, 'limit': 2,
                     'cases': [c for c in full['cases'] if c['id'] in selected],
                     'gold': [g for g in full['gold'] if g['id'] in selected]}
            name = f'{dataset}-{i}'
            path = root / (name + '.json')
            write(path, value)
            load_manifest(path)
            shards[name] = {'dataset': dataset, 'path': str(path.resolve()), 'sha256': digest(path)}
    jobs = [{'repeat': r, 'shard': shard, 'version': v,
             'name': f'r{r}-{shard}-{v}'} for r in range(3) for shard in shards for v in ('v4', 'v5')]
    random.Random(20261019).shuffle(jobs)
    plan = {'implementation': implementation_identity(), 'limits': old['limits'], 'model': old['model'],
            'selection': selection, 'shards': shards, 'jobs': jobs, 'repeats': 3, 'max_workers': 4,
            'gold_exclusions': old['gold_exclusions'], 'historical_hashes': hashes,
            'selection_rule': 'Four retained development canaries plus 8 stable-SHA-selected cases per dataset; no current outcome selection.',
            'sampling': 'Provider defaults, no controlled provider seed; stochastic repeats not independent new questions.',
            'test_evidence': str(prior / 'validation-final.json'), 'retry_errors': False}
    write(root / 'plan.json', plan)
    print(json.dumps({'unique_cases': 20, 'repeats': 3, 'predictions': 120, 'jobs': len(jobs)}))


def run(root, plan):
    assert implementation_identity() == plan['implementation']
    def worker(job):
        shard = plan['shards'][job['shard']]
        assert digest(Path(shard['path'])) == shard['sha256']
        output = root / 'runs' / job['name']
        done = root / (job['name'] + '.done.json')
        if done.exists():
            return json.loads(done.read_text())
        # Resume only unfinished cases; CLI does not rerun error attempts.
        args = [sys.executable, '-m', 'deepsearch_experiments.benchmarks.multihop', 'run',
                '--manifest', shard['path'], '--method', 'mainline-evidence-controlled-' + job['version'],
                '--output', str(output), '--llm-env', '.env', '--allow-remote', '--direct', '--max-steps', '6']
        if output.exists():
            args.append('--resume')
        for flag, key in [('k','k'), ('max-model-calls','model_calls'), ('max-search-calls','search_calls'),
                         ('token-budget','token_budget'), ('max-output-tokens','max_output_tokens'),
                         ('context-window','context_window'), ('deadline-seconds','deadline_seconds')]:
            args += ['--' + flag, str(plan['limits'][key])]
        logs = list(root.glob(job['name'] + '.launch-*.log'))
        with (root / (job['name'] + f'.launch-{len(logs)}.log')).open('x', encoding='utf-8') as handle:
            result = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT)
        row = {**job, 'exit_code': result.returncode}
        write(done, row)
        print(json.dumps(row), flush=True)
        return row
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, plan['jobs']))
    assert implementation_identity() == plan['implementation']
    write(root / 'batch-result.json', results)
    assert all(r['exit_code'] == 0 for r in results)


def stability(pairs, seed=20261019):
    """Question-cluster bootstrap: keep each question's repeats together."""
    known = [p for p in pairs if p['v4_tokens'] is not None and p['v5_tokens'] is not None]
    by_id = defaultdict(list)
    for p in known:
        by_id[p['dataset'], p['id']].append(p)
    # Incomplete clusters are explicitly excluded from cost CI, never zero-filled.
    clusters = [rows for rows in by_id.values() if len(rows) == 3]
    question_means=[{'dataset':rows[0]['dataset'],'id':rows[0]['id'],
                    'v4_mean_tokens':sum(r['v4_tokens'] for r in rows)/3,
                    'v5_mean_tokens':sum(r['v5_tokens'] for r in rows)/3,
                    'mean_delta_tokens':sum(r['v5_tokens']-r['v4_tokens'] for r in rows)/3}
                   for rows in clusters]
    rng = random.Random(seed)
    ratios = []
    for _ in range(10000 if clusters else 0):
        sample = [r for _ in clusters for r in rng.choice(clusters)]
        a, b = sum(r['v4_tokens'] for r in sample), sum(r['v5_tokens'] for r in sample)
        if a: ratios.append((a-b)/a)
    ratios.sort()
    repeats = []
    for repeat in range(3):
        rows = [p for p in pairs if p['repeat'] == repeat]
        complete = all(p['v4_tokens'] is not None and p['v5_tokens'] is not None for p in rows)
        a = sum(p['v4_tokens'] for p in rows) if complete else None
        b = sum(p['v5_tokens'] for p in rows) if complete else None
        repeats.append({'repeat':repeat,'paired_count':len(rows),'v4_tokens':a,'v5_tokens':b,
                        'reduction_fraction':(a-b)/a if a else None})
    return {'paired_count':len(pairs),'known_usage_pairs':len(known),'complete_question_clusters':len(clusters),
            'question_mean_costs':question_means,
            'question_mean_cost_win_fraction':sum(q['mean_delta_tokens']<0 for q in question_means)/len(question_means) if question_means else None,
            'median_paired_delta_tokens':median(p['v5_tokens']-p['v4_tokens'] for p in known) if known else None,
            'paired_cost_win_fraction':sum(p['v5_tokens'] < p['v4_tokens'] for p in known)/len(known) if known else None,
            'question_cluster_bootstrap_95_interval': [ratios[int(.025*len(ratios))],ratios[min(len(ratios)-1,int(.975*len(ratios)))]] if ratios else None,
            'bootstrap_resamples':len(ratios),'per_repeat':repeats,
            'interval_scope':'Conditional on complete three-repeat question clusters; selected development cases, not population generalization.'}


def quality(pairs):
    eligible=[p for p in pairs if not p['gold_excluded']]
    clusters=defaultdict(list)
    for p in eligible:clusters[p['dataset'],p['id']].append(p)
    rng=random.Random(20261019)
    values=list(clusters.values())
    deltas=[]
    for _ in range(10000 if values else 0):
        sample=[r for _ in values for r in rng.choice(values)]
        deltas.append(sum(r['v5_em']-r['v4_em'] for r in sample)/len(sample))
    deltas.sort()
    return {'eligible_pairs':len(eligible),'question_clusters':len(clusters),
            'v4_em':sum(p['v4_em'] for p in eligible)/len(eligible) if eligible else None,
            'v5_em':sum(p['v5_em'] for p in eligible)/len(eligible) if eligible else None,
            'accuracy_delta_cluster_bootstrap_95_interval':[deltas[int(.025*len(deltas))],deltas[int(.975*len(deltas))]] if deltas else None,
            'correct_to_wrong':sum(p['v4_em']==1 and p['v5_em']!=1 for p in eligible),
            'wrong_to_correct':sum(p['v4_em']!=1 and p['v5_em']==1 for p in eligible),
            'correct_to_wrong_with_defer':sum(p['v4_em']==1 and p['v5_em']!=1 and p['v5_deferred_paths']>0 for p in eligible),
            'causal_warning':'Separate stochastic runs; a paired regression with deferral is a risk signal, not proof deferral caused it.'}


def finalize(root, plan):
    assert implementation_identity() == plan['implementation']
    assert all(digest(Path(p)) == h for p, h in plan['historical_hashes'].items())
    records, deferred = [], []
    for job in plan['jobs']:
        shard = plan['shards'][job['shard']]
        manifest = load_manifest(shard['path'])
        output = root / 'runs' / job['name']
        report = score_run(manifest, output)
        assert report['run']['implementation'] == plan['implementation']
        assert report['run']['model'] == plan['model'] and report['run']['limits'] == plan['limits']
        attempts, _ = read_attempts(output / 'attempts.jsonl', manifest, report['run'])
        assert len(attempts) == 2 and all(a['attempt'] == 1 for a in attempts)
        scores = {s['id']:s for s in report['per_case']}
        for a in attempts:
            excluded = a['id'] in plan['gold_exclusions']
            score = scores[a['id']]
            answer = a['prediction']['answer']
            group = ('failure' if a['prediction']['status'] != 'ok' else 'unknown' if excluded or answer.strip().upper() in {'','UNKNOWN'} else 'success' if score['answer_em']==1 else 'failure')
            events = a['events']
            proposed, data, gaps, reviews_by_id = None, {}, {}, {}
            for index,e in enumerate(events):
                if e['kind']=='mainline.communication.model_input' and e.get('role')=='Answerer': data=e['data']
                if e['kind']=='mainline.candidates.proposed': proposed=e
                if e['kind']=='mainline.candidates.gap_state': gaps=e['gaps']
                if e['kind']=='mainline.candidates.reviewed' and e.get('semantic_review_performed',True):
                    reviews_by_id[fingerprint(e['review'])]=e
                if e['kind']=='mainline.candidates.gap_review_deferred':
                    for path in e['paths']:
                        ci,pi=path['candidate_index'],path['path_index']
                        original=reviews_by_id.get(path['review_id'],{})
                        prior_candidate=next((c for c in original.get('candidates',[]) if c['candidate_id']==path['candidate_id']),None)
                        prior_claims=[[s['claim'] for s in p['steps']] for p in prior_candidate['paths']] if prior_candidate else []
                        current_claims=[s['claim'] for s in proposed['candidates'][ci]['paths'][pi]['steps']]
                        deferred.append({'id':a['id'],'job':job['name'],'event_index':index,
                            'source':str((output/'attempts.jsonl').relative_to(root)), 'source_sha256':digest(output/'attempts.jsonl'),
                            'decision':path,'candidate':proposed['candidates'][ci], 'path':proposed['candidates'][ci]['paths'][pi],
                            'all_candidates':proposed['candidates'],
                            'prior_review_found':bool(original),'prior_path_claims':prior_claims,
                            'current_path_claims':current_claims,
                            'claims_changed_from_all_prior_paths':current_claims not in prior_claims if prior_claims else None,
                            'prior_gap':gaps.get(path['candidate_id']), 'evidence':proposed['evidence'],
                            'question':data['question'],'requirements':data['requirements'],
                            'human_path_correct':None,'human_false_defer':None,'reviewer':None})
            records.append({'dataset':shard['dataset'],'id':a['id'],'repeat':job['repeat'],'version':job['version'],
                'anchor':a['id'] in plan['selection'][shard['dataset']]['anchors'], 'gold_excluded':excluded,
                'score':score,'group':group,'prediction':a['prediction'],'usage':a['usage'],'role_cost':role_cost(events),
                'gap_deferred_paths':sum(len(e['paths']) for e in events if e['kind']=='mainline.candidates.gap_review_deferred'),
                'verification_skips':dict(Counter(e['reason'] for e in events if e['kind']=='mainline.candidates.verification_skipped')),
                'schema_repairs':sum(e['kind']=='mainline.rag.schema_repair' for e in events),
                'source':str((output/'attempts.jsonl').relative_to(root)),'source_sha256':digest(output/'attempts.jsonl')})
    assert len(records)==120
    paired=defaultdict(dict)
    for r in records: paired[r['dataset'],r['id'],r['repeat']][r['version']]=r
    pairs=[]
    for (dataset,ident,repeat),rows in sorted(paired.items()):
        a,b=rows['v4'],rows['v5']
        pairs.append({'dataset':dataset,'id':ident,'repeat':repeat,'anchor':a['anchor'],
            'gold_excluded':a['gold_excluded'],'v4_tokens':a['usage']['reported_total_tokens'],
            'v5_tokens':b['usage']['reported_total_tokens'],'v4_em':a['score']['answer_em'],
            'v5_em':b['score']['answer_em'],'v4_status':a['prediction']['status'],'v5_status':b['prediction']['status'],
            'v5_deferred_paths':b['gap_deferred_paths']})
    totals={}
    for version in ('v4','v5'):
        rows=[r for r in records if r['version']==version];eligible=[r for r in rows if not r['gold_excluded']]
        totals[version]={'predictions':len(rows),'eligible':len(eligible),'eligible_em':sum(r['score']['answer_em'] for r in eligible)/len(eligible),
            'normal_abstentions':sum(r['prediction']['status']=='ok' and r['prediction']['answer'].strip().upper() in {'','UNKNOWN'} for r in rows),
            'quarantined_gold_predictions':sum(r['gold_excluded'] for r in rows),
            'groups':dict(Counter(r['group'] for r in rows)),'statuses':dict(Counter(r['prediction']['status'] for r in rows)),
            'reported_total_tokens':sum(r['usage']['reported_total_tokens'] for r in rows) if all(r['usage']['reported_total_tokens'] is not None for r in rows) else None,
            'known_reported_tokens':sum(r['usage']['known_reported_tokens'] for r in rows),
            'usage_missing_calls':sum(r['usage']['usage_missing_calls'] for r in rows),
            'model_calls':sum(r['usage']['model_calls'] for r in rows),'schema_repairs':sum(r['schema_repairs'] for r in rows),
            'gap_deferred_paths':sum(r['gap_deferred_paths'] for r in rows),
            'cases_with_gap_defer':len({(r['dataset'],r['id']) for r in rows if r['gap_deferred_paths']}),
            'runs_with_gap_defer':sum(r['gap_deferred_paths']>0 for r in rows)}
    write(root/'per-attempt.json',records);write(root/'paired.json',pairs)
    write(root/'summary.json',{'totals':totals,'stability':stability(pairs),
        'new_cases_stability':stability([p for p in pairs if not p['anchor']]),
        'quality':quality(pairs),'new_cases_quality':quality([p for p in pairs if not p['anchor']]),
        'per_repeat_quality':{str(i):quality([p for p in pairs if p['repeat']==i]) for i in range(3)},
        'true_false_defer_rate':None,'human_labels_assigned':0,
        'correct_to_wrong_pairs':[p for p in pairs if not p['gold_excluded'] and p['v4_em']==1 and p['v5_em']!=1]})
    for name,rows in [('deferred-review',deferred)]+[(g,[r for r in records if r['group']==g]) for g in ('success','failure','unknown')]:
        with (root/(name+'.jsonl')).open('x',encoding='utf-8') as f:
            for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps(totals,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','run','finalize']);p.add_argument('--output',type=Path,default=ROOT)
    args=p.parse_args()
    if args.command=='prepare':prepare(args.output)
    else:
        plan=json.loads((args.output/'plan.json').read_text(encoding='utf-8'))
        (run if args.command=='run' else finalize)(args.output,plan)
