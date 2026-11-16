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
