"""Explicit, evidence-scoped wiki compilation; no background model calls."""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from deepsearch_agent.workspace.llm_wiki import WikiView, PAGE_ID
from deepsearch_agent.application.wiki import WikiCompiler
from deepsearch_agent.application.snapshots import snapshot_answer_service
from deepsearch_agent.application.scope import resolve_sources


router = APIRouter(prefix='/api/wiki', tags=['Wiki'])


class CompileWikiRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] = Field(min_length=1, max_length=20)
    top_k: int = Field(default=5, ge=1, le=10)
    page_id: str | None = Field(default=None, pattern='^' + PAGE_ID + '$')
    expected_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


def registry_for(kb):
    images = getattr(kb, 'image_index', None)
    return {chunk.chunk_id: chunk for chunk in [*kb.chunks, *(images.chunks if images else [])]}


@router.get('')
def list_wiki(request: Request, query: str = Query(default='', max_length=2000)):
    state = request.app.state
    with state.lock:
        registry = registry_for(state.kb)
        view = WikiView(state.wiki, {c.source_path for c in registry.values()}, registry)
        if query:
            return view.search(query, limit=20)
        return [{'id': p.id, 'title': p.title, 'revision': p.revision, 'updated_at': p.updated_at}
                for p in view.pages()]


@router.get('/lint')
def lint_wiki(request: Request):
    state = request.app.state
    with state.lock:
        return {'issues': state.wiki.lint(registry_for(state.kb)), 'semantic_check': 'not_performed'}


@router.get('/index', response_class=PlainTextResponse)
def wiki_index(request: Request):
    pages = list_wiki(request, query='')
    lines = ['# Wiki', '']
    for page in pages:
        label = page['title'].replace('[', '\\[').replace(']', '\\]').replace('\n', ' ')
        lines.append(f"- [{label}]({page['id']}.md)")
    return '\n'.join(lines) + '\n'


@router.post('/compile', status_code=201)
def compile_wiki(body: CompileWikiRequest, request: Request):
    state = request.app.state
    if not body.title.strip() or not body.question.strip():
        raise HTTPException(422, detail='wiki_empty_input')
    if (body.page_id is None) != (body.expected_revision is None):
        raise HTTPException(422, detail='wiki_update_requires_revision')
    with state.lock:
        sources = resolve_sources(state, body.document_ids)
        if body.page_id is not None and state.wiki.get(body.page_id).revision != body.expected_revision:
            raise HTTPException(409, detail='wiki_conflict')
        service = snapshot_answer_service(state)
    page = WikiCompiler(service).compile(body.title, body.question, sources,
                                        page_id=body.page_id, top_k=body.top_k)
    with state.lock:
        # Sources may have been removed while the model was generating.
        registry = registry_for(state.kb)
        view = WikiView(state.wiki, sources, registry)
        if not view.allows(page):
            raise HTTPException(409, detail='wiki_sources_changed')
        used = {s.source_path for s in page.sources}
        page.related = [p.id for p in view.pages() if p.id != page.id
                        and used.intersection(s.source_path for s in p.sources)][:20]
        if page.related:
            page.markdown += '\n\n## Related\n\n' + '\n'.join(
                f'- [{identifier}]({identifier}.md)' for identifier in page.related)
        return state.wiki.save(page, body.expected_revision)


@router.get('/{page_id}')
def read_wiki(page_id: str, request: Request):
    state = request.app.state
    with state.lock:
        registry = registry_for(state.kb)
        return WikiView(state.wiki, {c.source_path for c in registry.values()}, registry).read(page_id)


@router.delete('/{page_id}')
def delete_wiki(page_id: str, request: Request,
                expected_revision: str = Query(pattern=r'^[a-f0-9]{64}$')):
    with request.app.state.lock:
        request.app.state.wiki.delete(page_id, expected_revision)
    return {'deleted': True}
