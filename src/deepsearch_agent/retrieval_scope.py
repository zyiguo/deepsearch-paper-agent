"""Resolve request document IDs against indexed PDF sources, before paid calls."""
import re
from fastapi import HTTPException


def resolve_sources(state, document_ids: list[str] | None) -> set[str] | None:
    if state.demo_retrieval and document_ids is None:
        return None  # Preserve the isolated demo/testing API.
    available = {}
    image_index = getattr(state.kb, 'image_index', None)
    image_chunks = image_index.chunks if image_index is not None else []
    for chunk in [*state.kb.chunks, *image_chunks]:
        match = re.fullmatch(r'(?:pdf|image)://([a-f0-9]{64})/.+', chunk.source_path)
        if match:
            available.setdefault(match[1], set()).add(chunk.source_path)
    if document_ids is None:
        if not available:
            return None  # 库中还没有任何可选 PDF（如仅导入过演示数据），不强制选择。
        ids = set(available)
    else:
        ids = set(document_ids)
    if not ids:
        raise HTTPException(422, '请至少选择一份已入库资料。')
    if not ids <= available.keys():
        raise HTTPException(422, '所选文献不存在或尚未入库，请刷新文献列表重新选择。')
    return {source for ident in ids for source in available[ident]}
