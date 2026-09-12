"""Small, repeatable retrieval benchmark. Run with python -m deepsearch_agent.evaluation."""
import argparse
from copy import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.knowledge_base import KnowledgeBase
from deepsearch_agent.pdf import FakePdfReader
from deepsearch_agent.retrieval_pipeline import retrieve


def page_keys(chunk):
    pages = [s["page_number"] for s in chunk.page_spans] or [chunk.page_number]
    return {(chunk.source_path, p) for p in pages}


def score(chunks, relevant, k):
    """Recall over unique gold pages; RR uses original chunk rank, truncated at k."""
    if k <= 0 or not relevant:
        raise ValueError("k and relevant labels must be nonempty/positive")
    gold = {(r["source_path"], r["page_number"]) for r in relevant}
    found, rr = set(), 0.0
    for rank, chunk in enumerate(chunks[:k], 1):
        matches = page_keys(chunk) & gold
        found.update(matches)
        if matches and not rr:
            rr = 1 / rank
    return {"recall": len(found) / len(gold), "rr": rr}


def validate_cases(cases, chunks):
    if not cases:
        raise ValueError("Empty evaluation set")
    pages = {}
    for c in chunks:
        if c.page_spans:
            for span in c.page_spans:
                pages.setdefault((c.source_path, span["page_number"]), []).append(
                    c.text[span["chunk_start"]:span["chunk_end"]])
        else:
            pages.setdefault((c.source_path, c.page_number), []).append(c.text)
    ids, questions = set(), set()
    for case in cases:
        ident, question = case["id"], case["question"].strip()
        if not ident or ident in ids or not question or question in questions:
            raise ValueError("Empty or duplicated ID/question")
        ids.add(ident)
        questions.add(question)
        if case.get("split") not in {"dev", "test"} or not case.get("relevant"):
            raise ValueError(f"Invalid split/labels: {ident}")
        for r in case["relevant"]:
            key = (r["source_path"], r["page_number"])
            evidence = ''.join(r.get("evidence", "").split())
            if type(r["page_number"]) is not int or r["page_number"] < 1 or key not in pages:
                raise ValueError(f"Gold page missing from snapshot: {ident}")
            if not evidence or not any(evidence in ''.join(t.split()) for t in pages[key]):
                raise ValueError(f"Evidence not found on gold page: {ident}")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CachedEmbedding:
    """Model-keyed persistent query vectors; never reuse vectors from another model."""
    def __init__(self, client, path, identity):
        self.client, self.path, self.identity = client, path, identity
        self.values = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["identity"] != identity:
                raise ValueError("Query cache model mismatch")
            self.values = data["vectors"]

    def embed(self, texts):
        for text in texts:
            if text not in self.values:
                if self.client is None:
                    raise ValueError("Query vector missing; use --allow-remote to populate cache")
                self.values[text] = self.client.embed([text])[0]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"identity": self.identity, "vectors": self.values}), encoding="utf-8")
        return [self.values[t] for t in texts]


