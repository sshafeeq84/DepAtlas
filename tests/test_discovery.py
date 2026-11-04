"""Service discovery: names, import names, the registry and the manifest built from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from depatlas.discovery import (
    build_manifest,
    describe_service,
    discover_workspace,
    find_codeowners,
    infer_service_name,
    load_registry,
    resolve_manifest,
)


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- names ----


def test_spring_application_name_wins_over_the_pom(tmp_path):
    _write(tmp_path / "pom.xml", "<project><artifactId>spring-pay-svc</artifactId></project>")
    _write(tmp_path / "src/main/resources/application.yml", "spring:\n  application:\n    name: payments-service\n")
    assert infer_service_name(tmp_path) == ("payments-service", "spring.application.name")


def test_spring_name_in_properties_and_multi_document_yaml(tmp_path):
    a = tmp_path / "a"
    _write(a / "src/main/resources/application.properties", "spring.application.name = orders\n")
    assert infer_service_name(a)[0] == "orders"
    b = tmp_path / "b"
    _write(b / "src/main/resources/application.yml", "server:\n  port: 1\n---\nspring:\n  application:\n    name: billing\n")
    assert infer_service_name(b)[0] == "billing"


def test_placeholder_names_are_ignored_in_favour_of_the_pom(tmp_path):
    _write(tmp_path / "pom.xml", '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>real-name</artifactId></project>')
    _write(tmp_path / "src/main/resources/application.yml", "spring:\n  application:\n    name: ${APP_NAME:foo}\n")
    assert infer_service_name(tmp_path) == ("real-name", "pom.xml")


def test_gradle_python_and_npm_names(tmp_path):
    _write(tmp_path / "g/settings.gradle", "rootProject.name = 'ledger'\n")
    assert infer_service_name(tmp_path / "g") == ("ledger", "settings.gradle")

    _write(tmp_path / "p1/pyproject.toml", '[project]\nname = "pay-api"\n')
    assert infer_service_name(tmp_path / "p1")[0] == "pay-api"
    _write(tmp_path / "p2/pyproject.toml", '[tool.poetry]\nname = "poetry-svc"\n')
    assert infer_service_name(tmp_path / "p2")[0] == "poetry-svc"
    _write(tmp_path / "p3/setup.py", 'setup(name="legacy-svc", version="1")\n')
    assert infer_service_name(tmp_path / "p3")[0] == "legacy-svc"

    _write(tmp_path / "n/package.json", json.dumps({"name": "@acme/checkout"}))
    assert infer_service_name(tmp_path / "n") == ("checkout", "package.json")


def test_folder_name_is_the_last_resort(tmp_path):
    svc = tmp_path / "mystery"
    svc.mkdir()
    assert infer_service_name(svc) == ("mystery", "folder name")


# ---- import names ----


def test_python_packages_become_import_names_but_generic_and_stdlib_do_not(tmp_path):
    _write(tmp_path / "pyproject.toml", '[project]\nname = "auth-svc"\n')
    for pkg in ("auth_lib", "utils", "json", "tests"):
        _write(tmp_path / "src" / pkg / "__init__.py")
    svc = describe_service(tmp_path)
    assert svc.import_names == ["auth_lib"]


def test_python_without_packages_falls_back_to_the_project_name(tmp_path):
    _write(tmp_path / "pyproject.toml", '[project]\nname = "pay-client"\n')
    _write(tmp_path / "scripts_dir" / "run.py", "x = 1\n")  # no package, no top-level module
    assert describe_service(tmp_path).import_names == ["pay_client"]


def test_java_package_prefix_is_the_common_prefix(tmp_path):
    for sub in ("model", "web"):
        _write(tmp_path / f"src/main/java/com/acme/pay/{sub}/A.java", f"package com.acme.pay.{sub};\nclass A {{}}\n")
    assert describe_service(tmp_path).import_names == ["com.acme.pay"]


def test_too_short_a_java_prefix_is_not_registered(tmp_path):
    _write(tmp_path / "src/main/java/org/x/A.java", "package org.x;\nclass A {}\n")
    assert describe_service(tmp_path).import_names == []


def test_npm_package_name_is_the_import_name(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"name": "@acme/checkout"}))
    _write(tmp_path / "index.js", "export const x = 1;\n")
    assert describe_service(tmp_path).import_names == ["@acme/checkout"]


# ---- workspace walk ----


def test_workspace_finds_services_and_skips_aggregators(tmp_path):
    _write(tmp_path / "pom.xml", "<project><artifactId>parent</artifactId><modules><module>a</module></modules></project>")
    _write(tmp_path / "a/pom.xml", "<project><artifactId>svc-a</artifactId></project>")
    _write(tmp_path / "a/src/main/java/com/acme/a/X.java", "package com.acme.a;\nclass X {}\n")
    _write(tmp_path / "web/package.json", json.dumps({"name": "web-ui"}))
    _write(tmp_path / "web/app.js", "export const a = 1;\n")
    _write(tmp_path / "mono/package.json", json.dumps({"name": "root", "workspaces": ["p/*"]}))
    _write(tmp_path / "node_modules/dep/package.json", json.dumps({"name": "dep"}))
    _write(tmp_path / "node_modules/dep/i.js", "x\n")

    found = {s.name: s for s in discover_workspace(tmp_path)}

    assert set(found) == {"svc-a", "web-ui"}
    assert found["svc-a"].path == "a" and found["svc-a"].languages == ["java"]


# ---- registry -> manifest ----

REGISTRY = [
    {"name": "pay-service", "import_names": ["pay_lib"]},
    {"name": "auth-service", "import_names": ["auth_lib"]},
    {"name": "web", "import_names": ["shared_x"]},
    {"name": "mail-service", "import_names": ["shared_x"]},
]


def test_manifest_maps_other_services_and_excludes_self():
    services, endpoints, warnings = build_manifest("pay-service", REGISTRY)
    assert services["__self__"] == "pay-service"
    assert services["auth_lib"] == "auth-service"
    assert "pay_lib" not in services and "pay-service" not in endpoints.values()
    assert endpoints["://auth-service"] == "auth-service"


def test_an_import_name_claimed_by_two_services_is_dropped_with_a_warning():
    services, _, warnings = build_manifest("pay-service", REGISTRY)
    assert "shared_x" not in services
    assert any("shared_x" in w for w in warnings)


def test_host_pattern_matches_urls_but_not_path_segments():
    from depatlas.resolution import resolve_endpoint

    services, endpoints, _ = build_manifest("pay-service", REGISTRY)
    assert resolve_endpoint("http://auth-service:8080/x", endpoints, services)[0] == "auth-service"
    assert resolve_endpoint("lb://auth-service/x", endpoints, services)[0] == "auth-service"
    assert resolve_endpoint("https://example.com/auth-service/x", endpoints, services) == (None, None)


def test_explicit_services_file_alone_behaves_exactly_as_before(tmp_path):
    f = _write(tmp_path / "s.json", json.dumps({"__self__": "me", "lib": "lib-service", "endpoints": {"a.b": "x"}}))
    services, endpoints = resolve_manifest(tmp_path, f, None, None)
    assert services == {"__self__": "me", "lib": "lib-service"} and endpoints == {"a.b": "x"}


def test_explicit_file_overrides_the_registry(tmp_path):
    reg = _write(tmp_path / "r.json", json.dumps({"services": REGISTRY}))
    f = _write(tmp_path / "s.json", json.dumps({"auth_lib": "renamed-auth"}))
    services, _ = resolve_manifest(tmp_path, f, reg, "pay-service")
    assert services["auth_lib"] == "renamed-auth" and services["__self__"] == "pay-service"


def test_registry_is_read_from_a_graph_export_and_bad_files_are_explained(tmp_path):
    graph = _write(tmp_path / "g.json", json.dumps({"nodes": [], "edges": [], "registry": REGISTRY}))
    assert len(load_registry(graph)) == 4
    with pytest.raises(ValueError, match="no service registry"):
        load_registry(_write(tmp_path / "x.json", json.dumps({"nodes": [], "edges": []})))


def test_codeowners_is_found_in_the_standard_places(tmp_path):
    assert find_codeowners(tmp_path) is None
    _write(tmp_path / ".github/CODEOWNERS", "* @team\n")
    assert find_codeowners(tmp_path) == tmp_path / ".github/CODEOWNERS"
