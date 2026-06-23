# LLM Comparison Tool - working

A platform for simultaneously comparing chat responses from multiple Large Language Models.
Sends one request, gets back a side-by-side comparison from every configured LLM — all calls happen concurrently.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Client (Python / any OpenAI-compatible SDK)        │
│            POST /v1/chat/completions  or  /v1/inference/chat_completion│
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      Model Router  (:8000)                            │
│                    Single entry point — no proxy required             │
│                                                                        │
│  On every request:                                                     │
│  1. Fire secondary LLM tasks immediately  ──────────────────────┐     │
│  2. Call primary LLM (streaming or non-streaming)               │     │
│  3. Return primary response to client                           │     │
│  4. Ship primary response to Collector                          │     │
│                                                                 │     │
│  Steps 1 and 2 start at the SAME instant.                       │     │
│  The client never waits for secondary calls.                    │     │
└────────────────┬────────────────────────────────────────────────┘     │
                 │  all calls via /v1/inference/chat_completion          │
                 ▼                                                        │
┌────────────────────────────────────┐                                   │
│         llama-stack  (:8321)       │ ◀─────────────────────────────────┘
│   Single server, all providers     │  (secondary calls also go here)
│                                    │
│  provider: vllm    → primary model │
│  provider: openai  → gpt-4o        │
│  provider: anthropic → claude      │
│  provider: together → llama-70b    │
└────────────────┬───────────────────┘
                 │ routes to correct backend
        ┌────────┴────────┐
        ▼                 ▼
   vLLM              Cloud APIs
   (self-hosted)  (OpenAI, Anthropic…)
                                   │
               all responses ──────▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Collector  (:8001)                                │
│               FastAPI + Redis  — keyed by x-request-id               │
│                                                                        │
│  GET /comparisons/{request_id}   — side-by-side results               │
│  GET /comparisons                — list recent comparisons            │
└──────────────────────────────────────────────────────────────────────┘
```

## How it works

1. The client sends a single `POST /v1/chat/completions` to the **Model Router** with the primary model name.
2. The router **immediately fires background tasks** for every enabled secondary LLM — they start running concurrently before the primary even responds.
3. The router calls the **primary LLM** through llama-stack. If streaming is requested, tokens are forwarded to the client as they arrive (low latency to first token).
4. The client receives only the primary response. Secondaries are invisible to the client and never block it.
5. All responses — primary and secondary — are shipped to the **Collector** and stored in Redis under the same `request_id`.
6. Query `GET /comparisons/{request_id}` to retrieve the full side-by-side comparison.

## Key design points

- **No Envoy required** — concurrent fan-out is handled in Python via `asyncio.create_task()`
- **All LLMs go through llama-stack** — single unified inference API, no per-provider code in the router
- **Primary called exactly once** — the exact response the client receives is what gets stored in the collector
- **Streaming supported** — `stream: true` works on both `/v1/chat/completions` and `/v1/inference/chat_completion`
- **Per-LLM failure isolation** — one LLM timing out does not affect the primary response or other secondaries

## Components

| Component | Port | Description |
|---|---|---|
| `model-router/` | 8000 | Entry point. Concurrent fan-out to all LLMs via llama-stack. |
| `llama-stack/` | 8321 | Unified inference server. Fronts every LLM (primary + secondaries). |
| `collector/` | 8001 | Stores and serves comparison results (FastAPI + Redis). |
| `client/` | — | Python CLI for sending prompts and viewing comparisons. |
| `openshift/` | — | OpenShift 4.21 deployment manifests. |
| `helm/llm-comparison/` | — | Helm chart for full deployment. |

## Quick Start (Local with podman-compose)

### Prerequisites
- Podman + podman-compose (`pip install podman-compose`)
- A running vLLM instance **or** GPU available for the bundled vLLM service
- API keys for any cloud providers you want to compare against

### Setup

```bash
cp .env.example .env
# Fill in OPENAI_API_KEY and/or ANTHROPIC_API_KEY
# Set HUGGING_FACE_HUB_TOKEN if your model requires authentication
# Adjust PRIMARY_MODEL to match the model loaded in your vLLM instance

podman-compose up -d
podman-compose ps
```

> **vLLM note:** The compose file includes a bundled `vllm` service. GPU passthrough
> on Linux is enabled by uncommenting the `deploy.resources` block in `docker-compose.yaml`.
> On macOS/Windows or when running vLLM externally, set `VLLM_URL` in `.env` and
> remove/disable the `vllm` service.

### Send a prompt and compare

```bash
cd client
pip install -r requirements.txt

# Non-streaming
python compare_client.py --prompt "Explain transformers in machine learning"

# View results for a specific request
python compare_client.py --view <request_id>

# List recent comparisons
python compare_client.py --list

# Streaming (raw curl)
curl -N http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"meta-llama/Llama-3.1-8B-Instruct","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### Introspect the system

```bash
# What models are configured?
curl http://localhost:8080/models | python3 -m json.tool

# What models does llama-stack know about?
curl http://localhost:8321/v1/models | python3 -m json.tool

# What LLM providers are registered?
curl http://localhost:8321/v1/providers | python3 -m json.tool

# Collector health
curl http://localhost:8001/health
```

