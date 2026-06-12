"""
Model Router Configuration

Primary LLM is now a first-class config object, separate from secondary LLMs.
The model router is the single entry point — no Envoy required.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LlamaStackConfig:
    url: str = "http://llama-stack:8321"


@dataclass
class PrimaryConfig:
    """The LLM whose response is returned to the client."""
    name: str = "llama-stack-primary"
    model_id: str = "meta-llama/Llama-3.1-8B-Instruct"
    timeout: int = 120

    @classmethod
    def from_env(cls, raw: dict) -> "PrimaryConfig":
        model_id = os.environ.get(
            raw.get("model_id_env", "PRIMARY_MODEL"),
            raw.get("model_id", "meta-llama/Llama-3.1-8B-Instruct"),
        )
        return cls(
            name=raw.get("name", "llama-stack-primary"),
            model_id=model_id,
            timeout=raw.get("timeout", 120),
        )


@dataclass
class LLMConfig:
    """One secondary LLM that receives a concurrent fan-out call."""
    name: str
    model_id: str
    timeout: int = 60
    enabled: bool = True


@dataclass
class CollectorConfig:
    url: str = "http://collector:8001"
    timeout: int = 5


@dataclass
class AppConfig:
    llama_stack: LlamaStackConfig = field(default_factory=LlamaStackConfig)
    primary: PrimaryConfig = field(default_factory=PrimaryConfig)
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
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    llama_stack = LlamaStackConfig(**raw.get("llama_stack", {}))
    primary = PrimaryConfig.from_env(raw.get("primary", {}))
    llms = [LLMConfig(**e) for e in raw.get("llms", [])]
    collector = CollectorConfig(**raw.get("collector", {}))

    return AppConfig(
        llama_stack=llama_stack,
        primary=primary,
        llms=llms,
        collector=collector,
    )
