import deepsearch_agent


def test_project_name_returns_display_name() -> None:
    assert deepsearch_agent.project_name() == "DeepSearch Paper Agent"