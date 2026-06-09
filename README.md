# LLM Comparison Tool

A platform for simultaneously comparing chat responses from multiple Large Language Models using **Envoy traffic mirroring** and **llama-stack** for API standardization.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Client (Python)                              │
│              llama-stack-client / OpenAI-compatible SDK              │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  POST /v1/chat/completions
                            │  model: "primary-model"
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Envoy Proxy (:8080)                           │
│                      Traffic Mirroring                               │
└───────────┬────────────────────────────────┬────────────────────────┘
            │ Primary (100%)                 │ Mirror (100%, fire-and-forget)
            ▼                                ▼
┌────────────────────────┐      ┌────────────────────────────────────┐
│   llama-stack Server   │      │      Model Router Service           │
│   (Primary LLM 1)      │      │      (Python / FastAPI)             │
│   e.g. Llama-3.1-8B    │      │                                     │
│                        │      │  Receives mirrored request and      │
│   Returns response to  │      │  fans out to multiple LLMs with     │
│   client via Envoy     │      │  substituted model names:           │
└────────────────────────┘      │                                     │
                                │  ┌──────────────────────────────┐  │
                                │  │ LLM 2: GPT-4o (OpenAI)       │  │
                                │  │ LLM 3: claude-3-5-sonnet     │  │
                                │  │ LLM 4: llama3.1:70b (Ollama) │  │
                                │  │ LLM N: ...                   │  │
                                │  └──────────────────────────────┘  │
                                │              │                      │
                                └──────────────┼──────────────────────┘
                                               │ All responses stored
                                               ▼
                                ┌────────────────────────────────────┐
                                │       Collector Service             │
                                │       (Python / FastAPI)            │
                                │       Backed by Redis               │
                                │                                     │
                                │  GET /comparisons/{request_id}      │
                                │  GET /comparisons/latest            │
                                └────────────────────────────────────┘
```

## How Traffic Mirroring Works

1. The Python client sends a single `POST /v1/chat/completions` request to **Envoy** using the primary model name (e.g., `meta-llama/Llama-3.1-8B-Instruct`).
2. Envoy **forwards** the request to the primary **llama-stack** server (LLM 1) and simultaneously **mirrors** an identical copy to the **Model Router**.
3. The client receives the response from LLM 1. The mirror is fire-and-forget from Envoy's perspective.
4. The **Model Router** receives the mirrored request, rewrites the `model` field to each configured secondary LLM's model name, and calls all of them **concurrently** using `asyncio`.
5. Each LLM response — plus the primary — is stored in the **Collector** service (Redis-backed) keyed by a request correlation ID.
6. Use the **Collector API** or the provided client script to fetch and display a side-by-side comparison.

## Components

| Component | Language | Description |
|---|---|---|
| `envoy/` | YAML | Envoy proxy config with traffic mirroring |
| `model-router/` | Python (FastAPI) | Receives mirrored requests, substitutes model names, fans out |
| `collector/` | Python (FastAPI) | Stores and serves LLM response comparisons (Redis-backed) |
| `client/` | Python | llama-stack-based client + comparison viewer |
| `openshift/` | YAML | OpenShift 4.21 deployment manifests |

## Quick Start (Local with Docker Compose)

### Prerequisites
- Docker + Docker Compose
- At least one LLM backend (Ollama for local, or API keys for cloud providers)

### Setup

```bash
# Copy and fill in your environment variables
cp .env.example .env

# Start all services
docker compose up -d

# Wait for services to be ready
docker compose ps
```

### Run a comparison

```bash
cd client
pip install -r requirements.txt

# Send a prompt and compare results
python compare_client.py --prompt "Explain quantum entanglement in simple terms"

# View latest comparison results
python compare_client.py --view-latest
```

## Configuration

### Model Router (`model-router/config.yaml`)

Add or remove LLMs in the model router configuration:

```yaml
llms:
  - name: "openai-gpt4o"
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o"
    api_key_env: "OPENAI_API_KEY"
    provider: "openai"

  - name: "anthropic-claude"
    base_url: "https://api.anthropic.com/v1"
    model: "claude-3-5-sonnet-20241022"
    api_key_env: "ANTHROPIC_API_KEY"
    provider: "anthropic"

  - name: "ollama-llama70b"
    base_url: "http://ollama:11434/v1"
    model: "llama3.1:70b"
    api_key_env: ""
    provider: "openai_compatible"
```

### Primary LLM (llama-stack `run.yaml`)

Configure the llama-stack server provider in `openshift/llama-stack/run.yaml` or set `LLAMA_STACK_CONFIG` in your environment.

## Helm Chart (Recommended)

The chart lives at `helm/llm-comparison/` and supports both OpenShift (Routes) and vanilla Kubernetes (Ingress).

### Install on OpenShift 4.21

```bash
# Dry-run to preview all rendered manifests
helm template llm-comparison helm/llm-comparison \
  -n llm-comparison \
  -f helm/llm-comparison/values-openshift.yaml \
  --set apiKeys.openai=$OPENAI_API_KEY \
  --set apiKeys.anthropic=$ANTHROPIC_API_KEY

