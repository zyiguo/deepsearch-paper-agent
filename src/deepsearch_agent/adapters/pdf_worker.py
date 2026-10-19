"""Bounded subprocess tasks for untrusted PDF parsing/rendering; no network."""
import json
import sys
from pathlib import Path


def main():
    operation, source, target = sys.argv[1:4]
    if operation in {'extract', 'extract-visual'}:
        from pypdf import PdfReader
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ValueError('加密 PDF 暂不支持，请先在本机解密')
        if not 1 <= len(reader.pages) <= 100:
            raise ValueError('PDF 须为 1 至 100 页')
        pages = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ''
            total += len(text)
            if total > 100_000:
                raise ValueError('提取文本超过 100000 字符，请拆分 PDF')
            pages.append(text)
        if operation == 'extract' and sum(len(''.join(text.split())) for text in pages) < 10:
            raise ValueError('没有足够的可提取文字，扫描件需要 OCR，本轮暂不支持')
        Path(target).write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8')
    elif operation in {'render', 'render-vision'}:
        import pypdfium2 as pdfium
        page_number = int(sys.argv[4])
        with pdfium.PdfDocument(source) as doc:
            page = doc[page_number - 1]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise ValueError('PDF 页面尺寸无效')
                scale, max_side = (3, 3200) if operation == 'render-vision' else (1.5, 1800)
                bitmap = page.render(scale=min(scale, max_side / max(width, height)))
                try:
                    bitmap.to_pil().save(target, format='PNG')
                finally:
                    bitmap.close()
            finally:
                page.close()
    else:
        raise ValueError('未知 PDF 操作')


def cli():
    try:
        main()
    except ValueError as error:
        # Only allow our fixed validation messages, not parser-supplied text.
        allowed = ('加密 PDF', 'PDF 须为', '提取文本超过', '没有足够的', 'PDF 页面尺寸')
        message = str(error) if str(error).startswith(allowed) else 'PDF 无法解析或渲染，请检查文件是否损坏'
        print(json.dumps({'error': message}, ensure_ascii=True))
        sys.exit(1)
    except Exception:
        print(json.dumps({'error': 'PDF 无法解析或渲染，请检查文件是否损坏'}, ensure_ascii=True))
        sys.exit(1)


if __name__ == '__main__':
    cli()
