"""Deployment governance defaults; internal cost units are not currency prices."""
from dataclasses import dataclass
import os
from pathlib import Path
import math

from dotenv import dotenv_values


@dataclass(frozen=True)
class GovernanceSettings:
    user_daily_units: int | None = None
    workspace_daily_units: int | None = None
    global_research: int = 8
    workspace_research: int = 4
    user_research: int = 4
    global_ingestion: int = 4
    workspace_ingestion: int = 4
    user_ingestion: int = 4
    global_provider: int = 8
    workspace_provider: int = 4
    user_provider: int = 4
    llm_unit_rate: int = 1
    embedding_unit_rate: int = 1
    image_embedding_unit_rate: int = 1
    image_reserve_tokens: int = 8192
    unknown_alert_seconds: int = 300
    alert_for_seconds: int = 60
    failure_min_samples: int = 5
    failure_ratio: float = .25
    slow_provider_seconds: float = 30
    slow_research_seconds: float = 120

    def __post_init__(self):
        for name in ('user_daily_units', 'workspace_daily_units'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= 10**12):
                raise ValueError('Invalid governance daily quota')
        for name, value in vars(self).items():
            if name.startswith(('global_', 'workspace_', 'user_')) and not name.endswith('daily_units'):
                if type(value) is not int or not 1 <= value <= 128:
                    raise ValueError('Invalid governance concurrency')
            if name.endswith('_rate') and (type(value) is not int or not 0 <= value <= 100000):
                raise ValueError('Invalid governance unit rate')
        if (not 0 < self.failure_ratio <= 1 or not 1 <= self.failure_min_samples <= 10000
                or not 0 <= self.alert_for_seconds <= 86400 or not 1 <= self.unknown_alert_seconds <= 86400
                or not 1 <= self.image_reserve_tokens <= 1000000):
            raise ValueError('Invalid governance alert/reservation settings')
        if any(not math.isfinite(value) or not 0<value<=86400 for value in (self.slow_provider_seconds,self.slow_research_seconds)):
            raise ValueError('Invalid governance latency threshold')

    @classmethod
    def load(cls, path):
        values = dict(os.environ)
        if Path(path).is_file():
            values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
        kwargs = {}
        try:
            for name in cls.__dataclass_fields__:
                key = 'GOVERNANCE_' + name.upper()
                if key in values:
                    kwargs[name] = float(values[key]) if name in {'failure_ratio','slow_provider_seconds','slow_research_seconds'} else int(values[key])
            return cls(**kwargs)
        except (ValueError, TypeError):
            raise ValueError('Invalid .env.governance configuration') from None
