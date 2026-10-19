"""Compile reusable Wiki pages from independently verified indexed evidence."""
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from deepsearch_agent.workspace.llm_wiki import WikiPage, WikiSource, WikiError

class WikiCompiler:
    def __init__(self, service):
        self.service = service

    def compile(self, title, question, sources, *, page_id=None, top_k=5):
        service = self.service
        outcomes = []
        def observe(kind, payload):
            if kind == 'rag.outcome':
                outcomes.append(payload)
        answer = service.answer(question, top_k=top_k, allowed_sources=set(sources), event_sink=observe)
        kb = service.knowledge_base
        images = getattr(kb, 'image_index', None)
        indexed = {c.chunk_id for c in [*kb.chunks, *(images.chunks if images else [])]}
        if any(source.chunk_id not in indexed for source in answer.sources):
            raise WikiError('wiki_unverified')
        if not outcomes or outcomes[-1].get('status') != 'verified' or not answer.sources:
            raise WikiError('wiki_unverified')
        if any(source.source_path not in sources for source in answer.sources):
            raise WikiError('wiki_source_not_allowed')
        refs = [WikiSource(**{key: asdict(source)[key] for key in WikiSource.model_fields})
                for source in answer.sources]
        return WikiPage(id=page_id or 'wiki_' + uuid4().hex, title=title.strip(),
            markdown=answer.text, sources=refs, updated_at=datetime.now(timezone.utc).isoformat(),
            question=question, compiler_model=getattr(getattr(service.client, 'settings', None), 'model', 'unknown'))

