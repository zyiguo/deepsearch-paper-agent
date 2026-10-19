"""Independent, source-checked adaptations of IRCoT-QA and LogicRAG.

No upstream executable code or LogicRAG prompts are vendored here. In particular,
the GPL-3.0 LogicRAG implementation is not silently relicensed by this module.
IRCoT's separately supplied author demonstrations keep their upstream license.
These adapters are NOT reproductions of published scores; the runtime controls
the model, retrieval backend, budgets, and every model/search call.
"""


import hashlib
import json
import re
from collections import deque
from collections.abc import Sequence
from typing import Any, Protocol

from .structured import parse_object


METHODS = {
    "ircot": {
        "name": "ircot-adapted",
        "implementation": "independent-adaptation",
        "repository": "https://github.com/StonyBrookNLP/ircot",
        "revision": "3c1820f698eea5eeddb4fba3c56b64c961e063e4",
        "upstream_license": "Apache-2.0",
        "reference_files": [
            "commaqa/inference/ircot.py",
            "commaqa/inference/prompt_reader.py",
            "base_configs/ircot_qa_codex_musique.jsonnet",
        ],
        "adaptations": [
            "Unified runtime replaces the author model and Elasticsearch services.",
            "pysbd sentence boundaries replace spaCy en_core_web_sm.",
            "Content-ID deduplication replaces fuzzy paragraph matching.",
            "Frozen demonstrations are not dynamically pruned using GPT-2 tokens.",
            "Shared hard budgets propagate; no hidden retry or forced answer call.",
        ],
    },
    "logicrag": {
        "name": "logicrag-adapted",
        "response_protocol": "single-json-object-envelope-v2",
        "implementation": "independent-adaptation",
        "repository": "https://github.com/chensyCN/LogicRAG",
        "revision": "72a298c95ffd75890c7f9c28fd1c094201f669a7",
        "upstream_license": "GPL-3.0",
        "reference_files": ["src/models/logic_rag.py"],
        "adaptations": [
            "Independent implementation and rewritten prompts; not copied GPL code.",
            "Unified runtime replaces the author model and dense retrieval backend.",
            "JSON booleans must be booleans, not truthy strings.",
            "A single JSON object may have a Markdown/prose envelope; ambiguous or malformed JSON fails.",
            "Invalid dependency indices and cycles fail explicitly.",
            "Stable topological ordering preserves input order among ready nodes.",
            "Evidence IDs cover all retrieved rounds, not just the final round.",
            "Runtime errors and hard budgets propagate instead of being swallowed.",
        ],
    },
}


class BaselineRuntime(Protocol):
    def generate(self, prompt: str, *, stop: list[str] | None = None) -> str: ...

    def search(self, query: str) -> list[dict[str, Any]]: ...


