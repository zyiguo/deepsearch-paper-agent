"""Token estimation for prompt budget management (no external dependency).

The heuristic is deliberately conservative: it overestimates token counts
slightly, which is safe for budget enforcement — better to include fewer
chunks than overflow the model's context window.
"""
from math import ceil


def estimate_tokens(text: str) -> int:
    """Estimate token count for mixed CJK/ASCII text.

    CJK characters ≈ 1 token each (actual 0.5–1, so this overestimates).
    ASCII characters ≈ 4 chars per token (standard estimate, slightly
    conservative).  The result is rounded up via ceil.
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    return ceil(cjk + other * 0.25)


def truncate_text(text: str, max_tokens: int) -> str:
    """Truncate *text* to approximately *max_tokens* tokens.

    Cuts at a sentence boundary (。！？；.!?;) when possible; falls back
    to a hard character cut.  An ellipsis marker is appended if truncation
    occurred so the model knows the passage continues.
    """
    if max_tokens <= 0 or not text:
        return ''

    # Upper-bound character count: 1 CJK char = 1 token, 4 ASCII = 1 token.
    # Using max_tokens directly as the char budget is safe because every
    # character costs at least 0.25 tokens, so max_tokens chars can never
    # exceed max_tokens tokens.
    if len(text) <= max_tokens:
        return text

    candidate = text[:max_tokens]

    # Try to cut at the last sentence boundary within the candidate window.
    boundary = max(
        candidate.rfind('。'),
        candidate.rfind('！'),
        candidate.rfind('？'),
        candidate.rfind('；'),
        candidate.rfind('. '),
        candidate.rfind('! '),
        candidate.rfind('? '),
        candidate.rfind('; '),
    )
    if boundary > max_tokens // 2:
        candidate = candidate[:boundary + 1]

    return candidate.rstrip() + '…'
