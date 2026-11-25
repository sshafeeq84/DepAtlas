"""Found by the first real run of the nightly workflow (scanning DepAtlas itself)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from depatlas.cli.main import PARSERS, SIGNATURE_EXTRACTORS
from depatlas.fsutil import is_test_path, iter_files
from depatlas.graph.store import GraphStore
from depatlas.orgscan import scan_all

FEIGN = """\
package com.org.payments;

@FeignClient(name = "notification-service")
interface NotificationFeignClient {
    void notify(String message);
}
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_depatlas_own_folder_is_never_scanned(tmp_path):
    """A repo that keeps `.depatlas/cache/...` (a downloaded copy of itself, or
    of other repos) must not have that copy scanned as its own code."""
    _write(tmp_path / "app.py", "x = 1\n")
    _write(tmp_path / ".depatlas" / "cache" / "o__r" / "R" / "other.py", "y = 2\n")
    assert [p.name for p in iter_files(tmp_path, "*.py")] == ["app.py"]


@pytest.mark.parametrize(
    "path, expected",
    [
        # a sample service kept under tests/ (has its own src/main/)
        ("tests/fixtures/sample_repo_java/payments/src/main/java/com/org/Pay.java", True),
        ("fixtures/pay/src/main/java/com/org/Pay.java", True),
        ("testdata/pay/Pay.java", True),
        # production code is untouched, even in a package named like a test folder
        ("src/main/java/com/org/Pay.java", False),
        ("src/main/java/com/org/tests/Pay.java", False),
        ("src/main/java/com/org/fixtures/Pay.java", False),
        ("src/main/java/com/org/test/Pay.java", False),
        ("src/test/java/com/org/PayTest.java", True),
        ("services/pay/src/main/java/com/org/Pay.java", False),
        # python / js
        ("tests/fixtures/x.py", True),
        ("pkg/fixtures/x.py", True),
        ("pkg/app.py", False),
        ("test/a.js", True),
        ("src/__mocks__/a.js", True),
        ("fixtures/a.js", True),
        ("src/a.js", False),
        ("src/a.spec.js", True),
    ],
)
def test_test_and_fixture_folders(path, expected):
    assert is_test_path(Path(path)) is expected


def test_fixture_service_edges_are_marked_as_test_edges(tmp_path):
    """DepAtlas scanned itself and reported a 'production' dependency from a
    sample Java service under tests/fixtures/."""
    repo = tmp_path / "repo"
    _write(repo / "pyproject.toml", '[project]\nname = "toolkit"\n')
    _write(repo / "toolkit" / "__init__.py", "")
    _write(
        repo / "tests" / "fixtures" / "sample" / "payments" / "src" / "main" / "java" / "com" / "org" / "Pay.java",
        FEIGN,
    )
    _write(repo / ".depatlas" / "cache" / "o__r" / "R" / "src" / "main" / "java" / "com" / "org" / "Pay.java", FEIGN)
    config = tmp_path / "org.yml"
    config.write_text(yaml.safe_dump({"repos": [{"path": str(repo)}]}))
    db = tmp_path / "g.db"
    report = scan_all(config, db, PARSERS, SIGNATURE_EXTRACTORS, cache_dir=tmp_path / "cache")
    assert not report.failures
    store = GraphStore(db)
    try:
        edges = store.all_edges()
    finally:
        store.close()
    files = {e["source_file"]: e["from_test"] for e in edges}
    assert files, "the fixture's dependency is still recorded"
    assert not any(f.startswith(".depatlas") for f in files), files
    assert all(flag == 1 for flag in files.values()), files
