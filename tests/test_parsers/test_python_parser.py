from pathlib import Path

from depgraph.parsers.python_parser import parse_repo

FIXTURE_REPO = Path(__file__).parent.parent / "fixtures" / "sample_repo" / "payments"

KNOWN_SERVICES = {
    "__self__": "payments-service",
    "auth_client": "auth-service",
}


def test_finds_known_service_imports():
    edges = parse_repo(FIXTURE_REPO, KNOWN_SERVICES)

    assert len(edges) == 2
    assert all(e.source_service == "payments-service" for e in edges)
    assert all(e.target_service == "auth-service" for e in edges)
    assert all(e.confidence == "high" for e in edges)


def test_ignores_stdlib_and_unknown_imports():
    edges = parse_repo(FIXTURE_REPO, KNOWN_SERVICES)

    # charge.py also imports stdlib `json`, which must NOT show up as an edge
    modules_targeted = {e.target_service for e in edges}
    assert "json" not in modules_targeted


def test_line_numbers_are_captured():
    edges = parse_repo(FIXTURE_REPO, KNOWN_SERVICES)

    lines = sorted(e.source_line for e in edges)
    assert lines == [3, 4]  # `import auth_client` and `from auth_client...`
