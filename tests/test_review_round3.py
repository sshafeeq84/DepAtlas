"""Regression tests for the round-3 code review findings."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
import requests
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.fsutil import is_test_path
from depatlas.gitinfo import get_sha, write_sha
from depatlas.graph.models import DependencyEdge
from depatlas.graph.store import GraphStore
from depatlas.intelligence.alerts import (
    check_high_fan_in,
    check_new_cross_team_dependencies,
    check_orphaned_service,
    comparable_signatures,
)
from depatlas.resolution import resolve_endpoint


def _edge(src, dst, file="a.py", line=1, from_test=False):
    return DependencyEdge(
        source_service=src, target_service=dst, type="import",
        source_file=file, source_line=line, confidence="high", from_test=from_test,
    )


# ---- 1. legacy edges of remote repos are replaced, not duplicated ----


def test_legacy_null_repo_edges_of_a_remote_repo_are_deleted(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE edges (
            source_service TEXT NOT NULL, target_service TEXT NOT NULL,
            type TEXT NOT NULL, source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL, confidence TEXT NOT NULL,
            detected_at_sha TEXT
        );
        INSERT INTO edges VALUES
          ('pay','auth','import','/work/.depatlas/cache/pay/x.py',1,'high',NULL),
          ('other','auth','import','/work/other/y.py',1,'high',NULL);
        """
    )
    conn.commit()
    conn.close()

    s = GraphStore(db)
    s.replace_edges_for_repo("github:acme/pay", [_edge("pay", "auth", "x.py")], self_service="pay")
    assert len(s.edges_for_service("pay", direction="downstream")) == 1  # not 2
    assert len(s.edges_for_service("other", direction="downstream")) == 1  # untouched
    s.close()


# ---- 2. CodeCommit pins to a commit id even when --ref is a branch ----


def test_codecommit_pins_to_commit_id_when_ref_is_a_branch(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo

    calls = []

    class Client:
        def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
            calls.append(commitSpecifier)
            if folderPath == "/":
                return {"commitId": "c1", "files": [], "subFolders": [{"absolutePath": "/s"}]}
            return {"files": [{"absolutePath": "/s/b.py"}], "subFolders": []}

        def get_file(self, repositoryName, filePath, commitSpecifier=None):
            calls.append(commitSpecifier)
            return {"fileContent": b"x"}

    result = fetch_repo("r", Client(), tmp_path, commit_specifier="main")

    assert calls[0] == "main"
    assert calls[1:] == ["c1", "c1"]
    assert get_sha(result) == "c1"


# ---- 3. Java anonymous classes are not public API ----


def test_java_anonymous_class_methods_are_not_signatures(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures

    (tmp_path / "Outer.java").write_text(
        "public class Outer {\n"
        "  public void real() {}\n"
        "  void use() { Runnable r = new Runnable() { public void run() {} }; }\n"
        "  enum E { A { public void f() {} } }\n"
        "}\n"
    )
    names = {s.name for s in extract_signatures(tmp_path, "svc")}
    assert "real" in names
    assert "run" not in names and "f" not in names


# ---- 4. --fail-on semantics ----


def _scan_args(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir(exist_ok=True)
    (repo / "a.py").write_text("x = 1\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc"}))
    db = tmp_path / "g.db"
    return ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)], db


def test_severity_filter_does_not_hide_alerts_from_fail_on(tmp_path):
    base, db = _scan_args(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, base)
    # Only LOW alerts exist here; filtering to a severity with none must not
    # change the exit decision for --fail-on low.
    r = runner.invoke(cli, ["alerts", "--db", str(db), "--severity", "high", "--fail-on", "low"])
    assert r.exit_code == 3
    r = runner.invoke(cli, ["alerts", "--db", str(db), "--severity", "low", "--fail-on", "high"])
    assert r.exit_code == 0


def test_no_alerts_with_fail_on_is_a_usage_error(tmp_path):
    base, _ = _scan_args(tmp_path)
    r = CliRunner().invoke(cli, base + ["--no-alerts", "--fail-on", "high"])
    assert r.exit_code == 2


# ---- 5. resolver: scheme required for guesses; boundaries; longest first ----


def test_scheme_less_text_is_not_a_hostname_guess():
    services = {"__self__": "me", "orders-service": "orders-service"}
    assert resolve_endpoint("orders/123", {}, services) == (None, None)
    assert resolve_endpoint("http://orders/123", {}, services) == ("orders-service", "low")


def test_registry_match_has_a_trailing_boundary():
    services = {"__self__": "me"}
    reg = {"auth.internal": "auth-service"}
    assert resolve_endpoint("http://auth.internal/x", reg, services)[0] == "auth-service"
    assert resolve_endpoint("http://auth.internal:8080/x", reg, services)[0] == "auth-service"
    assert resolve_endpoint("http://auth.internal.evil.com/", reg, services) == (None, None)
    assert resolve_endpoint("http://auth.internalx/", reg, services) == (None, None)


def test_longest_registry_pattern_wins():
    services = {"__self__": "me"}
    reg = {"/api": "gateway", "/api/payments": "payments"}
    assert resolve_endpoint("http://h/api/payments/1", reg, services)[0] == "payments"


# ---- 6. broken symlinks never crash a scan ----


