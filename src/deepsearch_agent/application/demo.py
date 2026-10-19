"""Explicit offline demo selection in application composition."""
from deepsearch_agent.application.retrieval_answer import RetrievalAnswerService


class DemoAnswerService(RetrievalAnswerService):
    """Only selected when the configured chat backend is demo."""
