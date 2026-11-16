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


@pytest.mark.parametrize("command", ["demo-seed", "demo-refresh"])
def test_password_falls_back_to_the_environment(command, monkeypatch):
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs["password"])

    async def runner(**kwargs):
        fake_run(**kwargs)

    monkeypatch.setattr(cli, "run_demo", runner)
    monkeypatch.setenv("SMARTHEALTH_DEMO_PASSWORD", "from-env")
    cli.main([command, "--allow-demo-data"])
    cli.main([command, "--allow-demo-data", "--password", "flag"])
    assert seen == ["from-env", "flag"]


@pytest.mark.parametrize("command", ["demo-seed", "demo-refresh"])
def test_no_password_anywhere_is_an_error(command, monkeypatch, capsys):
    monkeypatch.delenv("SMARTHEALTH_DEMO_PASSWORD", raising=False)
    ran = []
    monkeypatch.setattr(cli, "run_demo", lambda **kwargs: ran.append(kwargs))
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--allow-demo-data"])
    assert exit_info.value.code == 1 and not ran
    assert "SMARTHEALTH_DEMO_PASSWORD" in capsys.readouterr().err
