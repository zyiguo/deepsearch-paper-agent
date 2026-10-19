from deepsearch_agent.application import library_service
from deepsearch_agent.application.commands import TurnCommand
from fastapi import Request
from dataclasses import asdict


from fastapi import APIRouter
from deepsearch_agent.web.schemas import HealthResponse, PersistenceStatus, PersistenceResponse, IngestResponse, DocumentResponse, AskRequest, AskResponse

router=APIRouter()

@router.get("/api/health", response_model=HealthResponse, response_model_exclude_none=True)
def health(request: Request):
    return library_service.health(request.app.state)


@router.post("/api/demo/ingest", response_model=IngestResponse)
def ingest_demo(request: Request):
    return library_service.ingest_demo(request.app.state)


@router.get(
    "/api/documents",
    response_model=list[DocumentResponse],
)
def list_documents(request: Request):
    return library_service.list_documents(request.app.state)


@router.post("/api/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request):
    result = library_service.ask(TurnCommand(**body.model_dump()), request.app.state)
    return AskResponse(mode=request.app.state.mode, text=result.text,
        skills_used=result.skills_used,
        sources=[{'citation_id': index, **asdict(source)}
                 for index, source in enumerate(result.sources, start=1)])


@router.get(
"/api/knowledge-base/status",
response_model=PersistenceStatus,
)
def persistence_status(request: Request):
    return library_service.persistence_status(request.app.state)

@router.post(
    "/api/knowledge-base/save",
    response_model=PersistenceResponse,
)
def save_knowledge_base(request: Request):
    return library_service.save_knowledge_base(request.app.state)


@router.post(
    "/api/knowledge-base/load",
    response_model=PersistenceResponse,
)
def load_knowledge_base(request: Request):
    return library_service.load_knowledge_base(request.app.state)
