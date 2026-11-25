"""The exclude option: .depatlasignore, --exclude and scan-all `exclude:`."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from depatlas.check import run_check
from depatlas.cli.main import PARSERS, SIGNATURE_EXTRACTORS, cli
from depatlas.discovery import describe_service
from depatlas.fsutil import excluding, iter_files, load_excludes, path_globs
from depatlas.orgscan import load_org_config, scan_all


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _names(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in iter_files(root)}


def test_iter_files_skips_excluded_paths(tmp_path):
    for rel in ("a.py", "generated/b.py", "src/generated/c.py", "src/keep.py", "x.pb.java"):
        _write(tmp_path / rel, "x = 1\n")
    with excluding(tmp_path, ["generated/", "*.pb.java"]):
        assert _names(tmp_path) == {"a.py", "src/keep.py"}
    assert "generated/b.py" in _names(tmp_path)  # only while active


def test_anchored_vs_unanchored(tmp_path):
    for rel in ("legacy/a.py", "pkg/legacy/b.py", "keep.py"):
        _write(tmp_path / rel, "x = 1\n")
    with excluding(tmp_path, ["/legacy"]):
        assert _names(tmp_path) == {"pkg/legacy/b.py", "keep.py"}
    with excluding(tmp_path, ["legacy"]):
        assert _names(tmp_path) == {"keep.py"}


def test_depatlasignore_file_is_read(tmp_path):
    _write(tmp_path / ".depatlasignore", "# generated code\n\ngenerated/\n  \n")
    assert load_excludes(tmp_path, ["extra/"]) == ["generated/", "extra/"]
    assert load_excludes(tmp_path / "nowhere") == []


def test_path_globs_shared_with_codeowners():
    from depatlas.ownership.codeowners import _globs

    assert _globs("/docs/") == path_globs("/docs/")


def _service(tmp_path: Path) -> Path:
    repo = tmp_path / "orders"
    _write(repo / "pyproject.toml", '[project]\nname = "orders"\n')
    _write(repo / "orders" / "app.py", "import requests\n\ndef place(a):\n    return requests.get('http://payments/pay')\n")
    _write(repo / "generated" / "client.py", "import requests\n\ndef gen(a):\n    return requests.get('http://billing/x')\n")
    return repo


REGISTRY = {"registry": [{"name": "payments", "import_names": []}, {"name": "billing", "import_names": []}]}


def _scan(tmp_path: Path, repo: Path, *extra: str):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(REGISTRY))
    db = tmp_path / "g.db"
    out = tmp_path / "edges.json"
    result = CliRunner().invoke(
        cli,
        ["scan", "--repo", str(repo), "--registry", str(reg), "--db", str(db), "--output", str(out), "--language", "python", *extra],
    )
    assert result.exit_code == 0, result.output
    return {e["target_service"] for e in json.loads(out.read_text())["edges"]}


def test_scan_without_exclude_sees_both(tmp_path):
    assert _scan(tmp_path, _service(tmp_path)) == {"payments", "billing"}


def test_scan_exclude_option(tmp_path):
    assert _scan(tmp_path, _service(tmp_path), "--exclude", "generated/") == {"payments"}


def test_scan_reads_depatlasignore(tmp_path):
    repo = _service(tmp_path)
    _write(repo / ".depatlasignore", "generated/\n")
    assert _scan(tmp_path, repo) == {"payments"}


def test_excluded_files_leave_no_public_signatures(tmp_path):
    repo = _service(tmp_path)
    with excluding(repo, ["generated/"]):
        sigs = SIGNATURE_EXTRACTORS["python"](repo, "orders")
    assert {s.name for s in sigs} == {"place"}


def test_describe_service_ignores_excluded_files(tmp_path):
    repo = tmp_path / "svc"
    _write(repo / "only_generated" / "a.py", "x = 1\n")
    _write(repo / "pyproject.toml", '[project]\nname = "svc"\n')
    assert "python" in describe_service(repo).languages
    assert describe_service(repo, exclude=["only_generated/"]).languages == []


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True,
    )


def test_check_applies_exclude_to_both_sides(tmp_path):
    repo = tmp_path / "orders"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write(repo / "pyproject.toml", '[project]\nname = "orders"\n')
    _write(repo / "orders" / "app.py", "def place(a):\n    return a\n")
    _write(repo / "generated" / "g.py", "def gen(a, b):\n    return a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "change")
    _write(repo / "generated" / "g.py", "def gen(a):\n    return a\n")  # would be breaking
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({
        "nodes": [{"id": "orders", "team": "a"}, {"id": "web", "team": "b"}],
        "edges": [{"source_service": "web", "target_service": "orders", "target_symbol": "gen",
                   "source_file": "w.py", "line": 1, "kind": "import"}],
    }))
    ks = {"__self__": "orders"}
    def run(**kw):
        return run_check(repo, "main", ks, {}, ["python"], PARSERS, SIGNATURE_EXTRACTORS, graph_path=graph, **kw)
    assert any(a.type == "BREAKING_CHANGE" for a in run()["alerts"])
    assert not run(exclude=["generated/"])["alerts"]
    _write(repo / ".depatlasignore", "generated/\n")
    assert not run()["alerts"]


def test_scan_all_config_exclude_global_and_per_repo(tmp_path):
    a = _service(tmp_path)
    b = tmp_path / "billing"
    _write(b / "pyproject.toml", '[project]\nname = "billing"\n')
    _write(b / "billing" / "m.py", "x = 1\n")
    _write(b / "legacy" / "old.py", "import requests\nrequests.get('http://orders/x')\n")
    cfg = tmp_path / "org.yaml"
    cfg.write_text(
        "exclude: [generated/]\nrepos:\n  - path: orders\n  - path: billing\n    exclude: [legacy/]\n"
    )
    repos, settings = load_org_config(cfg)
    assert settings["exclude"] == ("generated/",)
    assert [r.exclude for r in repos] == [(), ("legacy/",)]
    report = scan_all(cfg, tmp_path / "o.db", PARSERS, SIGNATURE_EXTRACTORS)
    assert not report.failures
    from depatlas.graph.store import GraphStore

    pairs = {(e["source_service"], e["target_service"]) for e in GraphStore(tmp_path / "o.db").all_edges()}
    assert ("billing", "orders") not in pairs  # legacy/ excluded for billing


def test_scan_all_rejects_bad_exclude(tmp_path):
    (tmp_path / "s").mkdir()
    cfg = tmp_path / "org.yaml"
    cfg.write_text("exclude: 5\nrepos:\n  - path: s\n")
    with pytest.raises(ValueError, match="exclude"):
        load_org_config(cfg)
