"""Corpus retrieval eval: BM25 offline, optional hybrid/rerank with --allow-remote.

Default corpus: RFC text PDFs + ACL BERT + NeurIPS RAG (local under output/).
"""
from __future__ import annotations

import argparse
import json
import re
from copy import copy
from pathlib import Path

from pypdf import PdfReader

from deepsearch_agent.evaluation import evaluate
from deepsearch_agent.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


class TextPdfReader:
    def read(self, path: str) -> list[str]:
        return [page.extract_text() or "" for page in PdfReader(path).pages]


def compact(text: str) -> str:
    # Join soft hyphens from PDF line wraps, then drop remaining whitespace.
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"‐|‑|–|—", "-", text)
    return re.sub(r"\s+", "", text)


def gold_pages(pages: list[str], quote: str) -> list[int]:
    needle = compact(quote)
    if not needle:
        return []
    hits = [i + 1 for i, page in enumerate(pages) if needle in compact(page)]
    if hits:
        return hits
    # Also try without hyphens (BERT PDF keeps Param-eters).
    needle2 = needle.replace("-", "")
    hits = [i + 1 for i, page in enumerate(pages) if needle2 in compact(page).replace("-", "")]
    if hits:
        return hits
    fragments = [compact(p) for p in re.split(r"[\n;,]|(?:\.\s+)", quote) if len(compact(p)) >= 16]
    for frag in sorted(fragments, key=len, reverse=True):
        for n in (frag, frag.replace("-", "")):
            hits = [i + 1 for i, page in enumerate(pages)
                    if n in compact(page).replace("-", "")]
            if hits:
                return hits
    return []