# Install (creates namespace automatically)
helm install llm-comparison helm/llm-comparison \
  -n llm-comparison --create-namespace \
  -f helm/llm-comparison/values-openshift.yaml \
  --set apiKeys.openai=$OPENAI_API_KEY \
  --set apiKeys.anthropic=$ANTHROPIC_API_KEY

# Check rollout
oc rollout status deployment -n llm-comparison
oc get routes -n llm-comparison
```

### Install on Vanilla Kubernetes

```bash
helm install llm-comparison helm/llm-comparison \
  -n llm-comparison --create-namespace \
  --set routes.enabled=false \
  --set ingress.enabled=true \
  --set "ingress.hosts[0].host=llm-comparison.example.com" \
  --set "ingress.hosts[0].paths[0].path=/" \
  --set "ingress.hosts[0].paths[0].pathType=Prefix" \
  --set apiKeys.openai=$OPENAI_API_KEY \
  --set apiKeys.anthropic=$ANTHROPIC_API_KEY
```

### Common Helm Operations

```bash
# Upgrade with new values
helm upgrade llm-comparison helm/llm-comparison -n llm-comparison \
  -f helm/llm-comparison/values-openshift.yaml

# Enable an additional LLM (Together AI)
helm upgrade llm-comparison helm/llm-comparison -n llm-comparison \
  --reuse-values \
  --set "modelRouter.llms[2].enabled=true" \
  --set apiKeys.together=$TOGETHER_API_KEY

# Lint the chart
helm lint helm/llm-comparison/

# Uninstall
helm uninstall llm-comparison -n llm-comparison
```

### Chart Values Reference

| Key | Default | Description |
|---|---|---|
| `global.imageRegistry` | `""` | Prefix for all image refs (set to your mirror registry) |
| `apiKeys.existingSecret` | `""` | Use a pre-created Secret instead of chart-managed one |
| `apiKeys.openai` | `""` | OpenAI API key |
| `apiKeys.anthropic` | `""` | Anthropic API key |
| `modelRouter.llms` | see values.yaml | List of secondary LLMs with model name substitutions |
| `llamaStack.primaryModel` | `meta-llama/Llama-3.1-8B-Instruct` | Primary model name |
| `llamaStack.provider` | `ollama` | Backend: `ollama` or `vllm` |
| `llamaStack.backendUrl` | `http://ollama:11434` | URL of the inference backend |
| `redis.persistence.size` | `1Gi` | Redis PVC size |
| `llamaStack.persistence.size` | `10Gi` | llama-stack PVC size |
| `routes.enabled` | `true` | Create OpenShift Routes |
| `ingress.enabled` | `false` | Create Kubernetes Ingress |

## OpenShift Deployment (Raw Manifests)

```bash
# Create the namespace
oc apply -f openshift/namespace.yaml

# Create secrets (fill in API keys first)
cp openshift/secret.yaml.example openshift/secret.yaml
# Edit openshift/secret.yaml with your API keys
oc apply -f openshift/secret.yaml

# Create ConfigMaps
oc apply -f openshift/configmap.yaml
oc apply -f openshift/envoy/configmap.yaml

# Deploy all services
oc apply -f openshift/redis/
oc apply -f openshift/llama-stack/
oc apply -f openshift/collector/
oc apply -f openshift/model-router/
oc apply -f openshift/envoy/
oc apply -f openshift/routes.yaml

# Verify deployments
oc get pods -n llm-comparison
oc get routes -n llm-comparison
```

## API Reference

### Collector Service

| Endpoint | Method | Description |
|---|---|---|
| `/comparisons/{request_id}` | GET | Fetch all LLM responses for a request ID |
| `/comparisons/latest` | GET | List most recent comparisons |
| `/comparisons/latest?limit=10` | GET | List N most recent comparisons |
| `/health` | GET | Health check |

### Model Router

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | Receives mirrored requests (OpenAI format) |
| `/health` | GET | Health check |
| `/models` | GET | List configured secondary LLMs |

## Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `TOGETHER_API_KEY` | Together AI API key |
| `OLLAMA_URL` | Ollama server URL (default: `http://ollama:11434`) |
| `COLLECTOR_URL` | Collector service URL (default: `http://collector:8001`) |
| `REDIS_URL` | Redis URL (default: `redis://redis:6379`) |
| `PRIMARY_LLM_URL` | Primary llama-stack server URL |
| `PRIMARY_MODEL` | Primary model name for the client |

## Target Platform

Designed and tested for **OpenShift 4.21**. Manifests use:
- `apps/v1` Deployments
- OpenShift `Route` resources (no Ingress required)
- Non-root container security contexts (compatible with OpenShift SCCs)
- ConfigMaps for Envoy and application configuration
