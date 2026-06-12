"""
Model Router Configuration Loader

All LLM calls are routed through a single llama-stack server.
LLMConfig no longer carries provider credentials or base URLs — those live
exclusively in the llama-stack run.yaml.  Each entry here only needs the
model_id that is registered in llama-stack.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LlamaStackConfig:
    """Connection details for the shared llama-stack server."""
    url: str = "http://llama-stack:8321"


@dataclass
class LLMConfig:
    """One secondary LLM to fan out to via llama-stack."""
    name: str
    # Must match a model_id registered in the llama-stack run.yaml
    model_id: str
    timeout: int = 60
    enabled: bool = True


@dataclass
class CollectorConfig:
    url: str = "http://collector:8001"
    timeout: int = 5
    capture_primary: bool = True
    primary_name: str = "llama-stack-primary"
    primary_model_env: str = "PRIMARY_MODEL"
    primary_model_default: str = "meta-llama/Llama-3.1-8B-Instruct"

    @property
    def primary_model(self) -> str:
        return os.environ.get(self.primary_model_env, self.primary_model_default)


@dataclass
class AppConfig:
    llama_stack: LlamaStackConfig = field(default_factory=LlamaStackConfig)
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

    llama_stack = LlamaStackConfig(**raw.get("llama_stack", {}))
    llms = [LLMConfig(**entry) for entry in raw.get("llms", [])]
    collector = CollectorConfig(**raw.get("collector", {}))

    return AppConfig(llama_stack=llama_stack, llms=llms, collector=collector)
