"""Isolated real-embedding retrieval test. Requires explicit --live."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--live', action='store_true', help='Allow paid embedding calls for the original story and questions')
args = parser.parse_args()
if not args.live:
    raise SystemExit('No calls made. Pass --live to authorize embedding calls.')
settings = EmbeddingSettings.load(ROOT / '.env.embedding')
if settings.backend != 'openai_compatible':
    raise SystemExit('Real embedding configuration is required.')
pdf = ROOT / 'output/pdf/雾港来信-检索测试.pdf'
questions = json.loads(Path(__file__).with_name('questions.json').read_text(encoding='utf-8'))

class TextPdfReader:
    def read(self, path):
        # Real extraction. No demo reader and no answer-key text in the index.
        return [page.extract_text() or '' for page in PdfReader(path).pages]

compact = lambda s: re.sub(r'\s+', '', s)
reader = TextPdfReader()
pages = reader.read(str(pdf))
for q in questions:
    q['gold_pages'] = sorted({i + 1 for i, page in enumerate(pages)
                              if any(compact(e) in compact(page) for e in q['evidence'])})
    for evidence in q['evidence']:
        assert any(compact(evidence) in compact(page) for page in pages), evidence

remote = RemoteEmbeddingClient(settings)
kb = KnowledgeBase(reader, chunk_size=600, overlap=120,
                   embedding_client=remote, embedding_id=f'remote-{settings.identity}')
out = ROOT / 'artifacts' / ('novel-retrieval-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
out.mkdir(parents=True)
chunks = kb.ingest_pdf(str(pdf))
kb.save(out / 'index.json')
print(f'Indexed real PDF: {len(pages)} pages, {len(chunks)} chunks.', flush=True)

# Batch the query embeddings, then exercise existing KnowledgeBase.search with
# those same real vectors. This avoids one HTTP request for every test question.
query_vectors = remote.embed([q['question'] for q in questions])
mapping = {q['question']: vector for q, vector in zip(questions, query_vectors, strict=True)}
class QueryCache:
    def embed(self, texts):
        return [mapping[text] for text in texts]
kb.embedding_client = QueryCache()
rows = []
for q in questions:
    hits = kb.search(q['question'], top_k=5)
    row = dict(q)
    row['retrieved'] = [{'page': c.page_number, 'chunk_index': c.chunk_index, 'text': c.text} for c in hits]
    if q['evidence']:
        for k in [1, 3, 5]:
            retrieved_text = compact(' '.join(c.text for c in hits[:k]))
            matched = [compact(e) in retrieved_text for e in q['evidence']]
            row[f'evidence_recall@{k}'] = sum(matched) / len(matched)
            row[f'all_evidence@{k}'] = all(matched)
    rows.append(row)
answerable = [r for r in rows if r['evidence']]
summary = {f'all_evidence@{k}': f"{sum(r[f'all_evidence@{k}'] for r in answerable)}/{len(answerable)}" for k in [1, 3, 5]}
summary.update({'model': settings.model, 'dimensions': settings.dimensions, 'pages': len(pages),
                'chunks': len(chunks), 'chunk_size': 600, 'overlap': 120,
                'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
                'limitations': 'Synthetic original story; fixed queries before run; exact evidence coverage, not answer accuracy. Unanswerable query tests absence of rejection threshold. No LLM generation.'})
(out / 'results.json').write_text(json.dumps({'summary': summary, 'questions': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
report = ['# 小说 PDF 真实检索测试', '', f"- 模型：{settings.model}；维度：{settings.dimensions}",
          f'- PDF：{len(pages)} 页；片段：{len(chunks)}；切分：600 字符，重叠 120 字符。',
          '- 测试前固定问题和原文证据；只对 PDF 正文入库；未调用生成模型。',
          '- 指标：Top-K 返回片段是否覆盖该题全部指定原文证据，不是生成答案正确率。', '',
          '| 问题 | 任一证据短语出现页 | Top-1 页 | Top-3 全证据覆盖 | Top-5 全证据覆盖 |',
          '|---|---|---|---|---|']
for row in rows:
    report.append(f"| {row['question']} | {row['gold_pages']} | {row['retrieved'][0]['page']} | {row.get('all_evidence@3', '不适用')} | {row.get('all_evidence@5', '不适用')} |")
report += ['', '## 汇总', '', *[f'- {k}: {v}' for k, v in summary.items()], '',
           '无答案问题仍会返回最近片段：当前检索器没有相关性阈值，不能把返回结果等同于存在答案。',
           '完整证据片段见同目录 results.json。测试索引独立保存，未修改页面使用的知识库。']
(out / 'report.md').write_text('\n'.join(report), encoding='utf-8')
print(json.dumps({'summary': summary, 'report': str(out / 'report.md')}, ensure_ascii=False), flush=True)
