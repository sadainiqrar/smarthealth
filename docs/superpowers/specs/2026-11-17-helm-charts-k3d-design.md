# Helm charts, proven on k3d — design

**Date:** 2026-11-17 · **Status:** approved in conversation, awaiting written-spec review
**Part of:** deploying SmartHealth for assignment review on AWS, in three projects built in
order — (1) demo seed data (done, `docs/superpowers/specs/2026-11-16-demo-seed-data-design.md`),
(2) **an umbrella Helm chart with per-component charts and a slim profile, proven on local
k3d** (this document), (3) Terraform + k3s + Argo CD + CloudFront on AWS (`ap-southeast-2`,
Free plan, one `m7i-flex.large` — 2 vCPU, 8 GB — stopped between reviews).

## 1. Goal

Everything `docker-compose.infra.yml` runs (profiles `app` and `partb` together) runs on one
8 GB Kubernetes node, installed by one `helm upgrade --install`, reachable through one
address, and demonstrably working: a local k3d cluster capped at 8 GB is the stand-in for the
EC2 server, and a smoke script proves login, the assistant stream, the tool UIs and a demo
seed through real workflows inside the cluster. Sub-project 3 then points Argo CD at the same
charts with a different values file.

## 2. Decisions (with what was rejected)

| # | Decision | Rejected |
| --- | --- | --- |
| H-1 | **Our own charts for everything**, sharing a library chart `sh-lib`; infrastructure uses the same images, versions and settings as compose | Bitnami/official community charts for infrastructure: Bitnami's free images moved behind a paid catalogue in 2025 (`bitnamilegacy` is unmaintained), the official Temporal chart defaults to Cassandra + Elasticsearch, and all are sized for real clusters — the overrides needed to fit 8 GB would be larger than our charts. One generic chart installed N times: not the umbrella + per-component structure asked for |
| H-2 | **One host, path routing** through the Traefik ingress bundled with k3s | One hostname per tool: needs a CloudFront distribution or DNS record per UI |
| H-3 | **Tool UIs public under sub-paths behind one basic-auth login** (Traefik middleware) | Port-forward only (a reviewer cannot open them alone); public without login (Mailpit shows every email; Temporal UI and RabbitMQ can change state) |
| H-4 | **Images built and pushed from the laptop** (`scripts/push_images.sh`), tagged with the git SHA | GitHub Actions on push — declined by the user |
| H-5 | **Secrets from one Kubernetes Secret** referenced by every chart; created from dev defaults on k3d and from SSM Parameter Store on AWS, never committed | Secrets in values files |

## 3. Layout

```
deploy/helm/
  sh-lib/              library chart (type: library): deployment, PVC, service,
                       configmap, env-from-secret, probes, resources, wait-for initContainer
  charts/
    core/              Part A of the assignment (the modular monolith): ONE image, six
                       workloads core-api, core-worker, core-relay, core-consumers,
                       core-celery-worker, core-celery-beat
    knowledge/  assistant/  payments-mock/  frontend/  envoy/
    postgres/  redis/  mongo/  rabbitmq/  kafka/  schema-registry/  kafka-exporter/
    temporal/  temporal-ui/  qdrant/  mailpit/
    otel-collector/  jaeger/  prometheus/  grafana/
  smarthealth/         umbrella; Chart.yaml depends on every chart above by file://
    values.yaml        the slim 8 GB profile — the only profile
    values-local.yaml  k3d: locally imported images, host localhost, dev secrets
    values-aws.yaml    written in sub-project 3: GHCR tag, hostname, secrets.create=false
scripts/push_images.sh   build the 5 images; --local imports into k3d, --push to GHCR
scripts/k3d_up.sh, k3d_down.sh, k3d_smoke.py
scripts/sync_chart_files.py   copy ops/ config files into the charts that need them
```

- 21 component charts plus `sh-lib` and the umbrella. Part A's six processes share one chart,
  `core` (named for what it is; its README notes it is "Part A" of the assignment), because
  they ship as one image and are deployed together. The API's Service keeps the compose name
  `app`, like every other Service (below).
