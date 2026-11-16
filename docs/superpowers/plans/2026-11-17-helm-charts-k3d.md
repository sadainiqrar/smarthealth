# Helm charts on k3d — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything `docker-compose.infra.yml` runs is installed on one 8 GB Kubernetes node by one `helm upgrade --install`, reachable through one Traefik ingress, and proven on a local k3d cluster by a smoke script.

**Architecture:** A library chart `sh-lib` renders Deployments, Services, PVCs and ConfigMaps from a small, map-shaped values schema. 21 component charts (`core`, `knowledge`, …, `grafana`) are almost only values. The umbrella `smarthealth` depends on them (aliasing `postgres` ×3 and `redis` ×2), owns the Secret, the ingress, the Traefik middlewares and the optional `demo-seed` Job, and holds the 8 GB budget in its `values.yaml`.

**Tech Stack:** Helm 3, k3s/k3d (Traefik v2/v3 ingress, local-path storage), Python pytest meta-tests that shell out to `helm template`, bash scripts.

Spec: `docs/superpowers/specs/2026-11-17-helm-charts-k3d-design.md`.

---

## Ground rules the engineer must know

- **Branch:** `feat/helm-charts`. Commit only after every check of the task passes. LF line endings.
- **Windows host, Git Bash.** Scripts are bash; tests run with `python -m pytest`.
- **Helm on Windows:** `helm template` paths use forward slashes. Tests find helm with `shutil.which("helm")` and **skip** when absent, so the fast suite stays green on a machine without helm.
- **Names are compose's names.** Every Kubernetes Service is named after its compose service (`postgres`, `kafka`, `app`, `envoy`, …) so host settings carry over unchanged. Only the six Part A Deployments differ: `core-api`, `core-worker`, `core-relay`, `core-consumers`, `core-celery-worker`, `core-celery-beat` (the API's Service is still `app`).
- **`enableServiceLinks: false` on every pod.** Kubernetes otherwise injects `KAFKA_PORT=tcp://…`, `SCHEMA_REGISTRY_PORT=…` etc., which the Confluent images read as configuration and crash on. `sh-lib` sets it; never remove it.
- **Secret-backed env vars render before plain ones**, so a plain value can embed `$(SECRET_VAR)` (Kubernetes only expands variables defined earlier in the list).
- **Persistent volumes are standalone PVCs with `helm.sh/resource-policy: keep`** and their Deployments use `strategy: Recreate` (one replica, ReadWriteOnce). This replaces the spec's "StatefulSet" wording: on one replica a StatefulSet adds nothing, and `keep` means `helm uninstall` never deletes data. (Task 10 records this in the spec.)
- Lint: `python -m ruff check app tests services scripts`. Fast suite: `python -m pytest -m "not docker" -q`.
- **Docs gate:** a Stop hook blocks when mapped code changes without docs. Task 1 maps `deploy/**` and `scripts/k3d_*` in `docs/doc-map.txt`; Task 10 writes the docs. Until Task 10, end sessions with `DOCS_WAIVE="helm charts in progress; docs in Task 10"` if the hook fires.

## File structure

| Path | Responsibility |
| --- | --- |
| `deploy/helm/sh-lib/` | Library chart: `sh-lib.component` and its helpers |
| `deploy/helm/charts/<name>/` | One component chart each: `Chart.yaml`, `values.yaml`, `templates/component.yaml`, optional `files/` |
| `deploy/helm/smarthealth/` | Umbrella: `Chart.yaml`, `values.yaml` (budget), `values-local.yaml`, `templates/` (secret, ingress, middlewares, demo-seed job, NOTES) |
| `scripts/sync_chart_files.py` | Copies `ops/` config files into chart `files/` dirs; `--check` reports drift |
| `scripts/push_images.sh` | Builds the five images; `--local` imports into k3d, `--push` pushes to a registry |
| `scripts/k3d_up.sh`, `scripts/k3d_down.sh` | Create/delete the 8 GB k3d cluster and install the umbrella |
| `scripts/k3d_smoke.py` | End-to-end smoke through the ingress |
| `tests/meta/test_helm_charts.py` | Render-time invariants (lint, coverage, budget, secrets, images, copies) |

---

### Task 1: Tools, doc map, and the test harness

**Files:**
- Create: `tests/meta/test_helm_charts.py`
- Create: `deploy/helm/smarthealth/Chart.yaml`, `deploy/helm/smarthealth/values.yaml`, `deploy/helm/smarthealth/templates/.gitkeep`
- Modify: `docs/doc-map.txt`

- [ ] **Step 1: Install helm and k3d**

Run (PowerShell or Git Bash):
```bash
winget install --id Helm.Helm -e --accept-source-agreements --accept-package-agreements
winget install --id k3d.k3d -e --accept-source-agreements --accept-package-agreements
```
Open a new shell, then `helm version` (expect `v3.x`) and `k3d version` (expect `v5.x`). If winget is unavailable, download the release binaries into `C:\Users\<you>\bin` (already on PATH).

- [ ] **Step 2: Write the failing harness test**

```python
# tests/meta/test_helm_charts.py
"""Render-time invariants of the Helm charts (spec 2026-11-17-helm-charts-k3d-design.md).

Every test renders the umbrella with `helm template`; nothing needs a cluster. Skipped when
helm is not installed, so the fast suite stays green on a machine without it.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.meta

ROOT = Path(__file__).resolve().parents[2]
HELM_DIR = ROOT / "deploy" / "helm"
UMBRELLA = HELM_DIR / "smarthealth"
HELM = shutil.which("helm")

needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*args: str) -> str:
    result = subprocess.run([HELM, *args], capture_output=True, text=True, cwd=ROOT,
                            check=False)
    if result.returncode != 0:
        raise AssertionError(f"helm {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


@lru_cache(maxsize=8)
def render(*value_files: str, sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    _helm("dependency", "build", UMBRELLA.as_posix())
    args = ["template", "smarthealth", UMBRELLA.as_posix(), "--namespace", "smarthealth"]
    for name in value_files:
        args += ["-f", (UMBRELLA / name).as_posix()]
    for item in sets:
        args += ["--set", item]
    return tuple(doc for doc in yaml.safe_load_all(_helm(*args)) if doc)


def of_kind(docs, kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


@needs_helm
def test_the_umbrella_renders():
    render("values-local.yaml")
```

Do not create the chart yet: the test must fail first.

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/meta/test_helm_charts.py -q`
Expected: FAIL — helm reports the chart path does not exist.

- [ ] **Step 4: Create the umbrella skeleton**

```yaml
# deploy/helm/smarthealth/Chart.yaml
apiVersion: v2
name: smarthealth
description: SmartHealth -- every component of docker-compose.infra.yml on one 8 GB node
type: application
version: 0.1.0
appVersion: "1.0"
dependencies: []
```

```yaml
# deploy/helm/smarthealth/values.yaml
# The slim profile: everything on one 8 GB node (m7i-flex.large). Per-workload memory
# limits live here, under each component, so the whole budget reads in one file.
global:
  # Where the system is reached from a browser; sub-path tools build links from it.
  publicUrl: http://localhost:8080
  # The one Secret every chart reads credentials from (spec section 5).
  secretName: smarthealth-secrets
  image:
    registry: ""       # "" = images imported into the cluster (k3d); ghcr.io/<owner> on AWS
    tag: dev
    pullPolicy: IfNotPresent
```

```yaml
# deploy/helm/smarthealth/values-local.yaml
# k3d: the defaults already describe it; development secrets are added in Task 9.
global:
  publicUrl: http://localhost:8080
