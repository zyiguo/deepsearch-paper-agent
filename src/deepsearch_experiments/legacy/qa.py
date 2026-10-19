"""Question-answering service built on an LLMClient."""

from deepsearch_agent.core.answers import Answer



from deepsearch_agent.application.retrieval_answer import RetrievalAnswerService

class AnswerService(RetrievalAnswerService):
    """Offline quick-QA and query-planning comparison baselines."""
    def _answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources=None,
    memory_context=None, observer=None, input_evidence=None, skill_ids=None, working_memory=None,
    run_id=None, resume=False) -> Answer:
        if not self.use_graph or self.knowledge_base is None:
            return super()._answer(question, top_k, history, on_progress=on_progress,
                allowed_sources=allowed_sources, memory_context=memory_context, observer=observer,
                input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory,
                run_id=run_id, resume=resume)
        question = question.strip()
        kb, skills, wiki_view = self._prepare(question, top_k, history, on_progress=on_progress,
            allowed_sources=allowed_sources, memory_context=memory_context, observer=observer,
            input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory,
            run_id=run_id, resume=resume)
        if self.use_graph and kb is not None:
            from deepsearch_experiments.legacy.workflow import LegacyResearchWorkflow as ResearchWorkflow
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
            result = ResearchWorkflow(
                self.client, kb,
                context_window=self.context_window,
                max_output_tokens=self.max_output_tokens,
                reranker=self.reranker,
                on_progress=on_progress,
                memory_context=memory_context,
                search_intent=self.search_intent,
                observer=observer,
                visual_evidence=self.visual_evidence,
                skills=skills,
                tool_session=tool_session,
                mcp_settings=self.mcp_settings,
                wiki_view=wiki_view,
                checkpointer=self.checkpointer if run_id else None,
                run_id=run_id,
            ).answer(question, top_k, history, input_evidence=input_evidence, resume=resume)
            if self.history_store is not None:
                self.history_store.append(question=question, answer=result)
            return result

