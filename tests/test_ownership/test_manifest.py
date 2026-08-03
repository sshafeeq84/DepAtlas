from pathlib import Path

from depgraph.ownership.manifest import parse_ownership_manifest


def _write_manifest(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "depgraph.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_parses_name_to_team_mapping(tmp_path):
    path = _write_manifest(
        tmp_path,
        """
        version: 1
        services:
          - name: payments-service
            repo: org/payments
            team: payments-team
          - name: auth-service
            repo: org/auth
            team: platform-team
        """,
    )

    mapping = parse_ownership_manifest(path)

    assert mapping == {
        "payments-service": "payments-team",
        "auth-service": "platform-team",
    }


def test_entry_missing_team_is_skipped(tmp_path):
    path = _write_manifest(
        tmp_path,
        """
        version: 1
        services:
          - name: payments-service
            repo: org/payments
        """,
    )

    mapping = parse_ownership_manifest(path)

    assert mapping == {}


def test_empty_manifest_returns_empty_mapping(tmp_path):
    path = _write_manifest(tmp_path, "version: 1\n")

    mapping = parse_ownership_manifest(path)

    assert mapping == {}
