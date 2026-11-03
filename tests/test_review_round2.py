"""Regression tests for the round-2 code review findings."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.graph.models import DependencyEdge
from depatlas.graph.store import GraphStore


def _edge(src, dst, file="a.py", line=1):
    return DependencyEdge(
        source_service=src, target_service=dst, type="import",
        source_file=file, source_line=line, confidence="high",
    )


# ---- non-UTF-8 files never abort a JavaScript scan ----


def test_js_non_utf8_file_does_not_abort_the_scan(tmp_path, capsys):
    from depatlas.parsers.js_ts_parser import extract_signatures, parse_repo

    (tmp_path / "good.js").write_text("import x from 'auth-lib';\n")
    (tmp_path / "bad.js").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "package.json").write_bytes(b"\xff\xfe\x00bad")

    edges = parse_repo(tmp_path, {"__self__": "me", "auth-lib": "auth-service"})
    sigs = extract_signatures(tmp_path, "me")

    assert len(edges) == 1
    assert sigs == []
    assert "skipping" in capsys.readouterr().err


# ---- test files and private classes are not public API ----


def test_python_tests_and_private_classes_are_not_public_signatures(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_charge.py").write_text("def test_charge_works():\n    pass\n")
    (tmp_path / "service.py").write_text(
        "def charge(a):\n    pass\n\nclass _Helper:\n    def run(self):\n        pass\n"
    )

    assert [s.name for s in extract_signatures(tmp_path, "svc")] == ["charge"]


def test_java_tests_and_package_private_classes_are_not_public_signatures(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures

    test_dir = tmp_path / "src" / "test" / "java"
    test_dir.mkdir(parents=True)
    (test_dir / "ChargeTest.java").write_text("public class ChargeTest { public void works() {} }\n")
    main_dir = tmp_path / "src" / "main" / "java"
    main_dir.mkdir(parents=True)
    (main_dir / "Api.java").write_text("public class Api { public void charge(int a) {} }\n")
    (main_dir / "Internal.java").write_text("class Internal { public void helper() {} }\n")

    assert [s.name for s in extract_signatures(tmp_path, "svc")] == ["charge"]


# ---- stored paths are repo-relative, so moving the repo causes no churn ----


def test_edge_paths_are_relative_and_survive_moving_the_repo(tmp_path):
    from depatlas.intelligence.diff import compute_diff
    from depatlas.parsers.python_parser import parse_repo

    services = {"__self__": "me", "auth_client": "auth-service"}
    first = tmp_path / "one"
    first.mkdir()
    (first / "m.py").write_text("import auth_client\n")
    second = tmp_path / "elsewhere" / "copy"
    shutil.copytree(first, second)

    e1 = parse_repo(first, services)
    e2 = parse_repo(second, services)

    assert e1[0].source_file == "m.py"
    d = compute_diff([e.to_dict() for e in e1], [e.to_dict() for e in e2])
    assert d == {"added": [], "removed": []}


# ---- self-dependencies are dropped ----


def test_a_service_importing_its_own_package_is_not_a_dependency(tmp_path):
    from depatlas.parsers.python_parser import parse_repo

    (tmp_path / "m.py").write_text("import payments_pkg\nimport auth_client\n")
    edges = parse_repo(
        tmp_path,
        {"__self__": "payments-service", "payments_pkg": "payments-service",
         "auth_client": "auth-service"},
    )
    assert [e.target_service for e in edges] == ["auth-service"]


# ---- URL resolution: hostname only, longest first, no generic names ----


def test_url_guess_uses_the_hostname_only():
    from depatlas.resolution import resolve_endpoint

    services = {"__self__": "gateway", "a": "auth-service", "p": "payments-service"}
    assert resolve_endpoint("https://payments.example.com/api/auth/login", {}, services) == (
        "payments-service", "low",
    )
    assert resolve_endpoint("https://example.com/auth/login", {}, services) == (None, None)
    assert resolve_endpoint("/auth/login", {}, services) == (None, None)


def test_url_guess_prefers_the_longest_service_name_and_skips_generic_ones():
    from depatlas.resolution import resolve_endpoint

    services = {"__self__": "gw", "a": "auth-service", "b": "auth-admin-service", "c": "api-service"}
    assert resolve_endpoint("http://auth-admin/x", {}, services) == ("auth-admin-service", "low")
    assert resolve_endpoint("http://api.example.com/x", {}, services) == (None, None)


def test_registry_patterns_match_at_host_boundaries():
    from depatlas.resolution import resolve_endpoint

    endpoints = {"auth.internal": "auth-service"}
    assert resolve_endpoint("http://auth.internal/v", endpoints, {})[0] == "auth-service"
    assert resolve_endpoint("http://myauth.internal.example/v", endpoints, {})[0] is None


# ---- stored repo keys ----


def test_snapshots_and_diff_find_github_style_repo_keys(tmp_path):
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.save_snapshot("github:org/payments", [_edge("p", "a")])
    store.save_snapshot("github:org/payments", [_edge("p", "a"), _edge("p", "b", line=2)])
    store.close()

    runner = CliRunner()
    snaps = runner.invoke(cli, ["snapshots", "--repo", "github:org/payments", "--db", str(db)])
    diff = runner.invoke(cli, ["diff", "--last", "2", "--repo", "github:org/payments", "--db", str(db)])

    assert snaps.exit_code == 0 and "github:org/payments" in snaps.output
    assert diff.exit_code == 0 and "Not enough snapshots" not in diff.output


# ---- a repo with several languages is one service, one snapshot ----


def test_scan_with_several_languages_saves_one_combined_snapshot(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "a.py").write_text("import auth_client\n")
    (repo / "B.java").write_text("package x;\nimport com.org.ledger.Client;\nclass B {}\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps(
        {"__self__": "svc", "auth_client": "auth-service", "com.org.ledger": "ledger-service"}
    ))
    db = tmp_path / "g.db"
    args = ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db),
            "--language", "all"]
    runner = CliRunner()
    assert runner.invoke(cli, args).exit_code == 0
    assert runner.invoke(cli, args).exit_code == 0

    store = GraphStore(db)
    targets = sorted(e["target_service"] for e in store.all_edges())
    snaps = store.list_snapshots()
    store.close()
    assert targets == ["auth-service", "ledger-service"]
    d = runner.invoke(cli, ["diff", "--last", "2", "--repo", str(repo), "--db", str(db)])
    assert "auth-service" not in d.output  # nothing changed between identical scans
    assert len(snaps) == 2


# ---- store: atomic scans, legacy rows, placeholders ----


def test_transaction_rolls_back_every_write_on_failure(tmp_path):
    from depatlas.graph.models import ServiceNode

    store = GraphStore(tmp_path / "g.db")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert_services([ServiceNode(id="a", name="a", repo="/r", language="python")])
            store.replace_edges_for_repo("/r", [_edge("a", "b", "/r/x.py")])
            raise RuntimeError("crash mid-scan")

    assert store.all_services() == [] and store.all_edges() == []
    store.close()


def test_legacy_edges_under_a_longer_path_survive_a_shorter_repo_rescan(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE edges (source_service TEXT NOT NULL, target_service TEXT NOT NULL,
        type TEXT NOT NULL, source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
        confidence TEXT NOT NULL, detected_at_sha TEXT);
        INSERT INTO edges VALUES ('p','a','import','/repos/payments/x.py',1,'high',NULL);"""
    )
    conn.commit(); conn.close()

    store = GraphStore(db)
    store.replace_edges_for_repo("/repos/pay", [])
    assert len(store.all_edges()) == 1  # /repos/pay must not delete /repos/payments/...
    store.close()


