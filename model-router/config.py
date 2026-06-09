"""
Model Router Configuration Loader

Reads config.yaml (or path from MODEL_ROUTER_CONFIG_PATH env var) and
exposes typed configuration objects to the rest of the application.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LLMConfig:
    name: str
    base_url: str
    model: str
    provider: str  # "openai" | "anthropic" | "openai_compatible"
    api_key_env: str
    timeout: int = 60
    enabled: bool = True

    @property
    def api_key(self) -> Optional[str]:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)


@dataclass
class CollectorConfig:
    url: str = "http://collector:8001"
    timeout: int = 5
    capture_primary: bool = True
    primary_name: str = "llama-stack-primary"
    primary_url: str = "http://llama-stack:8321/v1"
    primary_model_env: str = "PRIMARY_MODEL"
    primary_model_default: str = "meta-llama/Llama-3.1-8B-Instruct"

    @property
    def primary_model(self) -> str:
        return os.environ.get(self.primary_model_env, self.primary_model_default)


@dataclass
class AppConfig:
    llms: List[LLMConfig] = field(default_factory=list)
    collector: CollectorConfig = field(default_factory=CollectorConfig)

    @property
    def enabled_llms(self) -> List[LLMConfig]:
        return [llm for llm in self.llms if llm.enabled]


def load_config(path: Optional[str] = None) -> AppConfig:
    config_path = path or os.environ.get(
        "MODEL_ROUTER_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "config.yaml"),
    )

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    llms = [LLMConfig(**entry) for entry in raw.get("llms", [])]

    collector_raw = raw.get("collector", {})
    collector = CollectorConfig(**collector_raw)

    return AppConfig(llms=llms, collector=collector)
