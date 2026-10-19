"""CLI: python -m deepsearch_experiments.benchmarks --help."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from deepsearch_experiments.benchmarks.datasets import chartqa_cases, pdfvqa_document_cases, read_data
from deepsearch_experiments.benchmarks.downloads import download_chartqa, download_pdfvqa, store_image, write_manifest
from deepsearch_experiments.benchmarks.runner import read_predictions, run_cases, summarize
from deepsearch_experiments.benchmarks.runtime import BenchmarkPredictor, build_index, load_index, save_index
from deepsearch_experiments.benchmarks.schema import inside, load_manifest, sha256
from deepsearch_agent.adapters.persistence.local_files import atomic_write


def parser():
    p = argparse.ArgumentParser(description='Isolated public multimodal evaluation; no application data writes')
    sub = p.add_subparsers(dest='command', required=True)
    download = sub.add_parser('download')
    download.add_argument('--dataset', choices=['chartqa', 'pdfvqa'], required=True)
    download.add_argument('--split', default='val')
    download.add_argument('--limit', type=int, default=100)
    download.add_argument('--seed', type=int, default=42)
    download.add_argument('--output', type=Path, required=True)
    download.add_argument('--allow-network', action='store_true')
    download.add_argument('--proxy', help='Explicit HTTP proxy for dataset downloads')
    import_chart = sub.add_parser('import-chartqa')
    import_chart.add_argument('--root', type=Path, required=True)
    import_chart.add_argument('--split', choices=['train', 'val', 'test'], default='val')
    import_chart.add_argument('--output', type=Path, required=True)
    import_pdf = sub.add_parser('import-pdfvqa')
    import_pdf.add_argument('--questions', type=Path, required=True)
    import_pdf.add_argument('--layout', type=Path, required=True)
    import_pdf.add_argument('--images', type=Path, required=True)
    import_pdf.add_argument('--task', choices=['A', 'B', 'C'], required=True)
    import_pdf.add_argument('--split', choices=['train', 'val', 'test'], default='val')
    import_pdf.add_argument('--output', type=Path, required=True)
    for name in ('index', 'run', 'score'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--manifest', type=Path, required=True)
        cmd.add_argument('--output', type=Path, required=True)
        if name in {'index', 'run'}:
            network = cmd.add_mutually_exclusive_group()
            network.add_argument('--proxy', help='Explicit HTTP proxy for all remote requests')
            network.add_argument('--direct', action='store_true', help='Bypass inherited proxies for cloud models')
            cmd.add_argument('--allow-remote', action='store_true')
            cmd.add_argument('--llm-env', type=Path, default=Path('.env'))
            cmd.add_argument('--vision-env', type=Path, default=Path('.env.vision'))
            cmd.add_argument('--embedding-env', type=Path, default=Path('.env.embedding'))
            cmd.add_argument('--image-embedding-env', type=Path, default=Path('.env.image_embedding'))
        if name == 'index':
            cmd.add_argument('--retrieval', choices=['bm25', 'image', 'hybrid'], default='bm25')
            cmd.add_argument('--analyze', action='store_true', help='Paid visual OCR/description extraction')
        else:
            cmd.add_argument('--mode', choices=['oracle', 'rerank', 'rag'], default='oracle')
            cmd.add_argument('--k', type=int, default=3)
        if name == 'run':
            cmd.add_argument('--snapshot', type=Path)
            cmd.add_argument('--limit', type=int)
            cmd.add_argument('--workers', type=int, default=2)
            cmd.add_argument('--pool-size', type=int, default=8)
            cmd.add_argument('--seed', type=int, default=42)
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--retry-errors', action='store_true')
        if name == 'score':
            cmd.add_argument('--predictions', type=Path, required=True)
    return p


def remote_components(args, *, text=False, images=False):
    from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
    from deepsearch_agent.adapters.image_embeddings import ImageEmbeddingSettings, SiliconFlowImageEmbeddingClient
    settings = EmbeddingSettings.load(args.embedding_env)
    embedding, embedding_id, image_embedding, image_id = None, None, None, None
    if text:
        if settings.backend != 'openai_compatible':
            raise ValueError('Real text embeddings required; demo vectors are not benchmark evidence')
        embedding, embedding_id = RemoteEmbeddingClient(settings), f'remote-{settings.identity}'
    if images:
        image_settings = ImageEmbeddingSettings.load(args.image_embedding_env, shared_embeddings=settings)
        if image_settings.backend != 'siliconflow':
            raise ValueError('Real image embeddings required')
        image_embedding, image_id = SiliconFlowImageEmbeddingClient(image_settings), image_settings.identity
    return dict(embedding=embedding, embedding_id=embedding_id,
                image_embedding=image_embedding, image_identity=image_id)


def main():
    p = parser()
    args = p.parse_args()
    if getattr(args, 'direct', False):
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(key, None)
        os.environ['NO_PROXY'] = '*'
    if getattr(args, 'proxy', None):
        proxy = urlsplit(args.proxy)
        if proxy.scheme not in {'http', 'https'} or not proxy.hostname or proxy.username or proxy.password or proxy.query or proxy.fragment:
            p.error('Proxy must be an HTTP(S) address without credentials or query parameters')
        # Process-local only: never change the user's machine or app environment.
        os.environ['HTTP_PROXY'] = os.environ['HTTPS_PROXY'] = args.proxy
        os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
    client = None
    try:
        if args.command == 'download':
            if not args.allow_network:
                p.error('Dataset download requires --allow-network; no cloud model calls')
            if args.output.exists():
                p.error('Manifest already exists; use a new output directory to preserve sample identity')
            split = args.split
            if args.dataset == 'pdfvqa' and split == 'val':
                split = 'validation'
            fn = download_chartqa if args.dataset == 'chartqa' else download_pdfvqa
            fn(args.output, split=split, limit=args.limit, seed=args.seed)
            data = load_manifest(args.output)
            print(json.dumps(dict(cases=len(data['cases']), pages=len(data['pages']), manifest_sha256=data['_manifest_sha256'])))
            return
        if args.command.startswith('import-'):
            if args.output.exists():
                p.error('Manifest exists; use a new output')
            if args.command == 'import-chartqa':
                cases = []
                for group in ('human', 'augmented'):
                    cases.extend(chartqa_cases(read_data(args.root / args.split / f'{args.split}_{group}.json'), args.split, group))
                pages = []
                for key in sorted({c['scope'][0] for c in cases}):
                    file = key.rsplit('/', 1)[1]
                    image, fingerprint = store_image(args.output, inside(args.root / args.split / 'png', file).read_bytes())
                    pages.append(dict(id=key, source=f'benchmark://chartqa/{args.split}/{file}', page_number=1,
                                      image=image, image_sha256=fingerprint, text='', elements=[]))
                dataset = 'chartqa'
                provenance = dict(source='https://github.com/vis-nlp/ChartQA', status='local-official-format',
                                  labels={g: sha256(args.root / args.split / f'{args.split}_{g}.json') for g in ('human', 'augmented')})
            else:
                pages, cases = pdfvqa_document_cases(read_data(args.questions), read_data(args.layout), args.task, args.split)
                for page in pages:
                    image, fingerprint = store_image(args.output, inside(args.images, page['image']).read_bytes())
                    page.update(image=image, image_sha256=fingerprint)
                dataset = 'pdfvqa'
                provenance = dict(source='https://github.com/adlnlp/pdfvqa', status='local-original-format',
                                  questions_sha256=sha256(args.questions), layout_sha256=sha256(args.layout), bbox_format='dataset-original')
            write_manifest(args.output, dataset, args.split, pages, cases, provenance)
            load_manifest(args.output)
            return
        manifest = load_manifest(args.manifest)
        if args.command == 'score':
            rows = read_predictions(args.predictions)
            report = dict(manifest_sha256=manifest['_manifest_sha256'],
                          summary=summarize(manifest['cases'], rows, args.k, args.mode, pages=manifest['pages']), mode=args.mode)
            atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
            print(json.dumps(report['summary']))
            return
        if args.command == 'run' and not args.allow_remote:
            p.error('Live benchmark requires --allow-remote; use score for offline predictions')
        if args.command == 'index' and (args.analyze or args.retrieval != 'bm25') and not args.allow_remote:
            p.error('Remote indexing requires --allow-remote')
        from deepsearch_agent.adapters.llm_config import LLMSettings
        from deepsearch_agent.adapters.vision_config import VisionSettings
        from deepsearch_agent.adapters.llm_gateway import OpenAICompatibleClient
        from deepsearch_agent.adapters.vision import VisionReader
        from deepsearch_agent.workspace.multimodal import PageAnalysisCache
        vision = None
        if args.command == 'run' or args.analyze:
            vision = VisionSettings.load(args.vision_env, shared_llm=LLMSettings.load(args.llm_env))
            if vision.llm is None:
                raise ValueError('Real vision configuration required')
            # Short structured answers, independently of the app's generation length.
            vision = replace(vision, llm=replace(vision.llm, max_output_tokens=1024))
            client = OpenAICompatibleClient(vision.llm)
        if args.command == 'index':
            components = remote_components(args, text=args.retrieval == 'hybrid', images=args.retrieval != 'bm25') if args.retrieval != 'bm25' else {}
            kb = build_index(manifest, retrieval=args.retrieval, analyzer=VisionReader(client, vision) if args.analyze else None,
                             analysis_cache=PageAnalysisCache(args.output.parent / 'analysis-cache'), **components)
            save_index(kb, manifest, args.output, dict(retrieval=args.retrieval, analyze=args.analyze,
                                                     vision_identity=vision.identity if vision else None))
            print(json.dumps(dict(text_chunks=len(kb.chunks), image_pages=len(kb.image_index.chunks) if kb.image_index else 0)))
            return
        if not 1 <= args.k <= 20 or not args.k <= args.pool_size <= 20:
            raise ValueError('Require 1 <= k <= pool-size <= 20')
        if args.limit is not None:
            if not 1 <= args.limit <= len(manifest['cases']):
                raise ValueError('Run limit outside selected manifest')
            manifest = {**manifest, 'cases': manifest['cases'][:args.limit]}
        kb, index_hash = None, None
        if args.mode == 'rag':
            if args.snapshot is None:
                p.error('--mode rag requires --snapshot from benchmark index command')
            stored = json.loads(args.snapshot.read_text(encoding='utf-8'))
            components = remote_components(args, text=stored['embedding_id'] is not None, images=stored['image_index'] is not None)
            kb = load_index(args.snapshot, manifest, **components)
            index_hash = sha256(args.snapshot)
        predict = BenchmarkPredictor(manifest, client, vision, mode=args.mode, k=args.k, pool_size=args.pool_size,
                                     seed=args.seed, knowledge_base=kb, index_hash=index_hash)
        report = run_cases(manifest, predict, args.output, predict.configuration, workers=args.workers,
                           resume=args.resume, retry_errors=args.retry_errors)
        print(json.dumps(report['summary'], ensure_ascii=False))
    except (ValueError, KeyError, OSError) as error:
        p.error(str(error))
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    main()
