"""Independent limits for the model-directed research tool loop."""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    evidence_control_enabled: bool = False
    evidence_policy_version: str = 'v1'
    max_tool_calls: int = 18
    max_steps_per_round: int = 8
    max_ocr_calls: int = 2
    max_evidence: int = 30
    max_images: int = 8
    timeout_seconds: float = 300
    observation_chars: int = 12000

    def __post_init__(self):
        if self.evidence_policy_version not in {'v1', 'v2', 'v3', 'v4', 'v5'}:
            raise ValueError('AGENT_EVIDENCE_POLICY_VERSION must be v1, v2, v3, v4 or v5')
        if type(self.enabled) is not bool or type(self.evidence_control_enabled) is not bool:
            raise ValueError('Invalid agent enabled flag')
        for name, low, high in [('max_tool_calls', 1, 64), ('max_steps_per_round', 1, 24),
                               ('max_ocr_calls', 0, 8), ('max_evidence', 1, 100),
                               ('max_images', 1, 16), ('observation_chars', 1000, 32000)]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid agent limit: {name}')
        if (type(self.timeout_seconds) not in (int, float)
                or not 1 <= self.timeout_seconds <= 1800):
            raise ValueError('Invalid agent timeout')

    @classmethod
    def load(cls, path: Path, *, environ: Mapping[str, str] | None = None):
        values = dict(os.environ if environ is None else environ)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
        flag = values.get('AGENTIC_RAG_ENABLED', 'true').strip().lower()
        if flag not in {'true', 'false'}:
            raise ValueError('AGENTIC_RAG_ENABLED must be true or false')
        defaults = cls()
        kwargs = {'enabled': flag == 'true'}
        control = values.get('AGENT_EVIDENCE_CONTROL_ENABLED', 'false').strip().lower()
        if control not in {'true', 'false'}:
            raise ValueError('AGENT_EVIDENCE_CONTROL_ENABLED must be true or false')
        kwargs['evidence_control_enabled'] = control == 'true'
        kwargs['evidence_policy_version'] = values.get('AGENT_EVIDENCE_POLICY_VERSION', 'v1').strip().lower()
        for name in ('max_tool_calls', 'max_steps_per_round', 'max_ocr_calls', 'max_evidence',
                     'max_images', 'observation_chars'):
            kwargs[name] = int(values.get('AGENT_' + name.upper(), getattr(defaults, name)))
        kwargs['timeout_seconds'] = float(values.get('AGENT_TIMEOUT_SECONDS', defaults.timeout_seconds))
        return cls(**kwargs)
