"""Question-answering service built on an LLMClient."""

from deepsearch_agent.core.answers import Answer, chunk_to_source_reference

from deepsearch_agent.core.evidence import TextChunk
from deepsearch_agent.core.prompts import build_qa_prompt
from deepsearch_tracing.context import notify


from deepsearch_agent.application.service_base import AuditedAnswerService
from deepsearch_agent.knowledge.context import ContextAssembler

class RetrievalAnswerService(ContextAssembler, AuditedAnswerService):
    """Retrieval-only use case reused by the demo and explicit offline baselines."""
    def _answer(self, question: str, top_k: int = 5,
    history: list[dict[str, str]] | None = None, *, on_progress=None, allowed_sources=None,
    memory_context=None, observer=None, input_evidence=None, skill_ids=None, working_memory=None,
    run_id=None, resume=False) -> Answer:
        question = question.strip()
        kb, skills, wiki_view = self._prepare(question, top_k, history, on_progress=on_progress,
            allowed_sources=allowed_sources, memory_context=memory_context, observer=observer,
            input_evidence=input_evidence, skill_ids=skill_ids, working_memory=working_memory,
            run_id=run_id, resume=resume)
        chunks: list[TextChunk] = list(input_evidence or [])

        if kb is not None:
            from deepsearch_agent.knowledge.retrieval_pipeline import retrieve
            chunks.extend(retrieve(kb, question, top_k, query_expander=self.query_expander,
                                   reranker=self.reranker, history=history, wiki_view=wiki_view))

        if not chunks:
            result = Answer(
                text="当前知识库未检索到可用证据，无法根据资料回答。",
                sources=[],
            )
        else:
            skill_prompt = skills.prompt() if skills else ''
            included, context = self._build_context(question, chunks, history, skill_prompt=skill_prompt)

            prompt = build_qa_prompt(
                question=question,
                context=context,
                history=history,
            )
            prompt += skill_prompt

            visual_context = [{'source': c.source_path, 'visual_pages': c.visual_pages} for c in included]
            if any(c.visual_pages for c in included):
                if self.visual_evidence is None:
                    from deepsearch_agent.core.models import ModelServiceError
                    raise ModelServiceError('请配置视觉模型后阅读图表或扫描页')
                text = self.visual_evidence.complete(prompt, visual_context, structured=False)
            else:
                text = self.client.complete(prompt)
            result = Answer(
                text=text,
                sources=[
                    chunk_to_source_reference(chunk)
                    for chunk in included
                ],
            )

        if skills is not None:
            result.skills_used = skills.used
            notify(observer, 'skills.loaded', {'skills': skills.used, 'warnings': skills.warnings})
        if self.history_store is not None:
            self.history_store.append(
                question=question,
                answer=result,
            )

        return result
