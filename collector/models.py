"""
Collector Service Data Models
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class LLMResponse:
    llm_name: str
    substituted_model: str
    success: bool
    response: Optional[Dict[str, Any]]
    error: Optional[str]
    latency_seconds: Optional[float]
    received_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def content(self) -> Optional[str]:
        """Extract the assistant message content from an OpenAI-format response."""
        if not self.success or not self.response:
            return None
        try:
            return self.response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None

    @property
    def usage(self) -> Optional[Dict[str, int]]:
        if not self.response:
            return None
        return self.response.get("usage")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "llm_name": self.llm_name,
            "substituted_model": self.substituted_model,
            "success": self.success,
            "content": self.content,
            "error": self.error,
            "latency_seconds": self.latency_seconds,
            "usage": self.usage,
            "received_at": self.received_at,
        }


@dataclass
class ComparisonRecord:
    request_id: str
    original_model: str
    responses: List[LLMResponse] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "original_model": self.original_model,
            "responses": [r.to_dict() for r in self.responses],
            "response_count": len(self.responses),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
