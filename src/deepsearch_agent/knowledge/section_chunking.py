from deepsearch_agent.core.chunking_config import SectionConfig
"""Conservative text-line headings, bounded structural chunks, exact page offsets."""
from dataclasses import asdict
import re

from deepsearch_agent.core.evidence import TextChunk




STANDARD = {'abstract', 'introduction', 'background', 'related work', 'methods', 'method', 'methodology',
            'materials and methods', 'experiments', 'experimental setup', 'results', 'discussion',
            'results and discussion', 'conclusion', 'conclusions', 'references', 'acknowledgments',
            'acknowledgements', '摘要', '引言', '绪论', '背景', '相关工作', '研究方法', '实验设置',
            '实验结果', '结果与讨论', '讨论', '结论', '参考文献', '致谢'}
NUMBERED = re.compile(r'^(?P<number>[1-9]\d?(?:\.\d{1,2}){0,4})\.?\s+(?P<title>\S.*)$')
CHINESE = re.compile(r'^第[一二三四五六七八九十百零〇\d]+(?P<unit>章|节)\s*(?P<title>\S.*)$')
EXCLUDE = re.compile(r'^(?:\[\d+\]|(?:fig(?:ure)?\.?|table|equation)\s*\d|[图表式]\s*\d)', re.I)
SENTENCE = re.compile(r'[。！？；!?;][”’」』]?\s*|[.]\s+')


def heading(line: str) -> tuple[int, str] | None:
    text = line.strip()
    if not text or len(text) > 100 or EXCLUDE.match(text):
        return None
    if re.search(r'\.{2,}|…|\s{2,}\d+\s*$|[=<>+]|[。！？；;!?]$', text):
        return None
    if text.casefold() in STANDARD:
        return 1, text
    match = CHINESE.fullmatch(text)
    if match:
        return (1 if match['unit'] == '章' else 2), text
    match = NUMBERED.fullmatch(text)
    if not match:
        return None
    title = match['title']
    if not re.match(r'[A-Za-z\u3400-\u9fff]', title) or re.search(r'[.,:：，。；!?！？]$', title):
        return None
    if re.search(r'\s\d+$', title) or len(title.split()) > 12:
        return None
    return len(match['number'].split('.')), text


def trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end-1].isspace():
        end -= 1
    return start, end


def units(text: str, start: int, end: int, config: SectionConfig) -> list[tuple[int, int]]:
    """Paragraphs first; only oversized paragraphs need sentence/hard splitting."""
    result = []
    cursor = start
    boundaries = [m.start() for m in re.finditer(r'\n[ \t\r]*\n+', text[start:end])]
    for boundary in [start + offset for offset in boundaries] + [end]:
        a, b = trim(text, cursor, boundary)
        cursor = boundary
        if a == b:
            continue
        if b-a <= config.target_size:
            result.append((a, b))
            continue
        previous = a
        stops = [a + m.end() for m in SENTENCE.finditer(text[a:b])] + [b]
        for stop in stops:
            left, right = trim(text, previous, stop)
            previous = stop
            while right-left > config.max_size:
                result.append((left, left+config.max_size))
                left += config.max_size
            if left < right:
                result.append((left, right))
    return result


def windows(text: str, start: int, end: int, config: SectionConfig):
    pieces = units(text, start, end, config)
    i = 0
    while i < len(pieces):
        j = i+1
        while j < len(pieces) and pieces[j][1]-pieces[i][0] <= config.target_size:
            j += 1
        # Absorb a short final tail only when the hard limit remains intact.
        if j < len(pieces) and pieces[-1][1]-pieces[j][0] < config.min_tail and pieces[-1][1]-pieces[i][0] <= config.max_size:
            j = len(pieces)
        yield pieces[i][0], pieces[j-1][1]
        if j == len(pieces):
            break
        next_i = j
        # Repeat whole paragraph/sentence units, never arbitrary trailing characters.
        while next_i > i+1 and pieces[j-1][1]-pieces[next_i-1][0] <= config.overlap:
            next_i -= 1
        # Overlap must leave room for at least one new unit.
        if pieces[j][1]-pieces[next_i][0] > config.max_size:
            next_i = j
        i = next_i


def section_chunks(pages: list[str], source_path: str, config: SectionConfig) -> list[TextChunk]:
    text = '\n'.join(pages)
    page_ranges = []
    offset = 0
    for number, page in enumerate(pages, 1):
        page_ranges.append((number, offset, offset+len(page)))
        offset += len(page)+1
    boundaries = []
    stack: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        found = heading(line)
        if found:
            level, title = found
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            boundaries.append((offset, [name for _, name in stack]))
        offset += len(line)
    sections = []
    if boundaries:
        if boundaries[0][0] > 0:
            sections.append((0, boundaries[0][0], []))
        for i, (start, path) in enumerate(boundaries):
            sections.append((start, boundaries[i+1][0] if i+1 < len(boundaries) else len(text), path))
    else:
        sections = [(start, end, []) for _, start, end in page_ranges]
    chunks = []
    for start, end, path in sections:
        for a, b in windows(text, start, end, config):
            spans = []
            for number, page_start, page_end in page_ranges:
                left, right = max(a, page_start), min(b, page_end)
                if left < right:
                    spans.append(dict(page_number=number, start=left-page_start, end=right-page_start,
                                      chunk_start=left-a, chunk_end=right-a))
            if not spans:
                continue
            chunk = TextChunk(text=text[a:b], source_path=source_path, page_number=spans[0]['page_number'],
                              chunk_index=len(chunks), section_path=list(path), page_end=spans[-1]['page_number'],
                              page_spans=spans, chunking=asdict(config))
            chunk.validate_structure()
            chunks.append(chunk)
    return chunks
