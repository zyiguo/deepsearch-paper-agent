"""Isolated, paired LangGraph ablations with inspectable stage traces."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import time

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_experiments.benchmarks.instrumentation import (
    MeasuredClient, RunTrace, TracedKnowledgeBase, TracedReranker, TracedToolSession,
)
from deepsearch_experiments.benchmarks.runtime import (
    BenchmarkPredictor, build_index, load_index, save_index, scoped_index,
)
from deepsearch_experiments.benchmarks.schema import inside, load_manifest, sha256, validate_prediction
from deepsearch_agent.adapters.persistence.local_files import atomic_write
from deepsearch_experiments.legacy.workflow import LegacyResearchWorkflow as ResearchWorkflow
from deepsearch_agent.agents.tool_service import ToolError


VARIANTS = ('fixed_workflow', 'agent_full', 'agent_no_rerank', 'agent_no_ocr', 'agent_no_visual', 'oracle')
SEED = 20261005


def write_json(path, data):
    atomic_write(Path(path), json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8'))


def write_rows(path, rows):
    atomic_write(Path(path), ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n'
                                   for r in rows).encode('utf-8'))


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def selected_cases(manifest, seed=SEED):
    """Stratify on supplied question type, never model results or answer labels."""
    rng = random.Random(seed)
    if manifest['dataset'] == 'chartqa':
        strata = [('human', 3), ('augmented', 3)]
    else:
        strata = [('counting', 1), ('existence', 2), ('object_recognition', 1), ('structural_understanding', 2)]
    selected = []
    for group, count in strata:
        pool = sorted((c for c in manifest['cases'] if c['group'] == group), key=lambda c: c['id'])
        if len(pool) < count:
            raise ValueError(f'Insufficient cases in stratum: {group}')
        selected.extend(rng.sample(pool, count))
    return selected


def catalog_text(page):
    if not page['elements']:
        return page['text']
    return '\n'.join('ELEMENT ' + json.dumps(e, ensure_ascii=False) for e in page['elements'])


def prepare(manifest_paths, output, seed=SEED):
    output = Path(output)
    if output.exists():
        raise ValueError('Preparation directory exists; preserve it and choose a new directory')
    loaded = [load_manifest(path) for path in manifest_paths]
    if {m['dataset'] for m in loaded} != {'chartqa', 'pdfvqa'} or len(loaded) != 2:
        raise ValueError('Exactly one manifest for each dataset is required')
    plan = {'schema_version': 1, 'seed': seed, 'datasets': {}, 'variants': list(VARIANTS),
            'protocol': {'chartqa': 'cross-image diagnostic with six random distractors; not official given-image accuracy',
                         'pdfvqa': 'provided-page scope for A/B; page recall is not open retrieval quality'},
            'sampling': '3 human + 3 augmented; A: 1 counting + 2 existence; B: 1 object + 2 structural',
            'gold_policy': 'Gold answers, relations, and gold element IDs are scoring-only; never indexed or prompted'}
    for manifest in loaded:
        cases = selected_cases(manifest, seed)
        ids = {p for c in cases for p in c['scope']}
        if manifest['dataset'] == 'chartqa':
            rng = random.Random(seed + 1)
            candidates = sorted({p['id'] for p in manifest['pages']} - ids)
            ids.update(rng.sample(candidates, min(6, len(candidates))))
        destination = output / manifest['dataset'] / 'manifest.json'
        pages = []
        for original in manifest['pages']:
            if original['id'] not in ids:
                continue
            page = {**original, 'text': catalog_text(original)}
            image = inside(destination.parent, page['image'])
            image.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(manifest['_root'], page['image']), image)
            pages.append(page)
        payload = {k: v for k, v in manifest.items() if not k.startswith('_')}
        payload.update(cases=cases, pages=pages, provenance={**manifest['provenance'],
            'parent_manifest_sha256': manifest['_manifest_sha256'], 'ablation_seed': seed,
            'input_text': 'provided element ID/text/box catalog; no gold relationships'})
        write_json(destination, payload)
        verified = load_manifest(destination)
        plan['datasets'][manifest['dataset']] = {'manifest': destination.relative_to(output).as_posix(),
            'manifest_sha256': verified['_manifest_sha256'], 'case_ids': [c['id'] for c in cases],
            'pages': len(pages)}
    write_json(output / 'plan.json', plan)
    return plan


class PassThroughReranker:
    last_status = 'disabled_control'
    last_scores = []

    def rerank(self, query, chunks, top_k):
        # Keep production's 4*k candidate fetch identical to the reranked arms.
        return chunks[:top_k]


class TextOnlyEvidence:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    def complete(self, prompt, evidence, *, structured=True):
        return (self.client.complete_structured if structured else self.client.complete)(prompt)


class AblationSession(TracedToolSession):
    def __init__(self, *args, ocr_enabled=True, **kwargs):
        self.ocr_enabled = ocr_enabled
        super().__init__(*args, **kwargs)
        if not ocr_enabled:
            self.tool_definitions.pop('ocr_page', None)

    def specs(self):
        return [t for t in super().specs() if self.ocr_enabled or t['name'] != 'ocr_page']


    def ocr_page(self, source, page):
        if not self.ocr_enabled:
            raise ToolError('ocr_unavailable')
        return super().ocr_page(source, page)


def format_instruction(task):
    contract = ('This is a benchmark output-format adapter. Keep the outer role schema unchanged. '
        + ('For every role, answer within the provided page only; do not request adjacent pages or '
           'expand to the whole paper. ' if task in {'A', 'B', 'C'} else '') +
        'For Answerer, return exactly one claim whose text is a JSON STRING encoding '
        '{"answer":"...","element_ids":[]}; citations remain valid 1-based evidence references. '
        'No explanation inside this answer JSON. Return no claims only if unsupported. '
        'For Verifier, verify the meaning of that encoded answer and its citations, not its presentation. ')
    if task in {'B', 'C'}:
        return contract + ('Select ALL and ONLY supplied ELEMENT catalog IDs that directly answer the question '
                           'as strings in element_ids; do not add merely supporting or related elements. '
                           'never use citation indices as element IDs; answer is empty. ')
    return contract + ('Use the short original-language scalar/category/number answer, not a Chinese translation. '
        + ('Task A answer is yes, no, or a digit 0 through 5. ' if task == 'A' else
           'ChartQA answer is a number, percentage, category, or yes/no. '))


class BenchmarkWorkflow(ResearchWorkflow):
    def __init__(self, *args, benchmark_task, trace, **kwargs):
        self.benchmark_task, self.trace = benchmark_task, trace
        super().__init__(*args, **kwargs)

    def call(self, role, instruction, schema, data):
        with self.trace.stage(role.lower()):
            if role == 'Answerer':
                instruction = ('Answer only the requested benchmark task using supplied evidence. '
                    'On revision address review_issues without adding unsolicited details. '
                    'citations must use integer evidence.id values, not ELEMENT IDs or page numbers.')
            elif role == 'Verifier':
                instruction = ('Check the encoded answer and whether its evidence citations support it. '
                    'supported contains zero-based claim indices. Check completeness ONLY against the original '
                    'question, not against all available document details. An ID-selection answer needs no '
                    'narrative expansion. Put genuinely unanswered requested items in missing_answerable with '
                    'valid evidence citations; otherwise return []. Use queries only for genuinely missing '
                    'evidence, not formatting or citation-index repairs.')
            if role in {'Answerer', 'Verifier'}:
                self.trace.observer('rag.benchmark_context', {'role': role, 'evidence': data.get('evidence', []),
                                                            'model_call_offset': len(self.trace.model_calls)})
            return super().call(role, instruction + '\n' + format_instruction(self.benchmark_task), schema, data)


def parse_outcome(case, outcome, allowed_ids):
    result = {'answer': '', 'element_ids': [], 'prediction_status': 'abstained', 'status': 'ok'}
    if outcome.get('status') == 'schema_validation_failed':
        return {**result, 'status': 'invalid_output', 'prediction_status': 'schema_validation_failed'}
    claims, supported = outcome.get('claims', []), outcome.get('supported', [])
    if not supported:
        return result
    if len(supported) != 1:
        return {**result, 'status': 'invalid_output', 'prediction_status': 'multiple_answer_claims'}
    try:
        prediction = validate_prediction(case, json.loads(claims[supported[0]]['text']), allowed_ids)
    except (ValueError, KeyError, IndexError, TypeError):
        return {**result, 'status': 'invalid_output', 'prediction_status': 'invalid_answer_json'}
    return {**result, **prediction.model_dump(), 'prediction_status': 'valid'}


def vision_settings(args):
    from deepsearch_agent.adapters.llm_config import LLMSettings
    from deepsearch_agent.adapters.vision_config import VisionSettings
    settings = VisionSettings.load(args.vision_env, shared_llm=LLMSettings.load(args.llm_env))
    if settings.llm is None:
        raise ValueError('Real cloud vision model required')
    output_tokens = getattr(args, 'max_output_tokens', None)
    changes = {name: getattr(args, name) for name in ('model', 'context_window', 'timeout_seconds')
               if getattr(args, name, None) is not None}
    return replace(settings, max_images=6, llm=replace(settings.llm,
        max_output_tokens=settings.llm.max_output_tokens if output_tokens is None else output_tokens, **changes))


def locations(manifest):
    return {(p['source'], p['page_number']): p['id'] for p in manifest['pages']}


def resolve_page(manifest, source, page):
    return next(p for p in manifest['pages'] if p['source'] == source and p['page_number'] == page)


def index_dataset(args, manifest, destination, vision):
    from deepsearch_experiments.benchmarks.__main__ import remote_components
    from deepsearch_agent.workspace.multimodal import PageAnalysisCache
    from deepsearch_agent.adapters.vision import VisionReader
    components = remote_components(args, images=True) if args.retrieval == 'bm25-image' else {}
    if destination.exists():
        saved = json.loads(destination.read_text(encoding='utf-8'))
        if bool(saved['image_index']) != (args.retrieval == 'bm25-image'):
            raise ValueError('Existing index retrieval mode differs; do not replace it')
        return load_index(destination, manifest, **components)
    trace = RunTrace(locations(manifest), max_model_calls=40)
    client = MeasuredClient(vision.llm, trace)
    try:
        with trace.stage('preparation_ocr'):
            kb = build_index(manifest, retrieval='hybrid' if args.retrieval == 'bm25-image' else 'bm25', **components,
                analyzer=VisionReader(client, replace(vision, llm=client.settings))
                         if manifest['dataset'] == 'chartqa' else None,
                analysis_cache=PageAnalysisCache(destination.parent / 'analysis-cache'))
        save_index(kb, manifest, destination, {'retrieval': args.retrieval,
            'preparation_ocr': manifest['dataset'] == 'chartqa', 'vision_identity': vision.identity,
            'text_embedding': False})
        return kb
    finally:
        trace_path = destination.parent / 'preparation-trace.json'
        previous = json.loads(trace_path.read_text(encoding='utf-8')) if trace_path.exists() else {}
        current = trace.to_dict()
        current['model_calls'] = [*previous.get('model_calls', []), *current['model_calls']]
        current['attempts'] = [*previous.get('attempts', []), {
            'finished_at': datetime.now(timezone.utc).isoformat(), 'retrieval': args.retrieval,
            'index_written': destination.exists()}]
        write_json(trace_path, current)
        client.close()


def predict_case(case, variant, manifest, kb, vision, settings, k, max_model_calls, *, ocr_output_tokens=None):
    from deepsearch_agent.knowledge.multimodal_reranker import MultimodalReranker
    from deepsearch_agent.adapters.vision import VisionReader
    from deepsearch_agent.adapters.visual_evidence import VisualEvidence
    started = time.perf_counter()
    trace = RunTrace(locations(manifest), max_model_calls=max_model_calls)
    client = MeasuredClient(vision.llm, trace)
    ocr_client = MeasuredClient(replace(vision.llm, max_output_tokens=ocr_output_tokens)
                                if ocr_output_tokens is not None else vision.llm, trace)
    row = {'id': case['id'], 'variant': variant, 'status': 'ok', 'answer': '', 'element_ids': [],
           'ranking': [], 'evidence_ranking': [], 'retrieved_element_ids': [], 'evidence_element_ids': [],
           'started_at': datetime.now(timezone.utc).isoformat()}
    session, scoped, evidence = None, None, []
    page_map = {p['id']: p for p in manifest['pages']}
    allowed = {e['id'] for p in case['scope'] for e in page_map[p]['elements']}
    try:
        if variant == 'oracle':
            with trace.stage('oracle'):
                row.update(BenchmarkPredictor(manifest, client, vision)(case))
            row['ranking'] = []
            row['evidence_ranking'] = case['scope']
            row['evidence_element_ids'] = sorted(allowed)
        else:
            scoped = scoped_index(kb, [page_map[p] for p in case['scope']]) if case['dataset'] == 'pdfvqa' else kb.restrict_sources({p['source'] for p in manifest['pages']})
            wrapped = TracedKnowledgeBase(scoped, trace)
            resolver = lambda source, page: inside(manifest['_root'], resolve_page(manifest, source, page)['image'])
            provider = VisualEvidence(client, vision, resolver)
            reranker = TracedReranker(PassThroughReranker() if variant == 'agent_no_rerank'
                                     else MultimodalReranker(provider), trace)
            visual = TextOnlyEvidence(client, vision) if variant == 'agent_no_visual' else provider
            session = None if variant == 'fixed_workflow' else AblationSession(wrapped, trace=trace,
                settings=settings, page_reader=lambda source, page: resolve_page(manifest, source, page),
                image_resolver=resolver, analyzer=VisionReader(ocr_client, replace(vision, llm=ocr_client.settings)),
                reranker=reranker, observer=trace.observer, ocr_enabled=variant != 'agent_no_ocr')
            workflow = BenchmarkWorkflow(client, wrapped, benchmark_task=case['task'], trace=trace,
                context_window=vision.llm.context_window, max_output_tokens=vision.llm.max_output_tokens,
                reranker=reranker, observer=trace.observer, visual_evidence=visual, tool_session=session)
            answer = workflow.answer(case['question'], k, [])
            outcomes = [e['payload'] for e in trace.events if e['kind'] == 'rag.outcome']
            outcome = outcomes[-1] if outcomes else {}
            row.update(parse_outcome(case, outcome, allowed), final_text=answer.text)
            row['ranking'] = list(dict.fromkeys(trace.locations[(s.source_path, s.page_number)] for s in answer.sources))
            evidence = outcome.get('evidence', [])
            row['evidence_ranking'] = list(dict.fromkeys(trace.locations[(c['source_path'], c['page_number'])]
                for c in evidence if (c.get('source_path'), c.get('page_number')) in trace.locations))
    except Exception as error:
        row.update(status='error', error_type=type(error).__name__)
        if trace.model_calls and trace.model_calls[-1]['status'] == 'budget_exceeded':
            row['error_code'] = 'model_call_budget'
        elif trace.model_calls and trace.model_calls[-1]['status'] == 'error':
            row['error_code'] = 'model_service_failure'
    finally:
        client.close()
        ocr_client.close()
    if variant != 'oracle':
        contexts = [e['payload'] for e in trace.events if e['kind'] == 'rag.benchmark_context'
                    and e['payload']['role'] == 'Answerer']
        last_context = contexts[-1] if contexts else {}
        context = last_context.get('evidence', [])
        row['context_ranking'] = list(dict.fromkeys(trace.locations[(c['source'], p)] for c in context
            for p in [c['page'], *c.get('visual_pages', [])] if (c['source'], p) in trace.locations))
        row['context_element_ids'] = element_ids_in_text(allowed, '\n'.join(c.get('text', '') for c in context))
        offset = last_context.get('model_call_offset', len(trace.model_calls))
        call = trace.model_calls[offset] if offset < len(trace.model_calls) else {}
        row['answerer_request_status'] = call.get('status', 'not_sent')
        row['original_image_pages'] = list(dict.fromkeys(trace.locations[(c['source'], p)] for c in context
            for p in c.get('visual_pages', []))) if call.get('image_count', 0) and call.get('status') != 'budget_exceeded' else []
        if row['status'] != 'ok':
            evidence = [*evidence, *([asdict(c) for c in session.evidence] if session else [])]
            row['evidence_ranking'] = list(dict.fromkeys([*row['context_ranking'],
                *(trace.locations[(c['source_path'], c['page_number'])] for c in evidence)]))
            # No final graph outcome exists on exceptions; retain what was observed.
            row['evidence_complete_observation'] = session is not None
        else:
            row['evidence_complete_observation'] = True
        row['evidence_element_ids'] = element_ids_in_text(allowed, '\n'.join(
            c.get('text', '') for c in [*evidence, *context]))
        if scoped is not None:
            chunk_map = {c.chunk_id: c for c in scoped.chunks}
            row['retrieved_element_ids'] = element_ids_in_text(allowed, '\n'.join(
                chunk_map[i].text for call in trace.retrieval_calls for i in call.get('chunk_ids', []) if i in chunk_map))
    row.update(trace=trace.to_dict(), latency_ms=round((time.perf_counter() - started) * 1000, 3),
               finished_at=datetime.now(timezone.utc).isoformat())
    return row


def element_ids_in_text(allowed, text):
    return [i for i in sorted(allowed) if '"id": ' + json.dumps(i, ensure_ascii=False) in text]


def code_fingerprint():
    from deepsearch_experiments.identity import source_fingerprint
    return source_fingerprint(excluded=('ablation_report.py',))


def validate_rows(rows, cases, variants):
    keys = [(r['id'], r['variant']) for r in rows]
    allowed = {(c['id'], v) for c in cases for v in variants}
    if len(keys) != len(set(keys)) or not set(keys) <= allowed:
        raise ValueError('Duplicate or unknown case/variant results')


def select_run(cases, case_ids=None, variants=None):
    ids = [c['id'] for c in cases] if case_ids is None else list(case_ids)
    variants = list(VARIANTS if variants is None else variants)
    if (not ids or len(ids) != len(set(ids)) or not set(ids) <= {c['id'] for c in cases}
            or not variants or len(variants) != len(set(variants)) or not set(variants) <= set(VARIANTS)):
        raise ValueError('Run selection contains empty, duplicate or unknown cases/variants')
    return [c for c in cases if c['id'] in ids], variants


def score(prepared, output):
    from deepsearch_experiments.benchmarks.ablation_metrics import summarize_ablation
    from deepsearch_experiments.benchmarks.badcases import attribute_cases
    manifests = [load_manifest(Path(prepared) / name / 'manifest.json') for name in ('chartqa', 'pdfvqa')]
    cases = [c for m in manifests for c in m['cases']]
    meta = Path(output) / 'run.json'
    identity = json.loads(meta.read_text(encoding='utf-8')) if meta.exists() else {}
    cases, variants = select_run(cases, identity.get('case_ids'), identity.get('variants'))
    rows = read_rows(Path(output) / 'predictions.jsonl')
    validate_rows(rows, cases, variants)
    report = summarize_ablation(cases, rows, k=3, variants=variants)
    badcases = attribute_cases(cases, rows, oracle_rows=[r for r in rows if r['variant'] == 'oracle'], k=3)
    row_locations = {(r['id'], r['variant']): i for i, r in enumerate(rows, 1)}
    for item in badcases:
        line = row_locations.get((item['id'], item['variant']))
        item['trace_locator'] = f'predictions.jsonl#L{line}' if line else None
        item['oracle_locator'] = f"predictions.jsonl#L{row_locations[(item['id'], 'oracle')]}" if (item['id'], 'oracle') in row_locations else None
    write_json(Path(output) / 'summary.json', report)
    write_rows(Path(output) / 'badcases.jsonl', badcases)
    return report


def run(args):
    output = args.output
    manifests = {name: load_manifest(args.prepared / name / 'manifest.json') for name in ('chartqa', 'pdfvqa')}
    cases = [c for m in manifests.values() for c in m['cases']]
    cases, variants = select_run(cases, getattr(args, 'case_ids', None), getattr(args, 'variants', None))
    vision = vision_settings(args)
    ocr_output_tokens = getattr(args, 'ocr_output_tokens', None)
    ocr_output_tokens = vision.llm.max_output_tokens if ocr_output_tokens is None else ocr_output_tokens
    replace(vision.llm, max_output_tokens=ocr_output_tokens)  # Validate before any paid request.
    settings = AgentSettings(enabled=True, max_tool_calls=6, max_steps_per_round=4, max_ocr_calls=1,
                             max_evidence=12, max_images=vision.max_images, timeout_seconds=600)
    indices = {name: load_index_for_run(args, m) for name, m in manifests.items()}
    identity = {'schema_version': 1, 'code_sha256': code_fingerprint(),
        'manifests': {name: m['_manifest_sha256'] for name, m in manifests.items()},
        'indices': {name: sha256(args.prepared / name / 'index.json') for name in manifests},
        'case_ids': [c['id'] for c in cases], 'variants': variants,
        'configuration': {'model': vision.llm.model, 'vision_identity': vision.identity,
            'context_window': vision.llm.context_window, 'max_output_tokens': vision.llm.max_output_tokens,
            'ocr_max_output_tokens': ocr_output_tokens,
            'image_token_budget': vision.image_token_budget, 'timeout_seconds': vision.llm.timeout_seconds,
            'max_model_calls': 18, 'k': 3, 'agent': asdict(settings), 'oracle': 'given-image',
            'adapter': 'single verified claim containing strict benchmark JSON',
            'disabled': ['memory', 'skills', 'external_MCP'],
            'no_visual': 'disable original images for Researcher/Answerer/Verifier; retain OCR and visual reranker',
            'retrieval': {name: json.loads((args.prepared / name / 'index.json').read_text(encoding='utf-8'))['metadata']['retrieval'] for name in manifests},
            'retrieval_fallback_reason': args.retrieval_fallback_reason,
            'ocr_cache': 'no cross-case or cross-variant runtime cache'}}
    meta, predictions = output / 'run.json', output / 'predictions.jsonl'
    rows = []
    if output.exists():
        if not args.resume or not meta.exists() or json.loads(meta.read_text(encoding='utf-8')) != identity:
            raise ValueError('Run identity mismatch; use a new directory or exact matching --resume')
        rows = read_rows(predictions) if predictions.exists() else []
        validate_rows(rows, cases, variants)
    else:
        write_json(meta, identity)
    done = {(r['id'], r['variant']) for r in rows}
    jobs = [(c, v) for c in cases for v in variants if (c['id'], v) not in done]
    random.Random(SEED + 2).shuffle(jobs)
    if args.limit_jobs is not None:
        jobs = jobs[:args.limit_jobs]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(predict_case, c, v, manifests[c['dataset']], indices[c['dataset']],
                              vision, settings, 3, 18, ocr_output_tokens=ocr_output_tokens) for c, v in jobs]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            write_rows(predictions, rows)
            print(json.dumps({'completed': len(rows), 'total': len(cases) * len(variants),
                'id': row['id'], 'variant': row['variant'], 'status': row['status'],
                'model_calls': len(row['trace']['model_calls']), 'tools': len(row['trace']['tool_calls'])}), flush=True)
    return score(args.prepared, output)


def load_index_for_run(args, manifest):
    from deepsearch_experiments.benchmarks.__main__ import remote_components
    path = args.prepared / manifest['dataset'] / 'index.json'
    snapshot = json.loads(path.read_text(encoding='utf-8'))
    return load_index(path, manifest, **(remote_components(args, images=True) if snapshot['image_index'] else {}))


def probe_tools(args):
    """Forced tool availability checks, explicitly separate from agent decisions."""
    from deepsearch_agent.adapters.vision import VisionReader
    vision = vision_settings(args)
    rows = []
    if args.output.exists():
        raise ValueError('Probe output exists; preserve previous results')
    for dataset in ('chartqa', 'pdfvqa'):
        manifest = load_manifest(args.prepared / dataset / 'manifest.json')
        case = manifest['cases'][0]
        page = next(p for p in manifest['pages'] if p['id'] == case['scope'][0])
        kb = scoped_index(load_index_for_run(args, manifest), [page])
        trace = RunTrace(locations(manifest), max_model_calls=2)
        client = MeasuredClient(vision.llm, trace)
        session = AblationSession(TracedKnowledgeBase(kb, trace), trace=trace,
            page_reader=lambda source, number: resolve_page(manifest, source, number),
            image_resolver=lambda source, number: inside(manifest['_root'], resolve_page(manifest, source, number)['image']),
            analyzer=VisionReader(client, replace(vision, llm=client.settings)), observer=trace.observer)
        requests = [('list_documents', {}), ('search_evidence', {'query': case['question'], 'top_k': 3}),
                    ('read_page', {'source': page['source'], 'page': page['page_number']}),
                    ('ocr_page', {'source': page['source'], 'page': page['page_number']})]
        try:
            for name, arguments in requests:
                try:
                    session.invoke(name, arguments)
                except Exception:
                    pass  # Failed attempts remain in the trace and the probe continues.
            if session.evidence:
                session.invoke('get_evidence', {'chunk_id': session.evidence[0].chunk_id})
        finally:
            client.close()
        rows.append({'id': case['id'], 'dataset': dataset, 'protocol': 'forced-given-page-tool-probe-not-agent-choice',
                     'page_id': page['id'], 'trace': trace.to_dict()})
        write_rows(args.output, rows)
        print(json.dumps({'dataset': dataset, 'tools': [(t['name'], t['status']) for t in trace.tool_calls]}), flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'index', 'run', 'score', 'probe'])
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--chartqa', type=Path, default=Path('data/benchmarks/chartqa-val100/manifest.json'))
    parser.add_argument('--pdfvqa', type=Path, default=Path('data/benchmarks/pdfvqa-val100/manifest.json'))
    parser.add_argument('--allow-remote', action='store_true')
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    parser.add_argument('--retrieval', choices=['bm25', 'bm25-image'], default='bm25-image')
    parser.add_argument('--retrieval-fallback-reason', choices=['image_embedding_http_402'])
    parser.add_argument('--limit-jobs', type=int)
    parser.add_argument('--max-output-tokens', type=int, default=None,
                        help='Per-call output budget, including OCR; defaults to VISION_MAX_OUTPUT_TOKENS')
    parser.add_argument('--ocr-output-tokens', type=int, help='Online OCR-only override for historical budget controls')
    parser.add_argument('--model', help='Evaluation-only model override; never edits .env')
    parser.add_argument('--context-window', type=int)
    parser.add_argument('--timeout-seconds', type=float)
    parser.add_argument('--case-id', dest='case_ids', action='append')
    parser.add_argument('--variants', nargs='+', choices=VARIANTS)
    parser.add_argument('--llm-env', type=Path, default=Path('.env'))
    parser.add_argument('--vision-env', type=Path, default=Path('.env.vision'))
    parser.add_argument('--embedding-env', type=Path, default=Path('.env.embedding'))
    parser.add_argument('--image-embedding-env', type=Path, default=Path('.env.image_embedding'))
    args = parser.parse_args()
    if args.direct:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(key, None)
        os.environ['NO_PROXY'] = '*'
    if args.command in {'run', 'index', 'probe'} and not args.allow_remote:
        parser.error('Cloud calls require --allow-remote')
    if args.command in {'run', 'score', 'probe'} and args.output is None:
        parser.error('--output required')
    if args.limit_jobs is not None and args.limit_jobs < 1:
        parser.error('--limit-jobs must be positive')
    if args.command == 'prepare':
        result = prepare([args.chartqa, args.pdfvqa], args.prepared)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'index':
        vision = vision_settings(args)
        for dataset in ('chartqa', 'pdfvqa'):
            manifest = load_manifest(args.prepared / dataset / 'manifest.json')
            kb = index_dataset(args, manifest, args.prepared / dataset / 'index.json', vision)
            print(json.dumps({'dataset': dataset, 'chunks': len(kb.chunks),
                              'image_pages': len(kb.image_index.chunks) if kb.image_index else 0}), flush=True)
    elif args.command == 'run':
        run(args)
    elif args.command == 'probe':
        probe_tools(args)
    else:
        score(args.prepared, args.output)


if __name__ == '__main__':
    main()
