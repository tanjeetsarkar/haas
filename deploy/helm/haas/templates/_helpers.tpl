{{/* Chart name. */}}
{{- define "haas.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "haas.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "haas.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release-wide selector labels (used by NetworkPolicies). */}}
{{- define "haas.releaseSelector" -}}
app.kubernetes.io/name: {{ include "haas.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Common labels. Call with (dict "root" $ "component" "api"). */}}
{{- define "haas.labels" -}}
helm.sh/chart: {{ include "haas.chart" .root }}
{{ include "haas.selectorLabels" . }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: haas
{{- end -}}

{{- define "haas.selectorLabels" -}}
{{ include "haas.releaseSelector" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "haas.componentName" -}}
{{- printf "%s-%s" (include "haas.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "haas.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/* Env from a map of NAME -> {name, key} Secret references. */}}
{{- define "haas.secretEnv" -}}
{{- range $envName, $ref := . }}
- name: {{ $envName }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "secret name for %s" $envName) $ref.name }}
      key: {{ required (printf "secret key for %s" $envName) $ref.key }}
{{- end }}
{{- end -}}

{{/* Env from a plain map. */}}
{{- define "haas.plainEnv" -}}
{{- range $envName, $value := . }}
- name: {{ $envName }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- end -}}

{{/* Common env for app containers. Call with (dict "root" $ "c" .Values.api "redis" true). */}}
{{- define "haas.appEnv" -}}
- name: HAAS_PROFILE
  value: {{ .root.Values.profile | quote }}
- name: HOME
  value: /tmp
- name: PYTHONDONTWRITEBYTECODE
  value: "1"
{{- include "haas.plainEnv" .c.env }}
{{- if .redis }}
{{- include "haas.secretEnv" .root.Values.redis.secretEnv }}
{{- end }}
{{- include "haas.secretEnv" .c.secretEnv }}
{{- end -}}

{{- define "haas.podSecurityContext" -}}
{{- toYaml .Values.podSecurityContext }}
{{- end -}}

{{- define "haas.containerSecurityContext" -}}
{{- toYaml .Values.containerSecurityContext }}
{{- end -}}

{{/* Scratch volumes: /tmp emptyDir plus the scratchpad (emptyDir or shared PVC). */}}
{{- define "haas.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.scratchpad.sizeLimit }}
- name: scratchpad
{{- if .Values.scratchpad.persistentVolumeClaim }}
  persistentVolumeClaim:
    claimName: {{ .Values.scratchpad.persistentVolumeClaim }}
{{- else }}
  emptyDir:
    sizeLimit: {{ .Values.scratchpad.sizeLimit }}
{{- end }}
{{- end -}}

{{- define "haas.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: scratchpad
  mountPath: /tmp/haas_scratchpad
{{- end -}}

{{- define "haas.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