def build_cases(pdf_paths: list[Path], questions_paths: list[Path]) -> tuple[list[dict], list]:
    page_cache: dict[str, list[str]] = {}
    by_name = {p.name: p for p in pdf_paths}
    cases, skipped = [], []
    for qpath in questions_paths:
        for item in json.loads(qpath.read_text(encoding="utf-8")):
            docs = item["document"] if isinstance(item["document"], list) else [item["document"]]
            relevant = []
            for doc in docs:
                pdf = by_name.get(doc)
                if pdf is None:
                    skipped.append({"id": item["id"], "reason": f"missing pdf {doc}"})
                    continue
                if doc not in page_cache:
                    page_cache[doc] = TextPdfReader().read(str(pdf))
                pages = gold_pages(page_cache[doc], item["support_quote"])
                if not pages:
                    skipped.append({"id": item["id"], "reason": f"quote not found in {doc}"})
                    continue
                relevant.append({
                    "source_path": str(pdf),
                    "page_number": pages[0],
                    "evidence": item["support_quote"],
                    "all_gold_pages": pages,
                })
            if not relevant:
                continue
            num = int(re.search(r"(\d+)$", item["id"]).group(1))
            cases.append({
                "id": item["id"],
                "question": item["question"],
                "split": "dev" if num % 2 == 0 else "test",
                "relevant": relevant,
                "note": item.get("note", ""),
            })
    return cases, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers-dir", type=Path, default=ROOT / "output" / "eval-corpus" / "papers")
    parser.add_argument("--rfc-dir", type=Path, default=ROOT / "output" / "eval-corpus" / "pdf")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "eval" / "corpus-ablation.json")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--configs", nargs="+",
                        default=["bm25"],
                        choices=["bm25", "semantic", "hybrid-0.25", "hybrid-0.5", "hybrid-0.75",
                                 "hybrid-rerank", "hybrid-expand", "hybrid-both"])
    parser.add_argument("--allow-remote", action="store_true",
                        help="Permit paid embedding/LLM calls for non-BM25 configs")
    parser.add_argument("--chunk-size", type=int, default=600)
    parser.add_argument("--overlap", type=int, default=120)
    args = parser.parse_args()

    pdfs = sorted(args.rfc_dir.glob("rfc*-text.pdf")) + [
        args.papers_dir / "acl-bert-2019.pdf",
        args.papers_dir / "rag-lewis-2020.pdf",
    ]
    pdfs = [p for p in pdfs if p.exists()]
    if len(pdfs) < 2:
        raise SystemExit("Need RFC and/or paper PDFs. Build RFCs first; papers under output/eval-corpus/papers/.")

    question_files = []
    for name in ("sample_questions.json", "paper_questions.json"):
        path = HERE / name
        if path.exists():
            question_files.append(path)
    cases, skipped = build_cases(pdfs, question_files)
    if not cases:
        raise SystemExit(f"No usable cases. skipped={skipped}")

    need_remote = any(c != "bm25" for c in args.configs)
    embedding_client = None
    embedding_id = "bm25-only"
    if need_remote:
        if not args.allow_remote:
            raise SystemExit("Non-BM25 configs require --allow-remote (paid embedding/LLM calls).")
        from deepsearch_agent.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
        settings = EmbeddingSettings.load(ROOT / ".env.embedding")
        if settings.backend != "openai_compatible":
            raise SystemExit("EMBEDDING_BACKEND must be openai_compatible")
        embedding_client = RemoteEmbeddingClient(settings)
        embedding_id = f"remote-{settings.identity}"

    reader = TextPdfReader()
    kb = KnowledgeBase(reader, chunk_size=args.chunk_size, overlap=args.overlap,
                       embedding_client=embedding_client, embedding_id=embedding_id,
                       retrieval_mode="hybrid" if need_remote else "bm25")
    for pdf in pdfs:
        chunks = kb.ingest_pdf(str(pdf))
        print(f"ingested {pdf.name}: {len(chunks)} chunks")
    print(f"total chunks={len(kb.chunks)} cases={len(cases)} skipped={len(skipped)}")

    llm = None
    llm_settings = None
    if any(c in {"hybrid-rerank", "hybrid-expand", "hybrid-both"} for c in args.configs):
        from deepsearch_agent.llm_config import LLMSettings
        from deepsearch_agent.llm_factory import build_llm
        llm_settings = LLMSettings.load(ROOT / ".env")
        if llm_settings.backend != "openai_compatible":
            raise SystemExit("LLM ablations require openai_compatible chat model")
        llm = build_llm(llm_settings)

    results = {}
    try:
        for name in args.configs:
            view = copy(kb)
            view.retrieval_mode = "bm25" if name == "bm25" else ("semantic" if name.startswith("semantic") else "hybrid")
            view.semantic_weight = float(name.split("-")[1]) if name.startswith("hybrid-0.") else 0.5
            view.ann_threshold = len(view.chunks) + 1
            view._ann, view._ann_size = None, 0
            view._bm25_index, view._bm25_count = None, 0
            expander = reranker = None
            if name in {"hybrid-expand", "hybrid-both"}:
                from deepsearch_agent.query_expander import LLMQueryExpander
                expander = LLMQueryExpander(llm)
            if name in {"hybrid-rerank", "hybrid-both"}:
                from deepsearch_agent.reranker import LLMReranker
                reranker = LLMReranker(llm, llm_settings.context_window, llm_settings.max_output_tokens)
            report = evaluate(view, cases, k=args.k, expander=expander, reranker=reranker)
            report["semantic_weight"] = view.semantic_weight
            results[name] = report
            print(f"{name}: Recall@{args.k}={report[f'recall@{args.k}']:.4f} "
                  f"MRR@{args.k}={report[f'mrr@{args.k}']:.4f} "
                  f"cand_recall={report['candidate_recall']:.4f}")
    finally:
        if llm is not None:
            llm.close()

    payload = {
        "corpus_pdfs": [p.name for p in pdfs],
        "cases": cases,
        "skipped": skipped,
        "k": args.k,
        "chunk_size": args.chunk_size,
        "overlap": args.overlap,
        "embedding_id": embedding_id,
        "results": results,
        "limitations": [
            "Gold pages from quote matching; human review still required",
            "Small sample; not a production retrieval claim",
            "Papers are copyrighted downloads for local testing only",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
