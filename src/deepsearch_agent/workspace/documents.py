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
from urllib.parse import quote, urlsplit

from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.workspace.multimodal import DocumentProcessor
from deepsearch_agent.adapters.persistence.local_files import atomic_write
from deepsearch_agent.workspace.image_assets import normalize_image, image_title

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACTIVE = {'queued', 'parsing', 'analyzing', 'embedding'}


class DocumentError(ValueError):
    pass


class ExtractedPages:
    def __init__(self, pages):
        self.pages = pages

    def read(self, path):
        return self.pages


class DocumentLibrary:
    def __init__(self, root: Path, index, *, processor: DocumentProcessor | None = None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.index = index
        self.processor = processor
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
        with self.index.lock:
            count = sum(c.source_path == record['source_path'] for c in self.index.kb.chunks)
            image_index = self.index.kb.image_index
            record['image_index_available'] = image_index is not None and image_index.client is not None
            record['image_indexed_pages'] = sum(c.source_path == record['source_path'] for c in image_index.chunks) if image_index else 0
        record['chunk_count'] = count
        if (count or record['image_indexed_pages']) and record['status'] not in ACTIVE | {'failed'}:
            record.update(status='ready', error=None)
        elif record['status'] == 'ready':
            record.update(status='not_indexed', error='当前索引未包含该文献，请重新入库')
        record['file_url'] = f'/api/documents/{ident}/file'
        record['vision_available'] = self.processor is not None
        record.setdefault('analysis_mode', 'text')
        record.setdefault('media_type', 'application/pdf')
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

    def retry(self, ident, *, analyze=False):
        if analyze and self.processor is None:
            raise DocumentError('请先在 .env.vision 配置视觉模型并重启后端')
        record = self.view(ident)
        if record['status'] == 'ready' and not analyze:
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

    def submit_image(self, content: bytes, filename: str):
        with self.index.lock:
            index = self.index.kb.image_index
            if self.processor is None and (index is None or index.client is None):
                raise DocumentError('请先启用视觉识别或图像向量服务')
        try:
            data, width, height = normalize_image(content)
        except ValueError as error:
            raise DocumentError(str(error)) from None
        ident = hashlib.sha256(data).hexdigest()
        title = image_title(filename)
        with self.lock:
            if ident not in self.records:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份资料，请稍后重试')
                atomic_write(self.path(ident, '.png'), data)
                record = dict(id=ident, title=title, source_path=f'image://{ident}/{quote(title, safe="")}',
                              page_count=1, chunk_count=0, status='queued', error=None, warnings=[],
                              media_type='image/png', width=width, height=height)
                self.persist(record)
                self.records[ident] = record
        return self.retry(ident)

    def index_images(self, ident):
        record = self.view(ident)
        if not record['image_index_available']:
            raise DocumentError('请配置 .env.image_embedding 并重启后端')
        if record['status'] in ACTIVE:
            return record
        if not record['page_count'] or not (record['chunk_count'] or record['image_indexed_pages']):
            raise DocumentError('请先完成文献入库')
        if record['image_indexed_pages'] == record['page_count']:
            return record
        with self.lock:
            if ident not in self.inflight:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份文献，请稍后重试')
                self.inflight.add(ident)
                record = {**self.records[ident], 'status': 'queued', 'error': None}
                self.persist(record)
                self.records[ident] = record
                self.executor.submit(self.process, ident, images_only=True)
        return self.view(ident)

    def worker(self, operation, ident, target, page=None):
        command = [sys.executable, '-B', '-m', 'deepsearch_agent.adapters.pdf_worker', operation,
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

    def process(self, ident, *, images_only=False):
        try:
            if images_only:
                self.process_images(ident)
                return
            self.update(ident, status='parsing')
            pages_path = self.path(ident, '.pages.json')
            processor = self.processor
            with self.index.lock:
                current_images = self.index.kb.image_index
                images_enabled = current_images is not None and current_images.client is not None
            native_path = self.path(ident, '.native-pages.json') if processor else pages_path
            with self.lock:
                is_image = self.records[ident].get('media_type') == 'image/png'
            if is_image:
                atomic_write(native_path, b'[""]')
            else:
                self.worker('extract-visual' if processor or images_enabled else 'extract', ident, native_path)
            pages = json.loads(native_path.read_text(encoding='utf-8'))
            warnings = []
            prepared = None
            with self.lock:
                source = self.records[ident]['source_path']
            if processor:
                self.update(ident, status='analyzing', page_count=len(pages))
                try:
                    prepared = processor.prepare(pages, source, ident, lambda n: self.render_page(ident, n, vision=True))
                except ValueError as error:
                    raise DocumentError(str(error)) from None
                pages, warnings = prepared.pages, prepared.warnings
                atomic_write(pages_path, json.dumps(pages, ensure_ascii=False).encode('utf-8'))
            blank_pages = [i + 1 for i, p in enumerate(pages) if not p.strip()]
            if blank_pages:
                warnings.append(f'第 {", ".join(map(str, blank_pages))} 页没有可提取文字' + ('；可查看原页与视觉证据' if processor or images_enabled else '，已跳过（可能需 OCR）'))
            self.update(ident, status='embedding', page_count=len(pages), warnings=warnings)
            with self.index.lock:
                if self.index.write_blocked:
                    raise DocumentError('知识库写入受保护，请先修复快照')
                original = self.index.kb
                client, identity = original.embedding_client, original.embedding_id
            temporary = KnowledgeBase(ExtractedPages(pages), chunking_strategy='section-rules-v1',
                                      embedding_client=client, embedding_id=identity)
            if prepared:
                temporary.ingest_chunks(prepared.chunks)
            else:
                temporary.ingest_pdf(source)
            if original.image_index is not None and original.image_index.client is not None:
                temporary.image_index = original.image_index.prepare(source, [
                    self.render_page(ident, page, vision=True) for page in range(1, len(pages) + 1)])
            with self.index.lock:
                if self.index.write_blocked:
                    raise DocumentError('知识库写入受保护，请先修复快照')
                candidate = deepcopy(self.index.kb)
                image_sources = {c.source_path for c in candidate.image_index.chunks} if candidate.image_index else set()
                preserved_images = candidate.image_index.restrict_sources({source}) if candidate.image_index else None
                candidate = candidate.restrict_sources(({c.source_path for c in candidate.chunks} | image_sources) - {source})
                candidate.merge_index(temporary)
                if candidate.image_index is not None:
                    candidate.image_index.merge(temporary.image_index if temporary.image_index is not None else preserved_images)
                self.index.publish(candidate)
            self.update(ident, status='ready', error=None, analysis_mode='vision' if processor else 'text')
        except (DocumentError, ModelServiceError) as error:
            self.fail(ident, str(error))
        except Exception:
            self.fail(ident, '文献处理或保存失败，原知识库未被本次失败操作替换；请检查磁盘与配置后重试')
        finally:
            with self.lock:
                self.inflight.discard(ident)

    def process_images(self, ident):
        self.update(ident, status='embedding')
        with self.lock:
            record = dict(self.records[ident])
        with self.index.lock:
            if self.index.write_blocked:
                raise DocumentError('知识库写入受保护，请先修复快照')
            image_index = self.index.kb.image_index
        prepared = image_index.prepare(record['source_path'], [
            self.render_page(ident, page, vision=True) for page in range(1, record['page_count'] + 1)])
        with self.index.lock:
            if self.index.write_blocked:
                raise DocumentError('知识库写入受保护，请先修复快照')
            candidate = deepcopy(self.index.kb)
            candidate.image_index = candidate.image_index.restrict_sources(
                {c.source_path for c in candidate.image_index.chunks} - {record['source_path']})
            candidate.image_index.merge(prepared)
            self.index.publish(candidate)
        self.update(ident, status='ready', error=None)

    def page(self, ident, page):
        record = self.view(ident)
        if page < 1 or page > record['page_count']:
            raise KeyError('页码不存在或文献尚未解析')
        try:
            pages = json.loads(self.path(ident, '.pages.json').read_text(encoding='utf-8'))
            return dict(id=ident, title=record['title'], page_number=page,
                        page_count=len(pages), text=pages[page - 1], media_type=record['media_type'])
        except (OSError, ValueError, IndexError):
            raise KeyError('页面文本暂不可用') from None

    def render_page(self, ident, page, *, vision=False):
        with self.lock:
            is_image = self.records[ident].get('media_type') == 'image/png'
        if is_image:
            if page != 1:
                raise KeyError('图片页码不存在')
            return self.path(ident, '.png')
        target = self.path(ident, f'.page-{page}{"-vision" if vision else ""}.png')
        with self.render_lock:
            if not target.exists():
                with tempfile.NamedTemporaryFile(dir=self.root, suffix='.png', delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    self.worker('render-vision' if vision else 'render', ident, temporary, page)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        return target

    def image(self, ident, page, *, vision=False):
        self.page(ident, page)
        return self.render_page(ident, page, vision=vision)

    def resolve_visual_image(self, source: str, page: int) -> Path:
        url = urlsplit(source)
        ident = url.netloc if url.scheme in {'pdf', 'image'} else ''
        self.check_id(ident)
        # Answer requests may hold the index lock. Originals only need metadata.
        with self.lock:
            record = dict(self.records[ident])
        if record['source_path'] != source or type(page) is not int or not 1 <= page <= record['page_count']:
            raise KeyError('来源不匹配')
        return self.render_page(ident, page, vision=True)
