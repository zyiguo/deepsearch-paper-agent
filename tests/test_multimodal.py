from pathlib import Path

import pytest

from deepsearch_agent.multimodal import MultimodalDocumentProcessor, PageAnalysis, PageAnalysisCache, VisualItem


class Analyzer:
    identity = 'abc123'

    def __init__(self):
        self.calls = []
        self.fail_on = None

    def analyze(self, image, native_text):
        self.calls.append(image)
        if image == self.fail_on:
            raise RuntimeError('Temporary failure')
        return PageAnalysis(ocr_text='Scanned measurement: 42 volts.' if not native_text else '',
                            items=[VisualItem(kind='table', label='Table 1', description='| Voltage |\n| 42 V |')],
                            warnings=['Tiny label unreadable'])


def test_prepares_scan_and_native_text_with_original_image_references(tmp_path):
    analyzer = Analyzer()
    processor = MultimodalDocumentProcessor(analyzer, PageAnalysisCache(tmp_path))
    native = ['2 Results\nNative text stays intact.', '']
    result = processor.prepare(native, 'paper.pdf', 'a' * 64, lambda n: Path(f'page-{n}.png'))
    assert native[1] == ''
    assert result.pages == [native[0], 'Scanned measurement: 42 volts.']
    assert len([c for c in result.chunks if c.evidence_kind == 'table']) == 2
    assert any(c.evidence_kind == 'ocr' and 2 in c.visual_pages for c in result.chunks)
    for chunk in result.chunks:
        chunk.validate_structure()
    assert result.warnings == ['第 1 页：Tiny label unreadable', '第 2 页：Tiny label unreadable']


def test_partial_failure_reuses_successful_pages_and_invalidates_for_new_model(tmp_path):
    analyzer = Analyzer()
    analyzer.fail_on = Path('page-2.png')
    processor = MultimodalDocumentProcessor(analyzer, PageAnalysisCache(tmp_path))
    with pytest.raises(RuntimeError):
        processor.prepare(['', ''], 'paper.pdf', 'b' * 64, lambda n: Path(f'page-{n}.png'))
    analyzer.fail_on = None
    processor.prepare(['', ''], 'paper.pdf', 'b' * 64, lambda n: Path(f'page-{n}.png'))
    assert analyzer.calls.count(Path('page-1.png')) == 1
    analyzer.identity = 'def456'
    processor.prepare(['', ''], 'paper.pdf', 'b' * 64, lambda n: Path(f'page-{n}.png'))
    assert analyzer.calls.count(Path('page-1.png')) == 2


def test_blank_unreadable_pdf_does_not_create_evidence(tmp_path):
    class Empty:
        identity = '123'
        def analyze(self, image, native_text):
            return PageAnalysis(ocr_text='', items=[], warnings=['Unreadable'])
    processor = MultimodalDocumentProcessor(Empty(), PageAnalysisCache(tmp_path))
    with pytest.raises(ValueError, match='没有识别'):
        processor.prepare([''], 'paper.pdf', 'a' * 64, lambda n: Path('page.png'))


def test_cache_rejects_paths_outside_its_root(tmp_path):
    with pytest.raises(ValueError):
        PageAnalysisCache(tmp_path).load('../escape', '123', 1)


def test_scan_regions_with_native_header_are_not_silently_omitted(tmp_path):
    class MixedPage:
        identity = 'abc'
        def analyze(self, image, native_text):
            return PageAnalysis(ocr_text='Scanned body: Voltage 42 V.', items=[], warnings=[])
    processor = MultimodalDocumentProcessor(MixedPage(), PageAnalysisCache(tmp_path))
    result = processor.prepare(['A selectable native header'], 'paper.pdf', 'a' * 64, lambda n: Path('page.png'))
    assert 'A selectable native header' in result.pages[0]
    assert 'Scanned body' in result.pages[0]
    assert all(c.evidence_kind == 'ocr' and c.visual_pages == [1] for c in result.chunks)
