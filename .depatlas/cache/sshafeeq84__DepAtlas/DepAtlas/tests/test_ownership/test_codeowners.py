from pathlib import Path

from depatlas.ownership.codeowners import parse_codeowners, resolve_owner


def _write_codeowners(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "CODEOWNERS"
    p.write_text(content, encoding="utf-8")
    return p


def test_parses_basic_rules(tmp_path):
    path = _write_codeowners(
        tmp_path,
        """
        # comment line, should be skipped
        *.py @payments-team

        /tests/ @qa-team
        """,
    )

    rules = parse_codeowners(path)
    patterns = [r.pattern for r in rules]

    assert "*.py" in patterns
    assert "/tests/" in patterns
    assert len(rules) == 2  # comment and blank lines excluded


def test_resolves_owner_for_matching_file(tmp_path):
    (tmp_path / "charge.py").write_text("# fake")
    _write_codeowners(tmp_path, "*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    owner = resolve_owner(str(tmp_path / "charge.py"), tmp_path, rules)
    assert owner == "@payments-team"


def test_unmatched_file_has_no_owner(tmp_path):
    (tmp_path / "notes.txt").write_text("# fake")
    _write_codeowners(tmp_path, "*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    owner = resolve_owner(str(tmp_path / "notes.txt"), tmp_path, rules)
    assert owner is None


def test_last_matching_rule_wins(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("# fake")
    _write_codeowners(
        tmp_path,
        "*.py @payments-team\n/tests/ @qa-team\n",
    )
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    # Both rules match this file -- *.py matches, and /tests/ matches too.
    # /tests/ comes later in the file, so it should win.
    owner = resolve_owner(str(tmp_path / "tests" / "test_x.py"), tmp_path, rules)
    assert owner == "@qa-team"