def prepare_ircot_prompt(
    text: str, question_ids: Sequence[str] | None = None
) -> tuple[str, list[str]]:
    """Remove author metadata; optionally select examples in config-list order.

    Upstream ``order_by_key='qid'`` actually follows the order in valid_qids,
    not lexical ordering. No model-dependent truncation happens here. A plain
    preselected prompt is accepted, but cannot claim metadata-verified qids.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("IRCoT requires a non-empty frozen few-shot prompt")
    examples: list[tuple[str, str]] = []
    current_id: str | None = None
    lines: list[str] = []
    prefix: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("# METADATA:"):
            if current_id is not None:
                examples.append((current_id, "\n".join(lines).strip()))
            metadata = json.loads(line.strip().split(":", 1)[1])
            if not isinstance(metadata, dict) or not isinstance(metadata.get("qid"), str):
                raise ValueError("IRCoT demonstration metadata requires a string qid")
            current_id = metadata["qid"]
            lines = []
        elif current_id is None:
            prefix.append(line)
        else:
            lines.append(line)
    if current_id is not None:
        examples.append((current_id, "\n".join(lines).strip()))
    if not examples:
        if question_ids is not None:
            raise ValueError("Cannot select demonstration qids without metadata")
        return text.strip(), []
    by_id = dict(examples)
    if len(by_id) != len(examples) or any(not body for _, body in examples):
        raise ValueError("Demonstration qids must be unique and examples non-empty")
    ids = list(question_ids) if question_ids is not None else [qid for qid, _ in examples]
    if not ids or len(set(ids)) != len(ids) or any(qid not in by_id for qid in ids):
        raise ValueError("Requested demonstration qids must be unique and present")
    parts = ["\n".join(prefix).strip(), *(by_id[qid] for qid in ids)]
    return "\n\n\n".join(part for part in parts if part), ids


def _result(
    name: str,
    evidence: dict[str, dict[str, Any]],
    trace: list[dict[str, Any]],
    *,
    answer: str = "",
    status: str = "ok",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "answer": answer,
        "status": status,
        "evidence_ids": list(evidence),
        "trace": trace,
        "method": dict(METHODS[name]),
        **extra,
    }


def _answer_marker(text: str) -> str | None:
    matches = list(re.finditer(r"\banswer\s+is\s*:?\s*([^\n]+)", text, re.IGNORECASE))
    if not matches:
        return None
    answer = matches[-1].group(1).strip()
    return answer.removesuffix(".").strip() or None


def _ircot_query(question: str, sentences: list[str]) -> str:
    # The author retriever skips inference-only sentences when selecting its query.
    inference_prefix = re.compile(r"^(?:thus[ ,]|so[ ,]|that is,|therefore|hence)", re.IGNORECASE)
    arithmetic = re.compile(r"\d[\d,.]*\s+[+-]\s+\d[\d,.]*\s+=\s+\d")
    for sentence in reversed(sentences):
        if sentence.strip() and not inference_prefix.search(sentence) and not arithmetic.search(sentence):
            return sentence.strip()
    return question


def _ircot(
    question: str,
    runtime: BaselineRuntime,
    max_steps: int,
    few_shot_examples: str | None,
    demonstration_ids: Sequence[str] | None,
) -> dict[str, Any]:
    prompt, prompt_ids = prepare_ircot_prompt(few_shot_examples or "", demonstration_ids)
    # This is an optional benchmark dependency, not a model or tokenizer download.
    import pysbd

    splitter = pysbd.Segmenter(language="en", clean=False)
    evidence: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    sentences: list[str] = []
    stop_reason = "iteration_limit"

    def qa_prompt(previous: list[str]) -> str:
        paragraphs = []
        for hit in evidence.values():
            text = " ".join(hit["text"].split(" ")[:350]).strip()
            if not text.startswith("Wikipedia Title: "):
                text = f"Wikipedia Title: {hit['title']}\n{text}"
            paragraphs.append(text)
        context = "\n\n".join(paragraphs)
        return f"{prompt}\n\n\n{context}\n\nQ: {question}\nA: {' '.join(previous)}".strip()

    for step in range(max_steps):
        query = _ircot_query(question, sentences)
        hits = runtime.search(query)
        for hit in hits:
            if len(hit["text"].split(" ")) <= 600 and len(evidence) < 15:
                evidence.setdefault(hit["id"], hit)
        generation = runtime.generate(qa_prompt(sentences), stop=["\n\n\n"])
        segments = splitter.segment(generation.strip())
        sentence = segments[0].strip() if segments else ""
        sentences.append(sentence)
        trace.append({"phase": "interleaved_retrieval", "step": step + 1, "query": query, "evidence_ids": list(evidence)})
        if not sentence:
            stop_reason = "empty_generation"
            break
        if _answer_marker(sentence) is not None:
            stop_reason = "answer_marker"
            break
    # IRCoT-QA answers afresh using the accumulated paragraphs, not its last thought.
    final = runtime.generate(qa_prompt([]), stop=["\n\n\n"]).strip()
    answer = _answer_marker(final)
    marker_found = answer is not None
    if answer is None:
        answer = final
    trace.append({"phase": "final_qa", "evidence_ids": list(evidence)})
    return _result(
        "ircot", evidence, trace, answer=answer, status="ok" if answer else "invalid_output",
        stop_reason=stop_reason, final_answer_marker_found=marker_found,
        demonstration_ids=prompt_ids,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_selection="selected_qids" if demonstration_ids is not None else "provided_file_order",
    )


def _topological_order(dependencies: list[str], pairs: Any) -> list[str] | None:
    if not isinstance(pairs, list):
        return None
    outgoing: list[set[int]] = [set() for _ in dependencies]
    indegree = [0] * len(dependencies)
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        dependent, prerequisite = pair
        if any(type(i) is not int or not 0 <= i < len(dependencies) for i in pair):
            return None
        if dependent == prerequisite:
            return None
        if dependent not in outgoing[prerequisite]:
            outgoing[prerequisite].add(dependent)
            indegree[dependent] += 1
    ready = deque(index for index, degree in enumerate(indegree) if degree == 0)
    result = []
    while ready:
        index = ready.popleft()
        result.append(dependencies[index])
        for dependent in sorted(outgoing[index]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return result if len(result) == len(dependencies) else None


def _logicrag(question: str, runtime: BaselineRuntime, max_steps: int) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []

    def structured(text: str, stage: str) -> dict[str, Any] | None:
        parsed = parse_object(text)
        trace.append({"phase": "structured_response", "stage": stage,
                      "envelope": parsed.envelope, "error_code": parsed.error_code})
        return parsed.value

    def invalid(stage: str, schema_error: str) -> dict[str, Any]:
        parse_error = trace[-1].get('error_code') if trace[-1]['phase'] == 'structured_response' else None
        return _result("logicrag", evidence, trace, status="invalid_output",
                       failure_stage=stage, failure_code=parse_error or schema_error)

    def retrieve_and_summarize(query: str, summary: str) -> str:
        hits = runtime.search(query)
        for hit in hits:
            evidence.setdefault(hit["id"], hit)
        trace.append({"phase": "retrieve", "query": query, "evidence_ids": [hit["id"] for hit in hits]})
        return runtime.generate(
            "Update a compact factual summary for the question using the retrieved excerpts. "
            "Keep relevant names, dates, quantities, and details; merge useful previous facts "
            "without repetition. Return only the updated summary.\n"
            + json.dumps({"question": question, "previous_summary": summary, "excerpts": hits}, ensure_ascii=False)
        ).strip()

    def answer(summary: str, reason: str) -> dict[str, Any]:
        final = runtime.generate(
            "Answer the question using the information summary. Output only the shortest "
            "complete answer, without explanation.\n"
            + json.dumps({"question": question, "summary": summary}, ensure_ascii=False)
        ).strip()
        trace.append({"phase": "final_qa", "stop_reason": reason})
        return _result("logicrag", evidence, trace, answer=final,
                       status="ok" if final else "invalid_output", stop_reason=reason)

    summary = retrieve_and_summarize(question, "")
    analysis = structured(runtime.generate(
        "Check whether the summary fully answers the question. Identify missing information "
        "and the information dependencies required to solve it. Return a JSON object with "
        "can_answer (JSON boolean), missing_info (string), subquery (string), "
        "current_understanding (string), dependencies (array of strings), "
        "and missing_reason (string).\n"
        + json.dumps({"question": question, "summary": summary}, ensure_ascii=False)
    ), "warmup_analysis")
    if analysis is None or type(analysis.get("can_answer")) is not bool:
        return invalid("warmup_analysis", "invalid_can_answer")
    trace.append({"phase": "warmup_analysis", "can_answer": analysis["can_answer"]})
    if analysis["can_answer"]:
        return answer(summary, "warmup_sufficient")
    dependencies = analysis.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies or any(not isinstance(item, str) or not item.strip() for item in dependencies):
        return invalid("dependencies", "invalid_dependencies")
    graph = structured(runtime.generate(
        "Identify ordering constraints among the numbered information dependencies. "
        "Return JSON {\"dependency_pairs\": [[dependent_index, prerequisite_index], ...]}. "
        "Use zero-based indices; use an empty array when all dependencies are independent.\n"
        + json.dumps({"question": question, "dependencies": list(enumerate(dependencies))}, ensure_ascii=False)
    ), "dependency_graph")
    ordered = _topological_order(dependencies, graph.get("dependency_pairs")) if graph else None
    if ordered is None:
        return invalid("dependency_graph", "invalid_dependency_graph")
    trace.append({"phase": "dependency_graph", "ordered_dependencies": ordered})
    for index, dependency in enumerate(ordered[:max_steps]):
        summary = retrieve_and_summarize(dependency, summary)
        decision = structured(runtime.generate(
            "Check whether the original question is now fully answerable from the summary, "
            "considering its ordered dependencies and the dependency just researched. "
            "Return JSON with can_answer (JSON boolean) and current_understanding (string).\n"
            + json.dumps({"question": question, "summary": summary,
                          "dependencies": ordered, "current_dependency": dependency}, ensure_ascii=False)
        ), "dependency_analysis")
        if decision is None or type(decision.get("can_answer")) is not bool:
            return invalid("dependency_analysis", "invalid_can_answer")
        trace.append({"phase": "dependency_analysis", "step": index + 1, "can_answer": decision["can_answer"]})
        if decision["can_answer"]:
            return answer(summary, "dependencies_sufficient")
    return answer(summary, "iteration_limit" if len(ordered) > max_steps else "dependencies_exhausted")


def run_baseline(
    name: str,
    question: str,
    runtime: BaselineRuntime,
    *,
    max_steps: int = 8,
    few_shot_examples: str | None = None,
    demonstration_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run only the author-method adapter; runtime budget exceptions propagate.

    LogicRAG's warmup is additional to max_steps, as in the author code. Hard
    model/search/token budgets must therefore be enforced by the shared runtime.
    IRCoT refuses to silently substitute zero-shot prompting for demonstrations.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty")
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    if name == "ircot":
        return _ircot(question, runtime, max_steps, few_shot_examples, demonstration_ids)
    if name == "logicrag":
        return _logicrag(question, runtime, max_steps)
    raise ValueError(f"Unknown multi-hop baseline: {name}")
