"""Static checks of deploy/terraform (spec 2026-11-17-aws-deploy-design.md section 8).
Nothing here talks to AWS: fmt and validate run with -backend=false. Skipped without
terraform on the path."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "deploy" / "terraform"
TERRAFORM = shutil.which("terraform")
needs_terraform = pytest.mark.skipif(TERRAFORM is None, reason="terraform is not installed")


def _tf(directory: Path, data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    # Its own TF_DATA_DIR: once the stack has been deployed, deploy/terraform/.terraform
    # names the S3 backend, and an offline init there would ask for AWS credentials.
    env = {**os.environ, "TF_DATA_DIR": str(data_dir)}
    return subprocess.run([TERRAFORM, f"-chdir={directory.as_posix()}", *args],
                          capture_output=True, text=True, check=False, env=env)


@needs_terraform
@pytest.mark.parametrize("directory", [TF, TF / "bootstrap"], ids=["stack", "bootstrap"])
def test_terraform_is_formatted_and_valid(directory, tmp_path):
    fmt = _tf(directory, tmp_path, "fmt", "-check", "-recursive")
    assert fmt.returncode == 0, f"terraform fmt would change:\n{fmt.stdout}"
    init = _tf(directory, tmp_path, "init", "-backend=false", "-input=false")
    assert init.returncode == 0, init.stderr
    valid = _tf(directory, tmp_path, "validate")
    assert valid.returncode == 0, valid.stderr


def test_everything_is_in_the_one_region():
    text = "\n".join(p.read_text(encoding="utf-8") for p in TF.rglob("*.tf"))
    regions = set(re.findall(r'"((?:us|eu|ap|ca|sa|me|af)-[a-z]+-\d)"', text))
    assert regions <= {"ap-southeast-2"}, regions


USER_DATA = TF / "templates" / "server-user-data.sh.tftpl"
BOOT = TF / "templates" / "smarthealth-boot.sh"


def test_the_server_boot_scripts_pin_versions_and_hold_no_secrets():
    user_data = USER_DATA.read_text(encoding="utf-8")
    boot = BOOT.read_text(encoding="utf-8")
    assert "INSTALL_K3S_VERSION=${k3s_version}" in user_data
    assert "targetRevision: ${repo_revision}" in user_data
    assert 'version: "${argocd_chart_version}"' in user_data
    assert 'shutdown -h "+$AUTOSTOP_MINUTES"' in boot
    assert "get-parameters-by-path" in boot
    for text in (user_data, boot):
        lowered = text.lower()
        literal = re.search(r"password=[\"']?[a-z0-9]", lowered)
        assert not literal, "literal password in a boot script"
        assert "begin private key" not in lowered


def test_the_boot_script_has_no_template_syntax():
    # It is injected into the user-data template as a value, but a stray ${ would still be
    # confusing there and would break if the script were ever rendered itself.
    assert "${" not in BOOT.read_text(encoding="utf-8")


def test_bash_scripts_parse():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    scripts = [BOOT, *sorted((ROOT / "scripts").glob("*.sh"))]
    for script in scripts:
        result = subprocess.run([bash, "-n", script.as_posix()], capture_output=True,
                                text=True, check=False)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_cloudfront_reaches_the_server_privately_and_never_caches_the_api():
    text = (TF / "cloudfront.tf").read_text(encoding="utf-8")
    assert 'resource "aws_cloudfront_vpc_origin"' in text
    assert "vpc_origin_config" in text
    assert re.search(r'viewer_protocol_policy\s*=\s*"redirect-to-https"', text)
    assert "Managed-CachingDisabled" in text
    assert re.search(r'path_pattern\s*=\s*"/assets/\*"', text)
    assert "logging_config" in text
