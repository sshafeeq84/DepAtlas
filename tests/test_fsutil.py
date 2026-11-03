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


def test_modern_java_syntax_is_parsed_not_skipped(tmp_path):
    """Regression (found on Spring PetClinic): records, anonymous generic
    classes and `var` used to make the whole file unparseable, hiding the
    service-to-service call inside it."""
    from depatlas.parsers.java_parser import parse_repo as java_parse

    (tmp_path / "Rec.java").write_text("package x;\npublic record Point(int x, int y) {}\n")
    (tmp_path / "Loader.java").write_text(
        """package x;
import com.org.auth.Client;
class Loader {
    void load() {
        var vetsHostname = "http://vets-service/";
        var vets = webClient.get().uri(vetsHostname + "vets").retrieve()
            .bodyToMono(new ParameterizedTypeReference<List<Vet>>() {}).block();
    }
}
"""
    )

    edges = java_parse(
        tmp_path,
        {"__self__": "genai", "com.org.auth": "auth-service"},
        {"vets-service": "vets-service"},
    )

    assert sorted(e.target_service for e in edges) == ["auth-service", "vets-service"]


def test_garbage_java_file_does_not_abort_the_scan(tmp_path, capsys):
    from depatlas.parsers.java_parser import extract_signatures as java_sigs
    from depatlas.parsers.java_parser import parse_repo as java_parse

    (tmp_path / "Good.java").write_text("package x;\nimport com.org.auth.Client;\nclass Good {}\n")
    (tmp_path / "Junk.java").write_text("class {{{ ??? ;;;\n")
    (tmp_path / "Bytes.java").write_bytes(b"\xff\xfe\x00bad")

    edges = java_parse(tmp_path, {"__self__": "me", "com.org.auth": "auth-service"})
    sigs = java_sigs(tmp_path, "me")

    assert len(edges) == 1
    assert isinstance(sigs, list)
    assert "warning" in capsys.readouterr().err


def test_interface_methods_count_as_public_signatures(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures as java_sigs

    (tmp_path / "Api.java").write_text(
        "package x;\ninterface Api { String find(long id, java.util.List<String> tags); }\n"
    )
    sigs = java_sigs(tmp_path, "svc")

    assert [s.name for s in sigs] == ["find"]
    assert sigs[0].params == [{"name": "id", "type": "long"}, {"name": "tags", "type": "List"}]
