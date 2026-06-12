"""
Model Router Service

Receives HTTP-mirrored requests from Envoy and fans out to all configured
secondary LLMs via the shared llama-stack server.  llama-stack fronts every
model — including the primary — so all inference calls share a single,
uniform API regardless of the underlying provider.

The response to Envoy is always 200 OK (fire-and-forget pattern).
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from config import AppConfig, LLMConfig, load_config
from llm_client import LLMCallError, call_llm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app_config: Optional[AppConfig] = None
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_config, http_client
    app_config = load_config()
    http_client = httpx.AsyncClient()
    logger.info(
        "Model Router started. llama-stack=%s  Enabled LLMs: %s",
        app_config.llama_stack.url,
        [llm.name for llm in app_config.enabled_llms],
    )
    yield
    await http_client.aclose()
    logger.info("Model Router stopped.")


app = FastAPI(title="LLM Comparison Model Router", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "llama_stack_url": app_config.llama_stack.url,
        "enabled_llms": [l.name for l in app_config.enabled_llms],
    }


@app.get("/models")
async def list_models():
    return {
        "llama_stack_url": app_config.llama_stack.url,
        "llms": [
            {
                "name": llm.name,
                "model_id": llm.model_id,
                "enabled": llm.enabled,
            }
            for llm in app_config.llms
        ],
    }


@app.post("/v1/chat/completions")
async def mirror_chat_completions(request: Request):
    """
    Entry point for Envoy-mirrored chat completion requests (OpenAI format).

    1. Parse the mirrored request.
    2. Extract or generate a correlation ID.
    3. Fan out concurrently to all enabled LLMs via llama-stack.
    4. Optionally re-call the primary model through llama-stack to capture it.
    5. Ship all results to the Collector.
    6. Return 200 immediately — Envoy discards this response.
    """
    body = await _parse_body(request)
    if body is None:
        return Response(status_code=400)

    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or str(uuid.uuid4())
    )
    original_model = body.get("model", "unknown")

    logger.info(
        "Mirrored request received. request_id=%s original_model=%s",
        request_id,
        original_model,
    )

    asyncio.create_task(_fan_out_and_collect(request_id, original_model, body))

    return JSONResponse({"status": "mirroring", "request_id": request_id})


@app.post("/v1/inference/chat_completion")
async def mirror_llama_stack(request: Request):
    """Handle llama-stack native API format — translate and mirror."""
    body = await _parse_body(request)
    if body is None:
        return Response(status_code=400)

    openai_body = _llama_stack_to_openai_body(body)
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    original_model = openai_body.get("model", "unknown")

    asyncio.create_task(_fan_out_and_collect(request_id, original_model, openai_body))

    return JSONResponse({"status": "mirroring", "request_id": request_id})


# ── Fan-out logic ──────────────────────────────────────────────────────────────

async def _fan_out_and_collect(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
) -> None:
    llms_to_call: List[LLMConfig] = list(app_config.enabled_llms)

    if app_config.collector.capture_primary:
        primary = LLMConfig(
            name=app_config.collector.primary_name,
            model_id=app_config.collector.primary_model,
            timeout=120,
            enabled=True,
        )
        llms_to_call = [primary] + llms_to_call

    tasks = [
        _call_and_report(request_id, original_model, body, llm)
        for llm in llms_to_call
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.error("Unhandled fan-out error: %s", result)


async def _call_and_report(
    request_id: str,
    original_model: str,
    body: Dict[str, Any],
    llm: LLMConfig,
) -> None:
    try:
        response_data = await call_llm(
            client=http_client,
            llm=llm,
            llama_stack_url=app_config.llama_stack.url,
            request_body=body,
        )
        await _send_to_collector(
            request_id=request_id,
            original_model=original_model,
            llm_name=llm.name,
            substituted_model=llm.model_id,
            success=True,
            response=response_data,
            error=None,
        )
    except LLMCallError as exc:
        logger.warning("LLM call failed for %s: %s", llm.name, exc)
        await _send_to_collector(
            request_id=request_id,
            original_model=original_model,
            llm_name=llm.name,
            substituted_model=llm.model_id,
            success=False,
            response=None,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error calling %s", llm.name)
        await _send_to_collector(
            request_id=request_id,
            original_model=original_model,
            llm_name=llm.name,
            substituted_model=llm.model_id,
            success=False,
            response=None,
            error=str(exc),
        )


async def _send_to_collector(
    request_id: str,
    original_model: str,
    llm_name: str,
    substituted_model: str,
    success: bool,
    response: Optional[Dict[str, Any]],
    error: Optional[str],
) -> None:
    collector_url = app_config.collector.url.rstrip("/")
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
            f"{collector_url}/responses",
            json=payload,
            timeout=app_config.collector.timeout,
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "Collector returned %s for request_id=%s llm=%s",
                resp.status_code,
                request_id,
                llm_name,
            )
    except Exception as exc:
        logger.error(
            "Failed to send to collector request_id=%s llm=%s: %s",
            request_id,
            llm_name,
            exc,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _parse_body(request: Request) -> Optional[Dict[str, Any]]:
    try:
        return await request.json()
    except Exception as exc:
        logger.error("Failed to parse request body: %s", exc)
        return None


def _llama_stack_to_openai_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate llama-stack /v1/inference/chat_completion body → OpenAI format."""
    sampling = body.get("sampling_params", {})
    openai: Dict[str, Any] = {
        "model": body.get("model_id", body.get("model", "unknown")),
        "messages": body.get("messages", []),
    }
    if "temperature" in sampling:
        openai["temperature"] = sampling["temperature"]
    if "max_tokens" in sampling:
        openai["max_tokens"] = sampling["max_tokens"]
    if "top_p" in sampling:
        openai["top_p"] = sampling["top_p"]
    return openai
