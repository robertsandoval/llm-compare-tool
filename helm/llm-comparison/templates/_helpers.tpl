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
Namespace helper.
*/}}
{{- define "llm-comparison.namespace" -}}
{{- if .Values.namespaceOverride }}
{{- .Values.namespaceOverride }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Common labels.
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
Selector labels.
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
Resolve an image reference, optionally prepending global.imageRegistry.
Usage: {{ include "llm-comparison.image" (dict "image" .Values.modelRouter.image "global" .Values.global) }}
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
Service name helpers.
*/}}
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

{{- define "llm-comparison.redisUrl" -}}
{{- printf "redis://%s:6379" (include "llm-comparison.redisName" .) }}
{{- end }}

{{- define "llm-comparison.collectorUrl" -}}
{{- printf "http://%s:8001" (include "llm-comparison.collectorName" .) }}
{{- end }}

{{- define "llm-comparison.llamaStackUrl" -}}
{{- printf "http://%s:8321" (include "llm-comparison.llamaStackName" .) }}
{{- end }}

{{/*
Render the model-router config.yaml.
Primary LLM is configured as a first-class object; secondaries in llms list.
*/}}
{{- define "llm-comparison.modelRouterConfig" -}}
llama_stack:
  url: {{ include "llm-comparison.llamaStackUrl" . | quote }}

primary:
  name: {{ .Values.modelRouter.primary.name | quote }}
  model_id_env: "PRIMARY_MODEL"
  model_id: {{ .Values.llamaStack.primaryModel | quote }}
  timeout: {{ .Values.modelRouter.primary.timeout }}

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
{{- end }}

{{/*
Render the llama-stack run.yaml.
Registers all providers and all models (primary + secondary LLMs).
*/}}
{{- define "llm-comparison.llamaStackRunConfig" -}}
version: '2'
image_name: llm-comparison-multi-provider

providers:
  inference:
  - provider_id: vllm
    provider_type: remote::vllm
    config:
      url: {{ .Values.llamaStack.backendUrl | quote }}
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
- model_id: {{ .Values.llamaStack.primaryModel | quote }}
  provider_id: vllm
  provider_model_id: {{ .Values.llamaStack.primaryModelId | default .Values.llamaStack.primaryModel | quote }}
  model_type: llm
{{- range .Values.modelRouter.llms }}
- model_id: {{ .modelId | quote }}
  provider_id: {{ .providerId | quote }}
  provider_model_id: {{ .providerModelId | default .modelId | quote }}
  model_type: llm
{{- end }}
{{- end }}
