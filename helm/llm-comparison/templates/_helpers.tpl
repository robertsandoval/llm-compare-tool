{{/*
Expand the name of the chart.
*/}}
{{- define "llm-comparison.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "llm-comparison.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label value.
*/}}
{{- define "llm-comparison.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Namespace helper — respects namespaceOverride, then .Release.Namespace.
*/}}
{{- define "llm-comparison.namespace" -}}
{{- if .Values.namespaceOverride }}
{{- .Values.namespaceOverride }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "llm-comparison.labels" -}}
helm.sh/chart: {{ include "llm-comparison.chart" . }}
app.kubernetes.io/name: {{ include "llm-comparison.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels — stable subset used in matchLabels.
*/}}
{{- define "llm-comparison.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llm-comparison.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "llm-comparison.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "llm-comparison.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve an image reference, optionally prepending global.imageRegistry unless
the component provides a registryOverride.
Usage: {{ include "llm-comparison.image" (dict "image" .Values.envoy.image "global" .Values.global) }}
*/}}
{{- define "llm-comparison.image" -}}
{{- $registry := "" }}
{{- if .image.registryOverride }}
  {{- $registry = .image.registryOverride }}
{{- else if .global.imageRegistry }}
  {{- $registry = .global.imageRegistry }}
{{- end }}
{{- if $registry }}
  {{- printf "%s/%s:%s" $registry .image.repository .image.tag }}
{{- else }}
  {{- printf "%s:%s" .image.repository .image.tag }}
{{- end }}
{{- end }}

{{/*
Name of the API-keys secret.
*/}}
{{- define "llm-comparison.apiKeySecret" -}}
{{- if .Values.apiKeys.existingSecret }}
{{- .Values.apiKeys.existingSecret }}
{{- else }}
{{- include "llm-comparison.fullname" . }}-api-keys
{{- end }}
{{- end }}

{{/*
Envoy service name used as a cluster DNS target.
*/}}
{{- define "llm-comparison.envoyName" -}}
{{- printf "%s-envoy" (include "llm-comparison.fullname" .) }}
{{- end }}

{{- define "llm-comparison.modelRouterName" -}}
{{- printf "%s-model-router" (include "llm-comparison.fullname" .) }}
{{- end }}

{{- define "llm-comparison.collectorName" -}}
{{- printf "%s-collector" (include "llm-comparison.fullname" .) }}
{{- end }}

{{- define "llm-comparison.redisName" -}}
{{- printf "%s-redis" (include "llm-comparison.fullname" .) }}
{{- end }}

{{- define "llm-comparison.llamaStackName" -}}
{{- printf "%s-llama-stack" (include "llm-comparison.fullname" .) }}
{{- end }}

{{/*
Redis URL built from the internal service name.
*/}}
{{- define "llm-comparison.redisUrl" -}}
{{- printf "redis://%s:6379" (include "llm-comparison.redisName" .) }}
{{- end }}

{{/*
Collector URL built from the internal service name.
*/}}
{{- define "llm-comparison.collectorUrl" -}}
{{- printf "http://%s:8001" (include "llm-comparison.collectorName" .) }}
{{- end }}

{{/*
llama-stack primary URL built from the internal service name.
*/}}
{{- define "llm-comparison.llamaStackUrl" -}}
{{- printf "http://%s:8321/v1" (include "llm-comparison.llamaStackName" .) }}
{{- end }}

{{/*
Render the model-router config.yaml content from values.
All LLMs are reached via the shared llama-stack server.
*/}}
{{- define "llm-comparison.modelRouterConfig" -}}
llama_stack:
  url: {{ printf "http://%s:8321" (include "llm-comparison.llamaStackName" .) | quote }}

llms:
{{- range .Values.modelRouter.llms }}
  - name: {{ .name | quote }}
    model_id: {{ .modelId | quote }}
    timeout: {{ .timeout }}
    enabled: {{ .enabled }}
{{- end }}

collector:
  url: {{ include "llm-comparison.collectorUrl" . | quote }}
  timeout: {{ .Values.modelRouter.collector.timeout }}
  capture_primary: {{ .Values.modelRouter.collector.capturePrimary }}
  primary_name: {{ .Values.modelRouter.collector.primaryName | quote }}
  primary_model_env: "PRIMARY_MODEL"
  primary_model_default: {{ .Values.llamaStack.primaryModel | quote }}
{{- end }}

{{/*
Render the llama-stack run.yaml content from values.
Registers all configured models (primary + all secondary LLMs) with their providers.
*/}}
{{- define "llm-comparison.llamaStackRunConfig" -}}
version: '2'
image_name: llm-comparison-multi-provider

providers:
  inference:
  # Self-hosted primary backend
{{- if eq .Values.llamaStack.provider "vllm" }}
  - provider_id: vllm
    provider_type: remote::vllm
    config:
      url: {{ .Values.llamaStack.backendUrl | quote }}
{{- else }}
  - provider_id: ollama
    provider_type: remote::ollama
    config:
      url: {{ .Values.llamaStack.backendUrl | quote }}
{{- end }}
  # Cloud providers (credentials injected via env vars from Secret)
  - provider_id: openai
    provider_type: remote::openai
    config:
      api_key: ${env.OPENAI_API_KEY}
  - provider_id: anthropic
    provider_type: remote::anthropic
    config:
      api_key: ${env.ANTHROPIC_API_KEY}
  - provider_id: together
    provider_type: remote::together
    config:
      api_key: ${env.TOGETHER_API_KEY}

  memory:
  - provider_id: faiss
    provider_type: inline::faiss
    config:
      kvstore:
        type: sqlite
        namespace: null
        db_path: /root/.llama/faiss_store.db

metadata_store:
  namespace: null
  type: sqlite
  db_path: /root/.llama/registry.db

models:
# Primary self-hosted model
- model_id: {{ .Values.llamaStack.primaryModel | quote }}
  provider_id: {{ if eq .Values.llamaStack.provider "vllm" }}vllm{{ else }}ollama{{ end }}
  provider_model_id: {{ .Values.llamaStack.primaryModelId | default .Values.llamaStack.primaryModel | quote }}
  model_type: llm
# Secondary models — one entry per enabled LLM in modelRouter.llms
{{- range .Values.modelRouter.llms }}
- model_id: {{ .modelId | quote }}
  provider_id: {{ .providerId | quote }}
  provider_model_id: {{ .providerModelId | default .modelId | quote }}
  model_type: llm
{{- end }}
{{- end }}
