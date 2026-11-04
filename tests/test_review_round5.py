"""Round-5 (external) review findings."""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.fsutil import MAX_SOURCE_BYTES, iter_files
from depatlas.ownership.codeowners import OwnershipRule, resolve_owner
from depatlas.parsers.js_ts_parser import parse_repo as parse_js
from depatlas.parsers.python_parser import parse_repo as parse_python

ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---- 1. relative / own-module imports are never another service ----

SERVICES = {"__self__": "me", "orders": "orders"}


def test_python_relative_import_is_not_another_service(tmp_path):
    _write(tmp_path / "me" / "routes.py", "from .orders import Order\nfrom ..orders import X\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


def test_python_absolute_import_still_links(tmp_path):
    _write(tmp_path / "me" / "routes.py", "from orders.client import Order\n")
    assert [e.target_service for e in parse_python(tmp_path, SERVICES, {})] == ["orders"]


def test_python_import_of_a_module_this_repo_defines_is_its_own(tmp_path):
    _write(tmp_path / "orders.py", "x = 1\n")
    _write(tmp_path / "app.py", "import orders\nfrom orders import x\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


def test_python_own_package_under_src_is_its_own(tmp_path):
    _write(tmp_path / "src" / "orders" / "__init__.py", "")
    _write(tmp_path / "src" / "me" / "app.py", "import orders\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


@pytest.mark.parametrize("spec", ["./orders", "../orders/x", ".", "..", "/orders"])
def test_js_relative_specifier_is_not_another_service(tmp_path, spec):
    _write(tmp_path / "a.js", f'import o from "{spec}";\n')
    assert parse_js(tmp_path, SERVICES, {}) == []


def test_js_bare_specifier_still_links(tmp_path):
    _write(tmp_path / "a.js", 'import o from "orders/client";\n')
    assert [e.target_service for e in parse_js(tmp_path, SERVICES, {})] == ["orders"]


# ---- 4. CODEOWNERS folder patterns ----


@pytest.mark.parametrize(
    "pattern, path, matches",
    [
        ("docs/", "docs/a.md", True),
        ("docs/", "docs/deep/a.md", True),
        ("docs/", "x/docs/a.md", True),  # unanchored folder: any depth
        ("docs/", "docsx/a.md", False),
        ("/docs/", "docs/a.md", True),
        ("/docs/", "x/docs/a.md", False),  # anchored: root only
        ("/docs", "docs/a.md", True),  # anchored name, no slash: file or folder
        ("apps/payments/", "apps/payments/s.py", True),
        ("apps/payments/", "other/apps/payments/s.py", False),
        ("apps/payments", "apps/payments/s.py", True),
        ("*.py", "a/b.py", True),
        ("*.py", "a/b.js", False),
        ("build*", "src/build.py", True),
        ("Makefile", "sub/Makefile", True),
        ("Makefile", "sub/Makefile2", False),
        ("/Makefile", "sub/Makefile", False),
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "other/a.md", False),
    ],
)
def test_codeowners_patterns(pattern, path, matches):
    owner = resolve_owner(path, Path("/repo"), [OwnershipRule(pattern, "@t")])
    assert (owner == "@t") is matches


def test_codeowners_last_matching_rule_wins():
    rules = [OwnershipRule("*", "@all"), OwnershipRule("apps/payments/", "@pay")]
    assert resolve_owner("apps/payments/s.py", Path("/r"), rules) == "@pay"
    assert resolve_owner("apps/other/s.py", Path("/r"), rules) == "@all"


# ---- 2/5/8. check ----


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


SVC = {"__self__": "pay-service", "auth_client": "auth-service"}


def _graph(tmp_path, dependents=("billing-service",), own_edges=()):
    teams = {"pay-service": "payments", "auth-service": "identity", "billing-service": "money"}
    edges = [
        {"source_service": d, "target_service": "pay-service", "type": "import",
         "source_file": "x.py", "source_line": 1, "confidence": "high", "from_test": 0}
        for d in dependents
    ] + [
        {"source_service": "pay-service", "target_service": t, "type": "import",
         "source_file": "old.py", "source_line": 1, "confidence": "high", "from_test": 0}
        for t in own_edges
    ]
    path = tmp_path / "org.json"
    path.write_text(json.dumps({"nodes": [{"id": k, "team": v} for k, v in teams.items()], "edges": edges}))
    return path


def _check(tmp_path, repo, *extra):
    sp = tmp_path / "services.json"
    sp.write_text(json.dumps(SVC))
    return CliRunner().invoke(
        cli, ["check", "--repo", str(repo), "--base", "main", "--services", str(sp), *extra]
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pay"
    _init(r)
    _write(r / "charge.py", "def charge(amount, currency):\n    return amount\n")
    _commit(r, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    return r


def test_check_ignores_files_that_are_not_committed(tmp_path, repo):
    """A generated / git-ignored file on disk exists only on the head side
    when the head is read from the working tree -- a false 'new dependency'."""
    _write(repo / ".gitignore", "generated_pb2.py\n")
    _commit(repo, "ignore generated")
    _write(repo / "generated_pb2.py", "import auth_client\n")  # e.g. built by an earlier CI step
    r = _check(tmp_path, repo, "--graph", str(_graph(tmp_path)), "--fail-on", "medium")
    assert r.exit_code == 0, r.output
    assert "NEW_CROSS_TEAM_DEPENDENCY" not in r.output


def test_export_ignore_does_not_hide_a_breaking_change(tmp_path):
    """`git archive` leaves export-ignore files out, so the base never saw
    legacy.py and its changed signature looked like a brand-new function."""
    r0 = tmp_path / "pay"
    _init(r0)
    _write(r0 / ".gitattributes", "legacy.py export-ignore\n")
    _write(r0 / "legacy.py", "def old_api(a):\n    return a\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "legacy.py").write_text("def old_api(a, b):\n    return a\n")
    _commit(r0, "change")
    r = _check(tmp_path, r0, "--graph", str(_graph(tmp_path)), "--fail-on", "high")
    assert r.exit_code == 3, r.output
    assert "old_api" in r.output


def test_uncommitted_changes_are_reported_as_skipped_and_opt_in(tmp_path, repo):
    (repo / "charge.py").write_text("def other(x):\n    return x\n")  # removes charge(), uncommitted
    graph = _graph(tmp_path)

    default = _check(tmp_path, repo, "--graph", str(graph), "--fail-on", "high")
    assert default.exit_code == 0, default.output
    assert "uncommitted changes are not checked" in default.output

    opted_in = _check(tmp_path, repo, "--graph", str(graph), "--fail-on", "high", "--working-tree")
    assert opted_in.exit_code == 3, opted_in.output
    assert "uncommitted changes are not checked" not in opted_in.output


def test_a_new_folder_uses_the_org_graph_as_its_baseline(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    (mono / "README.md").write_text("x")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    svc = mono / "pay"
    svc.mkdir()
    (svc / "client.py").write_text("import auth_client\n")
    _commit(mono, "move service here")

    # The graph already knows pay-service -> auth-service: not a new dependency.
    known = _check(
        tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=(), own_edges=("auth-service",))),
        "--fail-on", "medium",
    )
    assert known.exit_code == 0, known.output
    assert "baseline" in known.output

    # A genuinely new service has no edges in the graph: still all-new.
    new = _check(tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")
    assert new.exit_code == 3, new.output


def test_fail_on_without_a_graph_is_a_usage_error(tmp_path, repo):
    r = _check(tmp_path, repo, "--fail-on", "high")
    assert r.exit_code == 2
    assert "--graph" in r.output


def test_check_without_fail_on_or_graph_still_runs(tmp_path, repo):
    r = _check(tmp_path, repo)
    assert r.exit_code == 0, r.output


# ---- 3/6/7. workflows ----


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / "examples" / "github-actions" / name).read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_nightly_refuses_to_publish_from_a_public_repo():
    steps = _steps(_workflow("depatlas-nightly.yml"))
    guard = next(i for i, s in enumerate(steps) if "isPrivate" in s.get("run", ""))
    scan = next(i for i, s in enumerate(steps) if "scan-all" in s.get("run", ""))
    assert guard < scan  # checked before any token is used
    assert "exit 1" in steps[guard]["run"]


def test_pr_check_uses_its_own_narrow_token_not_the_nightly_one():
    pr = (ROOT / "examples" / "github-actions" / "depatlas-pr-check.yml").read_text(encoding="utf-8")
    nightly = (ROOT / "examples" / "github-actions" / "depatlas-nightly.yml").read_text(encoding="utf-8")
    assert "DEPATLAS_GRAPH_TOKEN" in pr and "DEPATLAS_READ_TOKEN" not in pr.replace("# ", "").split("jobs:")[1]
    assert "DEPATLAS_READ_TOKEN" in nightly
    assert "fork" in pr.lower()  # the skipped-check caveat is explained


def test_workflows_pin_the_released_version():
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    for name in ("depatlas-nightly.yml", "depatlas-pr-check.yml"):
        installs = [s["run"] for s in _steps(_workflow(name)) if "pip install" in s.get("run", "")]
        assert installs == [f"pip install depatlas=={version}"], name


def test_grammar_packages_are_bounded():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("tree-sitter-java", "tree-sitter-javascript", "tree-sitter-typescript"):
        assert re.search(rf'"{name}>=[\d.]+,<[\d.]+"', text), name


# ---- minor: file size cap ----


def test_oversized_source_files_are_skipped_with_a_warning(tmp_path, capsys):
    (tmp_path / "small.js").write_text("x\n")
    big = tmp_path / "bundle.min.js"
    big.write_text("a" * (MAX_SOURCE_BYTES + 1))
    assert [p.name for p in iter_files(tmp_path, "*.js")] == ["small.js"]
    assert "bundle.min.js" in capsys.readouterr().err
