from deepsearch_agent.pdf import FakePdfReader, PdfReader


def test_fake_pdf_reader_returns_configured_pages() -> None:
    reader: PdfReader = FakePdfReader(pages=["第一页", "第二页"])

    assert isinstance(reader, PdfReader)
    assert reader.read("some.pdf") == ["第一页", "第二页"]