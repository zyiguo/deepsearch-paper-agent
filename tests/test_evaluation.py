import pytest
from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.evaluation import score, validate_cases


def chunk(page, source="paper", index=0):
    return TextChunk("evidence", source, page, index)


def test_recall_counts_unique_labels_but_rank_counts_chunk_slots():
    hits = [chunk(1), chunk(1, index=1), chunk(2)]
    gold = [{"source_path": "paper", "page_number": 1},
            {"source_path": "paper", "page_number": 2}]
    assert score(hits, gold, 2) == {"recall": .5, "rr": 1.0}
    assert score(hits, gold, 3)["recall"] == 1
    assert score(hits, gold[1:], 3)["rr"] == pytest.approx(1/3)


def test_wrong_paper_and_missing_hit_are_zero():
    gold = [{"source_path": "paper", "page_number": 1}]
    assert score([chunk(1, "other")], gold, 5) == {"recall": 0, "rr": 0}
    assert score([], gold, 5)["recall"] == 0


def test_only_explicit_page_spans_count_for_multpage_chunk():
    c = chunk(1)
    c.page_end = 3
    c.page_spans = [{"page_number": 1}, {"page_number": 3}]
    assert score([c], [{"source_path": "paper", "page_number": 3}], 1)["recall"] == 1
    assert score([c], [{"source_path": "paper", "page_number": 2}], 1)["recall"] == 0


def test_validate_rejects_missing_gold_and_duplicate_ids():
    c = {"id": "q1", "question": "what?", "split": "dev", "relevant":
         [{"source_path": "paper", "page_number": 1, "evidence": "evidence"}]}
    validate_cases([c], [chunk(1)])
    with pytest.raises(ValueError):
        validate_cases([c, c], [chunk(1)])
    with pytest.raises(ValueError):
        validate_cases([c], [chunk(2)])
    with pytest.raises(ValueError):
        validate_cases([], [chunk(1)])


def test_shared_pipeline_expansion_preserves_current_early_stop():
    from deepsearch_agent.retrieval_pipeline import retrieve
    class KB:
        def search(self, q, top_k):
            return [chunk(1)]
    class Expander:
        def expand(self, q, history):
            return ["alternate"]
    trace = {}
    assert retrieve(KB(), "q", 1, query_expander=Expander(), trace=trace)[0].page_number == 1
    assert len(trace["candidates"]) == 1


def test_offline_cli_all_non_llm_variants_and_comparison(tmp_path, monkeypatch):
    import json
    from deepsearch_agent.evaluation import main
    from deepsearch_agent.knowledge_base import KnowledgeBase
    from deepsearch_agent.pdf import FakePdfReader
    class Embed:
        def embed(self, texts):
            return [[1., 0.] for t in texts]
    kb = KnowledgeBase(FakePdfReader(["evidence"]), embedding_client=Embed(), embedding_id="test-v1")
    kb.ingest_pdf("paper")
    snapshot = tmp_path / "index.json"
    kb.save(snapshot)
    dataset = tmp_path / "labels.json"
    dataset.write_text(json.dumps({"cases": [{"id": "q1", "question": "evidence", "split": "dev",
        "relevant": [{"source_path": "paper", "page_number": 1, "evidence": "evidence"}]}]}))
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"identity": "test-v1", "vectors": {"evidence": [1., 0.]}}))
    output = tmp_path / "report.json"
    args = ["eval", "--snapshot", str(snapshot), "--dataset", str(dataset), "--query-cache", str(cache),
            "--output", str(output), "--configs", "bm25", "semantic", "hybrid-0.25", "hybrid-0.5",
            "hybrid-0.75", "semantic-ann"]
    monkeypatch.setattr("sys.argv", args)
    main()
    report = json.loads(output.read_text())
    assert len(report["results"]) == 6
    assert all(r["recall@5"] == 1 for r in report["results"].values())
    assert report["results"]["semantic-ann"]["ann_exact_topk_overlap"] == 1
    second = tmp_path / "second.json"
    monkeypatch.setattr("sys.argv", args + ["--baseline", str(output), "--output", str(second)])
    main()
    assert json.loads(second.read_text())["deltas"]["bm25"]["recall@5"] == 0


def test_weight_bounds():
    from deepsearch_agent.knowledge_base import KnowledgeBase
    from deepsearch_agent.pdf import FakePdfReader
    for weight in [-.1, 1.1, float("nan")]:
        with pytest.raises(ValueError):
            KnowledgeBase(FakePdfReader([]), semantic_weight=weight)
