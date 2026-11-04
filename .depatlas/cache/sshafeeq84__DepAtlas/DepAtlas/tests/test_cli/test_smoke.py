"""CLI smoke tests.

No other test file imports depatlas.cli.main at all -- every other test
exercises the underlying modules (parsers, store, connectors, etc.)
directly. That's normally the right call for fast, focused unit tests,
but it means a bug in the CLI wiring itself (a bad Click decorator, a
typo'd option name) can slip through with 100% green tests elsewhere.

This file's job is narrow on purpose: prove every command's argument
parsing is valid and every command is reachable, using Click's own
CliRunner rather than shelling out to a real subprocess. It is not
trying to re-test business logic already covered elsewhere.
"""

from __future__ import annotations

from click.testing import CliRunner

from depatlas.cli.main import cli


def test_cli_help_runs_without_error():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0


def test_every_command_help_runs_without_error():
    # This is the check that would have caught a duplicate Click option
    # name (like --from being declared with two variable names at once)
    # -- that kind of bug raises at decoration time, i.e. the moment
    # depatlas.cli.main is imported, which only happens when a test
    # actually imports this module.
    runner = CliRunner()
    for command_name in ("scan", "query", "snapshots", "diff", "alerts", "export"):
        result = runner.invoke(cli, [command_name, "--help"])
        assert result.exit_code == 0, f"'{command_name} --help' failed: {result.output}"


def test_scan_requires_a_source():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("services.json", "w") as f:
            f.write('{"__self__": "x"}')
        result = runner.invoke(cli, ["scan", "--services", "services.json"])
        assert result.exit_code != 0
        assert "Provide one of --repo, --github-repo, or --codecommit-repo" in result.output


def test_diff_requires_from_to_or_last():
    runner = CliRunner()
    result = runner.invoke(cli, ["diff"])
    assert result.exit_code != 0


def test_query_requires_service_or_team():
    runner = CliRunner()
    with runner.isolated_filesystem():
        import sqlite3

        sqlite3.connect("graph.db").close()
        result = runner.invoke(cli, ["query", "--db", "graph.db"])
        assert result.exit_code != 0
        assert "Provide either --service or --team" in result.output
