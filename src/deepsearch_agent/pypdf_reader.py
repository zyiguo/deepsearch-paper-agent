"""Real PDF reader backed by pypdf, implementing the PdfReader protocol."""

import pypdf


class PyPdfReader:
    def read(self, path: str) -> list[str]:
        reader = pypdf.PdfReader(path)
        return [page.extract_text() or "" for page in reader.pages]