"""CLI smoke tests (no DB required)."""

from __future__ import annotations

from typer.testing import CliRunner

from pgreplkit import __version__
from pgreplkit.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in [
        "discover",
        "plan",
        "preflight",
        "globals",
        "setup",
        "refresh",
        "status",
        "watch",
        "validate",
        "ready",
        "cutover",
        "reverse",
        "teardown",
        "guide",
    ]:
        assert cmd in result.stdout


def test_scaffolded_command_runs() -> None:
    # subcommand --help always exits 0 without needing config/DB
    result = runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
