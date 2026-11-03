from depatlas.fsutil import iter_files
from depatlas.parsers.python_parser import extract_signatures, parse_repo


def test_iter_files_skips_vendored_directories(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    for skipped in ("node_modules", ".venv", ".git", "build"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "junk.py").write_text("x = 1\n")

    assert [p.name for p in iter_files(tmp_path, "*.py")] == ["app.py"]


def test_one_unparseable_python_file_does_not_abort_the_scan(tmp_path, capsys):
    (tmp_path / "good.py").write_text("import auth_client\n")
    (tmp_path / "py2.py").write_text("print 'hello'\n")  # Python 2 syntax
    (tmp_path / "bad_bytes.py").write_bytes(b"\xff\xfe\x00bad")

    edges = parse_repo(tmp_path, {"auth_client": "auth-service", "__self__": "me"})
    sigs = extract_signatures(tmp_path, "me")

    assert len(edges) == 1
    assert isinstance(sigs, list)
    assert "skipping" in capsys.readouterr().err
