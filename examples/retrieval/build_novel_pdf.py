"""Build an original, searchable Chinese text PDF; no network calls."""
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.pagesizes import A4

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name('雾港来信.md')
OUT = ROOT / 'output' / 'pdf'
OUT.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('NovelSong', 'C:/Windows/Fonts/simsun.ttc', subfontIndex=0))
body = ParagraphStyle('Body', fontName='NovelSong', fontSize=11, leading=21,
                      wordWrap='CJK', firstLineIndent=22, spaceAfter=11)
heading = ParagraphStyle('Heading', fontName='NovelSong', fontSize=19, leading=28, spaceAfter=22)
small = ParagraphStyle('Small', fontName='NovelSong', fontSize=9, leading=15, spaceAfter=18)
text = SOURCE.read_text(encoding='utf-8')
story = []
chapters = text.split('\n## ')[1:]
for index, chapter in enumerate(chapters):
    title, content = chapter.split('\n', 1)
    if index:
        story.append(PageBreak())
    story.append(Paragraph('雾港来信', small))
    story.append(Paragraph(escape(title), heading))
    for paragraph in content.strip().split('\n\n'):
        story.append(Paragraph(escape(paragraph), body))

def footer(canvas, document):
    canvas.setFont('NovelSong', 9)
    canvas.drawCentredString(A4[0] / 2, 30, str(document.page))

target = OUT / '雾港来信-检索测试.pdf'
SimpleDocTemplate(str(target), pagesize=A4, rightMargin=55, leftMargin=55,
                  topMargin=47, bottomMargin=48, title='雾港来信',
                  author='原创检索测试文本').build(story, onFirstPage=footer, onLaterPages=footer)
pages = [page.extract_text() or '' for page in PdfReader(target).pages]
compact = lambda s: re.sub(r'\s+', '', s)
expected = compact(''.join(chapter.split('\n', 1)[1] for chapter in chapters))
actual = compact(''.join(pages))
assert all(compact(p) in actual for chapter in chapters for p in chapter.split('\n', 1)[1].strip().split('\n\n'))
assert len(pages) == 8, f'Unexpected pagination: {len(pages)}'
manifest = {'pdf': str(target), 'pages': len(pages), 'body_characters_without_whitespace': len(expected),
            'chinese_characters': len(re.findall(r'[\u4e00-\u9fff]', expected)),
            'bytes': target.stat().st_size, 'paragraph_extraction_verified': True}
(OUT / 'novel-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(manifest, ensure_ascii=False))
