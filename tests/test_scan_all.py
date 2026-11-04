"""Zero-config scans, `scan-all`, and the example workflows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from depatlas.cli.main import cli

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _py_service(root: Path, name: str, package: str, body: str = "") -> Path:
    svc = root / name
    _write(svc / "pyproject.toml", f'[project]\nname = "{name}"\n')
    _write(svc / package / "__init__.py", "")
    _write(svc / package / "main.py", body)
    return svc


@pytest.fixture
def two_services(tmp_path):
    auth = _py_service(tmp_path, "auth-service", "auth_lib", "def login(user):\n    return user\n")
    pay = _py_service(
        tmp_path, "pay-service", "pay_app",
        "import auth_lib\nimport requests\nrequests.get('http://auth-service:8080/verify')\n",
    )
    return auth, pay


def _run(*args):
    return CliRunner().invoke(cli, [str(a) for a in args])


# ---- scan with nothing hand-written ----


def test_scan_needs_no_services_file(tmp_path, two_services):
    auth, pay = two_services
    registry = tmp_path / "reg.json"
    assert _run("discover", "--root", tmp_path, "--output", registry).exit_code == 0

    r = _run("scan", "--repo", pay, "--registry", registry, "--db", tmp_path / "g.db", "--no-alerts")

    assert r.exit_code == 0, r.output
    assert "Service name: pay-service" in r.output
    edges = json.loads(r.output[r.output.index("{"): r.output.rindex("}") + 1])["edges"]
    kinds = {(e["type"], e["target_service"], e["confidence"]) for e in edges}
    assert ("import", "auth-service", "high") in kinds
    assert ("http_call", "auth-service", "medium") in kinds


def test_scan_without_registry_still_runs_and_names_the_service(tmp_path, two_services):
    _, pay = two_services
    r = _run("scan", "--repo", pay, "--db", tmp_path / "g.db", "--no-alerts")
    assert r.exit_code == 0 and "Service name: pay-service" in r.output


def test_name_option_overrides_inference(tmp_path, two_services):
    _, pay = two_services
    r = _run("scan", "--repo", pay, "--name", "payments", "--db", tmp_path / "g.db", "--no-alerts")
    assert r.exit_code == 0
    assert "Persisted" in r.output


def test_codeowners_in_the_standard_place_is_used_automatically(tmp_path, two_services):
    _, pay = two_services
    _write(pay / ".github" / "CODEOWNERS", "* @acme/payments\n")
    db = tmp_path / "g.db"
    assert _run("scan", "--repo", pay, "--db", db, "--no-alerts").exit_code == 0
    from depatlas.graph.store import GraphStore

    store = GraphStore(db)
    teams = {s["id"]: s["team"] for s in store.all_services()}
    store.close()
    assert teams["pay-service"] == "@acme/payments"


def test_a_bad_registry_is_a_clear_error(tmp_path, two_services):
    _, pay = two_services
    bad = _write(tmp_path / "bad.json", json.dumps({"nope": 1}))
    r = _run("scan", "--repo", pay, "--registry", bad, "--db", tmp_path / "g.db")
    assert r.exit_code == 1 and "no service registry" in r.output


# ---- scan-all ----


def _config(tmp_path, *entries, extra=""):
    return _write(tmp_path / "org.yml", "repos:\n" + "".join(f"  - {e}\n" for e in entries) + extra)


def test_scan_all_links_repos_and_exports_a_graph_with_its_registry(tmp_path, two_services):
    cfg = _config(tmp_path, "path: auth-service", "path: pay-service")
    db, out = tmp_path / "g.db", tmp_path / "org.json"

    r = _run("scan-all", "--config", cfg, "--db", db, "--export", out)

    assert r.exit_code == 0, r.output
    graph = json.loads(out.read_text())
    assert {n["id"] for n in graph["nodes"]} >= {"auth-service", "pay-service"}
    assert any(e["source_service"] == "pay-service" and e["target_service"] == "auth-service" for e in graph["edges"])
    assert {s["name"] for s in graph["registry"]} == {"auth-service", "pay-service"}


def test_scan_all_glob_means_one_service_per_folder(tmp_path, two_services):
    cfg = _config(tmp_path, "path: ./*-service")
    r = _run("scan-all", "--config", cfg, "--db", tmp_path / "g.db")
    assert r.exit_code == 0 and "Scanned 2 repo(s)" in r.output


def test_one_failing_repo_does_not_stop_the_others_and_blocks_the_export(tmp_path, two_services, monkeypatch):
    monkeypatch.delenv("DEPATLAS_GITHUB_TOKEN", raising=False)
    cfg = _config(tmp_path, "path: auth-service", "github: acme/missing", "path: pay-service")
    db, out = tmp_path / "g.db", tmp_path / "org.json"

    r = _run("scan-all", "--config", cfg, "--db", db, "--export", out)

    assert r.exit_code == 1
    assert "Scanned 2 repo(s)" in r.output and "github:acme/missing" in r.output
    assert not out.exists()  # a half-built graph is never published

    r = _run("scan-all", "--config", cfg, "--db", db, "--export", out, "--allow-partial")
    assert r.exit_code == 1 and out.exists()


def test_two_repos_with_the_same_name_are_reported(tmp_path, two_services):
    other = _py_service(tmp_path / "elsewhere", "auth-service", "other_pkg")
    cfg = _config(tmp_path, "path: auth-service", f"path: {other}")
    r = _run("scan-all", "--config", cfg, "--db", tmp_path / "g.db")
    assert r.exit_code == 1 and "already used" in r.output


def test_team_and_name_in_the_config_are_honoured(tmp_path, two_services):
    cfg = _config(tmp_path, "path: auth-service\n    team: '@acme/identity'", "path: pay-service\n    name: payments")
    db = tmp_path / "g.db"
    assert _run("scan-all", "--config", cfg, "--db", db).exit_code == 0
    from depatlas.graph.store import GraphStore

    store = GraphStore(db)
    teams = {s["id"]: s["team"] for s in store.all_services()}
    store.close()
    assert teams["auth-service"] == "@acme/identity" and "payments" in teams


def test_reset_starts_from_an_empty_database(tmp_path, two_services):
    cfg = _config(tmp_path, "path: auth-service")
    db = tmp_path / "g.db"
    _run("scan-all", "--config", cfg, "--db", db)
    cfg2 = _write(tmp_path / "org2.yml", "repos:\n  - path: pay-service\n")
    _run("scan-all", "--config", cfg2, "--db", db, "--reset")
    from depatlas.graph.store import GraphStore

    store = GraphStore(db)
    real = {s["id"] for s in store.all_services() if s["repo"]}
    store.close()
    assert real == {"pay-service"}


@pytest.mark.parametrize(
    "entry,message",
    [
        ("github: nope", "owner/repo"),
        ("path: ./does-not-exist", "no folder matches"),
        ("ref: main", "exactly one of"),
    ],
)
def test_config_mistakes_are_explained(tmp_path, entry, message):
    r = _run("scan-all", "--config", _config(tmp_path, entry), "--db", tmp_path / "g.db")
    assert r.exit_code == 1 and message in r.output


# ---- the whole pull-request loop, nothing hand-written ----


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


def test_org_graph_then_check_in_a_pull_request(tmp_path):
    auth = _py_service(tmp_path, "auth-service", "auth_lib", "def login(user, password):\n    return user\n")
    pay = _py_service(tmp_path, "pay-service", "pay_app", "import auth_lib\n")
    out = tmp_path / "org.json"
    cfg = _config(tmp_path, "path: auth-service", "path: pay-service")
    assert _run("scan-all", "--config", cfg, "--db", tmp_path / "g.db", "--export", out).exit_code == 0

    subprocess.run(["git", "init", "-q", str(auth)], check=True)
    _git(auth, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(auth, "add", "-A"); _git(auth, "commit", "-q", "-m", "base")
    _git(auth, "checkout", "-q", "-b", "feature")
    _write(auth / "auth_lib" / "main.py", "def login(user, password, mfa_code):\n    return user\n")
    _git(auth, "add", "-A"); _git(auth, "commit", "-q", "-m", "require mfa")

    r = _run("check", "--repo", auth, "--base", "main", "--graph", out, "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "login" in r.output and "mfa_code" in r.output


# ---- the example workflows stay in step with the CLI ----


def _commands_in(workflow: dict):
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            for line in str(step.get("run", "")).splitlines():
                line = line.strip()
                if line.startswith("depatlas "):
                    yield line


@pytest.mark.parametrize("name", ["depatlas-nightly.yml", "depatlas-pr-check.yml"])
def test_example_workflows_use_real_commands_and_flags(name):
    workflow = yaml.safe_load((REPO_ROOT / "examples" / "github-actions" / name).read_text())
    assert workflow[True] is not None or "on" in workflow  # PyYAML reads the key `on` as True
    commands = list(_commands_in(workflow))
    assert commands
    for line in commands:
        parts = line.split()
        command = cli.commands[parts[1]]
        known = {opt for param in command.params for opt in param.opts + param.secondary_opts}
        for flag in (p for p in parts[2:] if p.startswith("--")):
            assert flag in known, f"{name}: `{line}` uses {flag}, which `depatlas {parts[1]}` does not have"


def test_example_org_config_parses():
    from depatlas.orgscan import load_org_config

    repos, _ = load_org_config(REPO_ROOT / "examples" / "depatlas-org.yml")
    assert [r.target for r in repos] == ["YOUR-ORG/payments", "YOUR-ORG/orders", "YOUR-ORG/billing"]
    assert repos[1].team == "@YOUR-ORG/orders-team" and repos[2].name == "billing-service"


# ---- remote repos (GitHub downloads faked; the real API is not called) ----


def test_scan_all_with_github_repos_names_links_and_keys_them(tmp_path, monkeypatch):
    import io
    import zipfile

    import requests

    def make_zip(wrapper: str, files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, text in files.items():
                zf.writestr(f"{wrapper}/{name}", text)
            zf.comment = b"c" * 40
        return buf.getvalue()

    archives = {
        "acme/auth": make_zip("acme-auth-abc1234", {
            "pyproject.toml": '[project]\nname = "auth-service"\n',
            "auth_lib/__init__.py": "",
            "auth_lib/m.py": "def login(u):\n    return u\n",
        }),
        "acme/pay": make_zip("acme-pay-abc1234", {
            "pyproject.toml": '[project]\nname = "pay-service"\n',
            "pay_app/__init__.py": "",
            "pay_app/m.py": "import auth_lib\n",
            ".github/CODEOWNERS": "* @acme/payments\n",
        }),
    }

    class Response:
        status_code = 200
        text = ""

        def __init__(self, content):
            self.content = content

    def fake_get(url, headers=None, timeout=None):
        repo = url.split("/repos/")[1].split("/zipball")[0]
        return Response(archives[repo])

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("DEPATLAS_GITHUB_TOKEN", "t")
    cfg = _config(tmp_path, "github: acme/auth", "github: acme/pay", extra="cache_dir: cache\n")
    out, db = tmp_path / "org.json", tmp_path / "g.db"

    r = _run("scan-all", "--config", cfg, "--db", db, "--export", out)

    assert r.exit_code == 0, r.output
    graph = json.loads(out.read_text())
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["pay-service"]["repo"] == "github:acme/pay"        # a stable key, not a temp path
    assert nodes["pay-service"]["team"] == "@acme/payments"         # CODEOWNERS found in the download
    assert nodes["pay-service"]["last_analysed_sha"] == "c" * 40    # full SHA from the zip comment
    assert any(e["source_service"] == "pay-service" and e["target_service"] == "auth-service"
               for e in graph["edges"])
