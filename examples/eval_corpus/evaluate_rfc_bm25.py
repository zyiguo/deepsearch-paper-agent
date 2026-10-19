"""Offline BM25 retrieval eval on the RFC text-PDF corpus.

Builds a BM25-only index from output/eval-corpus/pdf, locates gold pages by
support quotes, then scores Recall@k / MRR@k with the shared evaluation helpers.
No embedding or LLM calls.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pypdf import PdfReader

from deepsearch_experiments.retrieval import evaluate, score
from deepsearch_agent.knowledge.knowledge_base import KnowledgeBase
from deepsearch_agent.knowledge.retrieval_pipeline import retrieve

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF_DIR = ROOT / "output" / "eval-corpus" / "pdf"
DEFAULT_QUESTIONS = Path(__file__).with_name("sample_questions.json")


class TextPdfReader:
    def read(self, path: str) -> list[str]:
        return [page.extract_text() or "" for page in PdfReader(path).pages]


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def gold_pages(pages: list[str], quote: str) -> list[int]:
    needle = compact(quote)
    if not needle:
        return []
    hits = [i + 1 for i, page in enumerate(pages) if needle in compact(page)]
    if hits:
        return hits
    # PDF extraction may hyphenate or insert spaces; try distinctive fragments.
    fragments = [compact(p) for p in re.split(r"[\n;,]|(?:\.\s+)", quote) if len(compact(p)) >= 16]
    for frag in sorted(fragments, key=len, reverse=True):
        hits = [i + 1 for i, page in enumerate(pages) if frag in compact(page)]
        if hits:
            return hits
    # Last resort: rare tokens from the quote co-occurring on one page.
    tokens = [t for t in re.findall(r"[A-Za-z]{4,}", quote)
              if t.lower() not in {"that", "this", "with", "from", "have", "been"}]
    if len(tokens) >= 2:
        lowered = tokens[:4]
        return [
            i + 1
            for i, page in enumerate(pages)
            if all(token.lower() in page.lower() for token in lowered)
        ][:3]
    return []


def build_cases(pdf_dir: Path, questions_path: Path) -> tuple[list[dict], dict]:
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    page_cache: dict[str, list[str]] = {}
    cases = []
    skipped = []
    for item in questions:
        docs = item["document"] if isinstance(item["document"], list) else [item["document"]]
        relevant = []
        for doc in docs:
            pdf = pdf_dir / doc
            if not pdf.exists():
                skipped.append({"id": item["id"], "reason": f"missing pdf {doc}"})
                continue
            if doc not in page_cache:
                page_cache[doc] = TextPdfReader().read(str(pdf))
            pages = gold_pages(page_cache[doc], item["support_quote"])
            if not pages:
                skipped.append({"id": item["id"], "reason": f"quote not found in {doc}"})
                continue
            # One gold page per document is enough under Hit@k == Recall@k.
            relevant.append({
                "source_path": str(pdf),
                "page_number": pages[0],
                "evidence": item["support_quote"],
                "all_gold_pages": pages,
            })
        if not relevant:
            continue
        split = "dev" if int(item["id"].rsplit("-", 1)[-1], 10) % 2 == 0 else "test"
        cases.append({
            "id": item["id"],
            "question": item["question"],
            "split": split,
            "relevant": relevant,
            "note": item.get("note", ""),
        })
    meta = {
        "pages": {doc: len(pages) for doc, pages in page_cache.items()},
        "skipped": skipped,
    }
    return cases, meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "eval" / "rfc-bm25.json")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    pdfs = sorted(args.pdf_dir.glob("rfc*-text.pdf"))
    if not pdfs:
        raise SystemExit(
            f"No RFC PDFs in {args.pdf_dir}. "
            "Run: python examples/eval_corpus/build_text_pdfs.py --fetch"
        )

    reader = TextPdfReader()
    kb = KnowledgeBase(reader, chunk_size=600, overlap=120,
                       embedding_client=None, embedding_id="bm25-only",
                       retrieval_mode="bm25")
    for pdf in pdfs:
        chunks = kb.ingest_pdf(str(pdf))
        print(f"ingested {pdf.name}: {len(chunks)} chunks")

    cases, meta = build_cases(args.pdf_dir, args.questions)
    if not cases:
        raise SystemExit("No usable gold labels; check PDFs and support quotes.")

    # Warm BM25 then score with the shared pipeline evaluator.
    kb._lexical_index()
    report = evaluate(kb, cases, k=args.k)
    report.update({
        "corpus": "ietf-rfc-text-pdfs",
        "pdfs": [p.name for p in pdfs],
        "page_counts": meta["pages"],
        "skipped": meta["skipped"],
        "split_counts": {
            "dev": sum(1 for c in cases if c["split"] == "dev"),
            "test": sum(1 for c in cases if c["split"] == "test"),
        },
        "limitations": [
            "BM25 only; no embeddings or LLM expansion/rerank",
            "Gold pages located by support_quote matching; human review still required",
            "Sample set is tiny; not a production retrieval claim",
        ],
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cases": cases,
        "report": report,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "cases": report["count"],
        f"recall@{args.k}": round(report[f"recall@{args.k}"], 4),
        f"mrr@{args.k}": round(report[f"mrr@{args.k}"], 4),
        "candidate_recall": round(report["candidate_recall"], 4),
        "output": str(args.output),
        "skipped": meta["skipped"],
    }, ensure_ascii=False, indent=2))
    for row in report["rows"]:
        print(f"  {row['id']}: recall={row['recall']:.2f} rr={row['rr']:.2f}  {row['question'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
