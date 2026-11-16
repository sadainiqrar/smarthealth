"""The demo commands refuse to run unless asked to with --allow-demo-data."""

from __future__ import annotations

import pytest

from app import cli

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("command", ["demo-seed", "demo-refresh"])
def test_without_allow_demo_data_nothing_runs(command, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "run_demo", lambda **kwargs: ran.append(kwargs))
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--password", "x"])
    assert exit_info.value.code == 1 and not ran
    assert "--allow-demo-data" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["demo-seed", "demo-refresh"])
def test_demo_commands_parse_their_options(command):
    args = cli._parse_args([command, "--password", "p", "--allow-demo-data",
                            "--api-url", "http://api:8000", "--timezone", "Asia/Karachi"])
    assert (args.password, args.api_url, args.timezone, args.allow_demo_data) == (
        "p", "http://api:8000", "Asia/Karachi", True)
