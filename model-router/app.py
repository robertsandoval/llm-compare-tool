"""
Model Router — single entry point, no Envoy required.

Flow for every request:
  1. Parse the incoming request.
  2. Fire ALL secondary LLM tasks immediately via asyncio.create_task()
     — they run concurrently from this point, even while we await the primary.
  3a. Non-streaming: await primary response, return it to client.
  3b. Streaming:     open a streaming call to llama-stack, forward each SSE
                     chunk to the client as it arrives (first token is fast),
                     accumulate chunks in SseAccumulator.
  4. Ship primary response to Collector (after response or after stream ends).
  5. Secondary tasks complete in the background; each ships its result to
     the Collector independently.

The client always waits only for the primary LLM — secondary latency is hidden.
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from config import AppConfig, LLMConfig, load_config
from llm_client import (
    LLMCallError,
    SseAccumulator,
    call_llm,
    ls_chunk_to_openai_sse,
    stream_llm,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app_config: Optional[AppConfig] = None
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global app_config, http_client
    app_config = load_config()
    http_client = httpx.AsyncClient()
    logger.info(
        "Model Router started. primary=%s  llama-stack=%s  secondaries=%s",
        app_config.primary.model_id,
        app_config.llama_stack.url,
        [llm.name for llm in app_config.enabled_llms],
    )
    yield
    await http_client.aclose()
    logger.info("Model Router stopped.")


app = FastAPI(title="LLM Comparison Router", lifespan=lifespan)


# ── Introspection endpoints ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "primary": app_config.primary.model_id,
        "llama_stack_url": app_config.llama_stack.url,
        "secondaries": [l.name for l in app_config.enabled_llms],
    }


@app.get("/models")
async def list_models():
    return {
        "primary": {
            "name": app_config.primary.name,
            "model_id": app_config.primary.model_id,
        },
        "secondaries": [
            {"name": l.name, "model_id": l.model_id, "enabled": l.enabled}
            for l in app_config.llms
        ],
        "llama_stack_url": app_config.llama_stack.url,
    }


# ── OpenAI-compatible endpoint ─────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions.  Supports both stream=false and stream=true.
    """
    body = await _parse_body(request)
    if body is None:
        return Response(status_code=400)

    request_id = _get_request_id(request)
    original_model = body.get("model", app_config.primary.model_id)

    # Secondaries start NOW — concurrent with the primary call below
    _fire_secondaries(request_id, original_model, body)

    if body.get("stream", False):
        return StreamingResponse(
            _stream_openai(request_id, original_model, body),
            media_type="text/event-stream",
            headers={"x-request-id": request_id},
        )

    return await _call_primary_non_streaming(
        request_id, original_model, body, response_format="openai"
    )


# ── llama-stack native endpoint ────────────────────────────────────────────────

@app.post("/v1/inference/chat_completion")
async def llama_stack_chat_completion(request: Request):
    """
    llama-stack native chat completion.  Supports both stream=false and stream=true.
    Translates the body to OpenAI format for internal processing, then translates
    the response back to llama-stack format.
    """
    body = await _parse_body(request)
    if body is None:
        return Response(status_code=400)

    openai_body = _ls_body_to_openai(body)
    request_id = _get_request_id(request)
    original_model = openai_body.get("model", app_config.primary.model_id)

    _fire_secondaries(request_id, original_model, openai_body)

    if body.get("stream", False):
        return StreamingResponse(
            _stream_llama_stack(request_id, original_model, openai_body),
            media_type="text/event-stream",
            headers={"x-request-id": request_id},
        )

    return await _call_primary_non_streaming(
        request_id, original_model, openai_body, response_format="llama_stack"
    )


# ── Primary call handlers ──────────────────────────────────────────────────────

async def _call_primary_non_streaming(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
    response_format: str,
) -> Response:
    """
    Non-streaming primary path.

    All secondaries are already running (fired before this call).
    We await only the primary, return it to the client, then ship it to
    the collector in the background.
    """
    primary = _primary_llm()
    try:
        result = await call_llm(
            http_client, primary, app_config.llama_stack.url, body
        )
        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                True, result, None,
            )
        )
        if response_format == "llama_stack":
            return JSONResponse(
                _openai_to_ls_response(result),
                headers={"x-request-id": request_id},
            )
        return JSONResponse(
            _strip_meta(result),
            headers={"x-request-id": request_id},
        )

    except LLMCallError as exc:
        logger.error("Primary LLM call failed: %s", exc)
        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                False, None, str(exc),
            )
        )
        return JSONResponse({"error": str(exc)}, status_code=502)