```

`deploy/helm/smarthealth/templates/.gitkeep`: empty file.

- [ ] **Step 5: Map the new paths in the docs gate**

Append to `docs/doc-map.txt`:
```
deploy/**                -> README.md, docs/ARCHITECTURE.md, docs/index.html
scripts/k3d_*            -> README.md, docs/index.html
scripts/push_images.sh   -> README.md, docs/index.html
```

- [ ] **Step 6: Run to verify it passes**

Run: `python -m pytest tests/meta/test_helm_charts.py tests/tiers/t0_unit/test_docs_gate.py -q`
Expected: PASS (the docs-gate test still parses the map).

- [ ] **Step 7: Commit**

```bash
git add tests/meta/test_helm_charts.py deploy/helm/smarthealth docs/doc-map.txt
git commit -m "build(helm): umbrella skeleton and the render-time test harness"
```

---

### Task 2: The library chart, proven by `payments-mock`

**Files:**
- Create: `deploy/helm/sh-lib/Chart.yaml`, `deploy/helm/sh-lib/templates/_component.tpl`
- Create: `deploy/helm/charts/payments-mock/{Chart.yaml,values.yaml,templates/component.yaml}`
- Modify: `deploy/helm/smarthealth/Chart.yaml`, `deploy/helm/smarthealth/values.yaml`
- Test: `tests/meta/test_helm_charts.py`

**The values schema every component chart uses** (keys are maps so the umbrella can override one field):

```yaml
workloads:
  main:                       # key "main" -> name = chart name (or alias); else <chart>-<key>
    image: postgres:16-alpine # string = third-party; map {repository, own: true} = ours
    command: [..]             # optional
    args: [..]                # optional
    service:                  # optional; omitted = no Service
      name: app               # optional; default = workload name
      ports: {http: 8000}     # name -> port (targetPort = same)
    secretEnv: {VAR: secret-key}   # rendered first, from global.secretName; "key?" = optional
    env: {VAR: value}              # rendered after; values pass through tpl
    waitFor: ["postgres:5432"]     # initContainer waits for each host:port
    initContainers: [{name, command}]  # extra initContainers: main image, env and resources
    readinessProbe: {..}           # raw probe
    livenessProbe: {..}
    resources: {requests: {memory: 64Mi, cpu: 10m}, limits: {memory: 128Mi}}
    securityContext: {..}          # optional pod securityContext
    volumes:
      data: {mountPath: /var/lib/x, persistent: true, size: 2Gi}
      config: {mountPath: /etc/x, configMap: <chart>-config, subPath: x.yaml}  # subPath optional
      scratch: {mountPath: /tmp, emptyDir: true}
configMaps:
  config:                     # -> ConfigMap "<chart>-config"
    files: [files/a.yaml]     # each becomes a key named after its basename
    data: {key: value}        # literal entries (pass through tpl)
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/meta/test_helm_charts.py`:
```python
def workload(docs, name: str) -> dict:
    found = [d for d in of_kind(docs, "Deployment") if d["metadata"]["name"] == name]
    assert found, f"no Deployment {name}"
    return found[0]


def container(docs, name: str) -> dict:
    return workload(docs, name)["spec"]["template"]["spec"]["containers"][0]


@needs_helm
def test_a_component_renders_a_deployment_and_its_service():
    docs = render("values-local.yaml")
    pod = workload(docs, "payments-mock")["spec"]["template"]["spec"]
    assert pod["enableServiceLinks"] is False
    assert pod["containers"][0]["image"] == "smarthealth-payments-mock:dev"
    assert pod["containers"][0]["resources"]["limits"]["memory"]
    services = {s["metadata"]["name"]: s for s in of_kind(docs, "Service")}
    assert services["payments-mock"]["spec"]["ports"][0]["port"] == 8090


@needs_helm
def test_a_registry_prefixes_our_own_images():
    docs = render("values-local.yaml", sets=("global.image.registry=ghcr.io/example",
                                             "global.image.tag=abc123"))
    assert container(docs, "payments-mock")["image"] == \
        "ghcr.io/example/smarthealth-payments-mock:abc123"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/meta/test_helm_charts.py -q`
Expected: FAIL — `no Deployment payments-mock`.

- [ ] **Step 3: Write the library chart**

```yaml
# deploy/helm/sh-lib/Chart.yaml
apiVersion: v2
name: sh-lib
description: Shared templates for SmartHealth's component charts
type: library
version: 0.1.0
```

```yaml
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
```

Notes for the engineer:
- `claimName` on a volume lets a second workload mount another workload's PVC (Task 6: `core-celery-worker` mounts `core-api-documents`); the PVC is only rendered by the workload that does not set `claimName`.
- An `initContainers` entry (`name`, `command`, optional `image`) runs with the workload's image, env and resources.
- A `secretEnv` key ending in `?` (e.g. `llm-api-key?`) is optional: the pod starts without it. Every other key must exist in the Secret, so a missing password fails loudly at pod start instead of running with an empty one.

- [ ] **Step 4: The `payments-mock` chart**

```yaml
# deploy/helm/charts/payments-mock/Chart.yaml
apiVersion: v2
name: payments-mock
description: The card-payments stand-in (services/payments_mock)
type: application
version: 0.1.0
dependencies:
  - name: sh-lib
    version: 0.1.0
    repository: file://../../sh-lib
```

```yaml
# deploy/helm/charts/payments-mock/templates/component.yaml
{{ include "sh-lib.component" . }}
```

```yaml
# deploy/helm/charts/payments-mock/values.yaml
workloads:
  main:
    image: {repository: smarthealth-payments-mock, own: true}
    service:
      ports: {http: 8090}
    readinessProbe:
      httpGet: {path: /health, port: 8090}
      periodSeconds: 10
```

Every component chart from here on has the same `Chart.yaml` shape (its own `name`/`description`, the same `sh-lib` dependency) and the same one-line `templates/component.yaml`.

- [ ] **Step 5: Wire it into the umbrella**

In `deploy/helm/smarthealth/Chart.yaml` replace `dependencies: []` with:
```yaml
dependencies:
  - {name: payments-mock, version: 0.1.0, repository: file://../charts/payments-mock}
```
Append to `deploy/helm/smarthealth/values.yaml`:
```yaml
payments-mock:
  workloads:
    main:
      resources: {requests: {memory: 48Mi, cpu: 10m}, limits: {memory: 96Mi}}
```

Add `deploy/helm/**/charts/*.tgz` and `deploy/helm/**/Chart.lock` to `.gitignore` (dependency build output).

- [ ] **Step 6: Run to verify it passes**

Run: `python -m pytest tests/meta/test_helm_charts.py -q` and `helm lint deploy/helm/charts/payments-mock`
Expected: PASS; lint `0 chart(s) failed` (run `helm dependency build deploy/helm/charts/payments-mock` first).

- [ ] **Step 7: Commit**

```bash
git add deploy/helm .gitignore tests/meta/test_helm_charts.py
git commit -m "build(helm): sh-lib library chart, proven by the payments-mock chart"
```

---

### Task 3: Data stores — postgres (×3), redis (×2), mongo, rabbitmq, qdrant, mailpit

**Files:**
- Create: `deploy/helm/charts/{postgres,redis,mongo,rabbitmq,qdrant,mailpit}/…` (Chart.yaml as in Task 2, `templates/component.yaml` one-liner, `values.yaml` below)
- Modify: umbrella `Chart.yaml`, `values.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing test**

```python
@needs_helm
def test_aliased_charts_install_one_workload_per_alias():
    docs = render("values-local.yaml")
    names = {d["metadata"]["name"] for d in of_kind(docs, "Deployment")}
    assert {"postgres", "knowledge-db", "assistant-db", "redis", "assistant-redis",
            "mongo", "rabbitmq", "qdrant", "mailpit"} <= names
    db = container(docs, "knowledge-db")
    env = {e["name"]: e for e in db["env"]}
    assert env["POSTGRES_USER"]["value"] == "knowledge"
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == \
        "knowledge-db-password"
    claims = {p["metadata"]["name"] for p in of_kind(docs, "PersistentVolumeClaim")}
    assert {"postgres-data", "knowledge-db-data", "assistant-db-data", "mongo-data",
            "qdrant-data"} <= claims
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/meta/test_helm_charts.py -q -k aliased` → FAIL.

- [ ] **Step 3: The charts' values**

```yaml
# deploy/helm/charts/postgres/values.yaml -- installed three times by alias
user: smarthealth
database: smarthealth
passwordKey: postgres-password     # key in the shared Secret
workloads:
  main:
    image: postgres:16-alpine
    service:
      ports: {postgres: 5432}
    secretEnv: {}                  # set per alias by the umbrella (POSTGRES_PASSWORD)
    env:
      POSTGRES_USER: "{{ .Values.user }}"
      POSTGRES_DB: "{{ .Values.database }}"
      PGDATA: /var/lib/postgresql/data/pgdata
    readinessProbe:
      exec: {command: ["sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""]}
      periodSeconds: 5
    volumes:
      data: {mountPath: /var/lib/postgresql/data, persistent: true, size: 5Gi}
```

```yaml
# deploy/helm/charts/redis/values.yaml -- installed twice by alias
workloads:
  main:
    image: redis:7-alpine
    args: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    service:
      ports: {redis: 6379}
    readinessProbe:
      exec: {command: ["redis-cli", "ping"]}
      periodSeconds: 5
```

```yaml
# deploy/helm/charts/mongo/values.yaml
workloads:
  main:
    image: mongo:7
    args: ["--wiredTigerCacheSizeGB", "0.25"]
    service:
      ports: {mongo: 27017}
    readinessProbe:
      tcpSocket: {port: 27017}
      periodSeconds: 5
    volumes:
      data: {mountPath: /data/db, persistent: true, size: 2Gi}
```

```yaml
# deploy/helm/charts/rabbitmq/values.yaml
workloads:
  main:
    image: rabbitmq:3.13-management-alpine
    service:
      ports: {amqp: 5672, management: 15672, metrics: 15692}
    secretEnv:
      RABBITMQ_DEFAULT_PASS: rabbitmq-password
    env:
      RABBITMQ_DEFAULT_USER: smarthealth
      RABBITMQ_DEFAULT_VHOST: part-a
    readinessProbe:
      tcpSocket: {port: 5672}
      periodSeconds: 10
    volumes:
      config: {mountPath: /etc/rabbitmq/conf.d/20-smarthealth.conf, configMap: rabbitmq-config,
               subPath: 20-smarthealth.conf}
configMaps:
  config:
    data:
      # The management UI lives under /rabbitmq behind the ingress (spec section 4).
      20-smarthealth.conf: |
        management.path_prefix = /rabbitmq
```

```yaml
# deploy/helm/charts/qdrant/values.yaml
workloads:
  main:
    image: qdrant/qdrant:v1.12.4
    service:
      ports: {http: 6333, grpc: 6334}
    readinessProbe:
      tcpSocket: {port: 6333}
      periodSeconds: 10
    volumes:
      data: {mountPath: /qdrant/storage, persistent: true, size: 2Gi}
```

```yaml
# deploy/helm/charts/mailpit/values.yaml
workloads:
  main:
    image: axllent/mailpit:v1.20.0
    service:
      ports: {smtp: 1025, http: 8025}
    env:
      MP_WEBROOT: mailpit       # served under /mailpit behind the ingress
    readinessProbe:
      tcpSocket: {port: 1025}
      periodSeconds: 10
```

- [ ] **Step 4: Umbrella dependencies and budget**

