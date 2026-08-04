from pathlib import Path

from depgraph.parsers.python_parser import parse_repo

FIXTURE_REPO = Path(__file__).parent.parent / "fixtures" / "sample_repo" / "payments"

KNOWN_SERVICES = {
    "__self__": "payments-service",
    "auth_client": "auth-service",
    # Not actually imported anywhere -- exists only so the URL-heuristic
    # fallback below has a service name to match against.
    "shipping_client": "shipping-service",
}

KNOWN_ENDPOINTS = {
    "auth.internal": "auth-service",
}


def _scan():
    return parse_repo(FIXTURE_REPO, KNOWN_SERVICES, KNOWN_ENDPOINTS)


def test_finds_known_service_imports():
    edges = _scan()
    import_edges = [e for e in edges if e.type == "import"]

    assert len(import_edges) == 2
    assert all(e.source_service == "payments-service" for e in import_edges)
    assert all(e.target_service == "auth-service" for e in import_edges)
    assert all(e.confidence == "high" for e in import_edges)


def test_ignores_stdlib_and_unknown_imports():
    edges = _scan()

    # charge.py also imports stdlib `json`, which must NOT show up as an edge
    targets = {e.target_service for e in edges}
    assert "json" not in targets


def test_import_line_numbers_are_captured():
    edges = _scan()
    import_lines = sorted(e.source_line for e in edges if e.type == "import")

    assert import_lines == [3, 4]  # `import auth_client` and `from auth_client...`


def test_resolves_http_call_via_endpoint_registry():
    edges = _scan()
    http_edges = [e for e in edges if e.type == "http_call"]

    auth_call = next(e for e in http_edges if e.target_service == "auth-service")
    assert auth_call.confidence == "medium"
    assert auth_call.source_service == "payments-service"


def test_resolves_http_call_via_heuristic_fallback():
    edges = _scan()
    http_edges = [e for e in edges if e.type == "http_call"]

    shipping_call = next(e for e in http_edges if e.target_service == "shipping-service")
    assert shipping_call.confidence == "low"


def test_http_call_with_no_matching_service_is_skipped():
    edges = _scan()
    # There are exactly 2 requests calls in the fixture, and both resolve
    # (one via registry, one via heuristic) -- so we should see exactly 2
    # http_call edges, not more.
    http_edges = [e for e in edges if e.type == "http_call"]
    assert len(http_edges) == 2


# --- extract_signatures ---


def test_extract_signatures_finds_module_level_and_method_signatures(tmp_path):
    from depgraph.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text(
        "def public_func(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Handler:\n"
        "    def public_method(self, req):\n"
        "        return req\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")
    names = {s.name for s in signatures}

    assert names == {"public_func", "public_method"}
    public_func = next(s for s in signatures if s.name == "public_func")
    assert public_func.params == [{"name": "a", "type": None}, {"name": "b", "type": None}]
    assert public_func.service == "payments-service"


def test_extract_signatures_excludes_underscore_prefixed_functions(tmp_path):
    from depgraph.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text("def _private_func(x):\n    return x\n")

    signatures = extract_signatures(tmp_path, "payments-service")

    assert signatures == []


def test_extract_signatures_excludes_nested_functions(tmp_path):
    from depgraph.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text(
        "def outer():\n" "    def inner_helper():\n" "        return 1\n" "    return inner_helper()\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")
    names = {s.name for s in signatures}

    assert names == {"outer"}


def test_extract_signatures_captures_type_annotations(tmp_path):
    from depgraph.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text(
        "def create_charge(amount: int, currency: str) -> bool:\n" "    return True\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert signatures[0].params == [
        {"name": "amount", "type": "int"},
        {"name": "currency", "type": "str"},
    ]


def test_extract_signatures_untyped_params_get_none_type(tmp_path):
    from depgraph.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text("def create_charge(amount):\n" "    return True\n")

    signatures = extract_signatures(tmp_path, "payments-service")

    assert signatures[0].params == [{"name": "amount", "type": None}]
