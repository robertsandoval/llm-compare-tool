"""
Collector Service

Receives LLM responses from the Model Router, stores them in Redis keyed by
request_id, and provides a REST API for querying and comparing results.

Redis key layout:
  comparison:{request_id}          → JSON hash with metadata and responses list
  comparisons:index                → Redis sorted set of request_ids (score = timestamp)
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import ComparisonRecord, LLMResponse

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
# How long to keep comparison records in Redis (default: 24 hours)
RECORD_TTL_SECONDS = int(os.environ.get("RECORD_TTL_SECONDS", 86400))
INDEX_KEY = "comparisons:index"

redis_client: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Collector connected to Redis at %s", REDIS_URL)
    yield
    await redis_client.aclose()
    logger.info("Collector stopped.")


app = FastAPI(title="LLM Comparison Collector", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic request models ────────────────────────────────────────────────────

class ResponsePayload(BaseModel):
    request_id: str
    original_model: str
    llm_name: str
    substituted_model: str
    success: bool
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as exc:
        return {"status": "degraded", "redis": str(exc)}


@app.post("/responses", status_code=201)
async def store_response(payload: ResponsePayload):
    """
    Called by the Model Router to store a single LLM response.
    Creates or updates the comparison record for the given request_id.
    """
    redis_key = f"comparison:{payload.request_id}"

    # Fetch or create the record
    existing_raw = await redis_client.get(redis_key)
    if existing_raw:
        existing = json.loads(existing_raw)
    else:
        existing = {
            "request_id": payload.request_id,
            "original_model": payload.original_model,
            "responses": [],
            "created_at": _now_iso(),
        }

    # Extract latency from the response metadata if present
    latency = None
    if payload.response and "_comparison" in payload.response:
        latency = payload.response["_comparison"].get("latency_seconds")
        # Remove internal metadata before storing
        payload.response = {
            k: v for k, v in payload.response.items() if k != "_comparison"
        }

    response_entry = {
        "llm_name": payload.llm_name,
        "substituted_model": payload.substituted_model,
        "success": payload.success,
        "response": payload.response,
        "error": payload.error,
        "latency_seconds": latency,
        "received_at": _now_iso(),
    }
    existing["responses"].append(response_entry)
    existing["updated_at"] = _now_iso()

    await redis_client.setex(redis_key, RECORD_TTL_SECONDS, json.dumps(existing))

    # Add to sorted index (score = epoch time for ordering)
    await redis_client.zadd(INDEX_KEY, {payload.request_id: time.time()})
    await redis_client.expire(INDEX_KEY, RECORD_TTL_SECONDS * 2)

    logger.info(
        "Stored response for request_id=%s llm=%s success=%s",
        payload.request_id,
        payload.llm_name,
        payload.success,
    )
    return {"stored": True, "request_id": payload.request_id}


@app.get("/comparisons/{request_id}")
async def get_comparison(request_id: str):
    """Return all LLM responses for a specific request_id."""
    redis_key = f"comparison:{request_id}"
    raw = await redis_client.get(redis_key)
    if not raw:
        raise HTTPException(status_code=404, detail=f"No record for request_id={request_id}")
    record = json.loads(raw)
    return _enrich_record(record)


@app.get("/comparisons")
async def list_comparisons(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Return the most recent comparison records (paginated)."""
    # Sorted set: highest score = most recent
    total = await redis_client.zcard(INDEX_KEY)
    # ZREVRANGE returns highest scores first
    request_ids: List[str] = await redis_client.zrevrange(
        INDEX_KEY, offset, offset + limit - 1
    )
    records = []
    for rid in request_ids:
        raw = await redis_client.get(f"comparison:{rid}")
        if raw:
            records.append(_enrich_record(json.loads(raw)))
    return {"total": total, "offset": offset, "limit": limit, "records": records}


@app.delete("/comparisons/{request_id}")
async def delete_comparison(request_id: str):
    redis_key = f"comparison:{request_id}"
    deleted = await redis_client.delete(redis_key)
    await redis_client.zrem(INDEX_KEY, request_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": True}


@app.delete("/comparisons")
async def clear_all():
    """Delete all comparison records. Use with caution."""
    request_ids: List[str] = await redis_client.zrange(INDEX_KEY, 0, -1)
    keys = [f"comparison:{rid}" for rid in request_ids] + [INDEX_KEY]
    if keys:
        await redis_client.delete(*keys)
    return {"deleted_count": len(request_ids)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _enrich_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Add derived fields to a comparison record for display."""
    responses = record.get("responses", [])
    for r in responses:
        if r.get("success") and r.get("response"):
            try:
                r["content"] = r["response"]["choices"][0]["message"]["content"]
                r["usage"] = r["response"].get("usage")
            except (KeyError, IndexError, TypeError):
                r["content"] = None
                r["usage"] = None
        else:
            r["content"] = None
            r["usage"] = None
    record["response_count"] = len(responses)
    return record
