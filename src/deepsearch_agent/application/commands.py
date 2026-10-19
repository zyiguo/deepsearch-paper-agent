"""Validated at entry adapters; saved turn inputs have no HTTP dependency."""
from dataclasses import dataclass, field, asdict, fields


@dataclass
class TurnCommand:
    question: str
    top_k: int = 3
    document_ids: list[str] | None = None
    attachment_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_record(cls, record):
        return cls(**{f.name: record[f.name] for f in fields(cls) if f.name in record})