def test_dropped_dependency_placeholder_is_removed(tmp_path):
    from depatlas.graph.models import ServiceNode

    store = GraphStore(tmp_path / "g.db")
    store.upsert_services([
        ServiceNode(id="p", name="p", repo="/r", language="python"),
        ServiceNode(id="auth", name="auth", repo="", language=""),
    ])
    store.replace_edges_for_repo("/r", [_edge("p", "auth", "/r/x.py")])
    assert {s["id"] for s in store.all_services()} == {"p", "auth"}

    store.replace_edges_for_repo("/r", [])  # payments stopped depending on auth
    assert {s["id"] for s in store.all_services()} == {"p"}
    store.close()


# ---- CodeCommit: one pinned commit for the whole walk ----


def test_codecommit_pins_every_call_to_the_first_commit(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo
    from depatlas.gitinfo import get_sha

    calls = []

    class Client:
        def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
            calls.append(("folder", folderPath, commitSpecifier))
            if folderPath == "/":
                return {"commitId": "c1", "files": [{"absolutePath": "/a.py"}],
                        "subFolders": [{"absolutePath": "/sub"}]}
            return {"files": [{"absolutePath": "/sub/b.py"}], "subFolders": []}

        def get_file(self, repositoryName, filePath, commitSpecifier=None):
            calls.append(("file", filePath, commitSpecifier))
            return {"fileContent": b"x"}

    result = fetch_repo("r", Client(), tmp_path)

    assert calls[0][2] is None
    assert all(c[2] == "c1" for c in calls[1:])
    assert get_sha(result) == "c1"


def test_codecommit_empty_repo_does_not_inherit_a_previous_sha(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo
    from depatlas.gitinfo import get_sha, write_sha

    (tmp_path / "r").mkdir()
    write_sha(tmp_path / "r", "stale")

    class Empty:
        def get_folder(self, **kwargs):
            return {"files": [], "subFolders": []}

    assert get_sha(fetch_repo("r", Empty(), tmp_path)) is None


# ---- CLI: --version and --fail-on ----


def test_version_flag():
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0 and "version" in r.output.lower()


def test_fail_on_exits_nonzero_only_when_an_alert_reaches_the_threshold(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc"}))
    db = tmp_path / "g.db"
    base = ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)]
    runner = CliRunner()

    # An unowned, orphaned service raises only LOW alerts.
    assert runner.invoke(cli, base + ["--fail-on", "high"]).exit_code == 0
    assert runner.invoke(cli, base + ["--fail-on", "low"]).exit_code == 1
    assert runner.invoke(cli, ["alerts", "--db", str(db), "--fail-on", "low"]).exit_code == 1
    assert runner.invoke(cli, ["alerts", "--db", str(db), "--fail-on", "high"]).exit_code == 0
