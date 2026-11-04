"""`depatlas check --base`: the stateless pull-request check, against real git repos."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.intelligence.diff import compute_diff


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True,
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


SERVICES = {"__self__": "pay-service", "auth_client": "auth-service"}


def _graph(tmp_path, dependents=("billing-service",), teams=None):
    teams = teams or {"pay-service": "payments", "auth-service": "identity", "billing-service": "money"}
    graph = {
        "nodes": [{"id": k, "team": v} for k, v in teams.items()],
        "edges": [
            {"source_service": d, "target_service": "pay-service", "type": "import",
             "source_file": "x.py", "source_line": 1, "confidence": "high", "from_test": 0}
            for d in dependents
        ],
    }
    path = tmp_path / "org.json"
    path.write_text(json.dumps(graph))
    return path


def _run(tmp_path, repo, *extra, services=SERVICES):
    sp = tmp_path / "services.json"
    sp.write_text(json.dumps(services))
    return CliRunner().invoke(
        cli, ["check", "--repo", str(repo), "--base", "main", "--services", str(sp), *extra]
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pay"
    _init(r)
    (r / "charge.py").write_text("def charge(amount, currency):\n    return amount\n")
    _commit(r, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    return r


def test_removed_public_function_with_a_dependent_is_breaking_and_fails_the_job(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "BREAKING_CHANGE" in r.output and "charge" in r.output


def test_same_change_without_dependents_is_not_breaking(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "high")

    assert r.exit_code == 0 and "No alerts" in r.output


def test_new_cross_team_dependency_is_reported(tmp_path, repo):
    (repo / "client.py").write_text("import auth_client\n")
    _commit(repo, "depend on auth")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")

    assert r.exit_code == 3, r.output
    assert "NEW_CROSS_TEAM_DEPENDENCY" in r.output


def test_a_dependency_that_already_existed_is_not_new(tmp_path):
    r0 = tmp_path / "pay"
    _init(r0)
    (r0 / "client.py").write_text("import auth_client\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "client.py").write_text("\n\nimport auth_client  # moved down\n")
    _commit(r0, "move")

    r = _run(tmp_path, r0, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "low")

    assert r.exit_code == 0 and "No alerts" in r.output


def test_without_a_graph_it_says_why_nothing_can_fire(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo)

    assert r.exit_code == 0
    assert "--graph" in r.output


def test_history_missing_gives_an_actionable_error(tmp_path, repo):
    r = CliRunner().invoke(
        cli,
        ["check", "--repo", str(repo), "--base", "origin/nope", "--services",
         str(_write(tmp_path / "s.json", SERVICES))],
    )
    assert r.exit_code == 1
    assert "fetch-depth: 0" in r.output


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_monorepo_subfolder_is_compared_with_its_own_base(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    svc = mono / "services" / "pay"
    svc.mkdir(parents=True)
    (svc / "charge.py").write_text("def charge(amount, currency):\n    return amount\n")
    (mono / "other.py").write_text("def unrelated():\n    pass\n")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    (svc / "charge.py").write_text("def charge(amount):\n    return amount\n")
    _commit(mono, "drop a parameter")

    r = _run(tmp_path, svc, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "charge" in r.output and "unrelated" not in r.output


def test_a_brand_new_service_folder_has_no_base_and_does_not_crash(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    (mono / "README.md").write_text("x")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    svc = mono / "pay"
    svc.mkdir()
    (svc / "client.py").write_text("import auth_client\n")
    _commit(mono, "add service")

    r = _run(tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")

    assert r.exit_code == 3, r.output  # its new cross-team edge is reported
    assert "treating it as new" in r.output


def test_a_language_removed_in_the_change_is_still_compared(tmp_path):
    r0 = tmp_path / "pay"
    _init(r0)
    (r0 / "Api.java").write_text("public class Api { public void pay(int a) {} }\n")
    (r0 / "x.py").write_text("x = 1\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "Api.java").unlink()
    _commit(r0, "delete java")

    r = _run(tmp_path, r0, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "pay" in r.output


def test_bad_graph_file_is_a_clear_error(tmp_path, repo):
    bad = _write(tmp_path / "bad.json", {"nope": 1})
    r = _run(tmp_path, repo, "--graph", str(bad))
    assert r.exit_code == 1 and "graph export" in r.output


def test_diff_prefers_a_production_edge_over_a_test_edge_with_the_same_identity():
    e = lambda t: {"source_service": "a", "target_service": "b", "type": "import",
                   "source_file": "f", "source_line": 1, "from_test": t}
    # same dependency found in a prod file first and a test file later (and the reverse)
    for order in ([e(0), e(1)], [e(1), e(0)]):
        added = compute_diff([], order, by_location=False)["added"]
        assert len(added) == 1 and not added[0]["from_test"]