async def _stream_openai(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
) -> AsyncGenerator[bytes, None]:
    """
    Streaming primary path — OpenAI SSE output.

    Translates each llama-stack SSE chunk to OpenAI SSE format on the fly,
    forwards to the client immediately (low latency to first token), and
    accumulates the full content for the collector after the stream ends.
    """
    primary = _primary_llm()
    acc = SseAccumulator(primary.model_id, primary.name)

    try:
        async for chunk in stream_llm(
            http_client, primary, app_config.llama_stack.url, body
        ):
            acc.feed(chunk)
            yield ls_chunk_to_openai_sse(chunk, primary.model_id)

        yield b"data: [DONE]\n\n"

        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                True, acc.to_response(), None,
            )
        )

    except LLMCallError as exc:
        logger.error("Primary stream failed request_id=%s: %s", request_id, exc)
        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                False, None, str(exc),
            )
        )
        # Emit an error event so the client knows the stream failed
        yield f'data: {{"error": "{exc}"}}\n\n'.encode()


async def _stream_llama_stack(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
) -> AsyncGenerator[bytes, None]:
    """
    Streaming primary path — llama-stack native SSE output (pass-through).
    """
    primary = _primary_llm()
    acc = SseAccumulator(primary.model_id, primary.name)

    try:
        async for chunk in stream_llm(
            http_client, primary, app_config.llama_stack.url, body
        ):
            acc.feed(chunk)
            yield chunk  # pass through unchanged

        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                True, acc.to_response(), None,
            )
        )

    except LLMCallError as exc:
        logger.error("Primary stream failed request_id=%s: %s", request_id, exc)
        asyncio.create_task(
            _send_to_collector(
                request_id, original_model,
                primary.name, primary.model_id,
                False, None, str(exc),
            )
        )
        yield f'data: {{"error": "{exc}"}}\n\n'.encode()


# ── Secondary fan-out ──────────────────────────────────────────────────────────

def _fire_secondaries(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
) -> None:
    """
    Create background tasks for every enabled secondary LLM.
    Returns immediately — tasks run concurrently with the primary call.
    """
    for llm in app_config.enabled_llms:
        asyncio.create_task(
            _call_secondary(request_id, original_model, body, llm)
        )


async def _call_secondary(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
    llm: LLMConfig,
) -> None:
    try:
        result = await call_llm(
            http_client, llm, app_config.llama_stack.url, body
        )
        await _send_to_collector(
            request_id, original_model,
            llm.name, llm.model_id,
            True, result, None,
        )
    except LLMCallError as exc:
        logger.warning("Secondary %s failed: %s", llm.name, exc)
        await _send_to_collector(
            request_id, original_model,
            llm.name, llm.model_id,
            False, None, str(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error for secondary %s", llm.name)
        await _send_to_collector(
            request_id, original_model,
            llm.name, llm.model_id,
            False, None, str(exc),
        )


# ── Collector ──────────────────────────────────────────────────────────────────

async def _send_to_collector(
    request_id: str,
    original_model: str,
    llm_name: str,
    substituted_model: str,
    success: bool,
    response: Optional[Dict[str, Any]],
    error: Optional[str],
) -> None:
    payload = {
        "request_id": request_id,
        "original_model": original_model,
        "llm_name": llm_name,
        "substituted_model": substituted_model,
        "success": success,
        "response": response,
        "error": error,
    }
    try:
        resp = await http_client.post(
            f"{app_config.collector.url.rstrip('/')}/responses",
            json=payload,
            timeout=app_config.collector.timeout,
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "Collector %s for request_id=%s llm=%s",
                resp.status_code, request_id, llm_name,
            )
    except Exception as exc:
        logger.error(
            "Collector unreachable request_id=%s llm=%s: %s",
            request_id, llm_name, exc,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _primary_llm() -> LLMConfig:
    return LLMConfig(
        name=app_config.primary.name,
        model_id=app_config.primary.model_id,
        timeout=app_config.primary.timeout,
        enabled=True,
    )


def _get_request_id(request: Request) -> str:
    return (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or str(uuid.uuid4())
    )


async def _parse_body(request: Request) -> Optional[Dict[str, Any]]:
    try:
        return await request.json()
    except Exception as exc:
        logger.error("Failed to parse request body: %s", exc)
        return None


def _ls_body_to_openai(body: Dict[str, Any]) -> Dict[str, Any]:
    """llama-stack request format → OpenAI format (for internal processing)."""
    sampling = body.get("sampling_params", {})
    result: Dict[str, Any] = {
        "model": body.get("model_id", body.get("model", "unknown")),
        "messages": body.get("messages", []),
    }
    for key in ("temperature", "max_tokens", "top_p"):
        if key in sampling:
            result[key] = sampling[key]
    if body.get("stream"):
        result["stream"] = True
    return result


def _openai_to_ls_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI response format → llama-stack response format."""
    try:
        msg = data["choices"][0]["message"]
        return {
            "completion_message": {
                "role": msg.get("role", "assistant"),
                "content": msg.get("content", ""),
                "stop_reason": data["choices"][0].get("finish_reason", "end_of_turn"),
            }
        }
    except (KeyError, IndexError):
        return data


def _strip_meta(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove internal _comparison key before returning to client."""
    return {k: v for k, v in data.items() if k != "_comparison"}
