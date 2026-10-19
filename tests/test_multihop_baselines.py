import json

import pytest

from deepsearch_experiments.benchmarks.multihop.baselines import prepare_ircot_prompt, run_baseline


DEMO = 'Q: Demonstration question?\nA: So the answer is: demo.'


class FakeRuntime:
    def __init__(self, responses, hits=None):
        self.responses = iter(responses)
        self.hits = hits or [[{"id": "p1", "title": "Alpha", "text": "Alpha is in Beta."}]]
        self.queries = []
        self.prompts = []

    def generate(self, prompt, *, stop=None):
        self.prompts.append(prompt)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def search(self, query):
        self.queries.append(query)
        return self.hits[min(len(self.queries) - 1, len(self.hits) - 1)]


def test_prompt_selection_uses_requested_order_and_removes_metadata():
    raw = '# METADATA: {"qid": "b"}\nQ: B?\nA: B.\n\n# METADATA: {"qid": "a"}\nQ: A?\nA: A.'
    prompt, ids = prepare_ircot_prompt(raw, ["a", "b"])
    assert ids == ["a", "b"]
    assert prompt.index("Q: A?") < prompt.index("Q: B?")
    assert "METADATA" not in prompt


@pytest.mark.parametrize("text, ids", [("", None), (DEMO, ["missing"]), ('# METADATA: {"qid": "a"}\nQ: A?', ["missing"])])
def test_prompt_selection_rejects_missing_demonstrations(text, ids):
    with pytest.raises(ValueError):
        prepare_ircot_prompt(text, ids)


def test_ircot_requires_few_shot_before_runtime_calls():
    runtime = FakeRuntime([])
    with pytest.raises(ValueError, match="few-shot"):
        run_baseline("ircot", "Question?", runtime)
    assert not runtime.queries


def test_ircot_uses_first_sentence_as_query_and_separate_final_qa():
    runtime = FakeRuntime([
        "Dr. Smith founded Beta. This second sentence must not become the query.",
        "So the answer is: provisional.",
        "The final supporting fact is different. So the answer is: final.",
    ])
    result = run_baseline("ircot", "Who founded Beta?", runtime, few_shot_examples=DEMO)
    assert runtime.queries == ["Who founded Beta?", "Dr. Smith founded Beta."]
    assert len(runtime.prompts) == 3
    assert "Dr. Smith founded Beta." not in runtime.prompts[-1]
    assert result["answer"] == "final"
    assert result["stop_reason"] == "answer_marker"
    assert result["evidence_ids"] == ["p1"]


def test_ircot_skips_inference_only_sentence_and_handles_decimal():
    runtime = FakeRuntime(["The value is 3.14 units.", "Therefore more information is needed.", "So the answer is: 3.14.", "So the answer is: 3.14."])
    result = run_baseline("ircot", "Value?", runtime, few_shot_examples=DEMO)
    assert runtime.queries == ["Value?", "The value is 3.14 units.", "The value is 3.14 units."]
    assert result["answer"] == "3.14"


def test_ircot_still_finalizes_at_iteration_limit():
    runtime = FakeRuntime(["A relevant fact.", "Final short answer"])
    result = run_baseline("ircot", "Question?", runtime, few_shot_examples=DEMO, max_steps=1)
    assert result["answer"] == "Final short answer"
    assert result["stop_reason"] == "iteration_limit"
    assert not result["final_answer_marker_found"]


def test_ircot_context_cap_and_long_paragraph_policy():
    hits = [{"id": str(i), "title": str(i), "text": "fact"} for i in range(20)]
    hits.insert(0, {"id": "long", "title": "Long", "text": "word " * 601})
    runtime = FakeRuntime(["So the answer is: yes.", "Yes"], [hits])
    result = run_baseline("ircot", "Question?", runtime, few_shot_examples=DEMO)
    assert result["evidence_ids"] == [str(i) for i in range(15)]


def test_logicrag_warmup_shortcut():
    runtime = FakeRuntime(["Summary.", '{"can_answer": true}', "Beta"])
    result = run_baseline("logicrag", "Where?", runtime)
    assert result["answer"] == "Beta"
    assert len(runtime.queries) == 1
    assert len(runtime.prompts) == 3
    assert result["stop_reason"] == "warmup_sufficient"


def test_logicrag_dependency_order_and_cumulative_evidence():
    runtime = FakeRuntime([
        "First summary", json.dumps({"can_answer": False, "dependencies": ["Second", "First"]}),
        '{"dependency_pairs": [[0, 1]]}', "Second summary", '{"can_answer": false}',
        "Final summary", '{"can_answer": true}', "Answer",
    ], [
        [{"id": "p1", "title": "1", "text": "one"}],
        [{"id": "p2", "title": "2", "text": "two"}],
        [{"id": "p3", "title": "3", "text": "three"}],
    ])
    result = run_baseline("logicrag", "Question?", runtime)
    assert runtime.queries == ["Question?", "First", "Second"]
    assert result["evidence_ids"] == ["p1", "p2", "p3"]
    assert result["answer"] == "Answer"


@pytest.mark.parametrize("flag", ['"false"', '"true"', "1", "null"])
def test_logicrag_rejects_truthy_nonboolean(flag):
    runtime = FakeRuntime(["Summary", '{"can_answer": ' + flag + '}'])
    result = run_baseline("logicrag", "Question?", runtime)
    assert result["status"] == "invalid_output"
    assert result["failure_stage"] == "warmup_analysis"
    assert len(runtime.prompts) == 2


@pytest.mark.parametrize("pairs", [[[0, 1], [1, 0]], [[0, 0]], [[0, -1]], [[0, 2]], [[True, 0]], [[0]], "bad"])
def test_logicrag_rejects_cycles_and_invalid_edges(pairs):
    runtime = FakeRuntime(["Summary", '{"can_answer": false, "dependencies": ["A", "B"]}', json.dumps({"dependency_pairs": pairs})])
    result = run_baseline("logicrag", "Question?", runtime)
    assert result["status"] == "invalid_output"
    assert result["failure_stage"] == "dependency_graph"
    assert len(runtime.queries) == 1


def test_logicrag_warmup_is_additional_to_dependency_limit():
    runtime = FakeRuntime(["Summary", '{"can_answer": false, "dependencies": ["A", "B"]}', '{"dependency_pairs": []}', "Updated", '{"can_answer": false}', "Best answer"])
    result = run_baseline("logicrag", "Question?", runtime, max_steps=1)
    assert runtime.queries == ["Question?", "A"]
    assert result["stop_reason"] == "iteration_limit"


@pytest.mark.parametrize("name", ["ircot", "logicrag"])
def test_runtime_budget_errors_propagate(name):
    class BudgetExceeded(RuntimeError):
        pass

    runtime = FakeRuntime([BudgetExceeded("token cap")])
    with pytest.raises(BudgetExceeded, match="token cap"):
        run_baseline(name, "Question?", runtime, few_shot_examples=DEMO)


@pytest.mark.parametrize("value", [0, -1, True])
def test_iteration_limit_validation(value):
    with pytest.raises(ValueError, match="positive integer"):
        run_baseline("logicrag", "Question?", FakeRuntime([]), max_steps=value)
