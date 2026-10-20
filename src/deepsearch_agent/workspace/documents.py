"""Local PDF originals, background indexing, and crash-safe metadata."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlsplit

from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.identity import fingerprint
from deepsearch_agent.core.chunking_config import SectionConfig
from deepsearch_agent.knowledge.image_index import ImageVectorIndex
from deepsearch_agent.workspace.ingestion import IngestionJournal, IngestionBlocked
from deepsearch_agent.adapters.governed_calls import GovernedExecutor
from deepsearch_agent.core.governance import current_governance, GovernanceDenied
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
        self.fault = lambda stage, step: None
        self.lock = Lock()
        self.render_lock = Lock()
        self.records = {}
        self.inflight = set()
        self.executor = GovernedExecutor(ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-index'))
        self.phase_started = {}
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
            service = current_governance.get()
            if service is not None and 'status' in changes:
                prior = self.phase_started.pop(ident,None)
                if prior:
                    service.duration('ingestion_phase',time.monotonic()-prior[1],phase=prior[0])
                if changes['status'] in ACTIVE:
                    self.phase_started[ident] = (changes['status'],time.monotonic())

    def enqueue(self, ident, **kwargs):
        """Caller holds the library lock; failed admission must not strand queued UI."""
        try:
            self.executor.submit(self.process,ident,**kwargs)
        except BaseException:
            self.inflight.discard(ident)
            record = {**self.records[ident], 'status':'failed','error':'任务未接纳，请检查资源容量后重试'}
            self.persist(record)
            self.records[ident] = record
            raise

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
        try:
            record['retry_requires_confirmation'] = any(self.journal(ident, images_only=mode).pending_unknown()
                                                        for mode in (False, True))
        except (OSError, ValueError, KeyError, TypeError):
            record['retry_requires_confirmation'] = True
            record['error'] = '入库记录无法校验，请先检查备份；不会自动重新计费'
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

    def retry(self, ident, *, analyze=False, confirm_unknown=False):
        if analyze and self.processor is None:
            raise DocumentError('请先在 .env.vision 配置视觉模型并重启后端')
        record = self.view(ident)
        if record['status'] == 'ready' and not analyze:
            return record
        if record['status'] not in ACTIVE and not analyze:
            checked = self.consistency(ident, repair=True)
            if checked['repaired']:
                return self.view(ident)
        images_only = bool(record.get('ingestion_images_only', False)) and not analyze
        if self.journal(ident, images_only=images_only).pending_unknown() and not confirm_unknown:
            raise DocumentError('付费步骤结果未知；核对账单后请明确确认重试，可能重复计费')
        with self.lock:
            if ident not in self.inflight:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份文献，请稍后重试')
                self.inflight.add(ident)
                record = {**self.records[ident], 'status': 'queued', 'error': None}
                self.persist(record)
                self.records[ident] = record
                self.enqueue(ident, images_only=images_only, confirm_unknown=confirm_unknown)
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

    def index_images(self, ident, *, confirm_unknown=False):
        record = self.view(ident)
        if not record['image_index_available']:
            raise DocumentError('请配置 .env.image_embedding 并重启后端')
        if record['status'] in ACTIVE:
            return record
        if not record['page_count'] or not (record['chunk_count'] or record['image_indexed_pages']):
            raise DocumentError('请先完成文献入库')
        if record['image_indexed_pages'] == record['page_count']:
            return record
        if self.journal(ident, images_only=True).pending_unknown() and not confirm_unknown:
            raise DocumentError('图像向量步骤结果未知；核对账单后请明确确认重试，可能重复计费')
        with self.lock:
            if ident not in self.inflight:
                if len(self.inflight) >= 4:
                    raise DocumentError('最多同时排队 4 份文献，请稍后重试')
                self.inflight.add(ident)
                record = {**self.records[ident], 'status': 'queued', 'error': None}
                self.persist(record)
                self.records[ident] = record
                self.enqueue(ident, images_only=True, confirm_unknown=confirm_unknown)
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

    def journal(self, ident, *, images_only=False, confirm_unknown=False):
        with self.lock:
            source = self.records[ident]['source_path']
        with self.index.lock:
            kb = self.index.kb
            images = kb.image_index
            image_identity = images.identity if images and images.client is not None else None
            embedding = kb.embedding_id
        processor = self.processor
        analyzer = getattr(processor, 'analyzer', None)
        context = dict(protocol=1, document=ident, source=source, images_only=images_only,
                       embedding=embedding, image_embedding=image_identity,
                       processor=(getattr(analyzer, 'identity', None) or
                                  type(processor).__module__ + '.' + type(processor).__qualname__) if processor else None,
                       allow_image_only=getattr(processor, 'allow_image_only', False),
                       chunking=asdict(SectionConfig()))
        return IngestionJournal(self.root / '.ingestion', context,
                                confirm_unknown=confirm_unknown, fault=self.fault)

    @staticmethod
    def source_snapshot(kb, source, *, images_only=False):
        images = kb.image_index.restrict_sources({source}) if kb.image_index else None
        # Saving/loading normalizes vectors; ignore only floating point roundoff.
        image_payload = images.snapshot() if images and images.chunks else None
        if image_payload:
            for entry in image_payload['entries']:
                entry['vector'] = [round(v, 12) for v in entry['vector']]
        return {'text': [] if images_only else [
                    {'chunk': asdict(c), 'vector': [round(v, 12) for v in kb._vectors[i]] if kb._vectors else None}
                    for i, c in enumerate(kb.chunks) if c.source_path == source],
                'images': image_payload}

    def consistency(self, ident, *, repair=False):
        self.check_id(ident)
        with self.lock:
            record = dict(self.records[ident])
            active = ident in self.inflight
        result = {'id': ident, 'status': 'legacy_or_unpublished', 'repaired': False,
                  'original_exists': self.path(ident, '.png' if record.get('media_type') == 'image/png' else '.pdf').is_file()}
        if active:
            return {**result, 'status': 'active'}
        original_path = self.path(ident, '.png' if record.get('media_type') == 'image/png' else '.pdf')
        result['original_matches'] = result['original_exists'] and hashlib.sha256(original_path.read_bytes()).hexdigest() == ident
        for mode in (record.get('ingestion_images_only', False),):
            job = self.journal(ident, images_only=mode)
            if record.get('ingestion_identity') and record['ingestion_identity'] != job.identity:
                return {**result, 'status': 'context_changed'}
            publication = job.read('publication')
            if publication is None:
                continue
            with self.index.lock:
                actual = fingerprint(self.source_snapshot(self.index.kb, record['source_path'], images_only=mode))
                matches = actual == publication['expected']
                pages_exist = self.path(ident, '.pages.json').is_file()
                pages_match = pages_exist and fingerprint(json.loads(self.path(ident, '.pages.json').read_text(encoding='utf-8'))) == publication['pages_digest']
                complete = matches and result['original_matches'] and pages_match
                result.update(status='consistent' if complete else 'index_or_artifact_mismatch',
                              index_matches=matches, pages_exist=pages_exist, pages_match=pages_match)
                if complete and repair and record['status'] != 'ready':
                    self.update(ident, **publication['metadata'])
                    job.published()
                    result['repaired'] = True
            if matches:
                break
        return result

    def process(self, ident, *, images_only=False, confirm_unknown=False):
        try:
            job = self.journal(ident, images_only=images_only, confirm_unknown=confirm_unknown)
            self.update(ident, ingestion_identity=job.identity, ingestion_images_only=images_only)
            if images_only:
                self.process_images(ident, job=job)
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
            def extract():
                if is_image:
                    return ['']
                self.worker('extract-visual' if processor or images_enabled else 'extract', ident, native_path)
                return json.loads(native_path.read_text(encoding='utf-8'))
            native_pages = job.run('parse', extract)
            with self.lock:
                source = self.records[ident]['source_path']
            def prepare():
                warnings, pages = [], native_pages
                if processor:
                    self.update(ident, status='analyzing', page_count=len(pages))
                    prepared = processor.prepare(pages, source, ident, lambda n: self.render_page(ident, n, vision=True))
                    pages, warnings, chunks = prepared.pages, prepared.warnings, prepared.chunks
                else:
                    from deepsearch_agent.knowledge.section_chunking import section_chunks
                    chunks = section_chunks(pages, source, SectionConfig())
                blank_pages = [i + 1 for i, p in enumerate(pages) if not p.strip()]
                if blank_pages:
                    warnings.append(f'第 {", ".join(map(str, blank_pages))} 页没有可提取文字' + ('；可查看原页与视觉证据' if processor or images_enabled else '，已跳过（可能需 OCR）'))
                return {'pages': pages, 'warnings': warnings, 'chunks': [asdict(c) for c in chunks]}
            prepared = job.run('analysis', prepare, paid=processor is not None)
            pages, warnings = prepared['pages'], prepared['warnings']
            atomic_write(pages_path, json.dumps(pages, ensure_ascii=False).encode('utf-8'))
            self.update(ident, status='embedding', page_count=len(pages), warnings=warnings)
            with self.index.lock:
                if self.index.write_blocked:
                    raise DocumentError('知识库写入受保护，请先修复快照')
                original = self.index.kb
                client, identity = original.embedding_client, original.embedding_id
            temporary = KnowledgeBase(ExtractedPages(pages), chunking_strategy='section-rules-v1',
                                      embedding_client=client, embedding_id=identity)
            def embed():
                temporary.ingest_chunks([TextChunk(**c) for c in prepared['chunks']])
                return {'chunks': [asdict(c) for c in temporary.chunks], 'vectors': temporary._vectors}
            embedded = job.run('text_vectors', embed, paid=client is not None and bool(prepared['chunks']))
            temporary.chunks = [TextChunk(**c) for c in embedded['chunks']]
            for chunk in temporary.chunks:
                chunk.validate_structure()
            temporary._vectors = embedded['vectors']
            if original.image_index is not None and original.image_index.client is not None:
                images = job.run('image_vectors', lambda: original.image_index.prepare(source, [
                    self.render_page(ident, page, vision=True) for page in range(1, len(pages) + 1)]).snapshot(), paid=True)
                temporary.image_index = ImageVectorIndex.restore(images, original.image_index.client, original.image_index.identity)
            metadata = dict(status='ready', error=None, page_count=len(pages), warnings=warnings,
                            analysis_mode='vision' if processor else 'text')
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
                job.publication(fingerprint(self.source_snapshot(candidate, source)), metadata, pages_digest=fingerprint(pages))
                self.index.publish(candidate)
                self.fault('after_publish', 'publication')
                job.published()
            self.update(ident, **metadata)
        except GovernanceDenied as error:
            self.fail(ident, '本次付费步骤未执行：'+error.code)
        except (DocumentError, ModelServiceError, IngestionBlocked) as error:
            self.fail(ident, str(error))
        except Exception:
            self.fail(ident, '文献处理或状态保存失败；请先检查入库一致性，再恢复已保存步骤')
        finally:
            with self.lock:
                self.inflight.discard(ident)

    def process_images(self, ident, *, job=None):
        job = job or self.journal(ident, images_only=True)
        self.update(ident, status='embedding')
        with self.lock:
            record = dict(self.records[ident])
        with self.index.lock:
            if self.index.write_blocked:
                raise DocumentError('知识库写入受保护，请先修复快照')
            image_index = self.index.kb.image_index
        images = job.run('image_vectors', lambda: image_index.prepare(record['source_path'], [
            self.render_page(ident, page, vision=True) for page in range(1, record['page_count'] + 1)]).snapshot(), paid=True)
        prepared = ImageVectorIndex.restore(images, image_index.client, image_index.identity)
        metadata = dict(status='ready', error=None)
        with self.index.lock:
            if self.index.write_blocked:
                raise DocumentError('知识库写入受保护，请先修复快照')
            candidate = deepcopy(self.index.kb)
            candidate.image_index = candidate.image_index.restrict_sources(
                {c.source_path for c in candidate.image_index.chunks} - {record['source_path']})
            candidate.image_index.merge(prepared)
            job.publication(fingerprint(self.source_snapshot(candidate, record['source_path'], images_only=True)), metadata,
                            pages_digest=fingerprint(json.loads(self.path(ident, '.pages.json').read_text(encoding='utf-8'))))
            self.index.publish(candidate)
            self.fault('after_publish', 'publication')
            job.published()
        self.update(ident, **metadata)

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
