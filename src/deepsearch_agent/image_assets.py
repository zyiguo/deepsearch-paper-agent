"""Validated raster uploads and session-owned image attachments."""
import hashlib
import io
import json
import re
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError

from deepsearch_agent.chunking import TextChunk
from deepsearch_agent.local_files import atomic_write

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}


def image_title(filename: str) -> str:
    title = filename.replace('\\', '/').rsplit('/', 1)[-1]
    return ''.join(c for c in title if c.isprintable())[:160] or 'image.png'


def normalize_image(content: bytes) -> tuple[bytes, int, int]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('图片须非空且不超过 10 MiB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as original:
                if original.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError('仅支持 PNG、JPEG、WebP 图片')
                if original.width * original.height > MAX_IMAGE_PIXELS:
                    raise ValueError('图片不得超过 2000 万像素')
                if getattr(original, 'n_frames', 1) != 1:
                    raise ValueError('请上传静态图片')
                original.load()
                image = ImageOps.exif_transpose(original).convert('RGBA')
                image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                output = io.BytesIO()
                background.save(output, format='PNG')
                data = output.getvalue()
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError('处理后的图片超过 10 MiB，请缩小后上传')
                return data, background.width, background.height
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('图片无法解码或尺寸过大，请检查文件') from None


class AttachmentStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str, ident: str, suffix: str):
        try:
            if str(UUID(session_id)) != session_id or not re.fullmatch(r'[a-f0-9]{64}', ident):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise KeyError('图片附件不存在') from None
        return self.root / session_id / f'{ident}{suffix}'

    def save(self, session_id: str, content: bytes, filename: str):
        data, width, height = normalize_image(content)
        ident = hashlib.sha256(data).hexdigest()
        target = self._path(session_id, ident, '.png')
        metadata = self._path(session_id, ident, '.json')
        if metadata.exists():
            return self.get(session_id, ident)
        title = image_title(filename)
        record = dict(id=ident, title=title, width=width, height=height,
                      source_path=f'attachment://{session_id}/{ident}/{quote(title, safe="")}',
                      image_url=f'/api/sessions/{session_id}/attachments/{ident}/image')
        atomic_write(target, data)
        atomic_write(metadata, json.dumps(record, ensure_ascii=False).encode('utf-8'))
        return record

    def get(self, session_id: str, ident: str):
        try:
            record = json.loads(self._path(session_id, ident, '.json').read_text(encoding='utf-8'))
            if record['id'] != ident or not self._path(session_id, ident, '.png').is_file():
                raise KeyError()
            expected = f'attachment://{session_id}/{ident}/{quote(record["title"], safe="")}'
            if record['source_path'] != expected:
                raise KeyError()
            return record
        except (OSError, ValueError, TypeError, KeyError):
            raise KeyError('图片附件不存在或不属于当前会话') from None

    def image(self, session_id: str, ident: str):
        self.get(session_id, ident)
        return self._path(session_id, ident, '.png')

    def resolve_visual_image(self, source: str, page: int):
        url = urlsplit(source)
        parts = url.path.strip('/').split('/')
        if url.scheme != 'attachment' or len(parts) != 2 or page != 1:
            raise KeyError('图片附件来源无效')
        record = self.get(url.netloc, parts[0])
        if record['source_path'] != source:
            raise KeyError('图片附件来源不匹配')
        return self.image(url.netloc, parts[0])

    def evidence(self, session_id: str, identifiers: list[str]):
        from deepsearch_agent.content_identity import image_content_id
        records = [self.get(session_id, ident) for ident in identifiers]
        chunks = [TextChunk(text=f'User attached image: {record["title"]}', source_path=record['source_path'],
                            page_number=1, chunk_index=0, evidence_kind='image', visual_pages=[1],
                            content_id=image_content_id(self.image(session_id, record['id']).read_bytes()))
                  for record in records]
        return records, chunks

    def delete_session(self, session_id: str):
        directory = self._path(session_id, '0' * 64, '.png').parent
        if not directory.exists():
            return
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise ValueError('Invalid attachment directory')
        for path in directory.iterdir():
            if re.fullmatch(r'[a-f0-9]{64}\.(png|json)', path.name) and path.is_file():
                path.unlink()
        if not any(directory.iterdir()):
            directory.rmdir()
