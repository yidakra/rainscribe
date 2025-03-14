{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "rainscribe.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "rainscribe.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "rainscribe.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "rainscribe.labels" -}}
helm.sh/chart: {{ include "rainscribe.chart" . }}
{{ include "rainscribe.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "rainscribe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rainscribe.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "rainscribe.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "rainscribe.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Common environment variables
*/}}
{{- define "rainscribe.env" -}}
- name: GLADIA_API_KEY
  value: {{ .Values.global.env.GLADIA_API_KEY | quote }}
- name: GLADIA_API_URL
  value: {{ .Values.global.env.GLADIA_API_URL | quote }}
- name: HLS_STREAM_URL
  value: {{ .Values.global.env.HLS_STREAM_URL | quote }}
- name: AUDIO_SAMPLE_RATE
  value: {{ .Values.global.env.AUDIO_SAMPLE_RATE | quote }}
- name: AUDIO_BIT_DEPTH
  value: {{ .Values.global.env.AUDIO_BIT_DEPTH | quote }}
- name: AUDIO_CHANNELS
  value: {{ .Values.global.env.AUDIO_CHANNELS | quote }}
- name: TRANSCRIPTION_LANGUAGE
  value: {{ .Values.global.env.TRANSCRIPTION_LANGUAGE | quote }}
- name: TRANSLATION_LANGUAGES
  value: {{ .Values.global.env.TRANSLATION_LANGUAGES | quote }}
- name: WEBVTT_SEGMENT_DURATION
  value: {{ .Values.global.env.WEBVTT_SEGMENT_DURATION | quote }}
- name: SHARED_VOLUME_PATH
  value: {{ .Values.global.env.SHARED_VOLUME_PATH | quote }}
{{- end -}} 