Add to umbrella `Chart.yaml` `dependencies:`
```yaml
  - {name: postgres, alias: postgres, version: 0.1.0, repository: file://../charts/postgres}
  - {name: postgres, alias: knowledge-db, version: 0.1.0, repository: file://../charts/postgres}
  - {name: postgres, alias: assistant-db, version: 0.1.0, repository: file://../charts/postgres}
  - {name: redis, alias: redis, version: 0.1.0, repository: file://../charts/redis}
  - {name: redis, alias: assistant-redis, version: 0.1.0, repository: file://../charts/redis}
  - {name: mongo, version: 0.1.0, repository: file://../charts/mongo}
  - {name: rabbitmq, version: 0.1.0, repository: file://../charts/rabbitmq}
  - {name: qdrant, version: 0.1.0, repository: file://../charts/qdrant}
  - {name: mailpit, version: 0.1.0, repository: file://../charts/mailpit}
```
Append to umbrella `values.yaml`:
```yaml
postgres:           # Part A and Temporal, as in compose
  workloads:
    main:
      secretEnv: {POSTGRES_PASSWORD: postgres-password}
      resources: {requests: {memory: 160Mi, cpu: 50m}, limits: {memory: 320Mi}}
knowledge-db:
  user: knowledge
  database: knowledge
  workloads:
    main:
      secretEnv: {POSTGRES_PASSWORD: knowledge-db-password}
      resources: {requests: {memory: 64Mi, cpu: 10m}, limits: {memory: 160Mi}}
assistant-db:
  user: assistant
  database: assistant
  workloads:
    main:
      secretEnv: {POSTGRES_PASSWORD: assistant-db-password}
      resources: {requests: {memory: 64Mi, cpu: 10m}, limits: {memory: 160Mi}}
redis:
  workloads:
    main:
      resources: {requests: {memory: 32Mi, cpu: 10m}, limits: {memory: 96Mi}}
assistant-redis:
  workloads:
    main:
      resources: {requests: {memory: 16Mi, cpu: 5m}, limits: {memory: 64Mi}}
mongo:
  workloads:
    main:
      resources: {requests: {memory: 224Mi, cpu: 50m}, limits: {memory: 384Mi}}
rabbitmq:
  workloads:
    main:
      resources: {requests: {memory: 160Mi, cpu: 50m}, limits: {memory: 256Mi}}
qdrant:
  workloads:
    main:
      resources: {requests: {memory: 96Mi, cpu: 20m}, limits: {memory: 256Mi}}
mailpit:
  workloads:
    main:
      resources: {requests: {memory: 24Mi, cpu: 5m}, limits: {memory: 64Mi}}
```

- [ ] **Step 5: Verify** — `python -m pytest tests/meta/test_helm_charts.py -q` → PASS; `helm lint` each new chart after `helm dependency build` → 0 failed.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): data-store charts -- postgres x3 and redis x2 by alias, mongo, rabbitmq, qdrant, mailpit"
```

---

### Task 4: Kafka, Schema Registry, kafka-exporter, Temporal, Temporal UI

**Files:**
- Create: `deploy/helm/charts/{kafka,schema-registry,kafka-exporter,temporal,temporal-ui}/…`
- Modify: umbrella `Chart.yaml`, `values.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing test**

```python
@needs_helm
def test_kafka_binds_all_interfaces_and_advertises_its_service_name():
    docs = render("values-local.yaml")
    env = {e["name"]: e.get("value") for e in container(docs, "kafka")["env"]}
    assert env["KAFKA_ADVERTISED_LISTENERS"] == "PLAINTEXT://kafka:29092"
    assert "0.0.0.0:29092" in env["KAFKA_LISTENERS"]
    assert env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"
    assert "-Xmx" in env["KAFKA_HEAP_OPTS"]
    assert {"schema-registry", "kafka-exporter", "temporal", "temporal-ui"} <= {
        d["metadata"]["name"] for d in of_kind(docs, "Deployment")}
```

- [ ] **Step 2: Run to verify it fails** → FAIL (`no Deployment kafka`).

- [ ] **Step 3: The charts' values**

```yaml
# deploy/helm/charts/kafka/values.yaml
# Compose binds listeners to the hostname "kafka"; a pod cannot bind its Service's IP, so
# Kubernetes binds 0.0.0.0 and advertises the Service name. No EXTERNAL listener: nothing
# outside the cluster talks to Kafka.
workloads:
  main:
    image: confluentinc/cp-kafka:7.7.1
    service:
      ports: {plaintext: 29092}
    env:
      CLUSTER_ID: smarthealth-kraft-cluster
      KAFKA_NODE_ID: "1"
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@localhost:29093
      KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:29093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:29092
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: "1"
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: "1"
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: "1"
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: "0"
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"
      KAFKA_HEAP_OPTS: "-Xms256m -Xmx512m"
      KAFKA_LOG_DIRS: /var/lib/kafka/data/logs
    readinessProbe:
      tcpSocket: {port: 29092}
      periodSeconds: 10
      failureThreshold: 18
    volumes:
      data: {mountPath: /var/lib/kafka/data, persistent: true, size: 5Gi}
```

```yaml
# deploy/helm/charts/schema-registry/values.yaml
workloads:
  main:
    image: confluentinc/cp-schema-registry:7.7.1
    service:
      ports: {http: 8081}
    waitFor: ["kafka:29092"]
    env:
      SCHEMA_REGISTRY_HOST_NAME: schema-registry
      SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS: PLAINTEXT://kafka:29092
      SCHEMA_REGISTRY_LISTENERS: http://0.0.0.0:8081
      SCHEMA_REGISTRY_HEAP_OPTS: "-Xms128m -Xmx256m"
    readinessProbe:
      httpGet: {path: /subjects, port: 8081}
      periodSeconds: 10
      failureThreshold: 18
```

```yaml
# deploy/helm/charts/kafka-exporter/values.yaml
workloads:
  main:
    image: danielqsj/kafka-exporter:v1.8.0
    args: ["--kafka.server=kafka:29092"]
    service:
      ports: {metrics: 9308}
    waitFor: ["kafka:29092"]
```

```yaml
# deploy/helm/charts/temporal/values.yaml -- auto-setup creates its databases in postgres
workloads:
  main:
    image: temporalio/auto-setup:1.25.2
    service:
      ports: {frontend: 7233}
    waitFor: ["postgres:5432"]
    secretEnv:
      POSTGRES_PWD: postgres-password
    env:
      DB: postgres12
      DB_PORT: "5432"
      POSTGRES_SEEDS: postgres
      POSTGRES_USER: smarthealth
    readinessProbe:
      tcpSocket: {port: 7233}
      periodSeconds: 10
      failureThreshold: 18
```

```yaml
# deploy/helm/charts/temporal-ui/values.yaml
workloads:
  main:
    image: temporalio/ui:2.31.2
    service:
      ports: {http: 8080}
    env:
      TEMPORAL_ADDRESS: temporal:7233
      TEMPORAL_UI_PUBLIC_PATH: /temporal     # served under /temporal behind the ingress
      TEMPORAL_CORS_ORIGINS: "{{ .Values.global.publicUrl }}"
    readinessProbe:
      tcpSocket: {port: 8080}
      periodSeconds: 10
```

- [ ] **Step 4: Umbrella dependencies and budget**

`Chart.yaml` dependencies:
```yaml
  - {name: kafka, version: 0.1.0, repository: file://../charts/kafka}
  - {name: schema-registry, version: 0.1.0, repository: file://../charts/schema-registry}
  - {name: kafka-exporter, version: 0.1.0, repository: file://../charts/kafka-exporter}
  - {name: temporal, version: 0.1.0, repository: file://../charts/temporal}
  - {name: temporal-ui, version: 0.1.0, repository: file://../charts/temporal-ui}
```
`values.yaml`:
```yaml
kafka:
  workloads:
    main:
      resources: {requests: {memory: 640Mi, cpu: 100m}, limits: {memory: 768Mi}}
schema-registry:
  workloads:
    main:
      resources: {requests: {memory: 224Mi, cpu: 50m}, limits: {memory: 320Mi}}
kafka-exporter:
  workloads:
    main:
      resources: {requests: {memory: 16Mi, cpu: 5m}, limits: {memory: 48Mi}}
temporal:
  workloads:
    main:
      resources: {requests: {memory: 256Mi, cpu: 50m}, limits: {memory: 448Mi}}
temporal-ui:
  workloads:
    main:
      resources: {requests: {memory: 24Mi, cpu: 5m}, limits: {memory: 64Mi}}
```

- [ ] **Step 5: Verify** — tests PASS, `helm lint` each chart.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): kafka, schema-registry, kafka-exporter, temporal and its UI"
```

---

### Task 5: Observability — otel-collector, jaeger, prometheus, grafana — and config-file sync

**Files:**
- Create: `scripts/sync_chart_files.py`
- Create: `deploy/helm/charts/{otel-collector,jaeger,prometheus,grafana}/…` with `files/` populated by the sync script
- Create: `deploy/helm/charts/grafana/templates/datasources.yaml` (templated, not copied)
- Modify: umbrella `Chart.yaml`, `values.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing tests**

```python
import importlib.util


def _sync_module():
    spec = importlib.util.spec_from_file_location("sync_chart_files",
                                                  ROOT / "scripts" / "sync_chart_files.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chart_copies_of_ops_files_are_identical():
    assert _sync_module().drift() == []


@needs_helm
def test_grafana_points_at_jaeger_under_its_sub_path():
    docs = render("values-local.yaml")
    maps = {m["metadata"]["name"]: m for m in of_kind(docs, "ConfigMap")}
    datasources = maps["grafana-datasources"]["data"]["datasources.yaml"]
    assert "http://jaeger:16686/jaeger" in datasources
    env = {e["name"]: e.get("value") for e in container(docs, "grafana")["env"]}
    assert env["GF_SERVER_ROOT_URL"] == "http://localhost:8080/grafana/"
    assert env["GF_SERVER_SERVE_FROM_SUB_PATH"] == "true"
```

- [ ] **Step 2: Run to verify they fail** → FAIL (no module file / no ConfigMap).

- [ ] **Step 3: The sync script**

