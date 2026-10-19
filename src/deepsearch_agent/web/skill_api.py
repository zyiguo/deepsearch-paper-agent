"""Read-only skill catalog and preflight validation shared by QA routes."""
from fastapi import APIRouter, Request


router = APIRouter(prefix='/api/skills', tags=['技能'])


@router.get('')
def list_skills(request: Request):
    session = request.app.state.skills.session()
    return {'skills': session.catalog, 'warnings': session.warnings, 'max_active': 3,
            'automatic': request.app.state.answer_service.use_graph}
