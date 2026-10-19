from fastapi import Request
from deepsearch_agent.core.errors import ApplicationError
from fastapi.responses import JSONResponse
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.workspace.skills import SkillError


from deepsearch_agent.workspace.llm_wiki import WikiError

def register_error_handlers(app):
    @app.exception_handler(ApplicationError)
    async def handle_application_error(request, error):
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        # ServerErrorMiddleware is outside user middleware, so attach the ID here too.
        request_id = getattr(request.state, 'request_id', '')
        return JSONResponse(status_code=500,
            content={'detail': '服务内部错误，请使用请求编号排查', 'request_id': request_id},
            headers={'X-Request-ID': request_id})


    @app.exception_handler(WikiError)
    async def handle_wiki_error(request: Request, error: WikiError):
        code = str(error)
        status = 404 if code in {'wiki_not_found', 'wiki_not_available'} else 409
        return JSONResponse(status_code=status, content={'detail': code})


    @app.exception_handler(ModelServiceError)
    async def handle_model_error(
        request: Request,
        error: ModelServiceError,
    ):
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": str(error)},
        )


    @app.exception_handler(SkillError)
    async def handle_skill_error(request: Request, error: SkillError):
        return JSONResponse(status_code=422, content={'detail': str(error)})
