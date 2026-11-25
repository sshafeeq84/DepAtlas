"""Found by the noise audit: scanning 41 real projects (PyPI sdists, npm packages)
and looking at where each recorded dependency and public function came from."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from depatlas.cli.main import PARSERS, SIGNATURE_EXTRACTORS
from depatlas.discovery import describe_service
from depatlas.fsutil import is_test_path, iter_files
from depatlas.graph.store import GraphStore
from depatlas.orgscan import scan_all


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---- docs / examples / scripts / benchmarks are not the service ----


@pytest.mark.parametrize(
    "path, expected",
    [
        ("docs_src/tutorial001.py", True),
        ("examples/app/main.py", True),
        ("example/a.js", True),
        ("scripts/deploy.py", True),
        ("benchmarks/bench_json.py", True),
        ("doc/en/conf.py", True),
        ("docs/conf.py", True),
        ("Examples/App.java", True),  # case-insensitive
        ("examples/demo/src/main/java/com/acme/Demo.java", True),
        # only the TOP-LEVEL folder counts: a package may legitimately contain these names
        ("mypkg/scripts/cli.py", False),
        ("src/examples/helpers.py", False),
        ("src/main/java/com/acme/docs/Api.java", False),
        ("app/api.py", False),
        ("examples.py", False),  # a module, not a folder
    ],
)
def test_auxiliary_top_level_folders(path, expected):
    assert is_test_path(Path(path)) is expected


def test_public_signatures_skip_docs_and_examples(tmp_path):
    _write(tmp_path / "app" / "api.py", "def charge(amount):\n    return amount\n")
    _write(tmp_path / "docs_src" / "tutorial.py", "def charge_demo(amount):\n    return amount\n")
    _write(tmp_path / "scripts" / "release.py", "def publish(version):\n    return version\n")
    names = {s.name for s in SIGNATURE_EXTRACTORS["python"](tmp_path, "svc")}
    assert names == {"charge"}


# ---- package.json devDependencies ----


def test_dev_dependencies_are_recorded_but_never_alert(tmp_path):
    def service(name: str, deps=None, dev=None):
        root = tmp_path / name
        pkg = {"name": name, "version": "1.0.0"}
        if deps:
            pkg["dependencies"] = deps
        if dev:
            pkg["devDependencies"] = dev
        _write(root / "package.json", json.dumps(pkg))
        _write(root / "index.js", "module.exports = {};\n")
        return {"path": str(root)}

    repos = [service("lib-a"), service("lib-b"),
             service("app", deps={"lib-a": "1"}, dev={"lib-b": "1"})]
    config = tmp_path / "org.yml"
    config.write_text(yaml.safe_dump({"repos": repos}))
    db = tmp_path / "g.db"
    report = scan_all(config, db, PARSERS, SIGNATURE_EXTRACTORS, cache_dir=tmp_path / "cache")
    assert not report.failures
    store = GraphStore(db)
    try:
        flags = {e["target_service"]: e["from_test"] for e in store.all_edges() if e["source_service"] == "app"}
    finally:
        store.close()
    assert flags == {"lib-a": 0, "lib-b": 1}


# ---- import names ----


def test_importable_test_and_example_folders_are_not_registered(tmp_path):
    """celery and kombu ship their tests as a package called `t`."""
    for pkg in ("mypkg", "t", "examples", "benchmarks", "fixtures"):
        _write(tmp_path / pkg / "__init__.py", "")
    assert describe_service(tmp_path, name="svc").import_names == ["mypkg"]


def test_lib_layout_is_recognised(tmp_path):
    """SQLAlchemy keeps its package under lib/, so `import sqlalchemy` in
    another service (alembic) was never linked to it."""
    _write(tmp_path / "lib" / "sqlalchemy" / "__init__.py", "")
    _write(tmp_path / "examples" / "__init__.py", "")
    assert describe_service(tmp_path, name="SQLAlchemy").import_names == ["sqlalchemy"]


# ---- warnings ----


def test_large_data_files_are_skipped_without_a_warning(tmp_path, capsys):
    (tmp_path / "data.csv.bz2").write_bytes(b"0" * 2_100_000)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert [p.name for p in iter_files(tmp_path, "*")] == ["a.py"]
    assert capsys.readouterr().err == ""