def evaluate(kb, cases, k=5, expander=None, reranker=None):
    rows = []
    # Warm lexical/vector index: timings exclude construction, include query embedding and LLM.
    if kb.retrieval_mode in {"bm25", "hybrid"}:
        kb._lexical_index()
    if kb.retrieval_mode != "bm25" and kb._vectors:
        kb._ann_search(kb._vectors[0], 1)
    for case in cases:
        trace = {}
        start = time.perf_counter()
        hits = retrieve(kb, case["question"], k, query_expander=expander,
                        reranker=reranker, trace=trace)
        elapsed = (time.perf_counter() - start) * 1000
        candidates = trace["candidates"]
        rows.append({"id": case["id"], "question": case["question"],
                     **score(hits, case["relevant"], k), "latency_ms": elapsed,
                     "candidate_recall": score(candidates, case["relevant"], max(1, len(candidates)))["recall"],
                     "expansion_status": getattr(expander, "last_status", "disabled"),
                     "rerank_status": (getattr(reranker, "last_status", "unknown") if len(candidates)>1 else "skipped") if reranker else "disabled",
                     "queries": trace["queries"], "generated_queries": trace["generated_queries"], "hits": [asdict(c) for c in hits],
                     "candidates": [asdict(c) for c in candidates]})
    return {"count": len(rows), f"recall@{k}": sum(r["recall"] for r in rows)/len(rows),
            f"mrr@{k}": sum(r["rr"] for r in rows)/len(rows),
            "candidate_recall": sum(r["candidate_recall"] for r in rows)/len(rows),
            "mean_latency_ms": sum(r["latency_ms"] for r in rows)/len(rows),
            "ann_is_approximate": bool(kb._ann and kb._ann.is_approximate), "rows": rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=Path("eval/retrieval-v1.json"))
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("output/eval/report.json"))
    p.add_argument("--configs", nargs="+", default=["bm25"], choices=[
        "bm25", "semantic", "hybrid-0.25", "hybrid-0.5", "hybrid-0.75",
        "hybrid-expand", "hybrid-rerank", "hybrid-both", "semantic-ann"])
    p.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--allow-remote", action="store_true", help="Enable paid embedding/LLM calls")
    p.add_argument("--embedding-env", type=Path, default=Path(".env.embedding"))
    p.add_argument("--llm-env", type=Path, default=Path(".env"))
    p.add_argument("--query-cache", type=Path, default=Path("output/eval/query-vectors.json"))
    p.add_argument("--baseline", type=Path)
    args = p.parse_args()
    if args.k <= 0 or args.k > 20:
        p.error("k must be between 1 and 20 (reranker candidate limit)")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    identity = snapshot["embedding_id"]
    remote, llm, settings = None, None, None
    if args.allow_remote and any(c != "bm25" for c in args.configs):
        from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
        settings = EmbeddingSettings.load(args.embedding_env)
        if settings.backend != "openai_compatible" or f"remote-{settings.identity}" != identity:
            p.error("Embedding configuration does not match snapshot")
        remote = RemoteEmbeddingClient(settings)
    cache = CachedEmbedding(remote, args.query_cache, identity)
    base = KnowledgeBase(FakePdfReader([]), embedding_client=cache, embedding_id=identity)
    base.load(args.snapshot)
    validate_cases(dataset["cases"], base.chunks)
    cases = [c for c in dataset["cases"] if args.split == "all" or c["split"] == args.split]
    if not cases:
        p.error("Selected split is empty")
    need_llm = any(c in {"hybrid-expand", "hybrid-rerank", "hybrid-both"} for c in args.configs)
    llm_model = None
    if need_llm:
        if not args.allow_remote:
            p.error("LLM ablations require --allow-remote; BM25 is offline")
        from deepsearch_agent.llm_config import LLMSettings
        from deepsearch_agent.llm_factory import build_llm
        llm_settings = LLMSettings.load(args.llm_env)
        if llm_settings.backend != "openai_compatible":
            p.error("Real LLM configuration required; fake results are not benchmark evidence")
        llm = build_llm(llm_settings)
        llm_model = llm_settings.model
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "dataset_sha256": digest(args.dataset), "snapshot_sha256": digest(args.snapshot),
              "dataset_status": dataset.get("status"), "split": args.split, "k": args.k,
              "embedding_id": identity, "llm_model": llm_model,
              "pipeline": "quick-QA retrieval before context truncation; not ResearchWorkflow",
              "chunk_count": len(base.chunks),
              "timing_note": "warm index; query cache hits and remote calls mixed, not a latency benchmark",
              "limitations": ["draft labels require human review", "synthetic documents, same-document dev/test",
                              "LLM outputs can vary across runs; rows retain actual queries and rankings"],
              "results": {}}
    source_files = list(Path(__file__).parent.glob("*.py"))
    report["code_sha256"] = hashlib.sha256(b''.join(
        f.name.encode()+f.read_bytes() for f in sorted(source_files))).hexdigest()
    try:
        report["git_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        report["git_revision"] = None
    try:
        for name in args.configs:
            kb = copy(base)
            kb.retrieval_mode = "bm25" if name == "bm25" else ("semantic" if name.startswith("semantic") else "hybrid")
            kb.semantic_weight = float(name.split("-")[1]) if name.startswith("hybrid-0.") else .5
            kb.ann_threshold = 1 if name == "semantic-ann" else len(kb.chunks)+1
            kb._ann, kb._ann_size = None, 0
            kb._bm25_index, kb._bm25_count = None, 0
            expander = reranker = None
            if name in {"hybrid-expand", "hybrid-both"}:
                from deepsearch_agent.query_expander import LLMQueryExpander
                expander = LLMQueryExpander(llm)
            if name in {"hybrid-rerank", "hybrid-both"}:
                from deepsearch_agent.reranker import LLMReranker
                reranker = LLMReranker(llm, llm_settings.context_window, llm_settings.max_output_tokens)
            result = evaluate(kb, cases, args.k, expander, reranker)
            result["semantic_weight"] = kb.semantic_weight
            result["ann_threshold"] = kb.ann_threshold
            if name == "semantic-ann":
                exact = copy(base)
                exact.retrieval_mode = "semantic"
                exact.ann_threshold = len(exact.chunks)+1
                exact._ann, exact._ann_size = None, 0
                overlaps = []
                for case, row in zip(cases, result["rows"], strict=True):
                    exact_hits = exact.search(case["question"], args.k)
                    def key(c):
                        return (c.source_path, c.page_number, c.chunk_index, c.text)
                    expected = {key(c) for c in exact_hits}
                    actual = {key(TextChunk(**c)) for c in row["hits"]}
                    overlaps.append(len(expected & actual)/len(expected) if expected else 1.0)
                result["ann_exact_topk_overlap"] = sum(overlaps)/len(overlaps)
                result["ann_lists"] = len(kb._ann._lists)
                result["ann_probed_lists"] = kb._ann._probe_count() if kb._ann.is_approximate else 0
            report["results"][name] = result
            print(f"{name}: Recall@{args.k}={result[f'recall@{args.k}']:.4f}, MRR@{args.k}={result[f'mrr@{args.k}']:.4f}")
        if args.baseline:
            previous = json.loads(args.baseline.read_text(encoding="utf-8"))
            for field in ("dataset_sha256", "snapshot_sha256", "split", "k", "embedding_id", "llm_model"):
                if previous[field] != report[field]:
                    raise ValueError(f"Incomparable baseline: {field}")
            report["deltas"] = {name: {metric: result[metric]-previous["results"][name][metric]
                for metric in (f"recall@{args.k}", f"mrr@{args.k}")}
                for name, result in report["results"].items() if name in previous["results"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if llm is not None:
            llm.close()


if __name__ == "__main__":
    main()