```python
# scripts/sync_chart_files.py
"""Copy ops/ config files into the Helm charts that mount them.

Helm cannot read files outside a chart, so each chart keeps a copy under files/; this script
makes the copies and `--check` reports any that drifted (tests/meta/test_helm_charts.py
runs the check). ops/ stays the single source: edit there, then run this.
Grafana's datasources file is NOT copied: under Kubernetes Jaeger lives at /jaeger, so the
chart templates it (deploy/helm/charts/grafana/templates/datasources.yaml).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "deploy" / "helm" / "charts"

#: ops source (file or directory) -> chart files/ destination
COPIES: dict[str, str] = {
    "ops/envoy/envoy.yaml": "envoy/files/envoy.yaml",
    "ops/otel-collector/config.yaml": "otel-collector/files/config.yaml",
    "ops/prometheus/prometheus.yml": "prometheus/files/prometheus.yml",
    "ops/grafana/provisioning/dashboards/dashboards.yaml":
        "grafana/files/provisioning/dashboards.yaml",
    "ops/grafana/provisioning/alerting/rules.yaml": "grafana/files/provisioning/rules.yaml",
    "ops/grafana/dashboards": "grafana/files/dashboards",
}


def _pairs() -> list[tuple[Path, Path]]:
    pairs = []
    for source, dest in COPIES.items():
        src, dst = ROOT / source, CHARTS / dest
        if src.is_dir():
            pairs += [(f, dst / f.name) for f in sorted(src.iterdir()) if f.is_file()]
        else:
            pairs.append((src, dst))
    return pairs


def drift() -> list[str]:
    """Chart copies that are missing or differ from their ops/ original."""
    return [str(dst.relative_to(ROOT)) for src, dst in _pairs()
            if not dst.is_file() or dst.read_bytes() != src.read_bytes()]


def sync() -> int:
    for src, dst in _pairs():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return len(_pairs())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report drift, copy nothing")
    if parser.parse_args().check:
        stale = drift()
        for path in stale:
            print(f"stale: {path}")
        return 1 if stale else 0
    print(f"copied {sync()} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run `python scripts/sync_chart_files.py` once the chart directories below exist (it creates `files/`). Add `deploy/helm/charts/*/files/** -text` is NOT needed — but add to `.gitattributes`: `deploy/helm/charts/*/files/** eol=lf` only if the repo has a `.gitattributes`; otherwise make sure `git config core.autocrlf` does not rewrite them (copies must stay byte-identical; check with `--check` after commit).

- [ ] **Step 4: The charts' values**

```yaml
# deploy/helm/charts/otel-collector/values.yaml
workloads:
  main:
    image: otel/opentelemetry-collector-contrib:0.111.0
    args: ["--config=/etc/otelcol-contrib/config.yaml"]
    service:
      ports: {otlp-grpc: 4317, otlp-http: 4318, prom: 8889, health: 13133}
    readinessProbe:
      httpGet: {path: /, port: 13133}
      periodSeconds: 10
    volumes:
      config: {mountPath: /etc/otelcol-contrib/config.yaml, configMap: otel-collector-config,
               subPath: config.yaml}
configMaps:
  config:
    files: [files/config.yaml]
```

```yaml
# deploy/helm/charts/jaeger/values.yaml
workloads:
  main:
    image: jaegertracing/all-in-one:1.62.0
    service:
      ports: {ui: 16686, otlp-grpc: 4317}
    env:
      COLLECTOR_OTLP_ENABLED: "true"
      QUERY_BASE_PATH: /jaeger         # served under /jaeger behind the ingress
      MEMORY_MAX_TRACES: "20000"       # in-memory storage, capped for the 8 GB budget
    readinessProbe:
      tcpSocket: {port: 16686}
      periodSeconds: 10
```

```yaml
# deploy/helm/charts/prometheus/values.yaml
# External URL carries /prometheus for links; the route prefix stays "/" because the
# ingress strips /prometheus -- so Grafana keeps querying http://prometheus:9090.
workloads:
  main:
    image: prom/prometheus:v2.54.1
    args:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=2d
      - "--web.external-url={{ .Values.global.publicUrl }}/prometheus/"
      - --web.route-prefix=/
    service:
      ports: {http: 9090}
    readinessProbe:
      httpGet: {path: /-/ready, port: 9090}
      periodSeconds: 10
    volumes:
      config: {mountPath: /etc/prometheus/prometheus.yml, configMap: prometheus-config,
               subPath: prometheus.yml}
      data: {mountPath: /prometheus, emptyDir: true}
configMaps:
  config:
    files: [files/prometheus.yml]
```

Because `args` are not passed through `tpl` by `sh-lib`, change the `sh-lib.deployment` `args` line to render templated args:
```yaml
          {{- with $w.args }}
          args:
            {{- range . }}
            - {{ tpl (toString .) $root | quote }}
            {{- end }}
          {{- end }}
```
(Re-run Task 2–4 tests: unchanged behaviour for literal args.)

```yaml
# deploy/helm/charts/grafana/values.yaml
workloads:
  main:
    image: grafana/grafana:11.2.2
    service:
      ports: {http: 3000}
    secretEnv:
      GF_SECURITY_ADMIN_PASSWORD: grafana-admin-password
    env:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      # The ingress's tool login sends an Authorization header; Grafana must not try it as
      # one of its own users (it would answer 401). Grafana's admin login is its form.
      GF_AUTH_BASIC_ENABLED: "false"
      GF_SERVER_ROOT_URL: "{{ .Values.global.publicUrl }}/grafana/"
      GF_SERVER_SERVE_FROM_SUB_PATH: "true"
    readinessProbe:
      httpGet: {path: /api/health, port: 3000}
      periodSeconds: 10
    volumes:
      datasources: {mountPath: /etc/grafana/provisioning/datasources, configMap: grafana-datasources}
      providers: {mountPath: /etc/grafana/provisioning/dashboards, configMap: grafana-providers}
      alerting: {mountPath: /etc/grafana/provisioning/alerting, configMap: grafana-alerting}
      dashboards: {mountPath: /var/lib/grafana/dashboards, configMap: grafana-dashboards}
configMaps:
  providers:
    files: [files/provisioning/dashboards.yaml]
  alerting:
    files: [files/provisioning/rules.yaml]
  dashboards:
    files:
      - files/dashboards/assistant.json
      - files/dashboards/async-backbone.json
      - files/dashboards/platform-analytics.json
      - files/dashboards/service-overview.json
```

```yaml
# deploy/helm/charts/grafana/templates/datasources.yaml
# Templated, not copied from ops/: under Kubernetes Jaeger serves under /jaeger.
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-datasources
data:
  datasources.yaml: |-
    apiVersion: 1
    datasources:
      - name: Prometheus
        uid: prometheus
        type: prometheus
        access: proxy
        url: http://prometheus:9090
        isDefault: true
        editable: false
      - name: Analytics
        uid: analytics
        type: grafana-postgresql-datasource
        url: postgres:5432
        user: analytics_reader
        secureJsonData:
          password: analytics-reader
        jsonData:
          database: smarthealth
          sslmode: disable
          postgresVersion: 1600
        editable: false
      - name: Jaeger
        uid: jaeger
        type: jaeger
        access: proxy
        url: http://jaeger:16686/jaeger
        editable: false
```
(`analytics-reader` is the role's password set by migration 0008, already public in the repo.)

Check `ops/grafana/provisioning/dashboards/dashboards.yaml`'s `path:` is `/var/lib/grafana/dashboards`; if it differs, mount the dashboards ConfigMap where that file points.

- [ ] **Step 5: Umbrella dependencies and budget**

```yaml
  - {name: otel-collector, version: 0.1.0, repository: file://../charts/otel-collector}
  - {name: jaeger, version: 0.1.0, repository: file://../charts/jaeger}
  - {name: prometheus, version: 0.1.0, repository: file://../charts/prometheus}
  - {name: grafana, version: 0.1.0, repository: file://../charts/grafana}
```
```yaml
otel-collector:
  workloads:
    main:
      resources: {requests: {memory: 64Mi, cpu: 20m}, limits: {memory: 128Mi}}
jaeger:
  workloads:
    main:
      resources: {requests: {memory: 64Mi, cpu: 20m}, limits: {memory: 160Mi}}
prometheus:
  workloads:
    main:
      resources: {requests: {memory: 96Mi, cpu: 20m}, limits: {memory: 192Mi}}
grafana:
  workloads:
    main:
      resources: {requests: {memory: 96Mi, cpu: 20m}, limits: {memory: 192Mi}}
```

- [ ] **Step 6: Sync, verify** — `python scripts/sync_chart_files.py` (creates the envoy copy too; the envoy chart arrives in Task 7), then `python -m pytest tests/meta/test_helm_charts.py -q` → PASS, `python scripts/sync_chart_files.py --check` → exit 0, `helm lint` each chart.

- [ ] **Step 7: Commit**

```bash
git add scripts/sync_chart_files.py deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): observability charts; ops/ config copied into charts and drift-checked"
```

---

### Task 6: The `core` chart — Part A's six processes

**Files:**
- Create: `deploy/helm/charts/core/{Chart.yaml,values.yaml,templates/component.yaml,README.md}`
- Modify: umbrella `Chart.yaml`, `values.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing test**

```python
CORE = ["core-api", "core-worker", "core-relay", "core-consumers", "core-celery-worker",
        "core-celery-beat"]


@needs_helm
def test_core_runs_six_processes_from_one_image():
    docs = render("values-local.yaml")
    images = {container(docs, name)["image"] for name in CORE}
    assert images == {"smarthealth-core:dev"}
    services = {s["metadata"]["name"] for s in of_kind(docs, "Service")}
    assert "app" in services                      # the API keeps its compose name
    relay = workload(docs, "core-relay")["spec"]["template"]["spec"]
    setup = [c for c in relay["initContainers"] if c["name"] == "setup"][0]
    assert setup["command"] == ["python", "-m", "app.relay", "--setup"]
    api_env = {e["name"]: e for e in container(docs, "core-api")["env"]}
    assert api_env["UVICORN_ROOT_PATH"]["value"] == "/api"
    assert api_env["SMARTHEALTH_POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == \
        "postgres-password"
    beat = container(docs, "core-celery-worker")
    mounts = {m["mountPath"] for m in beat["volumeMounts"]}
    assert "/var/smarthealth/documents" in mounts
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: The chart**

`deploy/helm/charts/core/README.md`:
```markdown
# core

Part A of the assignment -- the modular monolith in `app/`. One image
(`smarthealth-core`), six processes, each its own Deployment: `core-api` (FastAPI, Service
`app`, runs migrations on start as in compose), `core-worker` (Temporal), `core-relay`
(outbox -> Kafka; its `setup` initContainer creates topics and registers schemas),
`core-consumers` (Kafka consumers), `core-celery-worker`, `core-celery-beat`.
```

`deploy/helm/charts/core/values.yaml`:
```yaml
# Settings shared by the six processes; each workload lists the subset compose gives it.
x-common: &common
  SMARTHEALTH_ENVIRONMENT: docker
  SMARTHEALTH_POSTGRES_HOST: postgres
  SMARTHEALTH_POSTGRES_PORT: "5432"
  SMARTHEALTH_POSTGRES_USER: smarthealth
  SMARTHEALTH_POSTGRES_DB: smarthealth
  SMARTHEALTH_OTEL_ENDPOINT_HTTP: http://otel-collector:4318
x-mongo: &mongo
  SMARTHEALTH_MONGO_HOST: mongo
  SMARTHEALTH_MONGO_PORT: "27017"
  SMARTHEALTH_MONGO_DB: smarthealth
x-redis: &redis
  SMARTHEALTH_REDIS_HOST: redis
  SMARTHEALTH_REDIS_PORT: "6379"
x-temporal: &temporal
  SMARTHEALTH_TEMPORAL_HOST: temporal
  SMARTHEALTH_TEMPORAL_PORT: "7233"
x-rabbit: &rabbit
  SMARTHEALTH_RABBITMQ_HOST: rabbitmq
  SMARTHEALTH_RABBITMQ_PORT: "5672"
  SMARTHEALTH_RABBITMQ_USER: smarthealth
x-kafka: &kafka
  SMARTHEALTH_KAFKA_BOOTSTRAP_SERVERS: kafka:29092
  SMARTHEALTH_SCHEMA_REGISTRY_URL: http://schema-registry:8081
x-celery: &celery
  SMARTHEALTH_SMTP_HOST: mailpit
  SMARTHEALTH_SMTP_PORT: "1025"
  SMARTHEALTH_DOCUMENTS_PATH: /var/smarthealth/documents
  SMARTHEALTH_PAYMENTS_URL: http://payments-mock:8090
x-pg-secret: &pgsecret
  SMARTHEALTH_POSTGRES_PASSWORD: postgres-password
x-rabbit-secret: &rabbitsecret
  SMARTHEALTH_RABBITMQ_PASSWORD: rabbitmq-password

image: &image {repository: smarthealth-core, own: true}

workloads:
  api:
    image: *image
    service:
      name: app
      ports: {http: 8000}
    waitFor: ["postgres:5432", "mongo:27017", "redis:6379", "temporal:7233"]
    secretEnv:
      <<: [*pgsecret, *rabbitsecret]
      SMARTHEALTH_JWT_PRIVATE_KEY: jwt-private-key
    env:
      <<: [*common, *mongo, *redis, *temporal, *rabbit]
      SMARTHEALTH_OTEL_ENDPOINT_GRPC: http://otel-collector:4317
      SMARTHEALTH_DOCUMENTS_PATH: /var/smarthealth/documents
      UVICORN_ROOT_PATH: /api       # the ingress strips /api; Swagger then fetches /api/openapi.json
    readinessProbe:
      httpGet: {path: /health, port: 8000}
      periodSeconds: 10
      failureThreshold: 12
    volumes:
      documents: {mountPath: /var/smarthealth/documents, persistent: true, size: 2Gi}
  worker:
    image: *image
    command: ["python", "-m", "app.worker"]
    waitFor: ["temporal:7233", "app:8000"]
    secretEnv:
      <<: [*pgsecret, *rabbitsecret]
    env:
      <<: [*common, *mongo, *temporal, *rabbit]
      SMARTHEALTH_OTEL_ENDPOINT_GRPC: http://otel-collector:4317
      SMARTHEALTH_NO_SHOW_GRACE_MINUTES: "15"
      SMARTHEALTH_PAYMENTS_URL: http://payments-mock:8090
    readinessProbe:
      exec: {command: ["python", "-m", "app.worker", "--check"]}
      periodSeconds: 60
      timeoutSeconds: 12
  relay:
    image: *image
    command: ["python", "-m", "app.relay"]
    waitFor: ["postgres:5432", "kafka:29092", "schema-registry:8081"]
    initContainers:
      - name: setup
        command: ["python", "-m", "app.relay", "--setup"]
    secretEnv:
      <<: *pgsecret
    env:
      <<: [*common, *kafka]
  consumers:
    image: *image
    command: ["python", "-m", "app.consumers"]
    waitFor: ["kafka:29092", "schema-registry:8081", "temporal:7233", "app:8000"]
    secretEnv:
      <<: [*pgsecret, *rabbitsecret]
    env:
      <<: [*common, *kafka, *temporal, *rabbit]
  celery-worker:
    image: *image
    command: ["celery", "-A", "app.celery_app", "worker", "--loglevel=INFO",
              "-Q", "notifications,maintenance,documents", "--concurrency=2"]
    waitFor: ["rabbitmq:5672", "app:8000"]
    secretEnv:
      <<: [*pgsecret, *rabbitsecret]
    env:
      <<: [*common, *mongo, *redis, *rabbit, *celery, *temporal]
    volumes:
      documents: {mountPath: /var/smarthealth/documents, persistent: true,
                  claimName: core-api-documents}
  celery-beat:
    image: *image
    command: ["celery", "-A", "app.celery_app", "beat", "--loglevel=INFO",
              "--schedule=/tmp/celerybeat-schedule"]
    waitFor: ["rabbitmq:5672"]
    secretEnv:
      <<: [*pgsecret, *rabbitsecret]
    env:
      <<: [*common, *mongo, *redis, *rabbit, *celery, *temporal]
```

Notes:
- YAML anchors/merge keys (`<<:`) are resolved by Helm's YAML parser; the `x-*` keys stay in the values as harmless extras. If `helm lint` complains, inline the maps instead — the rendered env must match compose (`docker-compose.infra.yml` services `app` … `celery-beat`).
- `core-relay`'s `setup` initContainer inherits the core image and the relay's env (sh-lib behaviour from Task 2).
- `core-api` runs migrations in its entrypoint (no `command`), exactly as compose.

- [ ] **Step 4: Umbrella dependency and budget**

```yaml
  - {name: core, version: 0.1.0, repository: file://../charts/core}
```
```yaml
core:
  workloads:
    api:
      resources: {requests: {memory: 192Mi, cpu: 100m}, limits: {memory: 320Mi}}
    worker:
      resources: {requests: {memory: 160Mi, cpu: 50m}, limits: {memory: 256Mi}}
    relay:
      resources: {requests: {memory: 96Mi, cpu: 20m}, limits: {memory: 160Mi}}
    consumers:
      resources: {requests: {memory: 128Mi, cpu: 20m}, limits: {memory: 192Mi}}
    celery-worker:
      resources: {requests: {memory: 192Mi, cpu: 50m}, limits: {memory: 256Mi}}
    celery-beat:
      resources: {requests: {memory: 96Mi, cpu: 10m}, limits: {memory: 160Mi}}
```

- [ ] **Step 5: Verify** — tests PASS; `helm lint deploy/helm/charts/core`.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): the core chart -- Part A's six processes from one image"
```

---

### Task 7: knowledge, assistant, envoy, frontend

**Files:**
- Create: `deploy/helm/charts/{knowledge,assistant,envoy,frontend}/…`
- Modify: umbrella `Chart.yaml`, `values.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing test**

```python
@needs_helm
def test_part_b_builds_database_urls_from_the_secret():
    docs = render("values-local.yaml")
    env = container(docs, "knowledge")["env"]
    names = [e["name"] for e in env]
    assert names.index("KNOWLEDGE_DB_PASSWORD") < names.index("KNOWLEDGE_DATABASE_URL")
    url = {e["name"]: e.get("value") for e in env}["KNOWLEDGE_DATABASE_URL"]
    assert url == ("postgresql+asyncpg://knowledge:$(KNOWLEDGE_DB_PASSWORD)"
                   "@knowledge-db:5432/knowledge")
    assistant = {e["name"]: e for e in container(docs, "assistant")["env"]}
    assert assistant["ASSISTANT_LLM_API_KEY"]["valueFrom"]["secretKeyRef"]["optional"] is True
    probe = workload(docs, "knowledge")["spec"]["template"]["spec"]["containers"][0]
    assert probe["readinessProbe"]["grpc"]["port"] == 50052
    assert {"envoy", "frontend"} <= {d["metadata"]["name"] for d in of_kind(docs, "Deployment")}
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: The charts' values**

```yaml
# deploy/helm/charts/knowledge/values.yaml
workloads:
  main:
    image: {repository: smarthealth-knowledge, own: true}
    service:
      ports: {grpc: 50052}
    waitFor: ["knowledge-db:5432", "qdrant:6333", "kafka:29092", "app:8000"]
    secretEnv:
      KNOWLEDGE_DB_PASSWORD: knowledge-db-password
    env:
      KNOWLEDGE_ENVIRONMENT: docker
      KNOWLEDGE_DATABASE_URL: postgresql+asyncpg://knowledge:$(KNOWLEDGE_DB_PASSWORD)@knowledge-db:5432/knowledge
      KNOWLEDGE_QDRANT_URL: http://qdrant:6333
      KNOWLEDGE_EMBEDDING_MODE: live
      FASTEMBED_CACHE_PATH: /models
      KNOWLEDGE_KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      KNOWLEDGE_SCHEMA_REGISTRY_URL: http://schema-registry:8081
      KNOWLEDGE_JWKS_URL: http://app:8000/.well-known/jwks.json
      KNOWLEDGE_OTEL_ENDPOINT_HTTP: http://otel-collector:4318
    readinessProbe:
      grpc: {port: 50052}
      periodSeconds: 10
      failureThreshold: 30           # first start downloads the embedding model
    volumes:
      models: {mountPath: /models, persistent: true, size: 2Gi}
```

```yaml
# deploy/helm/charts/assistant/values.yaml
workloads:
  main:
    image: {repository: smarthealth-assistant, own: true}
    service:
      ports: {grpc: 50051}
    waitFor: ["assistant-db:5432", "assistant-redis:6379", "knowledge:50052"]
    secretEnv:
      ASSISTANT_DB_PASSWORD: assistant-db-password
      ASSISTANT_LLM_API_KEY: llm-api-key?      # optional: without it the assistant runs offline
    env:
      ASSISTANT_ENVIRONMENT: docker
      ASSISTANT_REDIS_URL: redis://assistant-redis:6379/0
      ASSISTANT_KNOWLEDGE_TARGET: knowledge:50052
      ASSISTANT_KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      ASSISTANT_SCHEMA_REGISTRY_URL: http://schema-registry:8081
      ASSISTANT_JWKS_URL: http://app:8000/.well-known/jwks.json
      ASSISTANT_OTEL_ENDPOINT_HTTP: http://otel-collector:4318
      ASSISTANT_PARTA_URL: http://app:8000
      ASSISTANT_DATABASE_URL: postgresql://assistant:$(ASSISTANT_DB_PASSWORD)@assistant-db:5432/assistant
      ASSISTANT_DISPLAY_TIMEZONE: Australia/Sydney
      ASSISTANT_LLM_MODE: auto
      ASSISTANT_LLM_PROVIDER: groq
      ASSISTANT_LLM_MODEL: ""
    readinessProbe:
      grpc: {port: 50051}
      periodSeconds: 10
      failureThreshold: 12
```

```yaml
# deploy/helm/charts/envoy/values.yaml -- gRPC-Web -> gRPC for the assistant and knowledge
workloads:
  main:
    image: envoyproxy/envoy:v1.31.2
    args: ["-c", "/etc/envoy/envoy.yaml"]
    service:
      ports: {http: 8088}
    readinessProbe:
      tcpSocket: {port: 8088}
      periodSeconds: 10
    volumes:
      config: {mountPath: /etc/envoy/envoy.yaml, configMap: envoy-config, subPath: envoy.yaml}
configMaps:
  config:
    files: [files/envoy.yaml]
```

```yaml
# deploy/helm/charts/frontend/values.yaml -- the demo client (nginx serving the SPA)
# Its nginx.conf proxies /api to http://app:8000, resolved at start: the Service "app"
# exists from install, so nginx starts even before the API is ready. Under Kubernetes the
# ingress routes /api itself; the proxy is simply unused.
workloads:
  main:
    image: {repository: smarthealth-frontend, own: true}
    service:
      ports: {http: 80}
    readinessProbe:
      httpGet: {path: /, port: 80}
      periodSeconds: 10
```

- [ ] **Step 4: Umbrella dependencies and budget**

```yaml
  - {name: knowledge, version: 0.1.0, repository: file://../charts/knowledge}
  - {name: assistant, version: 0.1.0, repository: file://../charts/assistant}
  - {name: envoy, version: 0.1.0, repository: file://../charts/envoy}
  - {name: frontend, version: 0.1.0, repository: file://../charts/frontend}
```
```yaml
knowledge:
  workloads:
    main:
      resources: {requests: {memory: 320Mi, cpu: 50m}, limits: {memory: 448Mi}}
assistant:
  workloads:
    main:
      resources: {requests: {memory: 160Mi, cpu: 50m}, limits: {memory: 320Mi}}
envoy:
  workloads:
    main:
      resources: {requests: {memory: 32Mi, cpu: 10m}, limits: {memory: 64Mi}}
frontend:
  workloads:
    main:
      resources: {requests: {memory: 16Mi, cpu: 5m}, limits: {memory: 32Mi}}
```

- [ ] **Step 5: Verify** — `python scripts/sync_chart_files.py --check` → 0; tests PASS; `helm lint` each.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): knowledge, assistant, envoy and frontend charts"
```

---

### Task 8: The umbrella's own resources — Secret, ingress, middlewares, demo-seed Job

**Files:**
- Create: `deploy/helm/smarthealth/templates/{_helpers.tpl,secret.yaml,middlewares.yaml,ingress.yaml,demo-seed-job.yaml,NOTES.txt}`
- Modify: `deploy/helm/smarthealth/values.yaml`, `values-local.yaml`; `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing tests**

```python
@needs_helm
def test_the_secret_is_rendered_only_when_asked():
    local = render("values-local.yaml")
    secrets = [s for s in of_kind(local, "Secret")
               if s["metadata"]["name"] == "smarthealth-secrets"]
    assert len(secrets) == 1
    keys = set(secrets[0]["stringData"])
    assert {"postgres-password", "knowledge-db-password", "assistant-db-password",
            "rabbitmq-password", "grafana-admin-password", "jwt-private-key",
            "tools-basic-auth", "demo-password"} <= keys
    assert secrets[0]["stringData"]["tools-basic-auth"].startswith("reviewer:$2")
    external = render("values-local.yaml", sets=("secrets.create=false",))
    assert [s for s in of_kind(external, "Secret")
            if s["metadata"]["name"] == "smarthealth-secrets"] == []


def _routes(docs) -> dict[str, tuple[str, str]]:
    """path -> (service, middlewares annotation)"""
    out = {}
    for ing in of_kind(docs, "Ingress"):
        middlewares = ing["metadata"].get("annotations", {}).get(
            "traefik.ingress.kubernetes.io/router.middlewares", "")
        for rule in ing["spec"]["rules"]:
            for p in rule["http"]["paths"]:
                out[p["path"]] = (p["backend"]["service"]["name"], middlewares)
    return out


@needs_helm
def test_the_ingress_routes_by_path_and_guards_the_tools():
    routes = _routes(render("values-local.yaml"))
    assert routes["/"][0] == "frontend"
    assert routes["/api"][0] == "app" and "strip-api" in routes["/api"][1]
    assert routes["/smarthealth.assistant.v1.Assistant"][0] == "envoy"
    for path, svc in {"/grafana": "grafana", "/jaeger": "jaeger", "/temporal": "temporal-ui",
                      "/mailpit": "mailpit", "/rabbitmq": "rabbitmq",
                      "/prometheus": "prometheus"}.items():
        assert routes[path][0] == svc
        assert "tools-auth" in routes[path][1], path
    assert "tools-auth" not in routes["/api"][1]


@needs_helm
def test_the_demo_job_is_off_by_default_and_runs_the_cli_when_on():
    assert of_kind(render("values-local.yaml"), "Job") == []
    job = of_kind(render("values-local.yaml", sets=("demoSeed.enabled=true",
                                                    "demoSeed.mode=refresh")), "Job")[0]
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["command"][:4] == ["python", "-m", "app.cli", "demo-refresh"]
    assert "--allow-demo-data" in c["command"]
```

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Values**

Append to umbrella `values.yaml`:
```yaml
# The shared Secret (spec section 5). create=true renders it from `secrets.values`
# (k3d: public development defaults). create=false (AWS) expects it to exist already.
secrets:
  create: false
  values: {}

# One login in front of the tool UIs (Traefik basic-auth). With secrets.create the htpasswd
# line is computed from toolsAuth; otherwise the Secret's `tools-basic-auth` key holds it.
toolsAuth:
  user: reviewer
  password: ""

# The optional demo data Job (docs/superpowers/specs/2026-11-16-demo-seed-data-design.md).
demoSeed:
  enabled: false
  mode: seed          # seed | refresh
  timezone: Australia/Sydney
  # Bump to re-run: Job specs are immutable, so each run gets a new name.
  run: "1"
```

Replace `values-local.yaml` with:
```yaml
# k3d. Every value here is a public development default (as in .env.example).
global:
  publicUrl: http://localhost:8080
secrets:
  create: true
  values:
    postgres-password: smarthealth
    knowledge-db-password: knowledge
    assistant-db-password: assistant
    rabbitmq-password: smarthealth
    grafana-admin-password: smarthealth
    jwt-private-key: ""        # empty = the committed development key (refused in production)
    demo-password: demo-password-123
    llm-api-key: ""            # empty = the assistant runs offline
toolsAuth:
  user: reviewer
  password: smarthealth
```

- [ ] **Step 4: Templates**

```yaml
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
```

```yaml
# deploy/helm/smarthealth/templates/secret.yaml
{{- if .Values.secrets.create }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Values.global.secretName }}
type: Opaque
stringData:
  {{- range $key, $value := .Values.secrets.values }}
  {{ $key }}: {{ $value | quote }}
  {{- end }}
  tools-basic-auth: {{ htpasswd .Values.toolsAuth.user .Values.toolsAuth.password | quote }}
{{- end }}
```

```yaml
# deploy/helm/smarthealth/templates/middlewares.yaml
# Traefik (bundled with k3s/k3d) middlewares used by the ingresses.
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: tools-auth
spec:
  basicAuth:
    secret: {{ .Values.global.secretName }}-tools-auth
---
# Traefik reads basic-auth users from a Secret's `users` key; mirror the shared key into it.
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Values.global.secretName }}-tools-auth
type: Opaque
stringData:
  {{- if .Values.secrets.create }}
  users: {{ htpasswd .Values.toolsAuth.user .Values.toolsAuth.password | quote }}
  {{- else }}
  {{- $existing := lookup "v1" "Secret" .Release.Namespace .Values.global.secretName }}
  users: {{ if $existing }}{{ index $existing.data "tools-basic-auth" | b64dec | quote }}{{ else }}""{{ end }}
  {{- end }}
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-api
spec:
  stripPrefix:
    prefixes: ["/api"]
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-prometheus
spec:
  stripPrefix:
    prefixes: ["/prometheus"]
```

Note: `lookup` returns nothing under `helm template`, so with `secrets.create=false` the mirrored Secret renders `users: ""` offline; on a cluster it copies the real line. The meta-test therefore only asserts on `smarthealth-secrets`.

```yaml
# deploy/helm/smarthealth/templates/ingress.yaml
# One host, routes by path (spec section 4). Traefik picks the longest matching prefix.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: smarthealth-app
spec:
  ingressClassName: traefik
  rules:
    - http:
        paths:
          {{- include "smarthealth.path" (dict "path" "/" "svc" "frontend" "port" 80) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/smarthealth.assistant.v1.Assistant" "svc" "envoy" "port" 8088) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/smarthealth.knowledge.v1.Knowledge" "svc" "envoy" "port" 8088) | nindent 10 }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: smarthealth-api
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: {{ include "smarthealth.mw" (dict "Release" .Release "name" "strip-api") }}
spec:
  ingressClassName: traefik
  rules:
    - http:
        paths:
          {{- include "smarthealth.path" (dict "path" "/api" "svc" "app" "port" 8000) | nindent 10 }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: smarthealth-tools
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: {{ include "smarthealth.mw" (dict "Release" .Release "name" "tools-auth") }}
spec:
  ingressClassName: traefik
  rules:
    - http:
        paths:
          {{- include "smarthealth.path" (dict "path" "/grafana" "svc" "grafana" "port" 3000) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/jaeger" "svc" "jaeger" "port" 16686) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/temporal" "svc" "temporal-ui" "port" 8080) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/mailpit" "svc" "mailpit" "port" 8025) | nindent 10 }}
          {{- include "smarthealth.path" (dict "path" "/rabbitmq" "svc" "rabbitmq" "port" 15672) | nindent 10 }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: smarthealth-prometheus
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: {{ include "smarthealth.mw" (dict "Release" .Release "name" "tools-auth") }},{{ include "smarthealth.mw" (dict "Release" .Release "name" "strip-prometheus") }}
spec:
  ingressClassName: traefik
  rules:
    - http:
        paths:
          {{- include "smarthealth.path" (dict "path" "/prometheus" "svc" "prometheus" "port" 9090) | nindent 10 }}
```

```yaml
# deploy/helm/smarthealth/templates/demo-seed-job.yaml
{{- if .Values.demoSeed.enabled }}
{{- $g := .Values.global }}
apiVersion: batch/v1
kind: Job
metadata:
  name: demo-{{ .Values.demoSeed.mode }}-{{ .Values.demoSeed.run }}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      enableServiceLinks: false
      containers:
        - name: demo
          image: {{ if $g.image.registry }}{{ $g.image.registry }}/{{ end }}smarthealth-core:{{ $g.image.tag }}
          imagePullPolicy: {{ $g.image.pullPolicy }}
          command: ["python", "-m", "app.cli", "demo-{{ .Values.demoSeed.mode }}",
                    "--api-url", "http://app:8000", "--allow-demo-data",
                    "--timezone", {{ .Values.demoSeed.timezone | quote }}]
          env:
            - {name: SMARTHEALTH_POSTGRES_PASSWORD, valueFrom: {secretKeyRef: {name: {{ $g.secretName }}, key: postgres-password}}}
            - {name: SMARTHEALTH_DEMO_PASSWORD, valueFrom: {secretKeyRef: {name: {{ $g.secretName }}, key: demo-password}}}
            - {name: SMARTHEALTH_ENVIRONMENT, value: docker}
            - {name: SMARTHEALTH_POSTGRES_HOST, value: postgres}
            - {name: SMARTHEALTH_POSTGRES_USER, value: smarthealth}
            - {name: SMARTHEALTH_POSTGRES_DB, value: smarthealth}
            - {name: SMARTHEALTH_MONGO_HOST, value: mongo}
            - {name: SMARTHEALTH_MONGO_DB, value: smarthealth}
            - {name: SMARTHEALTH_DOCUMENTS_PATH, value: /var/smarthealth/documents}
          resources: {requests: {memory: 192Mi, cpu: 50m}, limits: {memory: 384Mi}}
          volumeMounts:
            - {name: documents, mountPath: /var/smarthealth/documents}
      volumes:
        - name: documents
          persistentVolumeClaim: {claimName: core-api-documents}
{{- end }}
```
Check `app/cli.py` for the exact settings `demo-seed` reads (Mongo for the audit, the documents path for result PDFs) and add any missing `SMARTHEALTH_*` variable the command needs; the job's container must reach the same stores as `core-api`.

```text
# deploy/helm/smarthealth/templates/NOTES.txt
SmartHealth is installed. Open {{ .Values.global.publicUrl }}/
Tools (login "{{ .Values.toolsAuth.user }}"): /grafana /jaeger /temporal /mailpit /rabbitmq /prometheus
Demo data: helm upgrade ... --set demoSeed.enabled=true --set demoSeed.mode=seed
```

- [ ] **Step 5: Verify** — `python -m pytest tests/meta/test_helm_charts.py -q` → PASS; `helm lint deploy/helm/smarthealth -f deploy/helm/smarthealth/values-local.yaml` → 0 failed.

- [ ] **Step 6: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py
git commit -m "build(helm): the umbrella's secret, path ingress, tool login and demo-seed job"
```

---

### Task 9: Whole-chart invariants — coverage, budget, images

**Files:**
- Modify: `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the tests**

```python
COMPOSE = ROOT / "docker-compose.infra.yml"
#: compose service -> Deployment name, where they differ
RENAMED = {"app": "core-api", "worker": "core-worker", "relay": "core-relay",
           "consumers": "core-consumers", "celery-worker": "core-celery-worker",
           "celery-beat": "core-celery-beat"}
BUDGET_GIB = 6.5


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _mib(quantity: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    for suffix, factor in units.items():
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    return float(quantity) / (1024 * 1024)


@needs_helm
def test_every_compose_service_has_a_workload():
    names = {d["metadata"]["name"] for d in of_kind(render("values-local.yaml"), "Deployment")}
    missing = [s for s in _compose() if RENAMED.get(s, s) not in names]
    assert not missing, f"compose services without a chart workload: {missing}"


@needs_helm
def test_every_container_has_memory_limits_within_the_budget():
    total = 0.0
    for d in of_kind(render("values-local.yaml"), "Deployment"):
        for c in d["spec"]["template"]["spec"]["containers"]:
            res = c.get("resources", {})
            assert res.get("requests", {}).get("memory"), d["metadata"]["name"]
            limit = res.get("limits", {}).get("memory")
            assert limit, d["metadata"]["name"]
            total += _mib(limit)
    assert total / 1024 <= BUDGET_GIB, f"memory limits total {total / 1024:.2f} GiB"


@needs_helm
def test_third_party_images_match_compose():
    compose_images = {s["image"] for s in _compose().values() if "image" in s}
    rendered = {c["image"] for d in of_kind(render("values-local.yaml"), "Deployment")
                for c in d["spec"]["template"]["spec"]["containers"]
                if not c["image"].startswith("smarthealth-")}
    assert rendered <= compose_images, rendered - compose_images
```

- [ ] **Step 2: Run** — `python -m pytest tests/meta/test_helm_charts.py -q` → PASS (if the budget test fails, the Task 3–7 numbers sum above 6.5 GiB: lower the largest limits in umbrella `values.yaml`, never remove one).

- [ ] **Step 3: Full fast suite and lint** — `python -m pytest -m "not docker" -q`; `python -m ruff check app tests services scripts` → green.

- [ ] **Step 4: Commit**

```bash
git add tests/meta/test_helm_charts.py deploy/helm
git commit -m "test(helm): every compose service has a workload; limits within 6.5 GiB; images match compose"
```

---

### Task 10: Images, k3d, and the smoke test

**Files:**
- Create: `scripts/push_images.sh`, `scripts/k3d_up.sh`, `scripts/k3d_down.sh`, `scripts/k3d_smoke.py`

- [ ] **Step 1: `push_images.sh`**

```bash
#!/usr/bin/env bash
# Build SmartHealth's five images, tagged with the current commit.
#   --local            import them into the k3d cluster "smarthealth" (tag also "dev")
#   --push REGISTRY    push to REGISTRY (e.g. ghcr.io/<owner>); `docker login` first
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="$(git rev-parse --short HEAD)"
declare -A IMAGES=(
  [smarthealth-core]=". -f Dockerfile"
  [smarthealth-knowledge]=". -f services/knowledge/Dockerfile"
  [smarthealth-assistant]=". -f services/assistant/Dockerfile"
  [smarthealth-payments-mock]="services/payments_mock"
  [smarthealth-frontend]="frontend"
)

for name in "${!IMAGES[@]}"; do
  echo ">> building $name:$TAG"
  # shellcheck disable=SC2086
  docker build -t "$name:$TAG" -t "$name:dev" ${IMAGES[$name]}
done

case "${1:-}" in
  --local)
    k3d image import -c smarthealth "${!IMAGES[@]/%/:dev}"
    ;;
  --push)
    registry="${2:?usage: push_images.sh --push REGISTRY}"
    for name in "${!IMAGES[@]}"; do
      docker tag "$name:$TAG" "$registry/$name:$TAG"
      docker push "$registry/$name:$TAG"
    done
    echo "pushed tag $TAG -- set global.image.registry=$registry global.image.tag=$TAG"
    ;;
  "") echo "built tag $TAG (use --local or --push REGISTRY)";;
  *) echo "unknown option: $1" >&2; exit 2;;
esac
```
Note: `"${!IMAGES[@]/%/:dev}"` expands to every name with `:dev` appended.

- [ ] **Step 2: `k3d_up.sh` and `k3d_down.sh`**

```bash
#!/usr/bin/env bash
# A local stand-in for the AWS server: one k3s node capped at 8 GB, SmartHealth installed.
# Stop the compose stacks first -- Docker Desktop needs ~10 GB free for this.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! k3d cluster list smarthealth >/dev/null 2>&1; then
  k3d cluster create smarthealth --servers 1 --agents 0 --servers-memory 8G \
    -p "8080:80@loadbalancer" --wait
fi
kubectl config use-context k3d-smarthealth
kubectl create namespace smarthealth --dry-run=client -o yaml | kubectl apply -f -

scripts/push_images.sh --local
python scripts/sync_chart_files.py --check
helm dependency build deploy/helm/smarthealth
helm upgrade --install smarthealth deploy/helm/smarthealth -n smarthealth \
  -f deploy/helm/smarthealth/values-local.yaml --wait --timeout 15m
echo "SmartHealth is up: http://localhost:8080/  (smoke: python scripts/k3d_smoke.py)"
```

```bash
#!/usr/bin/env bash
# Delete the local k3d cluster and everything in it (volumes included).
set -euo pipefail
k3d cluster delete smarthealth
```

- [ ] **Step 3: `k3d_smoke.py`**

```python
# scripts/k3d_smoke.py
"""Prove the k3d install works end to end through the ingress (spec section 8).

Fails on the first problem. Assumes `scripts/k3d_up.sh` has run and kubectl's context is
k3d-smarthealth. Creates a front-desk login with the CLI inside core-api, then checks the
API, gRPC-Web, the tool logins, runs the demo seed Job and prints memory per pod.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import httpx

NS = "smarthealth"
TOOLS = ["/grafana/", "/jaeger/", "/temporal/", "/mailpit/", "/rabbitmq/", "/prometheus/graph"]


def kubectl(*args: str, check: bool = True) -> str:
    result = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                            check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def step(name: str) -> None:
    print(f"-- {name}", flush=True)


def pods_ready() -> None:
    step("every pod Ready, nothing restarting")
    pods = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
    bad = []
    for pod in pods:
        if pod["status"].get("phase") == "Succeeded":
            continue
        statuses = pod["status"].get("containerStatuses", [])
        if not statuses or not all(s["ready"] for s in statuses):
            bad.append(pod["metadata"]["name"])
    if bad:
        raise SystemExit(f"not ready: {bad}")
    before = {p["metadata"]["name"]: sum(s["restartCount"] for s in
                                         p["status"].get("containerStatuses", []))
              for p in pods}
    time.sleep(60)
    after = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
    restarting = [p["metadata"]["name"] for p in after
                  if sum(s["restartCount"] for s in p["status"].get("containerStatuses", []))
                  > before.get(p["metadata"]["name"], 0)]
    if restarting:
        raise SystemExit(f"restarting: {restarting}")


def api(base: str, email: str, password: str) -> None:
    step("API through the ingress")
    r = httpx.get(f"{base}/api/ready", timeout=10)
    r.raise_for_status()
    docs = httpx.get(f"{base}/api/docs", timeout=10)
    if "/api/openapi.json" not in docs.text:
        raise SystemExit("Swagger does not point at /api/openapi.json (UVICORN_ROOT_PATH)")
    kubectl("exec", "deploy/core-api", "--", "python", "-m", "app.cli", "create-user",
            "--email", email, "--password", password, "--role", "front_desk", check=False)
    r = httpx.post(f"{base}/api/auth/login", json={"email": email, "password": password},
                   timeout=10)
    r.raise_for_status()


def grpc_web(base: str, email: str, password: str) -> None:
    step("gRPC-Web (assistant) through the ingress")
    result = subprocess.run([sys.executable, "scripts/grpcweb_smoke.py", "--edge", base,
                             "--email", email, "--password", password],
                            capture_output=True, text=True, check=False)
    print(result.stdout[-800:])
    if result.returncode != 0:
        raise SystemExit(f"grpc-web smoke failed:\n{result.stderr[-1500:]}")


def tools(base: str, user: str, password: str) -> None:
    step("tool UIs: 401 without the login, 200 with it")
    for path in TOOLS:
        anonymous = httpx.get(f"{base}{path}", timeout=10)
        if anonymous.status_code != 401:
            raise SystemExit(f"{path}: expected 401 without login, got {anonymous.status_code}")
        authed = httpx.get(f"{base}{path}", auth=(user, password), timeout=10,
                           follow_redirects=True)
        if authed.status_code != 200:
            raise SystemExit(f"{path}: expected 200 with login, got {authed.status_code}")


def demo_seed() -> None:
    step("demo-seed Job (takes several minutes)")
    subprocess.run(["helm", "upgrade", "smarthealth", "deploy/helm/smarthealth", "-n", NS,
                    "-f", "deploy/helm/smarthealth/values-local.yaml", "--reuse-values",
                    "--set", "demoSeed.enabled=true", "--set", "demoSeed.mode=seed"],
                   check=True)
    kubectl("wait", "--for=condition=complete", "job/demo-seed-1", "--timeout=40m")
    print(kubectl("logs", "job/demo-seed-1")[-1500:])


def memory() -> None:
    step("memory per pod (kubectl top)")
    print(kubectl("top", "pods", "--sort-by=memory", check=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--tools-user", default="reviewer")
    parser.add_argument("--tools-password", default="smarthealth")
    parser.add_argument("--skip-demo", action="store_true")
    args = parser.parse_args()
    email, password = "smoke.desk@medinova.example", "smoke-password-123"
    pods_ready()
    api(args.base, email, password)
    grpc_web(args.base, email, password)
    tools(args.base, args.tools_user, args.tools_password)
    if not args.skip_demo:
        demo_seed()
    memory()
    print("k3d smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
Check `app/cli.py create-user` for the exact role spelling (`front_desk` vs `front-desk`) and fix the call if it differs.

- [ ] **Step 4: Run it for real**

```bash
docker compose -p smarthealth -f docker-compose.infra.yml --profile app --profile partb stop
docker compose -p smarthealth-test --env-file .env.test -f docker-compose.infra.yml stop
bash scripts/k3d_up.sh
python scripts/k3d_smoke.py
```
Expected: `k3d smoke: OK`. Debug any failure with `kubectl -n smarthealth get pods`, `kubectl -n smarthealth describe pod <p>`, `kubectl -n smarthealth logs deploy/<name> [-c <init>]`. Fix the chart (not the app) and re-run `helm upgrade` (k3d_up.sh is idempotent).

- [ ] **Step 5: Tune the budget from measurements**

From the `kubectl top` output, set each workload's request to roughly its measured use and its limit to ~1.5× (min +64Mi) in umbrella `values.yaml`, keeping the total under 6.5 GiB (the meta-test checks). Add a one-line comment above each component in `values.yaml` with the measured value, e.g. `# measured 2026-11-17: 610Mi`. Re-run `helm upgrade` and the smoke with `--skip-demo` once more.

- [ ] **Step 6: Lint and commit**

```bash
python -m ruff check app tests services scripts
python -m pytest tests/meta/test_helm_charts.py -q
git add scripts/push_images.sh scripts/k3d_up.sh scripts/k3d_down.sh scripts/k3d_smoke.py deploy/helm
git commit -m "build(k3d): image build/import, an 8 GB k3d cluster, and an end-to-end smoke; budget from measurements"
```

---

### Task 11: Documentation

**Files:**
- Modify: `README.md`, `docs/index.html`, `docs/ARCHITECTURE.md`, `docs/PRD.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-11-17-helm-charts-k3d-design.md`

- [ ] **Step 1: README** — add a section "Kubernetes (k3d)" after the compose run instructions: prerequisites (`winget install Helm.Helm k3d.k3d`, Docker Desktop with ~10 GB, compose stacks stopped), `bash scripts/k3d_up.sh`, `python scripts/k3d_smoke.py`, `bash scripts/k3d_down.sh`; a table of URLs (`/`, `/api/docs`, `/grafana`, `/jaeger`, `/temporal`, `/mailpit`, `/rabbitmq`, `/prometheus`) with the tool login `reviewer` / `smarthealth` (k3d only); Qdrant via `kubectl -n smarthealth port-forward svc/qdrant 6333`; demo data via `helm upgrade … --set demoSeed.enabled=true`; the chart layout in five lines (umbrella, `sh-lib`, 21 component charts, `core` = Part A, aliases).

- [ ] **Step 2: Site** — in `docs/index.html` add a "Deployment" section: a Mermaid flowchart (browser → Traefik ingress → path routes → services, the tool routes behind "basic-auth"), the chart list, and the k3d commands in the commands table. Keep Mermaid labels free of `;` (it breaks sequence/flow parsing here).

- [ ] **Step 3: ARCHITECTURE** — a "Deployment (Kubernetes)" section: charts and why our own (H-1), one host/path routing (H-2), tool login (H-3), secrets (H-5), startup order (wait-for, relay setup init, migrations in the API), storage on one node, the budget.

- [ ] **Step 4: PRD** — add decisions for H-1…H-5 to §8 with the next free `D-` numbers (read the highest existing), each with its rejected alternative as in the spec's table.

- [ ] **Step 5: CLAUDE.md** — add to the commands table: `bash scripts/k3d_up.sh` / `python scripts/k3d_smoke.py` / `bash scripts/k3d_down.sh` (local 8 GB Kubernetes) and `python scripts/sync_chart_files.py [--check]` (copy `ops/` files into the charts; the meta-test fails on drift).

- [ ] **Step 6: Spec** — in the design spec §3/§6, replace "StatefulSet" wording with "Deployment + PVC (`Recreate`, `helm.sh/resource-policy: keep`)" and its reason; correct the pod count to 29 (compose has 29 services).

- [ ] **Step 7: Verify and commit**

```bash
python scripts/gen_docs_data.py --check
python -m ruff check app tests services scripts
python -m pytest -m "not docker" -q
git add README.md docs CLAUDE.md
git commit -m "docs: Kubernetes on k3d -- charts, routes, tool login, budget, decisions"
```
