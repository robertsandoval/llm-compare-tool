"""
LLM Client

Thin async wrappers around OpenAI-compatible and Anthropic APIs.
All providers are normalized to the OpenAI chat completion request/response format
so the model-router can handle them uniformly.
"""

import time
import logging
from typing import Any, Dict, Optional

import httpx

from config import LLMConfig

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    def __init__(self, llm_name: str, status_code: Optional[int], message: str):
        self.llm_name = llm_name
        self.status_code = status_code
        super().__init__(f"[{llm_name}] HTTP {status_code}: {message}")


async def call_llm(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    request_body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Send a chat completion request to a single LLM backend.

    Rewrites the `model` field in request_body to llm.model.
    Returns a normalized response dict with timing metadata.
    """
    body = {**request_body, "model": llm.model}

    # Anthropic uses a slightly different API format
    if llm.provider == "anthropic":
        return await _call_anthropic(client, llm, body)

    return await _call_openai_compatible(client, llm, body)


async def _call_openai_compatible(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if llm.api_key:
        headers["Authorization"] = f"Bearer {llm.api_key}"

    url = f"{llm.base_url.rstrip('/')}/chat/completions"

    # Disable streaming for comparison collection — we need the full response
    body = {**body, "stream": False}

    start = time.monotonic()
    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=llm.timeout,
        )
        elapsed = time.monotonic() - start

        if response.status_code != 200:
            raise LLMCallError(llm.name, response.status_code, response.text[:500])

        data = response.json()
        return _annotate_response(data, llm.name, elapsed)

    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, f"Timeout after {elapsed:.1f}s") from exc
    except httpx.RequestError as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, str(exc)) from exc


async def _call_anthropic(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Translate OpenAI chat completion format → Anthropic Messages API format,
    then translate the response back to OpenAI format.
    """
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if llm.api_key:
        headers["x-api-key"] = llm.api_key

    # Separate system prompt from messages
    messages = body.get("messages", [])
    system_content = None
    user_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            system_content = msg.get("content", "")
        else:
            user_messages.append(msg)

    anthropic_body: Dict[str, Any] = {
        "model": llm.model,
        "messages": user_messages,
        "max_tokens": body.get("max_tokens", 2048),
    }
    if system_content:
        anthropic_body["system"] = system_content
    if "temperature" in body:
        anthropic_body["temperature"] = body["temperature"]

    url = f"{llm.base_url.rstrip('/')}/messages"

    start = time.monotonic()
    try:
        response = await client.post(
            url,
            json=anthropic_body,
            headers=headers,
            timeout=llm.timeout,
        )
        elapsed = time.monotonic() - start

        if response.status_code != 200:
            raise LLMCallError(llm.name, response.status_code, response.text[:500])

        data = response.json()
        # Translate Anthropic response → OpenAI format
        openai_format = _anthropic_to_openai(data, llm.model)
        return _annotate_response(openai_format, llm.name, elapsed)

    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, f"Timeout after {elapsed:.1f}s") from exc
    except httpx.RequestError as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, str(exc)) from exc


def _anthropic_to_openai(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    content_blocks = data.get("content", [])
    text = " ".join(
        block.get("text", "") for block in content_blocks if block.get("type") == "text"
    )
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": data.get("stop_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
            "total_tokens": (
                data.get("usage", {}).get("input_tokens", 0)
                + data.get("usage", {}).get("output_tokens", 0)
            ),
        },
    }


def _annotate_response(
    data: Dict[str, Any], llm_name: str, elapsed: float
) -> Dict[str, Any]:
    """Attach comparison metadata to a response dict."""
    data["_comparison"] = {
        "llm_name": llm_name,
        "latency_seconds": round(elapsed, 3),
    }
    return data
