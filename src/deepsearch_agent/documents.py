"""Local PDF originals, background indexing, and crash-safe metadata."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.llm_gateway import ModelServiceError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACTIVE = {'queued', 'parsing', 'embedding'}


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            name = Path(handle.name)
            handle.write(content)
        os.replace(name, path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


class DocumentError(ValueError):
    pass


class ExtractedPages:
    def __init__(self, pages):
        self.pages = pages

    def read(self, path):
        return self.pages


class DocumentLibrary:
    def __init__(self, root: Path, state):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.lock = Lock()
        self.render_lock = Lock()
        self.records = {}
        self.inflight = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-index')
        for path in root.glob('*.meta.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                ident = record['id']
                self.check_id(ident)
                if path.name != f'{ident}.meta.json':
                    continue
                if record['status'] in ACTIVE:
                    record.update(status='failed', error='上次处理被中断，请手动重试；不会自动产生模型费用')
                    self.persist(record)
                self.records[ident] = record
            except (OSError, ValueError, KeyError, TypeError):
                # Preserve corrupt metadata on disk, do not delete originals.
                continue

    def close(self):
        self.executor.shutdown(wait=True)

    @staticmethod
    def check_id(ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{64}', ident):
            raise KeyError('文献不存在')

    def path(self, ident, suffix):
        self.check_id(ident)
        return self.root / f'{ident}{suffix}'

    def persist(self, record):
        atomic_write(self.path(record['id'], '.meta.json'), json.dumps(record, ensure_ascii=False).encode('utf-8'))

    def update(self, ident, **changes):
        with self.lock:
            record = {**self.records[ident], **changes}
            self.persist(record)
            self.records[ident] = record

    def fail(self, ident, message):
        try:
            self.update(ident, status='failed', error=message)
        except OSError:
            # Even a full/read-only disk must not leave the UI polling forever.
            with self.lock:
                self.records[ident] = {**self.records[ident], 'status': 'failed',
                                       'error': '本地磁盘无法保存处理状态，请检查空间和权限后重试'}

    def view(self, ident):
        self.check_id(ident)
        with self.lock:
            record = dict(self.records[ident])
        with self.state.lock:
            count = sum(c.source_path == record['source_path'] for c in self.state.kb.chunks)
        record['chunk_count'] = count
        if count:
            record.update(status='ready', error=None)
        elif record['status'] == 'ready':
            record.update(status='not_indexed', error='当前索引未包含该文献，请重新入库')
        record['file_url'] = f'/api/documents/{ident}/file'
        return record

    def list(self):
        with self.lock:
            ids = list(self.records)
        return [self.view(ident) for ident in ids]

    def submit(self, content, filename):
        ident = hashlib.sha256(content).hexdigest()
        with self.lock:
            exists = ident in self.records
        if exists:
            return self.retry(ident)
        title = filename.replace('\\', '/').rsplit('/', 1)[-1]
        title = ''.join(c for c in title if c.isprintable())[:160] or 'document.pdf'
        with self.lock:
            # Recheck under lock for concurrent identical uploads.
            if ident not in self.records:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份文献，请稍后重试')
                atomic_write(self.path(ident, '.pdf'), content)
                record = dict(id=ident, title=title, source_path=f'pdf://{ident}/{quote(title, safe="")}',
                              page_count=0, chunk_count=0, status='queued', error=None, warnings=[])
                self.persist(record)
                self.records[ident] = record
        return self.retry(ident)

    def retry(self, ident):
        record = self.view(ident)
        if record['status'] == 'ready':
            return record
        with self.lock:
            if ident not in self.inflight:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份文献，请稍后重试')
                self.inflight.add(ident)
                record = {**self.records[ident], 'status': 'queued', 'error': None}
                self.persist(record)
                self.records[ident] = record
                self.executor.submit(self.process, ident)
        return self.view(ident)

    def worker(self, operation, ident, target, page=None):
        command = [sys.executable, '-B', '-m', 'deepsearch_agent.pdf_worker', operation,
                   str(self.path(ident, '.pdf')), str(target)]
        if page is not None:
            command.append(str(page))
        try:
            result = subprocess.run(command, capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired:
            raise DocumentError('PDF 处理超过 30 秒，请拆分或检查文件') from None
        if result.returncode:
            try:
                message = json.loads(result.stdout)['error']
            except (ValueError, KeyError, TypeError):
                message = 'PDF 处理失败，请检查文件或本地依赖'
            raise DocumentError(message)

    def process(self, ident):
        try:
            self.update(ident, status='parsing')
            pages_path = self.path(ident, '.pages.json')
            self.worker('extract', ident, pages_path)
            pages = json.loads(pages_path.read_text(encoding='utf-8'))
            blank_pages = [i + 1 for i, p in enumerate(pages) if not p.strip()]
            warnings = [f'第 {", ".join(map(str, blank_pages))} 页没有可提取文字，已跳过（可能需 OCR）'] if blank_pages else []
            self.update(ident, status='embedding', page_count=len(pages), warnings=warnings)
            with self.lock:
                source = self.records[ident]['source_path']
            with self.state.lock:
                if self.state.write_blocked:
                    raise DocumentError('知识库写入受保护，请先修复快照')
                original = self.state.kb
                client, identity = original.embedding_client, original.embedding_id
            temporary = KnowledgeBase(ExtractedPages(pages), chunking_strategy='section-rules-v1',
                                      embedding_client=client, embedding_id=identity)
            temporary.ingest_pdf(source)
            with self.state.lock:
                if self.state.write_blocked:
                    raise DocumentError('知识库写入受保护，请先修复快照')
                if any(c.source_path == source for c in self.state.kb.chunks):
                    return
                candidate = deepcopy(self.state.kb)
                candidate.merge_index(temporary)
                candidate.save(self.state.index_path)
                self.state.kb = candidate
                self.state.answer_service.knowledge_base = candidate
                self.state.dirty = False
                self.state.persistence_error = None
            self.update(ident, status='ready', error=None)
        except (DocumentError, ModelServiceError) as error:
            self.fail(ident, str(error))
        except Exception:
            self.fail(ident, '文献处理或保存失败，原知识库未被本次失败操作替换；请检查磁盘与配置后重试')
        finally:
            with self.lock:
                self.inflight.discard(ident)

    def page(self, ident, page):
        record = self.view(ident)
        if page < 1 or page > record['page_count']:
            raise KeyError('页码不存在或文献尚未解析')
        try:
            pages = json.loads(self.path(ident, '.pages.json').read_text(encoding='utf-8'))
            return dict(id=ident, title=record['title'], page_number=page,
                        page_count=len(pages), text=pages[page - 1])
        except (OSError, ValueError, IndexError):
            raise KeyError('页面文本暂不可用') from None

    def image(self, ident, page):
        self.page(ident, page)
        target = self.path(ident, f'.page-{page}.png')
        with self.render_lock:
            if not target.exists():
                self.worker('render', ident, target, page)
        return target