- **Aliases instead of copies:** the umbrella installs `postgres` three times (`postgres` for
  Part A and Temporal, as in compose; `knowledge-db`; `assistant-db`) and `redis` twice
  (`redis`, `assistant-redis`).
- **Names are compose's names.** Each component gets a ClusterIP Service named as its compose
  service (`postgres`, `kafka`, `temporal`, `app`, `envoy`…), so every setting that names a
  host carries over unchanged.
- **Config files.** Helm cannot read files outside a chart, so the Envoy, OTel collector,
  Prometheus and Grafana files under `ops/` are copied into their charts by
  `sync_chart_files.py`; a meta-test fails if a copy differs from its original. Compose is
  not changed. The Prometheus scrape targets and Envoy cluster addresses already use compose
  service names, so the copies need no edits; any file that does need a Kubernetes-specific
  value is templated in the chart instead and excluded from the identity check, by name.

## 4. Networking

One host (`localhost:8080` on k3d; the CloudFront hostname in sub-project 3), one Traefik
ingress, routes by path:

| Path | Service | Notes |
| --- | --- | --- |
| `/` | `frontend` (nginx serving the SPA) | the image is unchanged; its own `/api` proxy is simply not used |
| `/api/*` | `app` :8000 | prefix stripped (as Envoy does in compose); FastAPI `root_path=/api` so Swagger `/api/docs` works |
| `/smarthealth.assistant.v1.Assistant/*`, `/smarthealth.knowledge.v1.Knowledge/*` | `envoy` :8088 → gRPC-Web | no buffering, no response timeout (a chat turn streams) |
| `/grafana`, `/jaeger`, `/temporal`, `/mailpit`, `/rabbitmq`, `/prometheus` | each tool | behind one basic-auth middleware |

- Sub-path settings: Grafana `GF_SERVER_ROOT_URL` + `GF_SERVER_SERVE_FROM_SUB_PATH`; Jaeger
  `QUERY_BASE_PATH`; Temporal UI `TEMPORAL_UI_PUBLIC_PATH`; Mailpit `MP_WEBROOT`; RabbitMQ
  `management.path_prefix`; Prometheus `--web.external-url` + `--web.route-prefix`.
- **Qdrant's dashboard stays internal** (it does not serve cleanly under a sub-path);
  `kubectl port-forward svc/qdrant 6333` reaches it. Grafana keeps its own login as well.
- Nothing else is exposed: no NodePorts, no LoadBalancer services besides Traefik's.
- If `root_path` for the API needs an application setting, it is added to `Settings`
  (default empty, so compose and tests are unaffected).

## 5. Secrets

Every chart reads credentials from one Secret, `smarthealth-secrets`: Postgres passwords (×3),
Mongo, RabbitMQ, Grafana admin, the JWT signing key, the basic-auth `htpasswd` line, the
optional Groq API key, and the demo password. `secrets.create: true` (k3d) renders it from
`values-local.yaml`, whose values are the public development defaults of `.env.example`;
`secrets.create: false` (AWS) expects it to exist — sub-project 3 creates it from SSM before
Argo CD syncs. No non-development secret is ever in git.

## 6. Startup, jobs, storage

- **Order:** `sh-lib` provides a `wait-for` initContainer (busybox, waits until a host:port
  accepts connections). Workloads that cannot start without a dependency use it (e.g. `app`
  waits for Postgres, Mongo, Redis, Temporal; `relay` for Kafka and Schema Registry); the
  rest rely on restart with backoff, which they already tolerate.
- **Migrations** stay in the `app` container's entrypoint (`alembic upgrade head`, then
  uvicorn) — one replica, so nothing races; the entrypoint's comment already notes a Job for
  multiple replicas.
- **Kafka topics and schemas:** `relay` runs `python -m app.relay --setup` as an
  initContainer (idempotent), then relays.
- **Knowledge corpus:** ingested by `knowledge` at start-up, as today; its model cache is a
  volume.
- **Demo data:** a `demo-seed` Job, disabled by default (`demoSeed.enabled`,
  `demoSeed.mode: seed|refresh`), runs `python -m app.cli demo-seed|demo-refresh
  --api-url http://app:8000 --allow-demo-data` with `SMARTHEALTH_DEMO_PASSWORD` from the
  Secret. Sub-project 3's start script triggers `refresh`.
