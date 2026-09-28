from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from fastapi.responses import StreamingResponse
import json
from queue import Queue, Empty
from threading import Thread

from deepsearch_agent.demo import DEMO_QUESTIONS
from deepsearch_agent.source_schemas import CitationResponse
from deepsearch_agent.retrieval_scope import resolve_sources
from deepsearch_agent.memory import MemoryManager, MemoryState
import logging


router = APIRouter(prefix="/api/sessions", tags=["会话"])


class CreateSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("会话名称不能为空")
        return value


class SendTurnRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=10)
    document_ids: list[str] | None = Field(default=None, max_length=1000)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class TurnAnswerResponse(BaseModel):
    text: str
    sources: list[CitationResponse]


class TurnResponse(BaseModel):
    id: int
    question: str
    answer: TurnAnswerResponse
    created_at: str


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class SessionDetailResponse(SessionResponse):
    turns: list[TurnResponse]
    memory: MemoryState | None = None


def require_session(store, session_id: str) -> dict:
    try:
        return store.get(session_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="会话不存在",
        ) from error


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(body: CreateSessionRequest, request: Request):
    state = request.app.state

    with state.lock:
        return state.conversations.create(body.title)


@router.get("", response_model=list[SessionResponse])
def list_sessions(request: Request):
    state = request.app.state

    with state.lock:
        return state.conversations.list_sessions()


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, request: Request):
    state = request.app.state

    with state.lock:
        session = require_session(state.conversations, session_id)
        return {**session, 'memory': MemoryManager(state.conversations).get(session_id)}


class MemoryEdit(BaseModel):
    research_goal: str = Field(default='', max_length=1000)
    language: str = Field(default='zh', max_length=40)
    answer_style: str = Field(default='简洁', max_length=100)


class MemoryScope(BaseModel):
    document_ids: list[str] = Field(max_length=1000)


@router.put('/{session_id}/memory/scope', response_model=MemoryState)
def set_memory_scope(session_id: str, body: MemoryScope, request: Request):
    with request.app.state.lock:
        require_session(request.app.state.conversations, session_id)
        if body.document_ids:
            resolve_sources(request.app.state, body.document_ids)
        return MemoryManager(request.app.state.conversations).set_scope(session_id, body.document_ids)


@router.get('/{session_id}/memory', response_model=MemoryState)
def get_memory(session_id: str, request: Request):
    with request.app.state.lock:
        require_session(request.app.state.conversations, session_id)
        return MemoryManager(request.app.state.conversations).get(session_id)


@router.patch('/{session_id}/memory', response_model=MemoryState)
def edit_memory(session_id: str, body: MemoryEdit, request: Request):
    with request.app.state.lock:
        require_session(request.app.state.conversations, session_id)
        return MemoryManager(request.app.state.conversations).edit(session_id, **body.model_dump())


@router.delete('/{session_id}/memory', response_model=MemoryState)
def clear_memory(session_id: str, request: Request):
    with request.app.state.lock:
        require_session(request.app.state.conversations, session_id)
        return MemoryManager(request.app.state.conversations).clear(session_id)


@router.delete('/{session_id}')
def delete_session(session_id: str, request: Request):
    state = request.app.state
    with state.lock:
        try:
            state.conversations.delete(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail='会话不存在') from None
    return {'message': '会话及全部问答记录已删除'}


@router.post(
    "/{session_id}/turns",
    response_model=TurnResponse,
    status_code=201,
)
def send_turn(
    session_id: str,
    body: SendTurnRequest,
    request: Request,
):
    return generate_turn(session_id, body, request)


def generate_turn(
    session_id: str,
    body: SendTurnRequest,
    request: Request,
    on_progress=None,
):
    state = request.app.state

    with state.lock:
        session = require_session(state.conversations, session_id)
        manager = MemoryManager(state.conversations)
        saved_memory = manager.get(session_id)
        ids = body.document_ids if body.document_ids is not None else saved_memory.document_ids
        sources = resolve_sources(state, ids)

        if state.demo_retrieval and body.question not in DEMO_QUESTIONS:
            raise HTTPException(
                status_code=400,
                detail="演示模式仅支持：" + "、".join(DEMO_QUESTIONS),
            )

        manager.set_scope(session_id, sorted({s.split('/')[2] for s in sources}) if sources is not None else None)
        memory_context, history = manager.context(session_id)

        progress = {'on_progress': on_progress} if on_progress is not None else {}
        if getattr(state.answer_service, 'run_recorder', None) is not None:
            progress['trace_context'] = {'conversation_id': session_id}
        if sources is not None:
            progress['allowed_sources'] = sources
        answer = state.answer_service.answer(
            question=body.question,
            top_k=body.top_k,
            history=history,
            memory_context=memory_context,
            **progress,
        )

        turn = state.conversations.append_turn(
            session_id=session_id,
            question=body.question,
            answer=answer,
        )
    # Memory failure must never undo a successfully persisted answer.
    try:
        client = getattr(state.answer_service, 'client', None)
        if client is not None and not state.demo_retrieval:
            manager.compress(session_id, client, on_progress)
    except Exception:
        logging.getLogger(__name__).warning('memory_update_failed')
    return turn


@router.post('/{session_id}/turns/stream')
def stream_turn(session_id: str, body: SendTurnRequest, request: Request):
    # Fail before sending HTTP 200 for a nonexistent session.
    with request.app.state.lock:
        require_session(request.app.state.conversations, session_id)
        saved = MemoryManager(request.app.state.conversations).get(session_id)
        resolve_sources(request.app.state, body.document_ids if body.document_ids is not None else saved.document_ids)
    events = Queue()

    def emit(stage):
        events.put(('status', {'stage': stage}))

    def work():
        try:
            turn = generate_turn(session_id, body, request, on_progress=emit)
            # Never expose a draft. Persistence must succeed before publishing.
            for index, paragraph in enumerate(turn['answer']['text'].split('\n\n')):
                events.put(('answer', {'text': ('\n\n' if index else '') + paragraph,
                                       'sources': turn['answer']['sources']}))
            events.put(('done', turn))
        except Exception:
            # No exception string: providers may include private request details.
            events.put(('error', {'message': '回答未完成，请刷新会话核对结果；不要自动重复发送。'}))

    def stream():
        yield 'event: status\ndata: {"stage":"queued"}\n\n'
        Thread(target=work, daemon=True).start()
        while True:
            try:
                event, data = events.get(timeout=10)
            except Empty:
                yield ': heartbeat\n\n'
                continue
            yield f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
            if event in {'done', 'error'}:
                break

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
