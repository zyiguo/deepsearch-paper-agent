"""Stable ASGI entry point; composition lives in application.bootstrap."""
from deepsearch_agent.application.bootstrap import create_app

app = create_app()
