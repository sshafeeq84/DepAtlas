from pathlib import Path

from depatlas.parsers.js_ts_parser import parse_repo

FIXTURE_REPO = Path(__file__).parent.parent / "fixtures" / "sample_repo_js" / "payments"

KNOWN_SERVICES = {
    "__self__": "payments-service",
    "auth_client": "auth-service",
    "shipping-lib": "shipping-library-service",
    # Not actually imported anywhere -- exists only so the URL-heuristic
    # fallback has a service name to match against.
    "shipping_client": "shipping-service",
}

KNOWN_ENDPOINTS = {
    "auth.internal": "auth-service",
}


def _scan():
    return parse_repo(FIXTURE_REPO, KNOWN_SERVICES, KNOWN_ENDPOINTS)


def test_finds_import_and_require():
    edges = _scan()
    import_edges = [e for e in edges if e.type == "import"]

    # `import authClient from "auth_client"` and
    # `require("auth_client/session")` both resolve to auth-service.
    assert len(import_edges) == 2
    assert all(e.source_service == "payments-service" for e in import_edges)
    assert all(e.target_service == "auth-service" for e in import_edges)
    assert all(e.confidence == "high" for e in import_edges)


def test_fetch_call_via_endpoint_registry():
    edges = _scan()
    http_edges = [e for e in edges if e.type == "http_call" and e.target_service == "auth-service"]

    assert len(http_edges) == 1
    assert http_edges[0].confidence == "medium"


def test_axios_call_via_heuristic_fallback():
    edges = _scan()
    http_edges = [
        e for e in edges if e.type == "http_call" and e.target_service == "shipping-service"
    ]

    assert len(http_edges) == 1
    assert http_edges[0].confidence == "low"


def test_package_json_dependency_is_shared_library_high_confidence():
    edges = _scan()
    lib_edges = [e for e in edges if e.type == "shared_library"]

    assert len(lib_edges) == 1
    assert lib_edges[0].target_service == "shipping-library-service"
    assert lib_edges[0].confidence == "high"


def test_unregistered_package_json_dependency_is_ignored():
    edges = _scan()
    targets = {e.target_service for e in edges}
    # "lodash" and "jest" aren't in KNOWN_SERVICES -- must not show up.
    assert "lodash" not in targets
    assert "jest" not in targets


def test_total_edge_count():
    # 2 imports + 1 fetch + 1 axios + 1 package.json dep = 5
    # (typed.ts is present in the fixture repo too, but contains genuine
    # TypeScript syntax esprima can't parse -- it must be skipped rather
    # than crashing the scan or contributing any edges.)
    edges = _scan()
    assert len(edges) == 5


def test_typescript_only_syntax_is_skipped_not_crashed(capsys):
    from depatlas.parsers.js_ts_parser import parse_file

    typed_file = FIXTURE_REPO / "src" / "typed.ts"
    edges = parse_file(typed_file, KNOWN_SERVICES, KNOWN_ENDPOINTS)

    assert edges == []
    captured = capsys.readouterr()
    assert "skipping" in captured.err
    assert "typed.ts" in captured.err


# --- extract_signatures ---


def test_extract_signatures_finds_exported_functions_only(tmp_path):
    from depatlas.parsers.js_ts_parser import extract_signatures

    (tmp_path / "service.js").write_text(
        "export function verifyToken(token, options) {\n"
        "  return true;\n"
        "}\n"
        "\n"
        "function notExported(x) {\n"
        "  return x;\n"
        "}\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert len(signatures) == 1
    assert signatures[0].name == "verifyToken"
    assert signatures[0].params == [
        {"name": "token", "type": None},
        {"name": "options", "type": None},
    ]


def test_extract_signatures_finds_export_default_function(tmp_path):
    from depatlas.parsers.js_ts_parser import extract_signatures

    (tmp_path / "handler.js").write_text(
        "export default function mainHandler(req, res) {\n" "  return res;\n" "}\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert len(signatures) == 1
    assert signatures[0].name == "mainHandler"
    assert signatures[0].params == [
        {"name": "req", "type": None},
        {"name": "res", "type": None},
    ]
