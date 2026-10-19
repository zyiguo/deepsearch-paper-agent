"""Question-answering service built on an LLMClient."""

from deepsearch_agent.core.answers import Answer


from deepsearch_agent.application.service_base import AuditedAnswerService
from deepsearch_agent.agents.agent_config import AgentSettings

class AnswerService(AuditedAnswerService):
    """Production path: isolated research child followed by independent verification."""
    def __init__(self, *args, use_graph=True, agent_settings=None, search_intent=None, **kwargs):
        if not use_graph or search_intent is not None:
            raise ValueError('Legacy QA/search policies are offline: deepsearch_experiments.legacy.qa')
        settings = agent_settings if agent_settings is not None else AgentSettings(enabled=True)
        if not settings.enabled:
            raise ValueError('AGENTIC_RAG_ENABLED=false moved to offline baselines; enable the production child agent')
        super().__init__(*args, use_graph=True, agent_settings=settings, **kwargs)
    def _answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources=None,
    memory_context=None, observer=None, input_evidence=None, skill_ids=None, working_memory=None,
    run_id=None, resume=False) -> Answer:
        question = question.strip()
        kb, skills, wiki_view = self._prepare(question, top_k, history, on_progress=on_progress,
            allowed_sources=allowed_sources, memory_context=memory_context, observer=observer,
            input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory,
            run_id=run_id, resume=resume)
        if kb is not None:
            from deepsearch_agent.agents.runtime import ResearchWorkflow
            tool_session = None
            if self.agent_settings is not None and self.agent_settings.enabled:
                memory_args = {'working_memory': working_memory} if working_memory is not None else {}
                if self.tool_session_factory is None:
                    from deepsearch_agent.agents.tool_service import PaperToolSession
                    tool_session = PaperToolSession(kb, settings=self.agent_settings, observer=observer,
                                                    input_evidence=input_evidence, reranker=self.reranker,
                                                    **memory_args)
                else:
                    tool_session = self.tool_session_factory(kb, settings=self.agent_settings,
                                                            observer=observer, input_evidence=input_evidence,
                                                            **memory_args)
            from deepsearch_agent.adapters.tool_adapters import langgraph_tools
            if tool_session is not None and self.tool_ledger is not None and run_id:
                from deepsearch_agent.application.tool_execution import ToolExecutor
                from deepsearch_agent.core.tool_execution import ToolExecutionBlocked
                if resume and not self.tool_ledger.has_run(run_id):
                    raise ToolExecutionBlocked('tool_ledger_run_missing')
                self.tool_ledger.begin_run(run_id)
                tool_session.execution = ToolExecutor(self.tool_ledger, run_id)
            from deepsearch_agent.agents.evidence_policy import EvidencePolicy, SessionResearchControl
            policy = (EvidencePolicy.v5() if self.agent_settings.evidence_policy_version == 'v5' else EvidencePolicy.v4() if self.agent_settings.evidence_policy_version == 'v4' else EvidencePolicy.v3() if self.agent_settings.evidence_policy_version == 'v3'
                      else EvidencePolicy.v2() if self.agent_settings.evidence_policy_version == 'v2'
                      else EvidencePolicy()) if self.agent_settings.evidence_control_enabled else None
            external = None
            if self.mcp_settings is not None and self.mcp_settings.enabled:
                from deepsearch_agent.adapters.mcp_runtime import external_tools
                external = lambda: external_tools(self.mcp_settings)
            result = ResearchWorkflow(
                self.client, kb,
                context_window=self.context_window,
                max_output_tokens=self.max_output_tokens,
                reranker=self.reranker,
                on_progress=on_progress,
                memory_context=memory_context,
                observer=observer,
                visual_evidence=self.visual_evidence,
                skills=skills,
                tool_session=tool_session,
                tools_factory=langgraph_tools, external_tools=external,
                mcp_settings=self.mcp_settings,
                wiki_view=wiki_view,
                checkpointer=self.checkpointer if run_id else None,
                run_id=run_id,
                evidence_policy=policy,
                budget_control=SessionResearchControl(tool_session) if policy else None,
            ).answer(question, top_k, history, input_evidence=input_evidence, resume=resume)
            if self.history_store is not None:
                self.history_store.append(question=question, answer=result)
            return result

        raise ValueError('Production research requires a knowledge base')