- **Storage:** k3s's `local-path` provisioner. Persistent: the three Postgres, Mongo, Kafka,
  Qdrant, the knowledge model cache and Part A documents — compose's volume set; they survive
  the server being stopped. Part A documents are shared by `app` and `celery-worker`; this
  works because there is **one node** (local-path volumes are node-local), an assumption the
  chart states. Redis, RabbitMQ, Prometheus and Jaeger are not persisted, as in compose.
  *As built:* each persistent volume is a standalone PVC annotated
  `helm.sh/resource-policy: keep`, mounted by a one-replica Deployment with
  `strategy: Recreate` -- not a StatefulSet. On one replica a StatefulSet adds nothing, and
  `keep` means `helm uninstall` never deletes the data.

## 7. The 8 GB budget

Every container has memory requests and limits.

| Group | Limit |
| --- | --- |
| Kafka (`KAFKA_HEAP_OPTS=-Xmx512m`), Schema Registry (`-Xmx256m`) | ~1.25 GB |
| Temporal, Temporal UI | ~0.5 GB |
| Postgres ×3, Mongo (`--wiredTigerCacheSizeGB 0.25`), Redis ×2, RabbitMQ, Qdrant | ~1.6 GB |
| Part A, six processes | ~1.2 GB |
| Knowledge (embedding model), assistant | ~0.8 GB |
| OTel collector, Jaeger (in-memory, max 20k traces), Prometheus (2-day retention), Grafana, kafka-exporter | ~0.6 GB |
| Envoy, frontend, Mailpit, payments-mock | ~0.25 GB |
| **Total** | **~6.2 GB** |

About 1.3 GB remains for k3s, Traefik and the OS. Requests sum to under 5 GB so the
scheduler never refuses a pod. The exact per-container numbers are set from `kubectl top`
measurements on k3d and recorded in `values.yaml` comments.

## 8. Proving it on k3d

`scripts/k3d_up.sh`:
1. creates a one-node k3d cluster `smarthealth` with `--servers-memory 8G`, port 8080 → the
   ingress, and the API server on `127.0.0.1:6550` (otherwise Docker Desktop's kubeconfig
   names `host.docker.internal`, which resolves to the LAN address and may be firewalled);
2. `push_images.sh --local` builds the five images and imports them;
3. `helm dependency update` on every component chart, then the umbrella, and `helm upgrade --install smarthealth deploy/helm/smarthealth
   -f values-local.yaml --wait --timeout 15m`.

`scripts/k3d_smoke.py` then fails on the first problem:
- every pod Ready, and no container restarting after start-up settles;
- `GET /api/ready` and `POST /api/auth/login` through the ingress;
- gRPC-Web through the ingress (reusing `scripts/grpcweb_smoke.py`);
- each tool path answers 401 without the login and 200 with it;
- the `demo-seed` Job runs to completion — booking workflows, Kafka and Celery inside the
  cluster;
- `kubectl top` per pod, printed against the budget.

`scripts/k3d_down.sh` deletes the cluster. Prerequisites: `helm` and `k3d` (`winget`), Docker.

## 9. Fast tests (no cluster)

Meta-tests that render the umbrella with `helm template` (skipped when `helm` is not on the
path):
- the chart renders and `helm lint` passes for every chart;
- every compose service in profiles `app`/`partb` plus the base set has a workload, so a new
  compose service fails until it has one;
- every container has memory requests and limits; total limits ≤ 6.5 GB;
- with `secrets.create: false`, no Secret is rendered;
- image references match compose's images and tags;
- `ops/` copies are identical to their originals.

## 10. Documentation

README: a "Kubernetes (k3d)" section — prerequisites, up/smoke/down, the URL table, the
basic-auth login. Site (`docs/index.html`): a deployment section with an ingress → path →
service diagram. ARCHITECTURE: a deployment section. PRD §8: decisions H-1…H-5, numbered as
the next free D- ids.

## 11. Out of scope

AWS, Terraform, Argo CD, CloudFront, TLS (sub-project 3; CloudFront terminates TLS there).
High availability, autoscaling, multi-node storage, backups. Changing compose.
