"""
LLM Client — llama-stack edition

All secondary LLM calls go through the shared llama-stack server via its
native /v1/inference/chat_completion endpoint.  llama-stack resolves the
model_id to the correct backend provider (Ollama, OpenAI, Anthropic, etc.)
and handles all provider-specific translation internally.

This module contains zero provider-specific logic.
"""

import time
import logging
from typing import Any, Dict, Optional

import httpx

from config import LLMConfig

logger = logging.getLogger(__name__)

# llama-stack native inference endpoint
_CHAT_COMPLETION_PATH = "/v1/inference/chat_completion"


class LLMCallError(Exception):
    def __init__(self, llm_name: str, status_code: Optional[int], message: str):
        self.llm_name = llm_name
        self.status_code = status_code
        super().__init__(f"[{llm_name}] HTTP {status_code}: {message}")


async def call_llm(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    llama_stack_url: str,
    request_body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Forward a single chat completion request to llama-stack.

    Translates the incoming OpenAI-format body to the llama-stack
    /v1/inference/chat_completion format, posts it, and translates the
    response back to OpenAI format so the collector receives a uniform shape.
    """
    ls_body = _openai_to_llama_stack(request_body, llm.model_id)
    url = f"{llama_stack_url.rstrip('/')}{_CHAT_COMPLETION_PATH}"

    start = time.monotonic()
    try:
        response = await client.post(
            url,
            json=ls_body,
            headers={"Content-Type": "application/json"},
            timeout=llm.timeout,
        )
        elapsed = time.monotonic() - start

        if response.status_code != 200:
            raise LLMCallError(llm.name, response.status_code, response.text[:500])

        data = response.json()
        openai_format = _llama_stack_to_openai(data, llm.model_id)
        return _annotate_response(openai_format, llm.name, elapsed)

    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, f"Timeout after {elapsed:.1f}s") from exc
    except httpx.RequestError as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, str(exc)) from exc


# ── Format translation helpers ─────────────────────────────────────────────────

def _openai_to_llama_stack(body: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """
    OpenAI chat completion format → llama-stack /v1/inference/chat_completion format.

    OpenAI:
      { "model": "...", "messages": [...], "temperature": 0.7, "max_tokens": 512 }

    llama-stack:
      { "model_id": "...", "messages": [...], "sampling_params": { "temperature": 0.7, "max_tokens": 512 } }
    """
    ls_body: Dict[str, Any] = {
        "model_id": model_id,
        "messages": body.get("messages", []),
    }

    sampling: Dict[str, Any] = {}
    if "temperature" in body:
        sampling["temperature"] = body["temperature"]
    if "max_tokens" in body:
        sampling["max_tokens"] = body["max_tokens"]
    if "top_p" in body:
        sampling["top_p"] = body["top_p"]
    if sampling:
        ls_body["sampling_params"] = sampling

    return ls_body


def _llama_stack_to_openai(data: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """
    llama-stack /v1/inference/chat_completion response → OpenAI chat completion format.

    llama-stack response:
      { "completion_message": { "role": "assistant", "content": "...", "stop_reason": "end_of_turn" } }
    """
    completion = data.get("completion_message", {})
    content = completion.get("content", "")
    # llama-stack may return content as a TextDelta object; flatten to string
    if isinstance(content, dict):
        content = content.get("text", "")

    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": 0,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": completion.get("role", "assistant"),
                    "content": content,
                },
                "finish_reason": str(completion.get("stop_reason", "stop")),
            }
        ],
        "usage": {
            "prompt_tokens": data.get("prompt_tokens", 0),
            "completion_tokens": data.get("completion_tokens", 0),
            "total_tokens": (
                data.get("prompt_tokens", 0) + data.get("completion_tokens", 0)
            ),
        },
    }


def _annotate_response(
    data: Dict[str, Any], llm_name: str, elapsed: float
) -> Dict[str, Any]:
    data["_comparison"] = {
        "llm_name": llm_name,
        "latency_seconds": round(elapsed, 3),
    }
    return data
