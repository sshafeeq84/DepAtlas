from pathlib import Path

from depgraph.parsers.java_parser import parse_repo

FIXTURE_REPO = Path(__file__).parent.parent / "fixtures" / "sample_repo_java" / "payments"

KNOWN_SERVICES = {
    "__self__": "payments-service",
    "com.org.auth": "auth-service",
    # Not actually imported anywhere -- exists only so the URL-heuristic
    # fallback has a service name to match against.
    "com.org.shipping": "shipping-service",
}

KNOWN_ENDPOINTS = {
    "auth.internal": "auth-service",
}


def _scan():
    return parse_repo(FIXTURE_REPO, KNOWN_SERVICES, KNOWN_ENDPOINTS)


def test_finds_known_package_imports():
    edges = _scan()
    import_edges = [e for e in edges if e.type == "import"]

    # Both com.org.auth.AuthClient and com.org.auth.session.SessionVerifier
    # resolve against the "com.org.auth" prefix.
    assert len(import_edges) == 2
    assert all(e.source_service == "payments-service" for e in import_edges)
    assert all(e.target_service == "auth-service" for e in import_edges)
    assert all(e.confidence == "high" for e in import_edges)


def test_ignores_unregistered_imports():
    edges = _scan()
    targets = {e.target_service for e in edges}
    # java.util.List and org.springframework.web.client.RestTemplate
    # aren't in the known_services map -- must not show up.
    assert "java.util" not in targets
    assert "org.springframework.web.client" not in targets


def test_feign_client_annotation_is_high_confidence():
    edges = _scan()
    feign_edges = [
        e for e in edges if e.target_service == "notification-service"
    ]

    assert len(feign_edges) == 1
    assert feign_edges[0].type == "http_call"
    assert feign_edges[0].confidence == "high"
    assert feign_edges[0].source_service == "payments-service"


def test_resttemplate_call_via_endpoint_registry():
    edges = _scan()
    auth_http_edges = [
        e for e in edges if e.type == "http_call" and e.target_service == "auth-service"
    ]

    assert len(auth_http_edges) == 1
    assert auth_http_edges[0].confidence == "medium"


def test_resttemplate_call_via_heuristic_fallback():
    edges = _scan()
    shipping_edges = [
        e for e in edges if e.type == "http_call" and e.target_service == "shipping-service"
    ]

    assert len(shipping_edges) == 1
    assert shipping_edges[0].confidence == "low"


def test_total_edge_count():
    # 2 imports + 1 FeignClient + 2 RestTemplate calls = 5
    edges = _scan()
    assert len(edges) == 5
