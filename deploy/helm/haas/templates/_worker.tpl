{{/*
Celery worker / long-running process Deployment.
Call with (dict "root" $ "component" "orchestrator" "c" .Values.orchestrator "nodeName" "orchestrator").
*/}}
{{- define "haas.workerDeployment" -}}
{{- $root := .root }}
{{- $c := .c }}
{{- $name := include "haas.componentName" (dict "root" $root "component" .component) }}
{{- $kedaManaged := and $root.Values.keda.enabled (has .component (list "orchestrator" "tool-worker")) }}
{{- $scaled := or (dig "autoscaling" "enabled" false $c) $kedaManaged }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels:
    {{- include "haas.labels" (dict "root" $root "component" .component) | nindent 4 }}
spec:
  {{- if not $scaled }}
  replicas: {{ $c.replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  {{- if eq .component "maintenance" }}
  strategy:
    type: Recreate
  {{- end }}
  selector:
    matchLabels:
      {{- include "haas.selectorLabels" (dict "root" $root "component" .component) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "haas.labels" (dict "root" $root "component" .component) | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
        {{- with $c.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      serviceAccountName: {{ $name }}
      automountServiceAccountToken: {{ $root.Values.serviceAccount.automountToken }}
      enableServiceLinks: false
      terminationGracePeriodSeconds: {{ $c.terminationGracePeriodSeconds }}
      {{- with $c.runtimeClassName }}
      runtimeClassName: {{ . }}
      {{- end }}
      {{- with $root.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- include "haas.podSecurityContext" $root | nindent 8 }}
      {{- with include "haas.scheduling" $c }}{{ . | nindent 6 }}{{- end }}
      containers:
        - name: {{ .component }}
          image: {{ include "haas.image" $root }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command:
          {{- if $c.command }}
            {{- toYaml $c.command | nindent 12 }}
          {{- else }}
            - celery
            - -A
            - app.worker.celery_app
            - worker
            - -Q
            - {{ join "," $c.queues }}
            - -c
            - {{ $c.concurrency | quote }}
            - --loglevel=INFO
            - -n
            - {{ printf "%s@%%h" .nodeName }}
          {{- end }}
          envFrom:
            - configMapRef:
                name: {{ include "haas.fullname" $root }}-config
          env:
            {{- include "haas.appEnv" (dict "root" $root "c" $c "redis" true) | nindent 12 }}
          securityContext:
            {{- include "haas.containerSecurityContext" $root | nindent 12 }}
          resources:
            {{- toYaml $c.resources | nindent 12 }}
          {{- with $c.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            {{- include "haas.volumeMounts" $root | nindent 12 }}
      volumes:
        {{- include "haas.volumes" $root | nindent 8 }}
{{- end -}}
