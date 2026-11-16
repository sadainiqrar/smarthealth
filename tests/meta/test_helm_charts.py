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
    _helm("dependency", "update", UMBRELLA.as_posix())
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