## Configuration

### Add or remove secondary LLMs

Edit `model-router/config.yaml`:

```yaml
llms:
  - name: "openai-gpt4o"
    model_id: "gpt-4o"       # must match a model registered in llama-stack's run.yaml
    timeout: 60
    enabled: true

  - name: "anthropic-claude-sonnet"
    model_id: "claude-3-5-sonnet-20241022"
    timeout: 60
    enabled: true
```

And add the corresponding model entry to `openshift/llama-stack/run.yaml` (or `helm/llm-comparison/values.yaml` for Helm-managed deployments):

```yaml
models:
  - model_id: "gpt-4o"
    provider_id: openai
    provider_model_id: "gpt-4o"
    model_type: llm
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PRIMARY_MODEL` | `meta-llama/Llama-3.1-8B-Instruct` | Primary model name (must match what vLLM is serving) |
| `VLLM_URL` | `http://vllm:8000` | vLLM backend URL |
| `HUGGING_FACE_HUB_TOKEN` | — | HF token for gated models |
| `OPENAI_API_KEY` | — | OpenAI API key (used by llama-stack) |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (used by llama-stack) |
| `TOGETHER_API_KEY` | — | Together AI API key (used by llama-stack) |
| `COLLECTOR_URL` | `http://collector:8001` | Collector service URL |
| `REDIS_URL` | `redis://redis:6379` | Redis URL |
| `LOG_LEVEL` | `INFO` | Log level for Python services |
| `RECORD_TTL_SECONDS` | `86400` | How long to keep results in Redis |

> **Note:** API keys are injected into the **llama-stack** pod, not the model-router. The router has no credentials — all provider authentication is handled by llama-stack.

## Helm Chart (Recommended for OpenShift)

```bash
# Dry-run
helm template llm-comparison helm/llm-comparison \
  -n llm-comparison \
  -f helm/llm-comparison/values-openshift.yaml \
  --set apiKeys.openai=$OPENAI_API_KEY \
  --set apiKeys.anthropic=$ANTHROPIC_API_KEY

# Install
helm install llm-comparison helm/llm-comparison \
  -n llm-comparison --create-namespace \
  -f helm/llm-comparison/values-openshift.yaml \
  --set apiKeys.openai=$OPENAI_API_KEY \
  --set apiKeys.anthropic=$ANTHROPIC_API_KEY \
  --set llamaStack.backendUrl=http://vllm:8000

# Check rollout
oc rollout status deployment -n llm-comparison
oc get routes -n llm-comparison
```

### Common Helm operations

```bash
# Enable Together AI as an additional secondary
helm upgrade llm-comparison helm/llm-comparison -n llm-comparison \
  --reuse-values \
  --set "modelRouter.llms[2].enabled=true" \
  --set apiKeys.together=$TOGETHER_API_KEY

# Scale the model router
helm upgrade llm-comparison helm/llm-comparison -n llm-comparison \
  --reuse-values \
  --set modelRouter.replicas=3

# Lint the chart
helm lint helm/llm-comparison/

# Uninstall
helm uninstall llm-comparison -n llm-comparison
```

## Building Container Images

All container images use `Containerfile` (Podman default). Build with:

```bash
podman build -t quay.io/your-org/llm-comparison-model-router:latest \
             -f model-router/Containerfile model-router/

podman build -t quay.io/your-org/llm-comparison-llama-stack:latest \
             -f llama-stack/Containerfile llama-stack/

podman build -t quay.io/your-org/llm-comparison-collector:latest \
             -f collector/Containerfile collector/

# Push all images
podman push quay.io/your-org/llm-comparison-model-router:latest
podman push quay.io/your-org/llm-comparison-llama-stack:latest
podman push quay.io/your-org/llm-comparison-collector:latest
```

## OpenShift Deployment (Raw Manifests)

```bash
oc apply -f openshift/namespace.yaml
oc apply -f openshift/configmap.yaml

# Fill in API keys first
cp openshift/secret.yaml.example openshift/secret.yaml
oc apply -f openshift/secret.yaml

oc apply -f openshift/redis/
oc apply -f openshift/llama-stack/
oc apply -f openshift/collector/
oc apply -f openshift/model-router/
oc apply -f openshift/routes.yaml

oc get pods -n llm-comparison
oc get routes -n llm-comparison
```

## Collector API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/comparisons/{request_id}` | GET | All LLM responses for a request |
| `/comparisons` | GET | List recent comparisons (`?limit=20&offset=0`) |
| `/comparisons/{request_id}` | DELETE | Delete a single record |
| `/comparisons` | DELETE | Clear all records |
| `/health` | GET | Health check (includes Redis connectivity) |

## Model Router API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible inference (streaming supported) |
| `/v1/inference/chat_completion` | POST | llama-stack native inference (streaming supported) |
| `/health` | GET | Health check |
| `/models` | GET | Lists primary + all configured secondary LLMs |

## Target Platform

Designed for **OpenShift 4.21**. All containers run as non-root (UID 1001) and drop all Linux capabilities for SCC compatibility.
