{{/* deploy/helm/sh-lib/templates/_component.tpl

`sh-lib.component` renders a component chart from its values: one Deployment per entry in
`workloads`, its Service and PVCs, and the chart's ConfigMaps. Schema: plan Task 2. */}}

{{- define "sh-lib.name" -}}
{{- if eq .key "main" -}}{{ .root.Chart.Name }}{{- else -}}{{ .root.Chart.Name }}-{{ .key }}{{- end -}}
{{- end -}}

{{- define "sh-lib.global" -}}
{{- toYaml (.Values.global | default dict) -}}
{{- end -}}

{{- define "sh-lib.image" -}}
{{- $g := fromYaml (include "sh-lib.global" .root) -}}
{{- $gi := $g.image | default dict -}}
{{- if kindIs "string" .image -}}
{{ .image }}
{{- else -}}
{{- $reg := $gi.registry | default "" -}}
{{- $tag := .image.tag | default $gi.tag | default "dev" -}}
{{ if $reg }}{{ $reg }}/{{ end }}{{ .image.repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{- define "sh-lib.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: smarthealth
app.kubernetes.io/component: {{ .root.Chart.Name }}
{{- end -}}

{{- define "sh-lib.component" -}}
{{- $root := . -}}
{{- range $key, $w := .Values.workloads }}
{{- $name := include "sh-lib.name" (dict "root" $root "key" $key) }}
{{- $ctx := dict "root" $root "w" $w "name" $name "key" $key }}
---
{{ include "sh-lib.deployment" $ctx }}
{{- if and $w.service $w.service.ports }}
---
{{ include "sh-lib.service" $ctx }}
{{- end }}
{{- range $vname, $v := $w.volumes }}
{{- if $v.persistent }}
---
{{ include "sh-lib.pvc" (dict "root" $root "name" $name "vname" $vname "v" $v) }}
{{- end }}
{{- end }}
{{- end }}
{{- range $cname, $cm := .Values.configMaps }}
---
{{ include "sh-lib.configmap" (dict "root" $root "cname" $cname "cm" $cm) }}
{{- end }}
{{- end -}}

{{- define "sh-lib.deployment" -}}
{{- $root := .root -}}{{- $w := .w -}}{{- $name := .name -}}
{{- $g := fromYaml (include "sh-lib.global" $root) -}}
{{- $secret := $g.secretName | default "smarthealth-secrets" -}}
{{- $pull := (($g.image | default dict).pullPolicy) | default "IfNotPresent" -}}
{{- $image := include "sh-lib.image" (dict "root" $root "image" $w.image) -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels:
    {{- include "sh-lib.labels" (dict "root" $root "name" $name) | nindent 4 }}
spec:
  replicas: 1
  strategy:
    type: {{ if $w.volumes }}Recreate{{ else }}RollingUpdate{{ end }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ $name }}
  template:
    metadata:
      labels:
        {{- include "sh-lib.labels" (dict "root" $root "name" $name) | nindent 8 }}
      annotations:
        checksum/values: {{ toYaml $w | sha256sum | trunc 16 }}
    spec:
      enableServiceLinks: false
      {{- with $w.securityContext }}
      securityContext:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- if or $w.waitFor $w.initContainers }}
      initContainers:
        {{- range $i, $target := $w.waitFor }}
        {{- $hp := splitList ":" $target }}
        - name: wait-{{ $i }}
          image: busybox:1.36
          command: ["sh", "-c", "until nc -z {{ index $hp 0 }} {{ index $hp 1 }}; do echo waiting for {{ $target }}; sleep 2; done"]
          resources: {requests: {memory: 8Mi, cpu: 5m}, limits: {memory: 16Mi}}
        {{- end }}
        {{- range $ic := $w.initContainers }}
        - name: {{ $ic.name }}
          image: {{ $ic.image | default $image }}
          imagePullPolicy: {{ $pull }}
          command: {{ toJson $ic.command }}
          {{- if or $w.secretEnv $w.env }}
          env:
            {{- include "sh-lib.env" (dict "root" $root "w" $w "secret" $secret) | nindent 12 }}
          {{- end }}
          resources:
            {{- toYaml ($w.resources | default (dict "limits" (dict "memory" "128Mi"))) | nindent 12 }}
        {{- end }}
      {{- end }}
      containers:
        - name: {{ $name }}
          image: {{ $image }}
          imagePullPolicy: {{ $pull }}
          {{- with $w.command }}
          command: {{ toJson . }}
          {{- end }}
          {{- with $w.args }}
          args: {{ toJson . }}
          {{- end }}
          {{- if or $w.secretEnv $w.env }}
          env:
            {{- include "sh-lib.env" (dict "root" $root "w" $w "secret" $secret) | nindent 12 }}
          {{- end }}
          {{- if and $w.service $w.service.ports }}
          ports:
            {{- range $pname, $port := $w.service.ports }}
            - name: {{ $pname }}
              containerPort: {{ $port }}
            {{- end }}
          {{- end }}
          {{- with $w.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $w.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          resources:
            {{- toYaml ($w.resources | default (dict "requests" (dict "memory" "64Mi" "cpu" "10m") "limits" (dict "memory" "128Mi"))) | nindent 12 }}
          {{- if $w.volumes }}
          volumeMounts:
            {{- range $vname, $v := $w.volumes }}
            - name: {{ $vname }}
              mountPath: {{ $v.mountPath }}
              {{- with $v.subPath }}
              subPath: {{ . }}
              {{- end }}
            {{- end }}
          {{- end }}
      {{- if $w.volumes }}
      volumes:
        {{- range $vname, $v := $w.volumes }}
        - name: {{ $vname }}
          {{- if $v.persistent }}
          persistentVolumeClaim:
            claimName: {{ $v.claimName | default (printf "%s-%s" $name $vname) }}
          {{- else if $v.configMap }}
          configMap:
            name: {{ $v.configMap }}
          {{- else }}
          emptyDir: {}
          {{- end }}
        {{- end }}
      {{- end }}
{{- end -}}

{{- define "sh-lib.env" -}}
{{- range $var, $key := .w.secretEnv }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef:
      name: {{ $.secret }}
      key: {{ trimSuffix "?" $key }}
      {{- if hasSuffix "?" $key }}
      optional: true
      {{- end }}
{{- end }}
{{- range $var, $value := .w.env }}
- name: {{ $var }}
  value: {{ tpl (toString $value) $.root | quote }}
{{- end }}
{{- end -}}

{{- define "sh-lib.service" -}}
{{- $w := .w -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ $w.service.name | default .name }}
  labels:
    {{- include "sh-lib.labels" (dict "root" .root "name" .name) | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {{ .name }}
  ports:
    {{- range $pname, $port := $w.service.ports }}
    - name: {{ $pname }}
      port: {{ $port }}
      targetPort: {{ $port }}
    {{- end }}
{{- end -}}

{{- define "sh-lib.pvc" -}}
{{- if not .v.claimName }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ printf "%s-%s" .name .vname }}
  annotations:
    helm.sh/resource-policy: keep
  labels:
    {{- include "sh-lib.labels" (dict "root" .root "name" .name) | nindent 4 }}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: {{ .v.size | default "1Gi" }}
{{- end }}
{{- end -}}

{{- define "sh-lib.configmap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .root.Chart.Name }}-{{ .cname }}
  labels:
    {{- include "sh-lib.labels" (dict "root" .root "name" .root.Chart.Name) | nindent 4 }}
data:
  {{- range .cm.files }}
  {{ base . }}: |-
    {{- $.root.Files.Get . | nindent 4 }}
  {{- end }}
  {{- range $k, $v := .cm.data }}
  {{ $k }}: |-
    {{- tpl (toString $v) $.root | nindent 4 }}
  {{- end }}
{{- end -}}
