"""Round-4 review findings against the 0.4.0 inference code."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from depatlas.discovery import (
    build_manifest,
    infer_service_name,
    resolve_manifest,
)
from depatlas.parsers.python_parser import parse_repo as parse_python


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---- Spring naming ----


def test_profile_file_does_not_override_the_base_name(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    _write(res / "application.yml", "spring:\n  application:\n    name: orders\n")
    _write(res / "application-test.yml", "spring:\n  application:\n    name: orders-test\n")
    _write(res / "application-dev.properties", "spring.application.name=orders-dev\n")
    assert infer_service_name(tmp_path) == ("orders", "spring.application.name")


def test_profile_file_is_used_only_when_nothing_else_names_the_service(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    _write(res / "application-prod.yml", "spring:\n  application:\n    name: orders-prod\n")
    assert infer_service_name(tmp_path)[0] == "orders-prod"


def test_flat_dotted_yaml_key(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    _write(res / "application.yml", "spring.application.name: billing\nserver.port: 8080\n")
    assert infer_service_name(tmp_path) == ("billing", "spring.application.name")


def test_profile_activated_document_is_skipped(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    _write(
        res / "application.yml",
        """\
        spring:
          config:
            activate:
              on-profile: cloud
          application:
            name: orders-cloud
        ---
        spring:
          application:
            name: orders
        """,
    )
    assert infer_service_name(tmp_path)[0] == "orders"


def test_bootstrap_beats_application(tmp_path):
    res = tmp_path / "src" / "main" / "resources"
    _write(res / "application.properties", "spring.application.name=from-app\n")
    _write(res / "bootstrap.properties", "spring.application.name=from-bootstrap\n")
    assert infer_service_name(tmp_path)[0] == "from-bootstrap"


# ---- setup.cfg / setup.py ----


def test_setup_cfg_name_must_come_from_the_metadata_section(tmp_path):
    _write(tmp_path / "setup.cfg", "[metadata]\nversion = 1\n\n[options.entry_points]\nname = oops\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    assert infer_service_name(tmp_path) == (tmp_path.name, "folder name")


def test_setup_cfg_name_is_read(tmp_path):
    _write(tmp_path / "setup.cfg", "[metadata]\nname = billing-api\nversion = 1\n")
    assert infer_service_name(tmp_path)[0] == "billing-api"


# ---- a service named like a stdlib / generic module ----


def test_service_named_like_a_stdlib_module_is_not_an_import_target(tmp_path):
    registry = [
        {"name": "email", "import_names": []},
        {"name": "config", "import_names": []},
        {"name": "payments", "import_names": ["payments"]},
    ]
    known_services, known_endpoints, _ = build_manifest("me", registry)
    _write(tmp_path / "me" / "app.py", "import email\nfrom config import X\nimport payments\n")
    targets = {e.target_service for e in parse_python(tmp_path, known_services, known_endpoints)}
    assert targets == {"payments"}


def test_such_a_service_is_still_reachable_by_url_and_hostname(tmp_path):
    registry = [{"name": "email", "import_names": []}]
    known_services, known_endpoints, _ = build_manifest("me", registry)
    _write(
        tmp_path / "me" / "app.py",
        'import requests\nrequests.get("http://email/send")\nrequests.get("https://email.internal.corp/x")\n',
    )
    edges = parse_python(tmp_path, known_services, known_endpoints)
    assert {e.target_service for e in edges} == {"email"}
    assert len(edges) == 2  # the explicit ://email entry and the hostname guess


# ---- the service's own name in CI ----


def test_warns_when_this_service_is_not_in_the_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    repo = tmp_path / "orders"
    _write(repo / "pyproject.toml", '[project]\nname = "orders"\n')
    registry = tmp_path / "reg.json"
    registry.write_text(json.dumps({"services": [{"name": "orders-service", "import_names": []}]}))
    messages: list[str] = []
    resolve_manifest(repo, None, registry, None, messages.append)
    assert any("not in the registry" in m and "--name" in m for m in messages)


def test_no_warning_when_the_name_matches(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    repo = tmp_path / "orders"
    _write(repo / "pyproject.toml", '[project]\nname = "orders"\n')
    registry = tmp_path / "reg.json"
    registry.write_text(json.dumps({"services": [{"name": "orders", "import_names": []}]}))
    messages: list[str] = []
    resolve_manifest(repo, None, registry, None, messages.append)
    assert not any("not in the registry" in m for m in messages)


def test_github_repository_picks_the_renamed_service(tmp_path, monkeypatch):
    """scan-all's config can rename a service ('name:'); in Actions the repo is
    recognised from GITHUB_REPOSITORY, so `check` needs no --name."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "Acme/Orders")
    repo = tmp_path / "orders"
    _write(repo / "pyproject.toml", '[project]\nname = "orders"\n')
    registry = tmp_path / "reg.json"
    registry.write_text(
        json.dumps(
            {
                "registry": [
                    {"name": "orders-service", "path": "github:acme/orders", "import_names": ["orders"]},
                    {"name": "payments", "path": "github:acme/payments", "import_names": ["payments"]},
                ]
            }
        )
    )
    messages: list[str] = []
    known_services, _ = resolve_manifest(repo, None, registry, None, messages.append)
    assert known_services["__self__"] == "orders-service"
    assert "orders" not in known_services  # itself is never a target
    assert not any("not in the registry" in m for m in messages)


def test_explicit_name_beats_github_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/orders")
    repo = tmp_path / "orders"
    repo.mkdir()
    registry = tmp_path / "reg.json"
    registry.write_text(
        json.dumps({"services": [{"name": "other", "path": "github:acme/orders", "import_names": []}]})
    )
    known_services, _ = resolve_manifest(repo, None, registry, "mine", lambda m: None)
    assert known_services["__self__"] == "mine"


# ---- hostile repos ----


def test_pom_with_entity_declarations_is_not_parsed(tmp_path):
    _write(
        tmp_path / "pom.xml",
        """\
        <?xml version="1.0"?>
        <!DOCTYPE project [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>
        <project><artifactId>&b;</artifactId></project>
        """,
    )
    assert infer_service_name(tmp_path) == (tmp_path.name, "folder name")