def test_broken_symlink_does_not_crash_python_parser(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures, parse_repo

    (tmp_path / "ok.py").write_text("import auth_lib\n")
    try:
        os.symlink(tmp_path / "missing.py", tmp_path / "dangling.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    parse_repo(tmp_path, {"__self__": "me", "auth_lib": "auth-service"})
    extract_signatures(tmp_path, "me")


# ---- 7. SHA handling ----


def test_cache_dir_inside_a_git_project_does_not_report_the_projects_head(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n",
         "commit", "-q", "--allow-empty", "-m", "x"], check=True,
    )
    cache = tmp_path / ".depatlas" / "cache" / "r"
    cache.mkdir(parents=True)
    write_sha(cache, None)  # a connector that has no SHA
    assert get_sha(cache) is None
    assert get_sha(tmp_path) is not None  # a real checkout still works


def test_github_uses_full_sha_from_zip_comment(tmp_path, monkeypatch):
    from depatlas.connectors.github import fetch_repo

    full = "a" * 40
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("acme-r-aaaaaaa/x.py", "x = 1\n")
        zf.comment = full.encode()

    class R:
        status_code = 200
        content = buf.getvalue()
        text = ""

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert get_sha(fetch_repo("acme", "r", "tok", tmp_path)) == full


def test_github_zip_without_single_top_folder_does_not_inherit_old_sha(tmp_path, monkeypatch):
    from depatlas.connectors.github import fetch_repo

    write_sha(tmp_path / "r", "stale")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("one/x.py", "x")
        zf.writestr("two/y.py", "y")

    class R:
        status_code = 200
        content = buf.getvalue()
        text = ""

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert get_sha(fetch_repo("acme", "r", "tok", tmp_path)) is None


# ---- 9. languages in snapshots ----


def test_language_set_change_does_not_flood_breaking_changes():
    sig = lambda f: {"service": "s", "name": "f", "params": [], "source_file": f, "source_line": 1}
    old = {"languages": ["java", "python"], "signatures": [sig("a.py"), sig("A.java")]}
    new = {"languages": ["java"], "signatures": [sig("A.java")]}
    o, n, skipped = comparable_signatures(old, new)
    assert [s["source_file"] for s in o] == ["A.java"]
    assert skipped == ["python"]
    # unchanged language sets, and pre-upgrade snapshots, compare everything
    assert comparable_signatures(old, old)[2] == []
    assert comparable_signatures({"signatures": old["signatures"]}, new)[0] == old["signatures"]


def test_snapshot_stores_languages_and_scan_detects_them(tmp_path):
    base, db = _scan_args(tmp_path)
    r = CliRunner().invoke(cli, base)
    assert r.exit_code == 0 and "Detected language(s): python" in r.output
    s = GraphStore(db)
    assert s.get_snapshot(s.list_snapshots()[0]["id"])["languages"] == ["python"]
    s.close()


# ---- 10. test paths and test edges ----


def test_test_path_rules_are_language_specific():
    assert not is_test_path(Path("src/main/java/com/org/test/Api.java"))
    assert not is_test_path(Path("src/main/java/Contest.java"))
    assert is_test_path(Path("src/test/java/com/org/Api.java"))
    assert is_test_path(Path("src/main/java/OrderIT.java"))
    assert is_test_path(Path("pkg/conftest.py"))
    assert is_test_path(Path("pkg/tests/helpers.py"))
    assert is_test_path(Path("web/Button.spec.tsx"))
    assert not is_test_path(Path("web/inspect.ts"))


def test_test_edges_are_ignored_by_alerts():
    teams = {"a": "x", "b": "y"}
    assert check_new_cross_team_dependencies([_edge("a", "b", from_test=True).to_dict()], teams) == []
    ups = [_edge(f"d{i}", "b", from_test=True).to_dict() for i in range(12)]
    assert check_high_fan_in("b", ups) == []
    assert check_orphaned_service("b", [], ups)  # only test edges -> still orphaned


def test_scan_tags_edges_from_test_files(tmp_path):
    repo = tmp_path / "svc"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("import auth_lib\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc", "auth_lib": "auth-service"}))
    db = tmp_path / "g.db"
    CliRunner().invoke(cli, ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)])
    s = GraphStore(db)
    edges = s.edges_for_service("svc")
    s.close()
    assert edges and all(e["from_test"] == 1 for e in edges)


# ---- 11. concurrency settings ----


def test_store_uses_wal(tmp_path):
    s = GraphStore(tmp_path / "g.db")
    assert s._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    s.close()


# ---- 12. team ownership ----


def test_removing_an_owner_clears_the_team_only_when_ownership_was_supplied(tmp_path):
    from depatlas.graph.models import ServiceNode

    s = GraphStore(tmp_path / "g.db")
    node = lambda team: ServiceNode(id="p", name="p", repo="/r", language="python", team=team)
    s.upsert_services([node("core")], ownership_inputs_supplied=True)
    s.upsert_services([node(None)])  # no ownership inputs: keep
    assert s.all_services()[0]["team"] == "core"
    s.upsert_services([node(None)], ownership_inputs_supplied=True)  # owner removed
    assert s.all_services()[0]["team"] is None
    s.close()
