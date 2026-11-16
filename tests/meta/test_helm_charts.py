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


@lru_cache(maxsize=1)
def build_dependencies() -> None:
    """Refresh every chart's packaged dependencies, innermost first: each component chart
    carries its own copy of sh-lib, and updating only the umbrella would package stale
    copies (an sh-lib edit would then not show in any render)."""
    for chart in sorted((HELM_DIR / "charts").iterdir()):
        if (chart / "Chart.yaml").is_file():
            _helm("dependency", "update", chart.as_posix())
    _helm("dependency", "update", UMBRELLA.as_posix())


@lru_cache(maxsize=8)
def render(*value_files: str, sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    build_dependencies()
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


import importlib.util  # noqa: E402


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
