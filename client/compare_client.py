"""
LLM Comparison Client

Uses the llama-stack Python client (or falls back to OpenAI-compatible HTTP) to
send chat prompts through the Envoy proxy (which mirrors them to all secondary
LLMs). Also provides commands to query the Collector for comparison results.

Usage:
  python compare_client.py --prompt "Explain quantum entanglement"
  python compare_client.py --prompt "Write a haiku about the ocean" --max-tokens 100
  python compare_client.py --view REQUEST_ID
  python compare_client.py --list
  python compare_client.py --list --limit 5
"""

import argparse
import json
import os
import sys
import textwrap
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────

ENVOY_URL = os.environ.get("ENVOY_URL", "http://localhost:8080")
COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://localhost:8001")
PRIMARY_MODEL = os.environ.get("PRIMARY_MODEL", "meta-llama/Llama-3.1-8B-Instruct")

# Whether to use the llama-stack client library (True) or raw OpenAI-compat HTTP (False)
USE_LLAMA_STACK_CLIENT = os.environ.get("USE_LLAMA_STACK_CLIENT", "true").lower() == "true"


# ── llama-stack client wrapper ─────────────────────────────────────────────────

def send_via_llama_stack(
    prompt: str,
    model: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a chat completion request using the llama-stack Python client.
    The client points to Envoy, which mirrors the request to the Model Router.
    """
    try:
        from llama_stack_client import LlamaStackClient
        from llama_stack_client.types import UserMessage, SystemMessage
    except ImportError:
        print(
            "[WARNING] llama-stack-client not installed. "
            "Falling back to OpenAI-compatible HTTP.\n"
            "Install with: pip install llama-stack-client"
        )
        return send_via_openai_compat(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            request_id=request_id,
        )

    client = LlamaStackClient(base_url=ENVOY_URL)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    rid = request_id or str(uuid.uuid4())

    # llama-stack inference API
    response = client.inference.chat_completion(
        model_id=model,
        messages=messages,
        sampling_params={
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        extra_headers={"x-request-id": rid},
    )

    content = response.completion_message.content
    if hasattr(content, "text"):
        content = content.text

    return {
        "request_id": rid,
        "model": model,
        "content": content,
        "stop_reason": str(response.completion_message.stop_reason),
        "usage": {
            "prompt_tokens": getattr(response, "prompt_tokens", 0),
            "completion_tokens": getattr(response, "completion_tokens", 0),
        },
    }


def send_via_openai_compat(
    prompt: str,
    model: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a chat completion request using the OpenAI-compatible HTTP API.
    Envoy proxies this to the primary LLM and mirrors to the Model Router.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    rid = request_id or str(uuid.uuid4())

    with httpx.Client(timeout=120) as client:
        response = client.post(
            f"{ENVOY_URL}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            headers={
                "Content-Type": "application/json",
                "x-request-id": rid,
            },
        )
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"]
    return {
        "request_id": rid,
        "model": model,
        "content": content,
        "usage": data.get("usage", {}),
        "finish_reason": data["choices"][0].get("finish_reason"),
    }


# ── Collector API helpers ──────────────────────────────────────────────────────

def fetch_comparison(request_id: str) -> Optional[Dict[str, Any]]:
    with httpx.Client(timeout=10) as client:
        try:
            r = client.get(f"{COLLECTOR_URL}/comparisons/{request_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            print(f"[ERROR] Collector unreachable: {exc}")
            return None


def fetch_latest_comparisons(limit: int = 10) -> List[Dict[str, Any]]:
    with httpx.Client(timeout=10) as client:
        try:
            r = client.get(f"{COLLECTOR_URL}/comparisons", params={"limit": limit})
            r.raise_for_status()
            return r.json().get("records", [])
        except httpx.HTTPError as exc:
            print(f"[ERROR] Collector unreachable: {exc}")
            return []


# ── Display helpers ────────────────────────────────────────────────────────────

TERM_WIDTH = 100
DIVIDER = "─" * TERM_WIDTH


def print_primary_response(result: Dict[str, Any]) -> None:
    print("\n" + "=" * TERM_WIDTH)
    print(f"  PRIMARY RESPONSE  (model: {result['model']})")
    print(f"  request_id: {result['request_id']}")
    print("=" * TERM_WIDTH)
    print(textwrap.fill(result["content"], width=TERM_WIDTH))
    usage = result.get("usage", {})
    if usage:
        print(f"\n  Tokens: prompt={usage.get('prompt_tokens', '?')}  "
              f"completion={usage.get('completion_tokens', '?')}")
    print()


def print_comparison(record: Dict[str, Any]) -> None:
    print("\n" + "=" * TERM_WIDTH)
    print(f"  COMPARISON RESULTS  —  request_id: {record['request_id']}")
    print(f"  Original model: {record['original_model']}")
    print(f"  Responses collected: {record['response_count']}")
    print("=" * TERM_WIDTH)

    for idx, resp in enumerate(record.get("responses", []), 1):
        status = "✓" if resp["success"] else "✗"
        latency = (
            f"{resp['latency_seconds']:.2f}s" if resp.get("latency_seconds") else "N/A"
        )
        print(f"\n[{idx}] {status} {resp['llm_name']}  "
              f"(model: {resp['substituted_model']})  "
              f"latency: {latency}")
        print(DIVIDER)
        if resp["success"] and resp.get("content"):
            print(textwrap.fill(resp["content"], width=TERM_WIDTH))
            usage = resp.get("usage") or {}
            if usage:
                print(f"\n  Tokens: prompt={usage.get('prompt_tokens', '?')}  "
                      f"completion={usage.get('completion_tokens', '?')}")
        else:
            print(f"  ERROR: {resp.get('error', 'unknown error')}")

    print("\n" + "=" * TERM_WIDTH + "\n")


def print_comparison_list(records: List[Dict[str, Any]]) -> None:
    if not records:
        print("No comparison records found.")
        return
    print(f"\n{'#':<4} {'request_id':<38} {'model':<35} {'responses':<10} {'created_at'}")
    print(DIVIDER)
    for i, record in enumerate(records, 1):
        created = record.get("created_at", "")[:19].replace("T", " ")
        print(
            f"{i:<4} {record['request_id']:<38} "
            f"{record['original_model']:<35} "
            f"{record['response_count']:<10} {created}"
        )
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LLM Comparison Tool — send prompts and compare responses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python compare_client.py --prompt "What is consciousness?"
              python compare_client.py --prompt "Write a haiku" --max-tokens 60
              python compare_client.py --view abc123-...
              python compare_client.py --list --limit 5
        """),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", "-p", type=str, help="Chat prompt to send")
    group.add_argument("--view", "-v", type=str, metavar="REQUEST_ID",
                       help="View comparison results for a request_id")
    group.add_argument("--list", "-l", action="store_true",
                       help="List recent comparison records")

    parser.add_argument("--model", "-m", type=str, default=PRIMARY_MODEL,
                        help=f"Primary model name (default: {PRIMARY_MODEL})")
    parser.add_argument("--system", "-s", type=str, default=None,
                        help="System prompt")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of records to list (default: 10)")
    parser.add_argument("--wait", type=float, default=3.0,
                        help="Seconds to wait for mirror responses before fetching "
                             "comparison (default: 3.0)")
    parser.add_argument("--no-llama-stack", action="store_true",
                        help="Use raw OpenAI-compatible HTTP instead of llama-stack client")
    parser.add_argument("--request-id", type=str, default=None,
                        help="Override auto-generated request_id")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.list:
        records = fetch_latest_comparisons(limit=args.limit)
        print_comparison_list(records)
        return

    if args.view:
        record = fetch_comparison(args.view)
        if record:
            print_comparison(record)
        else:
            print(f"No comparison found for request_id: {args.view}")
        return

    # Send prompt
    use_llama_stack = USE_LLAMA_STACK_CLIENT and not args.no_llama_stack

    print(f"\nSending prompt via {'llama-stack' if use_llama_stack else 'OpenAI-compat'} "
          f"→ Envoy → {args.model}")
    print(f"Prompt: {args.prompt[:80]}{'...' if len(args.prompt) > 80 else ''}\n")

    rid = args.request_id or str(uuid.uuid4())

    try:
        if use_llama_stack:
            result = send_via_llama_stack(
                prompt=args.prompt,
                model=args.model,
                system_prompt=args.system,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                request_id=rid,
            )
        else:
            result = send_via_openai_compat(
                prompt=args.prompt,
                model=args.model,
                system_prompt=args.system,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                request_id=rid,
            )
    except Exception as exc:
        print(f"[ERROR] Failed to get primary response: {exc}")
        sys.exit(1)

    print_primary_response(result)
    print(f"Waiting {args.wait:.0f}s for mirrored responses to complete...")

    import time
    time.sleep(args.wait)

    record = fetch_comparison(result["request_id"])
    if record:
        print_comparison(record)
    else:
        print(
            f"\nNo comparison data yet. Run again with:\n"
            f"  python compare_client.py --view {result['request_id']}\n"
        )


if __name__ == "__main__":
    main()
