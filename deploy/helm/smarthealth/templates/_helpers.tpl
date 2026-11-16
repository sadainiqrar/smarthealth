{{/* deploy/helm/smarthealth/templates/_helpers.tpl */}}
{{- define "smarthealth.mw" -}}
{{ .Release.Namespace }}-{{ .name }}@kubernetescrd
{{- end -}}

{{- define "smarthealth.path" -}}
- path: {{ .path }}
  pathType: Prefix
  backend:
    service:
      name: {{ .svc }}
      port:
        number: {{ .port }}
{{- end -}}
