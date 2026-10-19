"""Stable structural chunk metadata, independent of segmentation algorithms."""
from dataclasses import dataclass

@dataclass(frozen=True)
class SectionConfig:
    strategy: str = 'section-rules-v1'
    target_size: int = 600
    overlap: int = 120
    max_size: int = 900
    min_tail: int = 120

    def __post_init__(self):
        if self.strategy != 'section-rules-v1':
            raise ValueError('不支持的结构分块版本')
        if any(type(v) is not int for v in (self.target_size, self.overlap, self.max_size, self.min_tail)):
            raise ValueError('分块长度必须是整数')
        if not (0 <= self.overlap < self.target_size <= self.max_size and 0 <= self.min_tail <= self.target_size):
            raise ValueError('结构分块参数无效')
