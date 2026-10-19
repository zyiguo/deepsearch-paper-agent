from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from fastapi.responses import StreamingResponse
import json

from deepsearch_agent.web.source_schemas import CitationResponse
from deepsearch_agent.workspace.memory import MemoryState
from deepsearch_agent.application import session_runs as run_service
from deepsearch_agent.application.commands import TurnCommand


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
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    skill_ids: list[str] | None = Field(default=None, max_length=3)

    @field_validator('attachment_ids')
    @classmethod
    def unique_attachments(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('图片附件不能重复')
        return value

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
    skills_used: list[dict[str, str]] = Field(default_factory=list)


class TurnResponse(BaseModel):
    id: int
    question: str
    answer: TurnAnswerResponse
    created_at: str
    attachments: list[dict] = Field(default_factory=list)


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class SessionDetailResponse(SessionResponse):
    turns: list[TurnResponse]
    memory: MemoryState | None = None


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(body: CreateSessionRequest, request: Request):
    return run_service.create_session(body, request.app.state)


@router.get("", response_model=list[SessionResponse])
def list_sessions(request: Request):
    return run_service.list_sessions(request.app.state)


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, request: Request):
    return run_service.get_session(session_id, request.app.state)


class MemoryEdit(BaseModel):
    research_goal: str = Field(default='', max_length=1000)
    language: str = Field(default='zh', max_length=40)
    answer_style: str = Field(default='简洁', max_length=100)


class MemoryScope(BaseModel):
    document_ids: list[str] = Field(max_length=1000)


@router.put('/{session_id}/memory/scope', response_model=MemoryState)
def set_memory_scope(session_id: str, body: MemoryScope, request: Request):
    return run_service.set_memory_scope(session_id, body, request.app.state)


@router.get('/{session_id}/memory', response_model=MemoryState)
def get_memory(session_id: str, request: Request):
    return run_service.get_memory(session_id, request.app.state)


@router.patch('/{session_id}/memory', response_model=MemoryState)
def edit_memory(session_id: str, body: MemoryEdit, request: Request):
    return run_service.edit_memory(session_id, body.model_dump(), request.app.state)


@router.delete('/{session_id}/memory', response_model=MemoryState)
def clear_memory(session_id: str, request: Request):
    return run_service.clear_memory(session_id, request.app.state)


@router.get('/{session_id}/working-memory')
def get_working_memory(session_id: str, request: Request):
    return run_service.get_working_memory(session_id, request.app.state)


@router.delete('/{session_id}/working-memory')
def clear_working_memory(session_id: str, request: Request):
    return run_service.clear_working_memory(session_id, request.app.state)


@router.delete('/{session_id}')
def delete_session(session_id: str, request: Request):
    return run_service.delete_session(session_id, request.app.state)


@router.post(
    "/{session_id}/turns",
    response_model=TurnResponse,
    status_code=201,
)
def send_turn(session_id: str, body: SendTurnRequest, request: Request):
    return run_service.send_turn(session_id, TurnCommand(**body.model_dump()), request.app.state,
                                 request.headers.get('Idempotency-Key'))


@router.post('/{session_id}/turns/stream')
def stream_turn(session_id: str, body: SendTurnRequest, request: Request):
    return sse_response(run_service.stream_turn(session_id, TurnCommand(**body.model_dump()), request.app.state,
                                              request.headers.get('Idempotency-Key')))


@router.get('/{session_id}/runs')
def list_runs(session_id: str, request: Request):
    return run_service.list_runs(session_id, request.app.state)


@router.post('/{session_id}/runs/{run_id}/cancel')
def cancel_run(session_id: str, run_id: str, request: Request):
    return run_service.cancel_run(session_id, run_id, request.app.state)


@router.post('/{session_id}/runs/{run_id}/resume/stream')
def resume_run(session_id: str, run_id: str, request: Request):
    return sse_response(run_service.resume_run(session_id, run_id, request.app.state))


def sse_response(events):
    def encode():
        for event, data in events:
            if event is None:
                yield ': heartbeat\n\n'
                continue
            if event == 'done':
                data = TurnResponse.model_validate(data).model_dump()
            yield f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
    return StreamingResponse(encode(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
