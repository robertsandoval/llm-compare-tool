"""
LLM Client

Two call modes, both targeting the shared llama-stack server:

  call_llm()     — non-streaming, returns a full OpenAI-format response dict.
                   Used for all secondary LLMs and non-streaming primary calls.

  stream_llm()   — streaming, async-yields raw SSE bytes from llama-stack.
                   Used only for the primary call when the client requests
                   stream=true.  The caller is responsible for translating
                   chunks and accumulating content for the collector.
"""

import json
import time
import logging
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from config import LLMConfig

logger = logging.getLogger(__name__)

_CHAT_PATH = "/v1/inference/chat_completion"


class LLMCallError(Exception):
    def __init__(self, llm_name: str, status_code: Optional[int], message: str):
        self.llm_name = llm_name
        self.status_code = status_code
        super().__init__(f"[{llm_name}] HTTP {status_code}: {message}")


# ── Non-streaming ──────────────────────────────────────────────────────────────

async def call_llm(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    llama_stack_url: str,
    request_body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Send a single non-streaming chat completion request to llama-stack.
    Translates OpenAI → llama-stack request format and back.
    """
    body = _to_ls_body(request_body, llm.model_id, stream=False)
    url = f"{llama_stack_url.rstrip('/')}{_CHAT_PATH}"

    start = time.monotonic()
    try:
        resp = await client.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=llm.timeout,
        )
        elapsed = time.monotonic() - start

        if resp.status_code != 200:
            raise LLMCallError(llm.name, resp.status_code, resp.text[:500])

        data = resp.json()
        result = _ls_response_to_openai(data, llm.model_id)
        result["_comparison"] = {
            "llm_name": llm.name,
            "latency_seconds": round(elapsed, 3),
        }
        return result

    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - start
        raise LLMCallError(llm.name, None, f"Timeout after {elapsed:.1f}s") from exc
    except httpx.RequestError as exc:
        raise LLMCallError(llm.name, None, str(exc)) from exc


# ── Streaming ──────────────────────────────────────────────────────────────────

async def stream_llm(
    client: httpx.AsyncClient,
    llm: LLMConfig,
    llama_stack_url: str,
    request_body: Dict[str, Any],
) -> AsyncGenerator[bytes, None]:
    """
    Open a streaming request to llama-stack and yield raw SSE bytes.
    The caller forwards them to the client and feeds them to SseAccumulator.
    """
    body = _to_ls_body(request_body, llm.model_id, stream=True)
    url = f"{llama_stack_url.rstrip('/')}{_CHAT_PATH}"

    try:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=llm.timeout,
        ) as response:
            if response.status_code != 200:
                error_bytes = await response.aread()
                raise LLMCallError(
                    llm.name, response.status_code, error_bytes.decode()[:500]
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    except httpx.TimeoutException as exc:
        raise LLMCallError(llm.name, None, f"Stream timeout") from exc
    except httpx.RequestError as exc:
        raise LLMCallError(llm.name, None, str(exc)) from exc


# ── Format helpers ─────────────────────────────────────────────────────────────

def _to_ls_body(
    body: Dict[str, Any], model_id: str, stream: bool
) -> Dict[str, Any]:
    """
    OpenAI chat completion format → llama-stack /v1/inference/chat_completion.

    OpenAI:     { "model": "...", "messages": [...], "temperature": 0.7 }
    llama-stack:{ "model_id": "...", "messages": [...],
                  "sampling_params": {"temperature": 0.7}, "stream": true }
    """
    ls: Dict[str, Any] = {
        "model_id": model_id,
        "messages": body.get("messages", []),
        "stream": stream,
    }
    sampling: Dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p", "repetition_penalty"):
        if key in body:
            sampling[key] = body[key]
    if sampling:
        ls["sampling_params"] = sampling
    return ls


def _ls_response_to_openai(data: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """
    llama-stack /v1/inference/chat_completion response → OpenAI chat completion.

    llama-stack: { "completion_message": { "role": "assistant",
                                           "content": "...",
                                           "stop_reason": "end_of_turn" } }
    """
    msg = data.get("completion_message", {})
    content = msg.get("content", "")
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
                "message": {"role": msg.get("role", "assistant"), "content": content},
                "finish_reason": str(msg.get("stop_reason", "stop")),
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


# ── SSE accumulator ────────────────────────────────────────────────────────────

class SseAccumulator:
    """
    Consumes raw SSE bytes from a llama-stack streaming response and
    reconstructs a complete OpenAI-format response object for the collector.
    """

    def __init__(self, model_id: str, llm_name: str):
        self.model_id = model_id
        self.llm_name = llm_name
        self._parts: list[str] = []
        self._stop_reason = "stop"
        self._start = time.monotonic()

    def feed(self, chunk: bytes) -> None:
        for line in chunk.decode(errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
                self._ingest(data)
            except (json.JSONDecodeError, AttributeError):
                pass

    def _ingest(self, data: Dict[str, Any]) -> None:
        # llama-stack SSE event structure
        event = data.get("event", {})
        delta = event.get("delta", {})
        if isinstance(delta, str):
            self._parts.append(delta)
        elif isinstance(delta, dict):
            self._parts.append(delta.get("text", ""))
        stop = event.get("stop_reason")
        if stop:
            self._stop_reason = str(stop)

        # OpenAI SSE chunk structure (fallback / pass-through clients)
        for choice in data.get("choices", []):
            content = choice.get("delta", {}).get("content", "")
            if content:
                self._parts.append(content)
            finish = choice.get("finish_reason")
            if finish:
                self._stop_reason = finish

    def to_response(self) -> Dict[str, Any]:
        elapsed = round(time.monotonic() - self._start, 3)
        return {
            "id": "",
            "object": "chat.completion",
            "created": 0,
            "model": self.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(self._parts),
                    },
                    "finish_reason": self._stop_reason,
                }
            ],
            "usage": {},
            "_comparison": {
                "llm_name": self.llm_name,
                "latency_seconds": elapsed,
            },
        }


def ls_chunk_to_openai_sse(chunk: bytes, model_id: str) -> bytes:
    """
    Translate a single llama-stack SSE chunk to OpenAI SSE wire format.
    Falls back to passing the line through unchanged on parse errors.
    """
    out: list[str] = []
    for line in chunk.decode(errors="replace").split("\n"):
        if not line.startswith("data: "):
            out.append(line)
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
            event = data.get("event", {})
            event_type = event.get("event_type", "")
            delta = event.get("delta", {})

            text = delta if isinstance(delta, str) else delta.get("text", "")
            finish_reason = (
                str(event.get("stop_reason", "stop"))
                if event_type == "complete"
                else None
            )
            openai_chunk = {
                "object": "chat.completion.chunk",
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text} if text else {},
                        "finish_reason": finish_reason,
                    }
                ],
            }
            out.append(f"data: {json.dumps(openai_chunk)}")
        except (json.JSONDecodeError, AttributeError):
            out.append(line)

    result = "\n".join(out)
    if result and not result.endswith("\n"):
        result += "\n"
    return result.encode()
