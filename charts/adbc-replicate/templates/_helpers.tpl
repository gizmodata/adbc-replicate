{{- define "adbc-replicate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "adbc-replicate.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "adbc-replicate.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "adbc-replicate.name" .) | trunc 52 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "adbc-replicate.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "adbc-replicate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "adbc-replicate.secretName" -}}
{{- if .Values.existingSecret }}{{ .Values.existingSecret }}{{ else }}{{ include "adbc-replicate.fullname" . }}-env{{ end }}
{{- end }}
