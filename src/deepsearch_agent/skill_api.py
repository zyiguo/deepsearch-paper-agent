"""Read-only skill catalog and preflight validation shared by QA routes."""
from fastapi import APIRouter, HTTPException, Request

from deepsearch_agent.skills import SkillError

router = APIRouter(prefix='/api/skills', tags=['技能'])


def validate_skills(state, selected):
    if selected is None:
        return
    try:
        service = state.answer_service
        state.skills.session(selected, max_tokens=min(1600, max(0, (service.context_window - service.max_output_tokens) // 4)))
    except SkillError as error:
        raise HTTPException(422, str(error)) from None


@router.get('')
def list_skills(request: Request):
    session = request.app.state.skills.session()
    return {'skills': session.catalog, 'warnings': session.warnings, 'max_active': 3,
            'automatic': request.app.state.answer_service.use_graph}
