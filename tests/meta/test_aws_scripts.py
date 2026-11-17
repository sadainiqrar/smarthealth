"""The deploy scripts (scripts/aws_*.sh, tf.sh, release.sh, aws_smoke.py) parse, and keep the
fixes from the first AWS deploy (docs/ARCHITECTURE.md 8.8). Nothing here talks to AWS."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SHELL = sorted([*SCRIPTS.glob("aws_*.sh"), SCRIPTS / "tf.sh", SCRIPTS / "release.sh"])
BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
@pytest.mark.parametrize("script", SHELL, ids=lambda p: p.name)
def test_shell_script_parses(script):
    result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SHELL, ids=lambda p: p.name)
def test_no_exec_of_the_aws_wrapper(script):
    # "$AWS" is a shell function (it turns off Git Bash's path rewriting); exec cannot run one.
    assert not re.search(r'\bexec\s+"\$AWS"', script.read_text(encoding="utf-8")), script.name


def test_aws_calls_keep_ssm_names_unmangled():
    lib = (SCRIPTS / "aws_lib.sh").read_text(encoding="utf-8")
    assert "MSYS_NO_PATHCONV=1" in lib and "AWS=aws_cli" in lib


def test_ssm_run_does_not_use_the_100_second_waiter():
    lib = (SCRIPTS / "aws_lib.sh").read_text(encoding="utf-8")
    assert not re.search(r'"\$AWS" ssm wait command-executed', lib)


def test_python_finds_bash_on_path():
    # On Windows a bare "bash" resolves to System32's (WSL) before Git Bash.
    smoke = (SCRIPTS / "aws_smoke.py").read_text(encoding="utf-8")
    assert 'shutil.which("bash")' in smoke
    assert '["bash"' not in smoke
