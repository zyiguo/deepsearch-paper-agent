from pathlib import Path

from reportlab.pdfgen import canvas

from deepsearch_agent.pypdf_reader import PyPdfReader


def _write_two_page_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "Page one text")
    c.showPage()
    c.drawString(72, 720, "Page two text")
    c.showPage()
    c.save()


def test_pypdf_reader_extracts_text_per_page(tmp_path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _write_two_page_pdf(pdf_path)

    pages = PyPdfReader().read(str(pdf_path))

    assert len(pages) == 2
    assert "Page one text" in pages[0]
    assert "Page two text" in pages[1]