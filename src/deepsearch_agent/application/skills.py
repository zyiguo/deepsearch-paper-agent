from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.workspace.skills import SkillError

def validate_skills(state, selected):
    if selected is None:
        return
    try:
        service = state.answer_service
        state.skills.session(selected, max_tokens=min(1600, max(0, (service.context_window - service.max_output_tokens) // 4)))
    except SkillError as error:
        raise ApplicationError(422, str(error)) from None

