"""Convert public-domain / freely redistributable plain-text RFCs into text PDFs.

Sources are IETF RFC Editor plain-text files (no figures). Output PDFs stay
within the app limits when the RFC body is under ~100k characters.
Network is only used if --fetch is passed; otherwise use local .txt files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = Path(__file__).with_name("sources")
DEFAULT_OUT = ROOT / "output" / "eval-corpus" / "pdf"

# Short, fact-dense RFCs that fit upload limits after typesetting.
# Skip RFCs whose raw text already exceeds 100k chars (e.g. 6749 OAuth, 3986 URI).
DEFAULT_IDS = [
    "rfc8259",  # JSON
    "rfc7519",  # JWT
    "rfc7232",  # HTTP conditional requests
]


def fetch_rfc(rfc_id: str, dest: Path) -> None:
    url = f"https://www.rfc-editor.org/rfc/{rfc_id}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        dest.write_bytes(response.read())


def normalize_rfc_text(text: str) -> list[tuple[str, list[str]]]:
    """Return list of (heading, paragraphs). Drop page-break form feeds."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\f", "\n\n")
    # Strip common RFC page furniture lines.
    lines = []
    for line in text.split("\n"):
        if re.match(r"^\s*\[Page \d+\]\s*$", line):
            continue
        if re.match(r"^\s*RFC \d+\s+.+\s+\d{4}\s*$", line):
            continue
        lines.append(line.rstrip())
    body = "\n".join(lines).strip()

    # First non-empty block is the RFC header banner; keep title from first line.
    title = next((ln.strip() for ln in body.split("\n") if ln.strip()), "RFC")
    title = re.sub(r"\s+", " ", title)[:80]

    # Split on form-feed leftovers already removed; use blank-line paragraphs.
    chunks: list[tuple[str, list[str]]] = []
    current_heading = title
    buffer: list[str] = []

    def flush():
        nonlocal buffer
        paragraphs = []
        for raw in "\n".join(buffer).strip().split("\n\n"):
            para = re.sub(r"[ \t]+", " ", raw.strip())
            para = re.sub(r"\n+", " ", para)
            if para:
                paragraphs.append(para)
        if paragraphs:
            chunks.append((current_heading, paragraphs))
        buffer = []

    for line in lines:
        if not line.strip():
            if buffer:
                # keep going; blank ends paragraph handled later
                buffer.append("")
            continue
        # Section headings: "1. Introduction" / "3.2.1 Foo"
        if re.match(r"^\d+(\.\d+)*\.?\s+\S", line.strip()) and len(line.strip()) < 80:
            flush()
            current_heading = line.strip()
            continue
        buffer.append(line.strip())
    flush()
    if not chunks:
        chunks = [(title, [body])]
    return chunks


def build_pdf(rfc_id: str, src: Path, out_dir: Path) -> dict:
    text = src.read_text(encoding="utf-8", errors="replace")
    compact_len = len(re.sub(r"\s+", "", text))
    if compact_len > 100_000:
        raise ValueError(f"{rfc_id}: {compact_len} chars exceed app limit 100000")

    font_candidates = [
        ("Helvetica", None),
    ]
    # Prefer a monospaced-ish system font if present for ASCII fidelity.
    for name, path, index in [
        ("Consola", "C:/Windows/Fonts/consola.ttf", 0),
        ("CourierNew", "C:/Windows/Fonts/cour.ttf", 0),
    ]:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=index))
                font_candidates.insert(0, (name, None))
                break
            except Exception:
                pass

    font_name = font_candidates[0][0]
    body = ParagraphStyle(
        "Body",
        fontName=font_name,
        fontSize=9,
        leading=13,
        spaceAfter=6,
    )
    heading = ParagraphStyle(
        "Heading",
        fontName=font_name,
        fontSize=12,
        leading=16,
        spaceBefore=10,
        spaceAfter=8,
    )
    small = ParagraphStyle(
        "Small",
        fontName=font_name,
        fontSize=8,
        leading=11,
        spaceAfter=12,
    )

    def escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    sections = normalize_rfc_text(text)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{rfc_id}-text.pdf"
    story = []
    story.append(Paragraph(escape(rfc_id.upper()), small))
    story.append(Paragraph(escape(sections[0][0]), heading))
    for index, (head, paragraphs) in enumerate(sections):
        if index:
            story.append(Paragraph(escape(head), heading))
        for para in paragraphs:
            story.append(Paragraph(escape(para), body))
        story.append(Spacer(1, 4))

    def footer(canvas, document):
        canvas.setFont(font_name, 8)
        canvas.drawCentredString(A4[0] / 2, 28, f"{rfc_id}  ·  {document.page}")

    SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=48,
        rightMargin=48,
        topMargin=42,
        bottomMargin=42,
        title=rfc_id.upper(),
        author="IETF RFC Editor (text converted for local retrieval tests)",
    ).build(story, onFirstPage=footer, onLaterPages=footer)

    reader = PdfReader(str(target))
    pages = len(reader.pages)
    extracted = sum(len(page.extract_text() or "") for page in reader.pages)
    if pages > 100:
        raise ValueError(f"{rfc_id}: typeset {pages} pages exceed limit 100")
    return {
        "id": rfc_id,
        "pdf": str(target),
        "pages": pages,
        "extracted_chars": extracted,
        "source_chars": compact_len,
        "bytes": target.stat().st_size,
        "license": "IETF Trust (RFC text)",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="Download missing RFC .txt")
    parser.add_argument("--ids", nargs="*", default=DEFAULT_IDS)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    args.src.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rfc_id in args.ids:
        src = args.src / f"{rfc_id}.txt"
        if not src.exists():
            if not args.fetch:
                print(f"skip {rfc_id}: no local source (pass --fetch)", file=sys.stderr)
                continue
            print(f"fetch {rfc_id}")
            fetch_rfc(rfc_id, src)
        try:
            row = build_pdf(rfc_id, src, args.out)
        except ValueError as error:
            print(f"reject {rfc_id}: {error}", file=sys.stderr)
            continue
        manifest.append(row)
        print(json.dumps(row, ensure_ascii=False))

    path = args.out / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest {path}")
    return 0 if manifest else 1


if __name__ == "__main__":
    raise SystemExit(main())
