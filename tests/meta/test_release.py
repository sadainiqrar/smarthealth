"""scripts/set_release.py rewrites values-aws.yaml without AWS (used by scripts/release.sh)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

ROOT = Path(__file__).resolve().parents[2]
VALUES = ROOT / "deploy" / "helm" / "smarthealth" / "values-aws.yaml"


def _module():
    path = ROOT / "scripts" / "set_release.py"
    spec = importlib.util.spec_from_file_location("set_release", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_set_release_rewrites_tag_and_url_only():
    original = VALUES.read_text(encoding="utf-8")
    out = _module().set_release(original, "abc1234", "https://dxyz.cloudfront.net")
    assert '    tag: "abc1234"\n' in out
    assert "  publicUrl: https://dxyz.cloudfront.net\n" in out
    assert "tag: unreleased" not in out
    assert out.count("\n") == original.count("\n")
    assert "registry: ghcr.io/sadainiqrar" in out


def test_set_release_without_url_keeps_the_url():
    original = VALUES.read_text(encoding="utf-8")
    out = _module().set_release(original, "abc1234")
    # Whatever address is recorded (the placeholder, or the real distribution's) stays.
    url = next(line for line in original.splitlines() if line.startswith("  publicUrl: "))
    assert url + "\n" in out


def test_set_release_cli_writes_lf(tmp_path):
    copy = tmp_path / "values-aws.yaml"
    copy.write_bytes(VALUES.read_bytes())
    assert _module().main(["x", str(copy), "deadbee", "https://q.cloudfront.net"]) == 0
    data = copy.read_bytes()
    assert b"\r" not in data
    assert b'tag: "deadbee"' in data


def test_all_digit_tag_stays_a_string():
    # A short SHA can be all digits (3244061 was): unquoted, YAML reads it as a number and
    # Helm renders the image as smarthealth-core:3.244061e+06 -- InvalidImageName.
    import yaml

    out = _module().set_release(VALUES.read_text(encoding="utf-8"), "3244061")
    assert yaml.safe_load(out)["global"]["image"]["tag"] == "3244061"
