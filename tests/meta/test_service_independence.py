"""The three services stay three services (Weeks 3-5 spec D-19).

Part A and the two Part B services share channels -- Kafka, gRPC, Part A's public HTTP
API, the observability stack -- and never a store or a line of code. Each rule below is
one way that independence erodes quietly:

1. an import across services (Part A code inside a Part B process, or the reverse);
2. a Part B setting that could point at Part A's Postgres, Redis or Mongo;
3. a compose service given another service's connection settings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

import tests.harness.partb  # noqa: F401 - the services' import paths

ROOT = Path(__file__).resolve().parents[2]
PART_B = {"knowledge": "knowledge_service", "assistant": "assistant_service"}
PART_A_PACKAGES = {"app"}
PART_B_PACKAGES = set(PART_B.values()) | {"services"}

pytestmark = pytest.mark.meta


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _sources(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*.py") if "gen" not in p.parts]


@pytest.mark.parametrize("service", sorted(PART_B))
def test_a_part_b_service_imports_nothing_of_part_a_or_of_the_other(service):
    others = {v for k, v in PART_B.items() if k != service} | PART_A_PACKAGES
    offenders = {
        str(path.relative_to(ROOT)): sorted(_imports(path) & others)
        for path in _sources(ROOT / "services" / service)
        if _imports(path) & others
    }
    assert not offenders, f"{service} reaches into another service: {offenders}"


def test_part_a_imports_nothing_of_part_b():
    offenders = {
        str(path.relative_to(ROOT)): sorted(_imports(path) & PART_B_PACKAGES)
        for path in _sources(ROOT / "app") if _imports(path) & PART_B_PACKAGES
    }
    assert not offenders, f"Part A imports a Part B service: {offenders}"


def _settings_fields():
    from assistant_service.settings import AssistantSettings
    from knowledge_service.settings import KnowledgeSettings

    return {"knowledge": KnowledgeSettings, "assistant": AssistantSettings}


@pytest.mark.parametrize("service", sorted(PART_B))
def test_a_part_b_service_has_no_setting_for_part_as_stores(service):
    model = _settings_fields()[service]
    names = set(model.model_fields)
    assert not {n for n in names if n.startswith(("postgres", "mongo"))}, names
    defaults = {n: str(f.default) for n, f in model.model_fields.items()}
    # Part A's dev stores listen on 5432 (Postgres), 6379 (Redis), 27017 (Mongo).
    for name, value in defaults.items():
        for port in (":5432/", ":6379/", ":27017"):
            assert port not in value, f"{service}.{name} defaults to a Part A store: {value}"


def test_compose_gives_no_part_b_service_another_services_connections():
    compose = yaml.safe_load((ROOT / "docker-compose.infra.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in PART_B:
        environment = services[name].get("environment", {})
        assert not [k for k in environment if k.startswith("SMARTHEALTH_")], (
            f"{name} is configured with Part A's settings")
        for key, value in environment.items():
            for host in ("@postgres:", "//postgres:", "redis://redis:", "mongo:27017"):
                assert host not in str(value), f"{name}.{key} points at a Part A store: {value}"
