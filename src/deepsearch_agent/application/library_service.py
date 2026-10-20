"""Question answering and corpus snapshot use cases shared by entry adapters."""
from copy import deepcopy
from deepsearch_agent.adapters.demo import DEMO_QUESTIONS, DEMO_SOURCE
from deepsearch_agent.application.snapshots import snapshot_answer_service
from deepsearch_agent.application.skills import validate_skills
from deepsearch_agent.core.errors import ApplicationError
from contextlib import nullcontext

def health(state):
    return dict(status='ok', mode=state.mode, retrieval_mode=state.kb.retrieval_mode if not state.demo_retrieval else None, vision_enabled=True if state.vision_enabled else None, image_retrieval_enabled=True if state.image_retrieval_enabled else None, agentic_rag_enabled=True if state.agentic_rag_enabled else None, mcp_server_enabled=True if state.mcp_server_enabled else None)

def ingest_demo(state):
    with state.lock:
        if state.write_blocked:
            raise ApplicationError(status_code=409, detail='快照恢复异常，已禁止写入。请修复快照后重新加载。')
        if any((chunk.source_path == DEMO_SOURCE for chunk in state.kb.chunks)):
            return dict(added_chunks=0, message='演示文献已导入')
        candidate = deepcopy(state.kb)
        chunks = candidate.ingest_pdf(DEMO_SOURCE)
        try:
            state.index.publish(candidate)
        except (OSError, ValueError, TypeError) as error:
            state.persistence_error = f'导入保存失败：{error}'
            raise ApplicationError(status_code=500, detail='保存失败，本次导入未生效，原知识库保持不变。') from error
        return dict(added_chunks=len(chunks), message='演示文献已导入并保存')

def list_documents(state):
    uploaded = state.documents.list()
    uploaded_sources = {record['source_path'] for record in uploaded}
    with state.lock:
        counts: dict[str, int] = {}
        for chunk in state.kb.chunks:
            counts[chunk.source_path] = counts.get(chunk.source_path, 0) + 1
        return uploaded + [dict(id=source, title=source.replace('\\', '/').rsplit('/', 1)[-1], source_path=source, chunk_count=count) for source, count in counts.items() if source not in uploaded_sources]

def ask(body, state):
    if state.demo_retrieval and body.question not in DEMO_QUESTIONS:
        raise ApplicationError(status_code=400, detail='演示模式仅支持：' + '、'.join(DEMO_QUESTIONS))
    with state.lock:
        from deepsearch_agent.application.scope import resolve_sources
        validate_skills(state, body.skill_ids)
        sources = resolve_sources(state, body.document_ids)
        service = snapshot_answer_service(state)
    governor = getattr(state,'governance',None)
    with governor.resource('research') if governor else nullcontext():
        result = service.answer(body.question, top_k=body.top_k, **{'allowed_sources': sources} if sources is not None else {}, **{'skill_ids': body.skill_ids} if body.skill_ids is not None else {})
    return result

def persistence_status(state):
    with state.lock:
        return dict(snapshot_exists=state.index_path.exists(), chunk_count=len(state.kb.chunks), dirty=state.dirty, write_blocked=state.write_blocked, error=state.persistence_error)

def save_knowledge_base(state):
    with state.lock:
        if state.write_blocked:
            raise ApplicationError(status_code=409, detail='快照恢复异常，禁止覆盖。请修复后重新加载。')
        try:
            state.index.save()
        except (OSError, ValueError, TypeError) as error:
            state.dirty = True
            state.persistence_error = f'保存失败：{error}'
            raise ApplicationError(status_code=500, detail='知识库保存失败，请检查状态信息。') from error
        state.dirty = False
        state.persistence_error = None
        return dict(message='知识库已保存')

def load_knowledge_base(state):
    with state.lock:
        if not state.index_path.exists():
            state.persistence_error = '尚无可加载的知识库快照'
            raise ApplicationError(status_code=404, detail=state.persistence_error)
        try:
            state.index.restore()
        except (OSError, ValueError, KeyError, TypeError) as error:
            state.persistence_error = f'加载失败：{error}'
            state.write_blocked = True
            raise ApplicationError(status_code=409, detail='快照无法加载，当前内存知识库保持不变，已禁止覆盖写入。') from error
        state.dirty = False
        state.write_blocked = False
        state.persistence_error = None
        return dict(message='知识库已重新加载')

