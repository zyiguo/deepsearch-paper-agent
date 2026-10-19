"""LangGraph tool adaptation; the business service has no framework dependency."""
import json
from uuid import uuid4

from langchain_core.tools import StructuredTool

from deepsearch_agent.agents.tool_service import ToolError
from deepsearch_agent.core.tool_execution import ToolKind, ToolExecutionBlocked
from deepsearch_agent.core.cancellation import ResearchCancelled


def langgraph_tools(session, extra_tools=None):
    tools = []
    for name, (schema, description) in session.tool_definitions.items():
        def execute(_name=name, **kwargs):
            try:
                result = session.invoke(_name, kwargs)
                return result.content, {'trust': 'local', 'chunks': result.chunks}
            except ToolError as error:
                return json.dumps({'error': str(error)}), {'trust': 'local', 'chunks': []}
        tools.append(StructuredTool(name=name, description=description, args_schema=schema,
                                    func=execute, response_format='content_and_artifact'))
    names = set(session.tool_definitions) | {'working_memory'}
    for tool in extra_tools or []:
        if tool.name in names:
            continue
        names.add(tool.name)
        async def external(_tool=tool, **kwargs):
            async def invoke(operation_id):
                session.reserve(_tool.name)
                value = await _tool.ainvoke({'type': 'tool_call', 'name': _tool.name,
                                           'id': operation_id, 'args': kwargs})
                content = value.content
                if isinstance(content, list):
                    content = [({'type': 'image', 'omitted': True}
                                if isinstance(block, dict) and block.get('type') in {'image', 'image_url'}
                                else block) for block in content]
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                return content[:session.settings.observation_chars]
            try:
                execution = getattr(session, 'execution', None)
                if execution is None:
                    content = await invoke(uuid4().hex)
                else:
                    slot = getattr(session, 'operation_slot', None)
                    if slot is None:
                        raise ToolExecutionBlocked('tool_operation_identity_missing')
                    # MCP annotations are untrusted hints, never recovery capabilities.
                    outcome = await execution.aexecute(slot, _tool.name, ToolKind.EFFECTFUL, kwargs, invoke)
                    content = outcome.value
                    if outcome.replayed:
                        session.reserve(_tool.name)
                return content, {'trust': 'external', 'chunks': []}
            except (ToolExecutionBlocked, ResearchCancelled):
                raise
            except Exception:
                return '{"error":"external_tool_failed"}', {'trust': 'external', 'chunks': []}
        tools.append(StructuredTool(name=tool.name, description=tool.description, args_schema=tool.args_schema,
                                    coroutine=external, response_format='content_and_artifact'))
    return tools
