# DepAtlas 0.4.2 tests

The test suite (323 tests).

## Files

- `tests/fixtures/sample_repo/auth/session.py`
- `tests/fixtures/sample_repo/payments/charge.py`
- `tests/test_check.py`
- `tests/test_cli/test_end_to_end.py`
- `tests/test_cli/test_smoke.py`
- `tests/test_connectors/test_codecommit.py`
- `tests/test_connectors/test_github.py`
- `tests/test_discovery.py`
- `tests/test_fsutil.py`
- `tests/test_graph/test_builder.py`
- `tests/test_graph/test_store.py`
- `tests/test_intelligence/test_alerts.py`
- `tests/test_intelligence/test_diff.py`
- `tests/test_output/test_dot_export.py`
- `tests/test_output/test_json_export.py`
- `tests/test_ownership/test_codeowners.py`
- `tests/test_ownership/test_manifest.py`
- `tests/test_param_kinds.py`
- `tests/test_parsers/test_java_parser.py`
- `tests/test_parsers/test_js_ts_parser.py`
- `tests/test_parsers/test_python_parser.py`
- `tests/test_review_round2.py`
- `tests/test_review_round3.py`
- `tests/test_review_round4.py`
- `tests/test_review_round5.py`
- `tests/test_scan_all.py`


## `tests/fixtures/sample_repo/auth/session.py`

`````python
"""Auth service: session/token verification. No internal imports of its own."""


def verify_token(token: str) -> bool:
    return bool(token)


class AuthError(Exception):
    pass
`````


## `tests/fixtures/sample_repo/payments/charge.py`

`````python
"""Payments service: handles charge creation."""

import auth_client
from auth_client.session import verify_token
import json  # stdlib — should be ignored, not a known internal service
import requests


def create_charge(user_token: str, amount_cents: int) -> dict:
    if not verify_token(user_token):
        raise auth_client.AuthError("invalid token")

    # Explicit endpoint call — should resolve via the endpoints registry
    # at medium confidence.
    requests.get("https://auth.internal/session/validate")

    return json.dumps({"amount_cents": amount_cents, "status": "created"})


def notify_shipping(order_id: str) -> None:
    # No entry for this host in the endpoints registry, but the URL
    # contains "shipping" which happens to match a known service name —
    # should resolve at low confidence via the heuristic fallback.
    requests.post("https://internal-shipping-gateway.example.com/notify")
`````


## `tests/test_check.py`

`````python
"""`depatlas check --base`: the stateless pull-request check, against real git repos."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.intelligence.diff import compute_diff


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True,
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


SERVICES = {"__self__": "pay-service", "auth_client": "auth-service"}


def _graph(tmp_path, dependents=("billing-service",), teams=None):
    teams = teams or {"pay-service": "payments", "auth-service": "identity", "billing-service": "money"}
    graph = {
        "nodes": [{"id": k, "team": v} for k, v in teams.items()],
        "edges": [
            {"source_service": d, "target_service": "pay-service", "type": "import",
             "source_file": "x.py", "source_line": 1, "confidence": "high", "from_test": 0}
            for d in dependents
        ],
    }
    path = tmp_path / "org.json"
    path.write_text(json.dumps(graph))
    return path


def _run(tmp_path, repo, *extra, services=SERVICES):
    sp = tmp_path / "services.json"
    sp.write_text(json.dumps(services))
    return CliRunner().invoke(
        cli, ["check", "--repo", str(repo), "--base", "main", "--services", str(sp), *extra]
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pay"
    _init(r)
    (r / "charge.py").write_text("def charge(amount, currency):\n    return amount\n")
    _commit(r, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    return r


def test_removed_public_function_with_a_dependent_is_breaking_and_fails_the_job(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "BREAKING_CHANGE" in r.output and "charge" in r.output


def test_same_change_without_dependents_is_not_breaking(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "high")

    assert r.exit_code == 0 and "No alerts" in r.output


def test_new_cross_team_dependency_is_reported(tmp_path, repo):
    (repo / "client.py").write_text("import auth_client\n")
    _commit(repo, "depend on auth")

    r = _run(tmp_path, repo, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")

    assert r.exit_code == 3, r.output
    assert "NEW_CROSS_TEAM_DEPENDENCY" in r.output


def test_a_dependency_that_already_existed_is_not_new(tmp_path):
    r0 = tmp_path / "pay"
    _init(r0)
    (r0 / "client.py").write_text("import auth_client\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "client.py").write_text("\n\nimport auth_client  # moved down\n")
    _commit(r0, "move")

    r = _run(tmp_path, r0, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "low")

    assert r.exit_code == 0 and "No alerts" in r.output


def test_without_a_graph_it_says_why_nothing_can_fire(tmp_path, repo):
    (repo / "charge.py").write_text("def refund(amount):\n    return amount\n")
    _commit(repo, "remove charge")

    r = _run(tmp_path, repo)

    assert r.exit_code == 0
    assert "--graph" in r.output


def test_history_missing_gives_an_actionable_error(tmp_path, repo):
    r = CliRunner().invoke(
        cli,
        ["check", "--repo", str(repo), "--base", "origin/nope", "--services",
         str(_write(tmp_path / "s.json", SERVICES))],
    )
    assert r.exit_code == 1
    assert "fetch-depth: 0" in r.output


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_monorepo_subfolder_is_compared_with_its_own_base(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    svc = mono / "services" / "pay"
    svc.mkdir(parents=True)
    (svc / "charge.py").write_text("def charge(amount, currency):\n    return amount\n")
    (mono / "other.py").write_text("def unrelated():\n    pass\n")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    (svc / "charge.py").write_text("def charge(amount):\n    return amount\n")
    _commit(mono, "drop a parameter")

    r = _run(tmp_path, svc, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "charge" in r.output and "unrelated" not in r.output


def test_a_brand_new_service_folder_has_no_base_and_does_not_crash(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    (mono / "README.md").write_text("x")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    svc = mono / "pay"
    svc.mkdir()
    (svc / "client.py").write_text("import auth_client\n")
    _commit(mono, "add service")

    r = _run(tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")

    assert r.exit_code == 3, r.output  # its new cross-team edge is reported
    assert "treating it as new" in r.output


def test_a_language_removed_in_the_change_is_still_compared(tmp_path):
    r0 = tmp_path / "pay"
    _init(r0)
    (r0 / "Api.java").write_text("public class Api { public void pay(int a) {} }\n")
    (r0 / "x.py").write_text("x = 1\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "Api.java").unlink()
    _commit(r0, "delete java")

    r = _run(tmp_path, r0, "--graph", str(_graph(tmp_path)), "--fail-on", "high")

    assert r.exit_code == 3, r.output
    assert "pay" in r.output


def test_bad_graph_file_is_a_clear_error(tmp_path, repo):
    bad = _write(tmp_path / "bad.json", {"nope": 1})
    r = _run(tmp_path, repo, "--graph", str(bad))
    assert r.exit_code == 1 and "graph export" in r.output


def test_diff_prefers_a_production_edge_over_a_test_edge_with_the_same_identity():
    e = lambda t: {"source_service": "a", "target_service": "b", "type": "import",
                   "source_file": "f", "source_line": 1, "from_test": t}
    # same dependency found in a prod file first and a test file later (and the reverse)
    for order in ([e(0), e(1)], [e(1), e(0)]):
        added = compute_diff([], order, by_location=False)["added"]
        assert len(added) == 1 and not added[0]["from_test"]
`````


## `tests/test_cli/test_end_to_end.py`

`````python
"""End-to-end: scan -> change code -> scan again -> diff/alerts.

Unit tests passed while rescans of the same repo silently broke diffs,
because nothing exercised the whole flow in one run.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from depatlas.cli.main import cli


def _write_repo(root, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "charge.py").write_text(body)


def test_scan_twice_then_diff_last_sees_the_new_dependency(tmp_path):
    repo = tmp_path / "payments"
    services = tmp_path / "services.json"
    services.write_text(
        json.dumps({"__self__": "payments-service", "auth_client": "auth-service",
                    "ledger_client": "ledger-service"})
    )
    db = tmp_path / "graph.db"
    runner = CliRunner()
    base = ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)]

    _write_repo(repo, "import auth_client\n")
    r1 = runner.invoke(cli, base)
    assert r1.exit_code == 0, r1.output

    _write_repo(repo, "import auth_client\nimport ledger_client\n")
    r2 = runner.invoke(cli, base)
    assert r2.exit_code == 0, r2.output

    d = runner.invoke(cli, ["diff", "--last", "2", "--repo", str(repo), "--db", str(db)])
    assert d.exit_code == 0, d.output
    assert "ledger-service" in d.output


def test_alerts_command_diffs_a_repo_scanned_under_a_stable_key(tmp_path):
    """Regression: snapshots are saved under a stable repo key, so the
    service's `repo` must hold that same key or `alerts` finds no snapshots."""
    from depatlas.graph.store import GraphStore

    repo = tmp_path / "payments"
    services = tmp_path / "services.json"
    services.write_text(json.dumps({"__self__": "payments-service", "auth_client": "auth-service"}))
    db = tmp_path / "graph.db"
    _write_repo(repo, "import auth_client\n")
    CliRunner().invoke(
        cli, ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)]
    )

    store = GraphStore(db)
    snapshot_repos = {s["repo"] for s in store.list_snapshots()}
    service_repos = {s["repo"] for s in store.all_services() if s["repo"]}
    store.close()

    assert snapshot_repos == service_repos


def test_scan_records_commit_sha_for_a_local_git_repo(tmp_path):
    import subprocess

    repo = tmp_path / "payments"
    _write_repo(repo, "import auth_client\n")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.email=a@b.c", "-c", "user.name=t", "add", ".")
    run("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "x")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    services = tmp_path / "services.json"
    services.write_text(json.dumps({"__self__": "payments-service", "auth_client": "auth-service"}))
    db = tmp_path / "graph.db"
    CliRunner().invoke(cli, ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)])

    from depatlas.graph.store import GraphStore

    store = GraphStore(db)
    shas = {e["detected_at_sha"] for e in store.all_edges()}
    node_shas = {s["last_analysed_sha"] for s in store.all_services() if s["id"] == "payments-service"}
    store.close()
    assert shas == {head} and node_shas == {head}


def test_full_flow_scan_scan_diff_alerts(tmp_path):
    """The flow the code review asked for: scan -> change code -> scan ->
    diff -> alerts, in one run, through the real CLI. Covers both
    diff-based alerts (NEW_CROSS_TEAM_DEPENDENCY and BREAKING_CHANGE)."""
    manifest = tmp_path / "depatlas.yaml"
    manifest.write_text(
        "version: 1\nservices:\n"
        "  - {name: payments-service, repo: org/payments, team: payments-team}\n"
        "  - {name: auth-service, repo: org/auth, team: platform-team}\n"
        "  - {name: orders-service, repo: org/orders, team: orders-team}\n"
    )
    db = tmp_path / "graph.db"
    runner = CliRunner()

    def scan(repo, self_name, imports):
        services = tmp_path / f"{self_name}.json"
        services.write_text(json.dumps({"__self__": self_name, **imports}))
        result = runner.invoke(
            cli,
            ["scan", "--repo", str(repo), "--services", str(services),
             "--ownership-manifest", str(manifest), "--db", str(db)],
        )
        assert result.exit_code == 0, result.output
        return result

    orders, payments = tmp_path / "orders", tmp_path / "payments"

    # orders-service depends on payments-service, so payments has a dependent.
    _write_repo(orders, "import payments_client\n")
    scan(orders, "orders-service", {"payments_client": "payments-service"})

    # payments v1: public charge(amount: int), no dependencies.
    _write_repo(payments, "def charge(amount: int):\n    pass\n")
    scan(payments, "payments-service", {"auth_client": "auth-service"})

    # payments v2: now imports auth_client (cross-team) and charge() grew a parameter.
    _write_repo(
        payments,
        "import auth_client\n\ndef charge(amount: int, currency: str):\n    pass\n",
    )
    scan(payments, "payments-service", {"auth_client": "auth-service"})

    diff = runner.invoke(cli, ["diff", "--last", "2", "--repo", str(payments), "--db", str(db)])
    assert diff.exit_code == 0, diff.output
    assert "auth-service" in diff.output

    alerts = runner.invoke(cli, ["alerts", "--db", str(db)])
    assert alerts.exit_code == 0, alerts.output
    assert "NEW_CROSS_TEAM_DEPENDENCY" in alerts.output
    assert "BREAKING_CHANGE" in alerts.output
    assert "charge" in alerts.output
`````


## `tests/test_cli/test_smoke.py`

`````python
"""CLI smoke tests.

No other test file imports depatlas.cli.main at all -- every other test
exercises the underlying modules (parsers, store, connectors, etc.)
directly. That's normally the right call for fast, focused unit tests,
but it means a bug in the CLI wiring itself (a bad Click decorator, a
typo'd option name) can slip through with 100% green tests elsewhere.

This file's job is narrow on purpose: prove every command's argument
parsing is valid and every command is reachable, using Click's own
CliRunner rather than shelling out to a real subprocess. It is not
trying to re-test business logic already covered elsewhere.
"""

from __future__ import annotations

from click.testing import CliRunner

from depatlas.cli.main import cli


def test_cli_help_runs_without_error():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0


def test_every_command_help_runs_without_error():
    # This is the check that would have caught a duplicate Click option
    # name (like --from being declared with two variable names at once)
    # -- that kind of bug raises at decoration time, i.e. the moment
    # depatlas.cli.main is imported, which only happens when a test
    # actually imports this module.
    runner = CliRunner()
    for command_name in ("scan", "query", "snapshots", "diff", "alerts", "export"):
        result = runner.invoke(cli, [command_name, "--help"])
        assert result.exit_code == 0, f"'{command_name} --help' failed: {result.output}"


def test_scan_requires_a_source():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("services.json", "w") as f:
            f.write('{"__self__": "x"}')
        result = runner.invoke(cli, ["scan", "--services", "services.json"])
        assert result.exit_code != 0
        assert "Provide one of --repo, --github-repo, or --codecommit-repo" in result.output


def test_diff_requires_from_to_or_last():
    runner = CliRunner()
    result = runner.invoke(cli, ["diff"])
    assert result.exit_code != 0


def test_query_requires_service_or_team():
    runner = CliRunner()
    with runner.isolated_filesystem():
        import sqlite3

        sqlite3.connect("graph.db").close()
        result = runner.invoke(cli, ["query", "--db", "graph.db"])
        assert result.exit_code != 0
        assert "Provide either --service or --team" in result.output
`````


## `tests/test_connectors/test_codecommit.py`

`````python
import pytest

pytest.importorskip("botocore")  # optional extra: depatlas[aws]

from botocore.exceptions import ClientError  # noqa: E402

from depatlas.connectors.codecommit import (
    PROFILE_ENV_VAR,
    CodeCommitConnectorError,
    fetch_repo,
    get_client,
)


class _FakeCodeCommitClient:
    """A minimal stand-in for boto3's CodeCommit client. `folders` maps a
    folder path to the get_folder response it should return; `files` maps
    an absolutePath to the file content bytes it should return.
    """

    def __init__(self, folders: dict, files: dict, raise_code: str | None = None):
        self.folders = folders
        self.files = files
        self.raise_code = raise_code

    def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
        if self.raise_code:
            raise ClientError(
                {"Error": {"Code": self.raise_code, "Message": "fake error"}}, "GetFolder"
            )
        return self.folders[folderPath]

    def get_file(self, repositoryName, filePath, commitSpecifier=None):
        return {"fileContent": self.files[filePath]}


def test_fetch_repo_reconstructs_nested_tree(tmp_path):
    folders = {
        "/": {
            "files": [{"absolutePath": "README.md"}],
            "subFolders": [{"absolutePath": "src"}],
        },
        "src": {
            "files": [{"absolutePath": "src/charge.py"}],
            "subFolders": [],
        },
    }
    files = {
        "README.md": b"# payments\n",
        "src/charge.py": b"import auth_client\n",
    }
    client = _FakeCodeCommitClient(folders, files)

    result = fetch_repo("payments", client, tmp_path)

    assert result == tmp_path / "payments"
    assert (result / "README.md").read_bytes() == b"# payments\n"
    assert (result / "src" / "charge.py").read_bytes() == b"import auth_client\n"


def test_fetch_repo_overwrites_stale_cache(tmp_path):
    stale_dir = tmp_path / "payments"
    stale_dir.mkdir()
    (stale_dir / "old_stale_file.py").write_text("# should be gone after rescan")

    folders = {"/": {"files": [], "subFolders": []}}
    client = _FakeCodeCommitClient(folders, {})

    result = fetch_repo("payments", client, tmp_path)

    assert not (result / "old_stale_file.py").exists()


def test_fetch_repo_raises_on_repository_not_found(tmp_path):
    client = _FakeCodeCommitClient({}, {}, raise_code="RepositoryDoesNotExistException")

    with pytest.raises(CodeCommitConnectorError, match="not found"):
        fetch_repo("nonexistent", client, tmp_path)


def test_fetch_repo_treats_missing_folder_as_empty_not_an_error(tmp_path):
    client = _FakeCodeCommitClient({}, {}, raise_code="FolderDoesNotExistException")

    result = fetch_repo("empty-repo", client, tmp_path)

    assert result == tmp_path / "empty-repo"
    assert list(result.iterdir()) == []


def test_get_client_uses_profile_env_when_set(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, profile_name=None):
            calls["profile_name"] = profile_name

        def client(self, service, region_name=None):
            calls["service"] = service
            calls["region_name"] = region_name
            return "fake-client"

    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.setenv(PROFILE_ENV_VAR, "depatlas-profile")
    monkeypatch.setattr(codecommit_module.boto3, "Session", FakeSession)

    result = get_client("us-east-1")

    assert calls["profile_name"] == "depatlas-profile"
    assert calls["service"] == "codecommit"
    assert calls["region_name"] == "us-east-1"
    assert result == "fake-client"


def test_get_client_falls_back_to_default_session_without_profile(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, profile_name=None):
            calls["profile_name"] = profile_name

        def client(self, service, region_name=None):
            return "fake-client"

    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    monkeypatch.setattr(codecommit_module.boto3, "Session", FakeSession)

    get_client("us-east-1")

    assert calls["profile_name"] is None


def test_fetch_repo_records_commit_id(tmp_path):
    from depatlas.gitinfo import get_sha

    folders = {"/": {"files": [], "subFolders": [], "commitId": "deadbeef1234"}}
    result = fetch_repo("payments", _FakeCodeCommitClient(folders=folders, files={}), tmp_path)

    assert get_sha(result) == "deadbeef1234"


def test_get_client_explains_missing_boto3(monkeypatch):
    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.setattr(codecommit_module, "boto3", None)

    with pytest.raises(CodeCommitConnectorError, match=r"depatlas\[aws\]"):
        get_client("us-east-1")
`````


## `tests/test_connectors/test_github.py`

`````python
import io
import zipfile

import pytest
import requests

from depatlas.connectors.github import (
    GitHubConnectorError,
    TOKEN_ENV_VAR,
    fetch_repo,
    get_token,
)


def _make_fake_zip_bytes() -> bytes:
    """Build a small in-memory zip mimicking GitHub's zipball shape:
    everything wrapped in one top-level "owner-repo-<sha>" folder.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("payments-abc1234/charge.py", "import auth_client\n")
        zf.writestr("payments-abc1234/README.md", "# payments\n")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode(errors="ignore")


def test_get_token_raises_when_env_var_missing(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(GitHubConnectorError):
        get_token()


def test_get_token_returns_value_when_set(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "fake-token-123")
    assert get_token() == "fake-token-123"


def test_fetch_repo_extracts_zip_and_resolves_inner_dir(tmp_path, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        assert "payments" in url
        assert headers["Authorization"] == "Bearer fake-token"
        return _FakeResponse(200, content=_make_fake_zip_bytes())

    monkeypatch.setattr(requests, "get", fake_get)

    result = fetch_repo("org", "payments", "fake-token", tmp_path)

    assert result.name == "payments"  # SHA-named wrapper renamed to a stable name
    assert (result / "charge.py").exists()
    assert (result / "charge.py").read_text() == "import auth_client\n"


def test_fetch_repo_raises_on_404(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404))

    with pytest.raises(GitHubConnectorError, match="not found"):
        fetch_repo("org", "nonexistent", "fake-token", tmp_path)


def test_fetch_repo_raises_on_401(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(401))

    with pytest.raises(GitHubConnectorError, match="invalid or expired"):
        fetch_repo("org", "payments", "bad-token", tmp_path)


def test_fetch_repo_raises_on_network_error(tmp_path, monkeypatch):
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", raise_connection_error)

    with pytest.raises(GitHubConnectorError, match="Failed to reach"):
        fetch_repo("org", "payments", "fake-token", tmp_path)


def test_rescan_of_new_commit_leaves_one_stable_folder(tmp_path, monkeypatch):
    """Regression: a second scan (new commit SHA) used to leave the old
    SHA folder behind and return a different path, which broke diffs."""

    def zip_for(sha: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(f"payments-{sha}/charge.py", f"# {sha}\n")
        return buffer.getvalue()

    shas = iter(["aaa1111", "bbb2222"])
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, content=zip_for(next(shas)))
    )

    first = fetch_repo("org", "payments", "t", tmp_path)
    second = fetch_repo("org", "payments", "t", tmp_path)

    assert first == second
    assert (second / "charge.py").read_text() == "# bbb2222\n"
    assert len([p for p in (tmp_path / "org__payments").iterdir() if p.is_dir()]) == 1


def test_fetch_records_the_commit_sha(tmp_path, monkeypatch):
    from depatlas.gitinfo import get_sha

    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(200, content=_make_fake_zip_bytes())
    )
    result = fetch_repo("org", "payments", "t", tmp_path)

    assert get_sha(result) == "abc1234"
`````


## `tests/test_discovery.py`

`````python
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
`````


## `tests/test_fsutil.py`

`````python
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
        "package x;\npublic interface Api { String find(long id, java.util.List<String> tags); }\n"
    )
    sigs = java_sigs(tmp_path, "svc")

    assert [s.name for s in sigs] == ["find"]
    assert sigs[0].params == [{"name": "id", "type": "long"}, {"name": "tags", "type": "List"}]


def test_resolver_ignores_self_and_whole_word_only():
    from depatlas.resolution import resolve_endpoint

    services = {"__self__": "payments-service", "x": "auth-service", "y": "payments-service"}
    # The scanned service is never a target of its own URLs.
    assert resolve_endpoint("http://payments.internal/charge", {}, services) == (None, None)
    # "auth" must not match "oauth" or "author".
    assert resolve_endpoint("https://oauth.example.com/token", {}, services) == (None, None)
    assert resolve_endpoint("https://example.com/author/1", {}, services) == (None, None)
    # ...but a real reference still matches.
    assert resolve_endpoint("http://auth-internal.example.com/v", {}, services) == (
        "auth-service",
        "low",
    )


def test_java_config_get_is_not_an_http_call(tmp_path):
    from depatlas.parsers.java_parser import parse_repo as java_parse

    (tmp_path / "C.java").write_text(
        'class C { void a(){ config.get("auth.timeout"); client.get("http://auth-service/x"); } }\n'
    )
    edges = java_parse(tmp_path, {"__self__": "me", "k": "auth-service"})

    assert len(edges) == 1  # only the URL-shaped call
`````


## `tests/test_graph/test_builder.py`

`````python
from pathlib import Path

from depatlas.graph.builder import build_graph
from depatlas.graph.models import DependencyEdge


def _edge(source, target):
    return DependencyEdge(
        source_service=source,
        target_service=target,
        type="import",
        source_file="fake.py",
        source_line=1,
        confidence="high",
    )


def test_self_service_gets_full_node():
    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, Path("/repos/payments"), self_service="payments-service")

    self_node = next(n for n in nodes if n.id == "payments-service")
    assert self_node.repo == str(Path("/repos/payments"))
    assert self_node.language == "python"


def test_dependency_only_service_gets_placeholder_node():
    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, Path("/repos/payments"), self_service="payments-service")

    dep_node = next(n for n in nodes if n.id == "auth-service")
    assert dep_node.repo == ""
    assert dep_node.language == ""


def test_self_service_with_no_edges_still_gets_a_node():
    nodes = build_graph([], Path("/repos/leaf-service"), self_service="leaf-service")

    assert len(nodes) == 1
    assert nodes[0].id == "leaf-service"


def test_team_is_none_when_no_codeowners_rules_supplied():
    nodes = build_graph([], Path("/repos/leaf-service"), self_service="leaf-service")
    assert nodes[0].team is None


def test_team_resolved_via_codeowners_majority(tmp_path):
    from depatlas.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "b.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    nodes = build_graph([], tmp_path, self_service="payments-service", codeowners_rules=rules)
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team"


def test_dependency_placeholder_gets_no_team_from_codeowners_alone(tmp_path):
    # CODEOWNERS can't resolve a placeholder's team -- there's no file
    # tree for a service we haven't scanned directly. (The manifest CAN
    # resolve it -- see test_dependency_placeholder_gets_team_from_manifest.)
    from depatlas.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, tmp_path, self_service="payments-service", codeowners_rules=rules)

    dep_node = next(n for n in nodes if n.id == "auth-service")
    assert dep_node.team is None


def test_dependency_placeholder_gets_team_from_manifest(tmp_path):
    # Regression test: the manifest is a flat name->team lookup that
    # doesn't require having scanned a service's own repo, so it should
    # resolve a placeholder's team too -- not just the self_service's.
    manifest_teams = {
        "payments-service": "payments-team",
        "auth-service": "platform-team",
    }
    edges = [_edge("payments-service", "auth-service")]

    nodes = build_graph(
        edges, tmp_path, self_service="payments-service", manifest_teams=manifest_teams
    )

    dep_node = next(n for n in nodes if n.id == "auth-service")
    assert dep_node.team == "platform-team"


def test_codeowners_resolves_non_python_files_too(tmp_path):
    # Regression test: _resolve_team used to hardcode rglob("*.py"), so
    # CODEOWNERS resolution silently did nothing for Java/JS repos. A
    # repo with only a .java file must still resolve a team.
    from depatlas.ownership.codeowners import parse_codeowners

    (tmp_path / "PaymentsApplication.java").write_text("// fake")
    (tmp_path / "CODEOWNERS").write_text("*.java @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    nodes = build_graph([], tmp_path, self_service="payments-service", codeowners_rules=rules)
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team"


def test_manifest_used_when_no_codeowners_given(tmp_path):
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [], tmp_path, self_service="payments-service", manifest_teams=manifest_teams
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "payments-team-from-manifest"


def test_manifest_used_when_codeowners_resolves_nothing(tmp_path):
    from depatlas.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    # A CODEOWNERS file that matches nothing in this repo.
    (tmp_path / "CODEOWNERS").write_text("*.rb @some-other-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [],
        tmp_path,
        self_service="payments-service",
        codeowners_rules=rules,
        manifest_teams=manifest_teams,
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "payments-team-from-manifest"


def test_codeowners_takes_priority_over_manifest_when_both_resolve(tmp_path):
    from depatlas.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team-from-codeowners\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [],
        tmp_path,
        self_service="payments-service",
        codeowners_rules=rules,
        manifest_teams=manifest_teams,
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team-from-codeowners"


def test_unowned_when_neither_codeowners_nor_manifest_resolve(tmp_path):
    manifest_teams = {"some-other-service": "some-team"}

    nodes = build_graph(
        [], tmp_path, self_service="payments-service", manifest_teams=manifest_teams
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team is None
`````


## `tests/test_graph/test_store.py`

`````python
from pathlib import Path

import pytest

from depatlas.graph.models import DependencyEdge, PublicSignature, ServiceNode
from depatlas.graph.store import GraphStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / ".depatlas" / "graph.db"
    s = GraphStore(db_path)
    yield s
    s.close()


def _node(id_, repo="", language=""):
    return ServiceNode(id=id_, name=id_, repo=repo, language=language)


def _edge(source, target, source_file):
    return DependencyEdge(
        source_service=source,
        target_service=target,
        type="import",
        source_file=source_file,
        source_line=1,
        confidence="high",
    )


def test_upsert_and_read_back_services(store):
    store.upsert_services([_node("payments-service", repo="/repos/payments", language="python")])

    services = store.all_services()
    assert len(services) == 1
    assert services[0]["id"] == "payments-service"
    assert services[0]["repo"] == "/repos/payments"


def test_placeholder_does_not_overwrite_a_real_node(store):
    store.upsert_services([_node("auth-service", repo="/repos/auth", language="python")])
    # A later scan of some *other* repo mentions auth-service only as a
    # dependency -- it must not clobber the real, already-known node.
    store.upsert_services([_node("auth-service", repo="", language="")])

    services = store.all_services()
    assert services[0]["repo"] == "/repos/auth"


def test_rescanning_a_repo_replaces_its_edges(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )
    # Simulate a rescan where that import was removed from the code.
    store.replace_edges_for_repo("/repos/payments", [])

    edges = store.edges_for_service("payments-service", direction="downstream")
    assert edges == []


def test_edges_for_service_downstream_vs_upstream(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )

    downstream = store.edges_for_service("payments-service", direction="downstream")
    upstream = store.edges_for_service("auth-service", direction="upstream")

    assert len(downstream) == 1
    assert len(upstream) == 1
    assert downstream[0]["target_service"] == "auth-service"
    assert upstream[0]["source_service"] == "payments-service"


def test_save_snapshot_returns_incrementing_ids(store):
    id1 = store.save_snapshot("/repos/payments", [])
    id2 = store.save_snapshot("/repos/payments", [])

    assert id2 == id1 + 1


def test_get_snapshot_round_trips_edges(store):
    edges = [_edge("payments-service", "auth-service", "/repos/payments/charge.py")]
    snapshot_id = store.save_snapshot("/repos/payments", edges)

    snapshot = store.get_snapshot(snapshot_id)

    assert snapshot["repo"] == "/repos/payments"
    assert len(snapshot["edges"]) == 1
    assert snapshot["edges"][0]["target_service"] == "auth-service"


def test_get_snapshot_returns_none_for_unknown_id(store):
    assert store.get_snapshot(999) is None


def test_list_snapshots_most_recent_first(store):
    id1 = store.save_snapshot("/repos/payments", [])
    id2 = store.save_snapshot("/repos/payments", [])

    snapshots = store.list_snapshots(repo="/repos/payments")

    assert [s["id"] for s in snapshots] == [id2, id1]


def test_list_snapshots_filters_by_repo(store):
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/auth", [])

    payments_snapshots = store.list_snapshots(repo="/repos/payments")

    assert len(payments_snapshots) == 1
    assert payments_snapshots[0]["repo"] == "/repos/payments"


def test_save_snapshot_does_not_replace_previous_snapshots(store):
    # Unlike replace_edges_for_repo, snapshots accumulate -- this is
    # what gives the diff engine a real timeline.
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/payments", [])
    store.save_snapshot("/repos/payments", [])

    assert len(store.list_snapshots(repo="/repos/payments")) == 3


def test_all_edges_returns_every_edge_across_repos(store):
    store.replace_edges_for_repo(
        "/repos/payments",
        [_edge("payments-service", "auth-service", "/repos/payments/charge.py")],
    )
    store.replace_edges_for_repo(
        "/repos/auth", [_edge("auth-service", "billing-service", "/repos/auth/session.py")]
    )

    all_edges = store.all_edges()

    targets = {e["target_service"] for e in all_edges}
    assert targets == {"auth-service", "billing-service"}


def test_save_snapshot_round_trips_signatures(store):
    sig = PublicSignature(
        service="payments-service",
        name="createCharge",
        params=[{"name": "amount", "type": "int"}],
        source_file="/repos/payments/charge.py",
        source_line=5,
    )
    snapshot_id = store.save_snapshot("/repos/payments", [], signatures=[sig])

    snapshot = store.get_snapshot(snapshot_id)

    assert len(snapshot["signatures"]) == 1
    assert snapshot["signatures"][0]["name"] == "createCharge"
    assert snapshot["signatures"][0]["params"] == [{"name": "amount", "type": "int"}]


def test_save_snapshot_defaults_to_empty_signatures(store):
    snapshot_id = store.save_snapshot("/repos/payments", [])

    snapshot = store.get_snapshot(snapshot_id)

    assert snapshot["signatures"] == []


def test_existing_database_without_signatures_column_gets_migrated(tmp_path):
    # Simulate a database created before signatures_json existed (i.e.
    # before this migration was added) by building the old schema by
    # hand, then confirm opening it with GraphStore adds the column
    # instead of crashing -- this is the actual scenario a real user
    # hits when they pull in this change with an existing local graph.db.
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edges_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO snapshots (repo, created_at, edges_json) VALUES (?, ?, ?)",
        ("/repos/payments", "2026-01-01T00:00:00", "[]"),
    )
    conn.commit()
    conn.close()

    store = GraphStore(db_path)
    snapshot = store.get_snapshot(1)
    store.close()

    assert snapshot["signatures"] == []  # migrated column defaults to '[]'


def test_rescan_does_not_delete_a_sibling_repo_with_a_shared_prefix(store):
    # Regression: DELETE ... LIKE '/repos/pay%' used to wipe /repos/payments too.
    store.replace_edges_for_repo(
        "/repos/payments", [_edge("payments-service", "auth-service", "/repos/payments/a.py")]
    )
    store.replace_edges_for_repo("/repos/pay", [])

    assert len(store.edges_for_service("payments-service", direction="downstream")) == 1


def test_underscore_in_repo_key_is_not_a_wildcard(store):
    store.replace_edges_for_repo(
        "/repos/sampleXrepo", [_edge("a-service", "b-service", "/repos/sampleXrepo/a.py")]
    )
    store.replace_edges_for_repo("/repos/sample_repo", [])

    assert len(store.edges_for_service("a-service", direction="downstream")) == 1


def test_legacy_edges_without_repo_column_still_migrate_and_get_replaced(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE edges (
            source_service TEXT NOT NULL, target_service TEXT NOT NULL,
            type TEXT NOT NULL, source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL, confidence TEXT NOT NULL,
            detected_at_sha TEXT
        );
        INSERT INTO edges VALUES ('p','a','import','/repos/payments/x.py',1,'high',NULL);
        """
    )
    conn.commit()
    conn.close()

    s = GraphStore(db)  # must add the repo column without error
    s.replace_edges_for_repo("/repos/payments", [], self_service="p")
    assert s.edges_for_service("p", direction="downstream") == []
    s.close()
`````


## `tests/test_intelligence/test_alerts.py`

`````python
from depatlas.intelligence.alerts import (
    check_breaking_changes,
    check_high_fan_in,
    check_new_cross_team_dependencies,
    check_orphaned_service,
    check_unowned_service,
)


def _edge(source, target):
    return {"source_service": source, "target_service": target}


# --- NEW_CROSS_TEAM_DEPENDENCY ---


def test_flags_new_edge_crossing_team_boundary():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team", "auth-service": "platform-team"}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert len(alerts) == 1
    assert alerts[0].type == "NEW_CROSS_TEAM_DEPENDENCY"
    assert alerts[0].severity == "medium"


def test_does_not_flag_same_team_dependency():
    added = [_edge("payments-service", "billing-service")]
    teams = {"payments-service": "payments-team", "billing-service": "payments-team"}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


def test_does_not_flag_when_either_service_unowned():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team", "auth-service": None}

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


def test_does_not_flag_when_team_unknown_entirely():
    added = [_edge("payments-service", "auth-service")]
    teams = {"payments-service": "payments-team"}  # auth-service not in the map at all

    alerts = check_new_cross_team_dependencies(added, teams)

    assert alerts == []


# --- HIGH_FAN_IN ---


def test_flags_high_fan_in_at_threshold():
    upstream = [_edge(f"service-{i}", "auth-service") for i in range(10)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=10)

    assert len(alerts) == 1
    assert alerts[0].type == "HIGH_FAN_IN"
    assert alerts[0].severity == "medium"


def test_does_not_flag_below_threshold():
    upstream = [_edge(f"service-{i}", "auth-service") for i in range(9)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=10)

    assert alerts == []


def test_counts_distinct_dependents_not_raw_edges():
    # Same dependent service, 3 separate import edges -- should count as 1.
    upstream = [_edge("payments-service", "auth-service") for _ in range(3)]

    alerts = check_high_fan_in("auth-service", upstream, threshold=2)

    assert alerts == []


# --- ORPHANED_SERVICE ---


def test_flags_service_with_no_edges_at_all():
    alerts = check_orphaned_service("leaf-service", downstream_edges=[], upstream_edges=[])

    assert len(alerts) == 1
    assert alerts[0].type == "ORPHANED_SERVICE"
    assert alerts[0].severity == "low"


def test_does_not_flag_service_with_only_downstream_edges():
    alerts = check_orphaned_service(
        "payments-service",
        downstream_edges=[_edge("payments-service", "auth-service")],
        upstream_edges=[],
    )

    assert alerts == []


def test_does_not_flag_service_with_only_upstream_edges():
    alerts = check_orphaned_service(
        "auth-service",
        downstream_edges=[],
        upstream_edges=[_edge("payments-service", "auth-service")],
    )

    assert alerts == []


# --- UNOWNED_SERVICE ---


def test_flags_service_with_no_team():
    alerts = check_unowned_service("payments-service", team=None)

    assert len(alerts) == 1
    assert alerts[0].type == "UNOWNED_SERVICE"
    assert alerts[0].severity == "low"


def test_does_not_flag_service_with_a_team():
    alerts = check_unowned_service("payments-service", team="payments-team")

    assert alerts == []


# --- BREAKING_CHANGE ---


def _sig(name, params):
    """params: list of (name, type) tuples for brevity in tests --
    converted to the real {"name", "type"} dict shape here."""
    return {"name": name, "params": [{"name": n, "type": t} for n, t in params]}


def test_flags_removed_public_function_when_service_has_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = []
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert alerts[0].type == "BREAKING_CHANGE"
    assert alerts[0].severity == "high"


def test_flags_parameter_count_change_when_service_has_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "int"), ("currency", "str")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert "createCharge" in alerts[0].message


def test_flags_type_change_at_same_position_when_both_sides_typed():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "str")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 1
    assert "int" in alerts[0].message and "str" in alerts[0].message


def test_does_not_flag_pure_parameter_rename():
    # Same position, same type, only the name differs -- most real
    # callers invoke positionally, so this is deliberately NOT breaking.
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("total", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_type_change_when_one_side_has_no_type_info():
    # Only the new side is typed -- there's nothing concrete to compare
    # against on the old side, so this isn't flagged as a type change
    # (gaining an annotation isn't itself a behavior change).
    old_sigs = [_sig("createCharge", [("amount", None)])]
    new_sigs = [_sig("createCharge", [("amount", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_when_service_has_no_dependents():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = []  # removed, but nobody depends on this service

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream_edges=[])

    assert alerts == []


def test_does_not_flag_unchanged_signature():
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [_sig("createCharge", [("amount", "int")])]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_does_not_flag_newly_added_function():
    # Adding a new public function isn't breaking -- only removing or
    # changing an existing one is.
    old_sigs = [_sig("createCharge", [("amount", "int")])]
    new_sigs = [
        _sig("createCharge", [("amount", "int")]),
        _sig("refundCharge", [("chargeId", "str")]),
    ]
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert alerts == []


def test_multiple_breaking_changes_all_reported():
    old_sigs = [
        _sig("createCharge", [("amount", "int")]),
        _sig("refundCharge", [("chargeId", "str")]),
    ]
    new_sigs = [
        _sig("createCharge", [("amount", "int"), ("currency", "str")])
    ]  # refundCharge removed, createCharge's param count changed
    upstream = [_edge("checkout-service", "payments-service")]

    alerts = check_breaking_changes("payments-service", old_sigs, new_sigs, upstream)

    assert len(alerts) == 2


# --- signature identity (review fix): same name != same function ---


def _qsig(qualname, params, name=None):
    return {
        "name": name or qualname.split("::")[-1].split(".")[-1],
        "qualname": qualname,
        "params": [{"name": n, "type": t} for n, t in params],
    }


_DEPENDENT = [{"source_service": "orders-service"}]


def test_same_method_name_in_two_classes_is_not_confused():
    old = [_qsig("a.py::A.get", [("x", "int")]), _qsig("a.py::B.get", [("x", "str")])]
    new = [_qsig("a.py::A.get", [("x", "int")]), _qsig("a.py::B.get", [("x", "str")])]
    assert check_breaking_changes("svc", old, new, _DEPENDENT) == []


def test_change_in_one_of_two_same_named_methods_is_attributed_correctly():
    old = [_qsig("a.py::A.get", [("x", "int")]), _qsig("a.py::B.get", [("x", "str")])]
    new = [_qsig("a.py::A.get", [("x", "int")]), _qsig("a.py::B.get", [("x", "bytes")])]
    alerts = check_breaking_changes("svc", old, new, _DEPENDENT)
    assert len(alerts) == 1
    assert "B.get" in alerts[0].message


def test_java_overloads_are_compared_as_a_group():
    old = [_qsig("C.java::C.find", [("id", "long")]), _qsig("C.java::C.find", [("n", "String"), ("l", "int")])]
    same = list(old)
    assert check_breaking_changes("svc", old, same, _DEPENDENT) == []

    dropped = [_qsig("C.java::C.find", [("id", "long")])]
    alerts = check_breaking_changes("svc", old, dropped, _DEPENDENT)
    assert len(alerts) == 1
    assert "2 parameter" in alerts[0].message


def test_legacy_snapshots_without_qualname_still_compare_by_name():
    old = [{"name": "charge", "params": [{"name": "a", "type": "int"}]}]
    new = [_qsig("pay.py::charge", [("a", "int")])]
    assert check_breaking_changes("svc", old, new, _DEPENDENT) == []
`````


## `tests/test_intelligence/test_diff.py`

`````python
from depatlas.intelligence.diff import compute_diff


def _edge(source, target, edge_type="import", source_file="charge.py", line=1, confidence="high"):
    return {
        "source_service": source,
        "target_service": target,
        "type": edge_type,
        "source_file": source_file,
        "source_line": line,
        "confidence": confidence,
    }


def test_new_edge_shows_up_as_added():
    old_edges = []
    new_edges = [_edge("payments-service", "auth-service")]

    diff = compute_diff(old_edges, new_edges)

    assert len(diff["added"]) == 1
    assert diff["added"][0]["target_service"] == "auth-service"
    assert diff["removed"] == []


def test_removed_edge_shows_up_as_removed():
    old_edges = [_edge("payments-service", "auth-service")]
    new_edges = []

    diff = compute_diff(old_edges, new_edges)

    assert diff["added"] == []
    assert len(diff["removed"]) == 1
    assert diff["removed"][0]["target_service"] == "auth-service"


def test_unchanged_edge_shows_up_in_neither():
    edge = _edge("payments-service", "auth-service")

    diff = compute_diff([edge], [edge])

    assert diff["added"] == []
    assert diff["removed"] == []


def test_confidence_only_change_is_not_reported_as_added_or_removed():
    # Same source/target/type/file/line, but confidence differs -- per
    # the module's documented scope, this is NOT a dependency
    # appearing/disappearing, so it should show up in neither list.
    old_edge = _edge("payments-service", "auth-service", confidence="low")
    new_edge = _edge("payments-service", "auth-service", confidence="high")

    diff = compute_diff([old_edge], [new_edge])

    assert diff["added"] == []
    assert diff["removed"] == []


def test_mixed_diff_reports_both_added_and_removed():
    old_edges = [_edge("payments-service", "auth-service")]
    new_edges = [_edge("payments-service", "shipping-service")]

    diff = compute_diff(old_edges, new_edges)

    assert len(diff["added"]) == 1
    assert diff["added"][0]["target_service"] == "shipping-service"
    assert len(diff["removed"]) == 1
    assert diff["removed"][0]["target_service"] == "auth-service"


def test_same_dependency_different_line_counts_as_both_added_and_removed():
    # If a line moves (e.g. code got reformatted), the old identity
    # disappears and a new one appears -- this is a known, accepted
    # limitation of line-based identity rather than a bug: tracking
    # "the same logical edge moved" would need more than line-number
    # matching (e.g. AST-level move detection), which is out of scope.
    old_edge = _edge("payments-service", "auth-service", line=10)
    new_edge = _edge("payments-service", "auth-service", line=12)

    diff = compute_diff([old_edge], [new_edge])

    assert len(diff["added"]) == 1
    assert len(diff["removed"]) == 1


def test_moved_line_is_not_a_new_dependency_when_ignoring_location():
    old = [{"source_service": "a", "target_service": "b", "type": "import",
            "source_file": "x.py", "source_line": 3, "confidence": "high"}]
    new = [{"source_service": "a", "target_service": "b", "type": "import",
            "source_file": "x.py", "source_line": 9, "confidence": "high"}]

    assert compute_diff(old, new)["added"] != []  # default still sees the move
    assert compute_diff(old, new, by_location=False) == {"added": [], "removed": []}
`````


## `tests/test_output/test_dot_export.py`

`````python
from depatlas.output.dot_export import export_dot


def test_produces_valid_digraph_wrapper():
    text = export_dot([], [])

    assert text.startswith("digraph depatlas {")
    assert text.endswith("}")


def test_node_with_team_includes_team_in_label():
    nodes = [{"id": "payments-service", "team": "payments-team"}]

    text = export_dot(nodes, [])

    assert '"payments-service"' in text
    assert "payments-team" in text


def test_node_without_team_has_no_team_line_in_label():
    nodes = [{"id": "payments-service", "team": None}]

    text = export_dot(nodes, [])

    assert '"payments-service" [label="payments-service"];' in text


def test_edge_includes_type_and_confidence():
    edges = [
        {
            "source_service": "payments-service",
            "target_service": "auth-service",
            "type": "import",
            "confidence": "high",
        }
    ]

    text = export_dot([], edges)

    assert '"payments-service" -> "auth-service"' in text
    assert "import (high)" in text


def test_quotes_in_service_names_are_escaped():
    nodes = [{"id": 'weird"service', "team": None}]

    text = export_dot(nodes, [])

    assert 'weird\\"service' in text
`````


## `tests/test_output/test_json_export.py`

`````python
import json

from depatlas.graph.models import DependencyEdge
from depatlas.output.json_export import export_edges, export_graph_json


def test_export_edges_produces_valid_json(tmp_path):
    edge = DependencyEdge(
        source_service="payments-service",
        target_service="auth-service",
        type="import",
        source_file="charge.py",
        source_line=1,
        confidence="high",
    )

    text = export_edges([edge])
    parsed = json.loads(text)

    assert parsed["edges"][0]["target_service"] == "auth-service"


def test_export_edges_writes_to_file_when_given_a_path(tmp_path):
    edge = DependencyEdge(
        source_service="payments-service",
        target_service="auth-service",
        type="import",
        source_file="charge.py",
        source_line=1,
        confidence="high",
    )
    output_path = tmp_path / "out.json"

    export_edges([edge], output_path)

    assert output_path.exists()
    assert json.loads(output_path.read_text())["edges"][0]["source_service"] == "payments-service"


def test_export_graph_json_includes_both_nodes_and_edges():
    nodes = [{"id": "payments-service", "team": "payments-team"}]
    edges = [
        {
            "source_service": "payments-service",
            "target_service": "auth-service",
            "type": "import",
            "confidence": "high",
        }
    ]

    text = export_graph_json(nodes, edges)
    parsed = json.loads(text)

    assert parsed["nodes"] == nodes
    assert parsed["edges"] == edges


def test_export_graph_json_writes_to_file_when_given_a_path(tmp_path):
    output_path = tmp_path / "graph.json"

    export_graph_json([], [], output_path)

    assert output_path.exists()
    parsed = json.loads(output_path.read_text())
    assert parsed == {"nodes": [], "edges": []}
`````


## `tests/test_ownership/test_codeowners.py`

`````python
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
`````


## `tests/test_ownership/test_manifest.py`

`````python
from pathlib import Path

from depatlas.ownership.manifest import parse_ownership_manifest


def _write_manifest(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "depatlas.yaml"
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
`````


## `tests/test_param_kinds.py`

`````python
"""Parameter kinds: Python (positional-only, keyword-only, *args, defaults)
and JavaScript/TypeScript (optional, default, rest), and the BREAKING_CHANGE
rules built on them."""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.intelligence import alerts
from depatlas.intelligence.alerts import _param_problems, check_breaking_changes
from depatlas.parsers import js_ts_parser, python_parser


def _py_params(tmp_path, signature: str) -> list[dict]:
    d = tmp_path / f"py{abs(hash(signature))}"  # signatures contain characters illegal in Windows paths
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.py").write_text(f"def f({signature}): pass\n")
    return python_parser.extract_signatures(d, "svc")[0].params


def _ts_params(tmp_path, signature: str) -> list[dict]:
    d = tmp_path / f"ts{abs(hash(signature))}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.ts").write_text(f"export function f({signature}): void {{}}\n")
    return js_ts_parser.extract_signatures(d, "svc")[0].params


# ---- extraction ----


def test_python_parameter_kinds_and_defaults_are_recorded(tmp_path):
    params = _py_params(tmp_path, "a, b: int = 1, /, c=2, *args, d, e=3, **kw")
    assert [(p["name"], p["kind"], p["optional"]) for p in params] == [
        ("a", "positional_only", False),
        ("b", "positional_only", True),
        ("c", "positional_or_keyword", True),
        ("args", "var_positional", True),
        ("d", "keyword_only", False),
        ("e", "keyword_only", True),
        ("kw", "var_keyword", True),
    ]
    assert params[1]["type"] == "int"


def test_typescript_types_optionality_and_rest_are_recorded(tmp_path):
    params = _ts_params(tmp_path, "this: Foo, a: number, b?: string, c = 1, ...rest: number[]")
    assert [(p["name"], p["type"], p["kind"], p["optional"]) for p in params] == [
        ("a", "number", "positional", False),
        ("b", "string", "positional", True),
        ("c", None, "positional", True),
        ("rest", "number[]", "var_positional", True),
    ]


def test_typescript_exports_arrows_classes_and_reexports(tmp_path):
    (tmp_path / "a.ts").write_text(
        "export const g = async (user: User, opts: Opts = {}) => user;\n"
        "export const h = x => x;\n"
        "function local(p: string) {}\n"
        "export { local, local as renamed };\n"
        "export class Svc {\n"
        "  public run(job: Job, retries?: number) {}\n"
        "  private hidden(x: number) {}\n"
        "  protected prot(x: number) {}\n"
        "  #priv(x) {}\n"
        "  static make(cfg: Cfg) {}\n"
        "  constructor(a: string) {}\n"
        "}\n"
        "class NotExported { m(a) {} }\n"
        "export function over(a: string): void;\n"
        "export function over(a: number): void;\n"
        "export function over(a: any) {}\n"
    )
    sigs = js_ts_parser.extract_signatures(tmp_path, "svc")
    names = sorted(s.qualname.split("::")[1] for s in sigs)
    assert names == ["Svc.make", "Svc.run", "g", "h", "local", "over", "over", "renamed"]
    over = [s for s in sigs if s.name == "over"]
    assert {p["type"] for s in over for p in s.params} == {"string", "number"}  # not the `any` body


def test_declaration_files_and_tests_are_not_public_api(tmp_path):
    (tmp_path / "types.d.ts").write_text("export declare function f(a: string): void;\n")
    (tmp_path / "a.test.ts").write_text("export function t(a: string) {}\n")
    assert js_ts_parser.extract_signatures(tmp_path, "svc") == []


# ---- the rules, on real source ----

PYTHON_BREAKING = [
    ("a", "a, b"),                      # new required parameter
    ("a, b", "a"),                      # parameter removed
    ("a", "b"),                         # renamed: keyword callers break
    ("a, b", "b, a"),                   # reordered
    ("a, b=1", "a, b"),                 # default removed
    ("a, b", "a, *, b"),                # now keyword-only: positional callers break
    ("a, b", "a, /, b"),                # a is now positional-only: keyword callers break
    ("a, *, k", "a"),                   # keyword-only removed
    ("a", "a, *, k"),                   # new required keyword-only
    ("*, k", "*, j"),                   # keyword-only renamed
    ("*args", ""),                      # *args removed
    ("**kw", ""),                       # **kwargs removed
    ("a: int", "a: str"),               # type changed
]
PYTHON_SAFE = [
    ("a", "a, b=1"),                    # optional parameter added
    ("a", "a, *, k=1"),                 # optional keyword-only added
    ("a", "a, *args"),                  # *args added
    ("a", "a, **kw"),                   # **kwargs added
    ("a, b", "a, b=1"),                 # became optional
    ("a, /", "x, /"),                   # positional-only rename
    ("a, /", "a"),                      # positional-only became ordinary
    ("*, k", "k"),                      # keyword-only became ordinary
    ("a", "a: int"),                    # gained an annotation
    ("a, b, /", "a, *args"),            # b absorbed by *args
    ("a, b=1", "a, b=1"),               # unchanged
    ("self, x", "self, x"),
]

TS_BREAKING = [
    ("a: number", "a: number, b: string"),
    ("a: number, b: string", "a: number"),
    ("a: number, b: string", "b: string, a: number"),
    ("a: number, b?: string", "a: number, b: string"),
    ("a: number", "a: string"),
    ("...rest: number[]", ""),
]
TS_SAFE = [
    ("a: number", "a: number, b?: string"),
    ("a: number", "a: number, b = 1"),
    ("a: number", "a: number, ...rest: number[]"),
    ("a: number", "x: number"),           # a JS caller cannot use the name
    ("a: number, b: string", "a: number, b?: string"),
    ("a", "a: number"),
]


@pytest.mark.parametrize("old,new", PYTHON_BREAKING)
def test_python_breaking(tmp_path, old, new):
    assert _param_problems(_py_params(tmp_path / "o", old), _py_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", PYTHON_SAFE)
def test_python_safe(tmp_path, old, new):
    assert not _param_problems(_py_params(tmp_path / "o", old), _py_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", TS_BREAKING)
def test_typescript_breaking(tmp_path, old, new):
    assert _param_problems(_ts_params(tmp_path / "o", old), _ts_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", TS_SAFE)
def test_typescript_safe(tmp_path, old, new):
    assert not _param_problems(_ts_params(tmp_path / "o", old), _ts_params(tmp_path / "n", new))


def test_messages_say_what_broke():
    old = [{"name": "a", "type": None, "kind": "positional_or_keyword", "optional": False}]
    new = old + [{"name": "b", "type": None, "kind": "positional_or_keyword", "optional": False}]
    assert "new required parameter 'b'" in _param_problems(old, new)


# ---- snapshots saved before kinds existed ----


def _sig(params, qual="m.py::f"):
    return {"service": "s", "name": "f", "params": params, "source_file": "m.py",
            "source_line": 1, "qualname": qual}


def test_old_snapshot_vs_new_does_not_cry_wolf_but_still_reports_removals():
    legacy = [{"name": "self", "type": None}, {"name": "a", "type": None}]
    kinded = [
        {"name": "self", "type": None, "kind": "positional_or_keyword", "optional": False},
        {"name": "a", "type": None, "kind": "positional_or_keyword", "optional": False},
        {"name": "b", "type": None, "kind": "keyword_only", "optional": True},
    ]
    dependents = [{"source_service": "other", "target_service": "s"}]
    assert check_breaking_changes("s", [_sig(legacy)], [_sig(kinded)], dependents) == []
    removed = check_breaking_changes("s", [_sig(legacy)], [], dependents)
    assert len(removed) == 1


def test_java_style_parameters_keep_the_old_rules():
    java = lambda *types: [{"name": f"p{i}", "type": t} for i, t in enumerate(types)]
    assert _param_problems(java("int"), java("int", "String"))     # count change
    assert _param_problems(java("int"), java("String"))            # type change
    assert not _param_problems(java("int"), [{"name": "renamed", "type": "int"}])


# ---- end to end: a TypeScript service in a git repo ----


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_check_catches_a_new_required_parameter_in_a_typescript_service(tmp_path):
    repo = tmp_path / "pay"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "charge.ts").write_text("export function charge(amount: number): boolean { return true; }\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "charge.ts").write_text(
        "export function charge(amount: number, currency: string): boolean { return true; }\n"
    )
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "add param")

    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "pay-service"}))
    graph = tmp_path / "g.json"
    graph.write_text(json.dumps({
        "nodes": [],
        "edges": [{"source_service": "web", "target_service": "pay-service", "type": "import",
                   "source_file": "x", "source_line": 1, "confidence": "high", "from_test": 0}],
    }))
    r = CliRunner().invoke(cli, ["check", "--repo", str(repo), "--base", "main", "--services",
                                 str(services), "--graph", str(graph), "--fail-on", "high"])
    assert r.exit_code == 3, r.output
    assert "currency" in r.output

    # An optional parameter is not a breaking change.
    (repo / "charge.ts").write_text(
        "export function charge(amount: number, currency?: string): boolean { return true; }\n"
    )
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "optional")
    r = CliRunner().invoke(cli, ["check", "--repo", str(repo), "--base", "main", "--services",
                                 str(services), "--graph", str(graph), "--fail-on", "high"])
    assert r.exit_code == 0, r.output
`````


## `tests/test_parsers/test_java_parser.py`

`````python
from pathlib import Path

from depatlas.parsers.java_parser import parse_repo

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


# --- extract_signatures ---


def test_extract_signatures_finds_public_methods_only(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures

    (tmp_path / "Service.java").write_text(
        "package com.org.payments;\n"
        "public class Service {\n"
        "    public void publicMethod(String userId, int amount) {}\n"
        "    private void privateHelper() {}\n"
        "}\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert len(signatures) == 1
    assert signatures[0].name == "publicMethod"
    assert signatures[0].params == [
        {"name": "userId", "type": "String"},
        {"name": "amount", "type": "int"},
    ]
    assert signatures[0].service == "payments-service"


def test_webclient_uri_call_and_field_concatenation(tmp_path):
    from depatlas.parsers.java_parser import parse_repo

    (tmp_path / "Client.java").write_text(
        """package x;
class Client {
    private String hostname = "http://visits-service/";
    void a(){ web.get().uri("http://customers-service/owners/1").retrieve(); }
    void b(){ web.get().uri(hostname + "pets").retrieve(); }
}
"""
    )
    edges = parse_repo(
        tmp_path,
        {"__self__": "gw"},
        {"customers-service": "customers-service", "visits-service": "visits-service"},
    )

    assert sorted(e.target_service for e in edges) == ["customers-service", "visits-service"]
`````


## `tests/test_parsers/test_js_ts_parser.py`

`````python
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
    # `require("auth_client/session")` in charge.js, plus the import in
    # typed.ts (real TypeScript, parsed by tree-sitter) resolve to auth-service.
    assert len(import_edges) == 3
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
    # 3 imports (2 in charge.js, 1 in typed.ts) + 1 fetch + 1 axios
    # + 1 package.json dep = 6
    edges = _scan()
    assert len(edges) == 6


def test_typescript_syntax_is_parsed_not_skipped(capsys):
    from depatlas.parsers.js_ts_parser import parse_file

    typed_file = FIXTURE_REPO / "src" / "typed.ts"
    edges = parse_file(typed_file, KNOWN_SERVICES, KNOWN_ENDPOINTS)

    assert [(e.type, e.target_service) for e in edges] == [("import", "auth-service")]
    assert capsys.readouterr().err == ""


def test_syntax_errors_still_yield_the_parts_that_parsed(tmp_path, capsys):
    from depatlas.parsers.js_ts_parser import parse_file

    f = tmp_path / "broken.ts"
    f.write_text('import a from "auth_client";\nfunction ((( {\n')

    edges = parse_file(f, KNOWN_SERVICES, KNOWN_ENDPOINTS)

    assert len(edges) == 1
    assert "syntax errors" in capsys.readouterr().err


def test_reexports_and_dynamic_imports_are_dependencies(tmp_path):
    from depatlas.parsers.js_ts_parser import parse_file

    f = tmp_path / "m.js"
    f.write_text(
        'export * from "auth_client";\n'
        'export { x } from "auth_client/session";\n'
        'const lazy = () => import("auth_client");\n'
    )
    assert len(parse_file(f, KNOWN_SERVICES, KNOWN_ENDPOINTS)) == 3


def test_plain_string_and_template_urls_but_not_computed_ones(tmp_path):
    from depatlas.parsers.js_ts_parser import parse_file

    f = tmp_path / "m.js"
    f.write_text(
        'fetch(`http://auth.internal/a`);\n'
        'fetch(`http://auth.internal/${id}`);\n'
        'fetch("http://" + host);\n'
    )
    assert len(parse_file(f, KNOWN_SERVICES, KNOWN_ENDPOINTS)) == 1


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
    assert [(p["name"], p["type"], p["kind"], p["optional"]) for p in signatures[0].params] == [
        ("token", None, "positional", False),
        ("options", None, "positional", False),
    ]


def test_extract_signatures_finds_export_default_function(tmp_path):
    from depatlas.parsers.js_ts_parser import extract_signatures

    (tmp_path / "handler.js").write_text(
        "export default function mainHandler(req, res) {\n" "  return res;\n" "}\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert len(signatures) == 1
    assert signatures[0].name == "mainHandler"
    assert [p["name"] for p in signatures[0].params] == ["req", "res"]
`````


## `tests/test_parsers/test_python_parser.py`

`````python
from pathlib import Path

from depatlas.parsers.python_parser import parse_repo

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
    from depatlas.parsers.python_parser import extract_signatures

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
    assert [p["name"] for p in public_func.params] == ["a", "b"]
    assert public_func.service == "payments-service"


def test_extract_signatures_excludes_underscore_prefixed_functions(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text("def _private_func(x):\n    return x\n")

    signatures = extract_signatures(tmp_path, "payments-service")

    assert signatures == []


def test_extract_signatures_excludes_nested_functions(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text(
        "def outer():\n" "    def inner_helper():\n" "        return 1\n" "    return inner_helper()\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")
    names = {s.name for s in signatures}

    assert names == {"outer"}


def test_extract_signatures_captures_type_annotations(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text(
        "def create_charge(amount: int, currency: str) -> bool:\n" "    return True\n"
    )

    signatures = extract_signatures(tmp_path, "payments-service")

    assert [(p["name"], p["type"]) for p in signatures[0].params] == [
        ("amount", "int"),
        ("currency", "str"),
    ]


def test_extract_signatures_untyped_params_get_none_type(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "service.py").write_text("def create_charge(amount):\n" "    return True\n")

    signatures = extract_signatures(tmp_path, "payments-service")

    assert [(p["name"], p["type"]) for p in signatures[0].params] == [("amount", None)]
`````


## `tests/test_review_round2.py`

`````python
"""Regression tests for the round-2 code review findings."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.graph.models import DependencyEdge
from depatlas.graph.store import GraphStore


def _edge(src, dst, file="a.py", line=1):
    return DependencyEdge(
        source_service=src, target_service=dst, type="import",
        source_file=file, source_line=line, confidence="high",
    )


# ---- non-UTF-8 files never abort a JavaScript scan ----


def test_js_non_utf8_file_does_not_abort_the_scan(tmp_path, capsys):
    from depatlas.parsers.js_ts_parser import extract_signatures, parse_repo

    (tmp_path / "good.js").write_text("import x from 'auth-lib';\n")
    (tmp_path / "bad.js").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "package.json").write_bytes(b"\xff\xfe\x00bad")

    edges = parse_repo(tmp_path, {"__self__": "me", "auth-lib": "auth-service"})
    sigs = extract_signatures(tmp_path, "me")

    assert len(edges) == 1
    assert sigs == []
    assert "skipping" in capsys.readouterr().err


# ---- test files and private classes are not public API ----


def test_python_tests_and_private_classes_are_not_public_signatures(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_charge.py").write_text("def test_charge_works():\n    pass\n")
    (tmp_path / "service.py").write_text(
        "def charge(a):\n    pass\n\nclass _Helper:\n    def run(self):\n        pass\n"
    )

    assert [s.name for s in extract_signatures(tmp_path, "svc")] == ["charge"]


def test_java_tests_and_package_private_classes_are_not_public_signatures(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures

    test_dir = tmp_path / "src" / "test" / "java"
    test_dir.mkdir(parents=True)
    (test_dir / "ChargeTest.java").write_text("public class ChargeTest { public void works() {} }\n")
    main_dir = tmp_path / "src" / "main" / "java"
    main_dir.mkdir(parents=True)
    (main_dir / "Api.java").write_text("public class Api { public void charge(int a) {} }\n")
    (main_dir / "Internal.java").write_text("class Internal { public void helper() {} }\n")

    assert [s.name for s in extract_signatures(tmp_path, "svc")] == ["charge"]


# ---- stored paths are repo-relative, so moving the repo causes no churn ----


def test_edge_paths_are_relative_and_survive_moving_the_repo(tmp_path):
    from depatlas.intelligence.diff import compute_diff
    from depatlas.parsers.python_parser import parse_repo

    services = {"__self__": "me", "auth_client": "auth-service"}
    first = tmp_path / "one"
    first.mkdir()
    (first / "m.py").write_text("import auth_client\n")
    second = tmp_path / "elsewhere" / "copy"
    shutil.copytree(first, second)

    e1 = parse_repo(first, services)
    e2 = parse_repo(second, services)

    assert e1[0].source_file == "m.py"
    d = compute_diff([e.to_dict() for e in e1], [e.to_dict() for e in e2])
    assert d == {"added": [], "removed": []}


# ---- self-dependencies are dropped ----


def test_a_service_importing_its_own_package_is_not_a_dependency(tmp_path):
    from depatlas.parsers.python_parser import parse_repo

    (tmp_path / "m.py").write_text("import payments_pkg\nimport auth_client\n")
    edges = parse_repo(
        tmp_path,
        {"__self__": "payments-service", "payments_pkg": "payments-service",
         "auth_client": "auth-service"},
    )
    assert [e.target_service for e in edges] == ["auth-service"]


# ---- URL resolution: hostname only, longest first, no generic names ----


def test_url_guess_uses_the_hostname_only():
    from depatlas.resolution import resolve_endpoint

    services = {"__self__": "gateway", "a": "auth-service", "p": "payments-service"}
    assert resolve_endpoint("https://payments.example.com/api/auth/login", {}, services) == (
        "payments-service", "low",
    )
    assert resolve_endpoint("https://example.com/auth/login", {}, services) == (None, None)
    assert resolve_endpoint("/auth/login", {}, services) == (None, None)


def test_url_guess_prefers_the_longest_service_name_and_skips_generic_ones():
    from depatlas.resolution import resolve_endpoint

    services = {"__self__": "gw", "a": "auth-service", "b": "auth-admin-service", "c": "api-service"}
    assert resolve_endpoint("http://auth-admin/x", {}, services) == ("auth-admin-service", "low")
    assert resolve_endpoint("http://api.example.com/x", {}, services) == (None, None)


def test_registry_patterns_match_at_host_boundaries():
    from depatlas.resolution import resolve_endpoint

    endpoints = {"auth.internal": "auth-service"}
    assert resolve_endpoint("http://auth.internal/v", endpoints, {})[0] == "auth-service"
    assert resolve_endpoint("http://myauth.internal.example/v", endpoints, {})[0] is None


# ---- stored repo keys ----


def test_snapshots_and_diff_find_github_style_repo_keys(tmp_path):
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.save_snapshot("github:org/payments", [_edge("p", "a")])
    store.save_snapshot("github:org/payments", [_edge("p", "a"), _edge("p", "b", line=2)])
    store.close()

    runner = CliRunner()
    snaps = runner.invoke(cli, ["snapshots", "--repo", "github:org/payments", "--db", str(db)])
    diff = runner.invoke(cli, ["diff", "--last", "2", "--repo", "github:org/payments", "--db", str(db)])

    assert snaps.exit_code == 0 and "github:org/payments" in snaps.output
    assert diff.exit_code == 0 and "Not enough snapshots" not in diff.output


# ---- a repo with several languages is one service, one snapshot ----


def test_scan_with_several_languages_saves_one_combined_snapshot(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "a.py").write_text("import auth_client\n")
    (repo / "B.java").write_text("package x;\nimport com.org.ledger.Client;\nclass B {}\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps(
        {"__self__": "svc", "auth_client": "auth-service", "com.org.ledger": "ledger-service"}
    ))
    db = tmp_path / "g.db"
    args = ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db),
            "--language", "all"]
    runner = CliRunner()
    assert runner.invoke(cli, args).exit_code == 0
    assert runner.invoke(cli, args).exit_code == 0

    store = GraphStore(db)
    targets = sorted(e["target_service"] for e in store.all_edges())
    snaps = store.list_snapshots()
    store.close()
    assert targets == ["auth-service", "ledger-service"]
    d = runner.invoke(cli, ["diff", "--last", "2", "--repo", str(repo), "--db", str(db)])
    assert "auth-service" not in d.output  # nothing changed between identical scans
    assert len(snaps) == 2


# ---- store: atomic scans, legacy rows, placeholders ----


def test_transaction_rolls_back_every_write_on_failure(tmp_path):
    from depatlas.graph.models import ServiceNode

    store = GraphStore(tmp_path / "g.db")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert_services([ServiceNode(id="a", name="a", repo="/r", language="python")])
            store.replace_edges_for_repo("/r", [_edge("a", "b", "/r/x.py")])
            raise RuntimeError("crash mid-scan")

    assert store.all_services() == [] and store.all_edges() == []
    store.close()


def test_legacy_edges_under_a_longer_path_survive_a_shorter_repo_rescan(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE edges (source_service TEXT NOT NULL, target_service TEXT NOT NULL,
        type TEXT NOT NULL, source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
        confidence TEXT NOT NULL, detected_at_sha TEXT);
        INSERT INTO edges VALUES ('p','a','import','/repos/payments/x.py',1,'high',NULL);"""
    )
    conn.commit(); conn.close()

    store = GraphStore(db)
    store.replace_edges_for_repo("/repos/pay", [])
    assert len(store.all_edges()) == 1  # /repos/pay must not delete /repos/payments/...
    store.close()


def test_dropped_dependency_placeholder_is_removed(tmp_path):
    from depatlas.graph.models import ServiceNode

    store = GraphStore(tmp_path / "g.db")
    store.upsert_services([
        ServiceNode(id="p", name="p", repo="/r", language="python"),
        ServiceNode(id="auth", name="auth", repo="", language=""),
    ])
    store.replace_edges_for_repo("/r", [_edge("p", "auth", "/r/x.py")])
    assert {s["id"] for s in store.all_services()} == {"p", "auth"}

    store.replace_edges_for_repo("/r", [])  # payments stopped depending on auth
    assert {s["id"] for s in store.all_services()} == {"p"}
    store.close()


# ---- CodeCommit: one pinned commit for the whole walk ----


def test_codecommit_pins_every_call_to_the_first_commit(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo
    from depatlas.gitinfo import get_sha

    calls = []

    class Client:
        def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
            calls.append(("folder", folderPath, commitSpecifier))
            if folderPath == "/":
                return {"commitId": "c1", "files": [{"absolutePath": "/a.py"}],
                        "subFolders": [{"absolutePath": "/sub"}]}
            return {"files": [{"absolutePath": "/sub/b.py"}], "subFolders": []}

        def get_file(self, repositoryName, filePath, commitSpecifier=None):
            calls.append(("file", filePath, commitSpecifier))
            return {"fileContent": b"x"}

    result = fetch_repo("r", Client(), tmp_path)

    assert calls[0][2] is None
    assert all(c[2] == "c1" for c in calls[1:])
    assert get_sha(result) == "c1"


def test_codecommit_empty_repo_does_not_inherit_a_previous_sha(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo
    from depatlas.gitinfo import get_sha, write_sha

    (tmp_path / "r").mkdir()
    write_sha(tmp_path / "r", "stale")

    class Empty:
        def get_folder(self, **kwargs):
            return {"files": [], "subFolders": []}

    assert get_sha(fetch_repo("r", Empty(), tmp_path)) is None


# ---- CLI: --version and --fail-on ----


def test_version_flag():
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0 and "version" in r.output.lower()


def test_fail_on_exits_nonzero_only_when_an_alert_reaches_the_threshold(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc"}))
    db = tmp_path / "g.db"
    base = ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)]
    runner = CliRunner()

    # An unowned, orphaned service raises only LOW alerts.
    assert runner.invoke(cli, base + ["--fail-on", "high"]).exit_code == 0
    assert runner.invoke(cli, base + ["--fail-on", "low"]).exit_code == 3
    assert runner.invoke(cli, ["alerts", "--db", str(db), "--fail-on", "low"]).exit_code == 3
    assert runner.invoke(cli, ["alerts", "--db", str(db), "--fail-on", "high"]).exit_code == 0
`````


## `tests/test_review_round3.py`

`````python
"""Regression tests for the round-3 code review findings."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
import requests
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.fsutil import is_test_path
from depatlas.gitinfo import get_sha, write_sha
from depatlas.graph.models import DependencyEdge
from depatlas.graph.store import GraphStore
from depatlas.intelligence.alerts import (
    check_high_fan_in,
    check_new_cross_team_dependencies,
    check_orphaned_service,
    comparable_signatures,
)
from depatlas.resolution import resolve_endpoint


def _edge(src, dst, file="a.py", line=1, from_test=False):
    return DependencyEdge(
        source_service=src, target_service=dst, type="import",
        source_file=file, source_line=line, confidence="high", from_test=from_test,
    )


# ---- 1. legacy edges of remote repos are replaced, not duplicated ----


def test_legacy_null_repo_edges_of_a_remote_repo_are_deleted(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE edges (
            source_service TEXT NOT NULL, target_service TEXT NOT NULL,
            type TEXT NOT NULL, source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL, confidence TEXT NOT NULL,
            detected_at_sha TEXT
        );
        INSERT INTO edges VALUES
          ('pay','auth','import','/work/.depatlas/cache/pay/x.py',1,'high',NULL),
          ('other','auth','import','/work/other/y.py',1,'high',NULL);
        """
    )
    conn.commit()
    conn.close()

    s = GraphStore(db)
    s.replace_edges_for_repo("github:acme/pay", [_edge("pay", "auth", "x.py")], self_service="pay")
    assert len(s.edges_for_service("pay", direction="downstream")) == 1  # not 2
    assert len(s.edges_for_service("other", direction="downstream")) == 1  # untouched
    s.close()


# ---- 2. CodeCommit pins to a commit id even when --ref is a branch ----


def test_codecommit_pins_to_commit_id_when_ref_is_a_branch(tmp_path):
    from depatlas.connectors.codecommit import fetch_repo

    calls = []

    class Client:
        def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
            calls.append(commitSpecifier)
            if folderPath == "/":
                return {"commitId": "c1", "files": [], "subFolders": [{"absolutePath": "/s"}]}
            return {"files": [{"absolutePath": "/s/b.py"}], "subFolders": []}

        def get_file(self, repositoryName, filePath, commitSpecifier=None):
            calls.append(commitSpecifier)
            return {"fileContent": b"x"}

    result = fetch_repo("r", Client(), tmp_path, commit_specifier="main")

    assert calls[0] == "main"
    assert calls[1:] == ["c1", "c1"]
    assert get_sha(result) == "c1"


# ---- 3. Java anonymous classes are not public API ----


def test_java_anonymous_class_methods_are_not_signatures(tmp_path):
    from depatlas.parsers.java_parser import extract_signatures

    (tmp_path / "Outer.java").write_text(
        "public class Outer {\n"
        "  public void real() {}\n"
        "  void use() { Runnable r = new Runnable() { public void run() {} }; }\n"
        "  enum E { A { public void f() {} } }\n"
        "}\n"
    )
    names = {s.name for s in extract_signatures(tmp_path, "svc")}
    assert "real" in names
    assert "run" not in names and "f" not in names


# ---- 4. --fail-on semantics ----


def _scan_args(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir(exist_ok=True)
    (repo / "a.py").write_text("x = 1\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc"}))
    db = tmp_path / "g.db"
    return ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)], db


def test_severity_filter_does_not_hide_alerts_from_fail_on(tmp_path):
    base, db = _scan_args(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, base)
    # Only LOW alerts exist here; filtering to a severity with none must not
    # change the exit decision for --fail-on low.
    r = runner.invoke(cli, ["alerts", "--db", str(db), "--severity", "high", "--fail-on", "low"])
    assert r.exit_code == 3
    r = runner.invoke(cli, ["alerts", "--db", str(db), "--severity", "low", "--fail-on", "high"])
    assert r.exit_code == 0


def test_no_alerts_with_fail_on_is_a_usage_error(tmp_path):
    base, _ = _scan_args(tmp_path)
    r = CliRunner().invoke(cli, base + ["--no-alerts", "--fail-on", "high"])
    assert r.exit_code == 2


# ---- 5. resolver: scheme required for guesses; boundaries; longest first ----


def test_scheme_less_text_is_not_a_hostname_guess():
    services = {"__self__": "me", "orders-service": "orders-service"}
    assert resolve_endpoint("orders/123", {}, services) == (None, None)
    assert resolve_endpoint("http://orders/123", {}, services) == ("orders-service", "low")


def test_registry_match_has_a_trailing_boundary():
    services = {"__self__": "me"}
    reg = {"auth.internal": "auth-service"}
    assert resolve_endpoint("http://auth.internal/x", reg, services)[0] == "auth-service"
    assert resolve_endpoint("http://auth.internal:8080/x", reg, services)[0] == "auth-service"
    assert resolve_endpoint("http://auth.internal.evil.com/", reg, services) == (None, None)
    assert resolve_endpoint("http://auth.internalx/", reg, services) == (None, None)


def test_longest_registry_pattern_wins():
    services = {"__self__": "me"}
    reg = {"/api": "gateway", "/api/payments": "payments"}
    assert resolve_endpoint("http://h/api/payments/1", reg, services)[0] == "payments"


# ---- 6. broken symlinks never crash a scan ----


def test_broken_symlink_does_not_crash_python_parser(tmp_path):
    from depatlas.parsers.python_parser import extract_signatures, parse_repo

    (tmp_path / "ok.py").write_text("import auth_lib\n")
    try:
        os.symlink(tmp_path / "missing.py", tmp_path / "dangling.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    parse_repo(tmp_path, {"__self__": "me", "auth_lib": "auth-service"})
    extract_signatures(tmp_path, "me")


# ---- 7. SHA handling ----


def test_cache_dir_inside_a_git_project_does_not_report_the_projects_head(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n",
         "commit", "-q", "--allow-empty", "-m", "x"], check=True,
    )
    cache = tmp_path / ".depatlas" / "cache" / "r"
    cache.mkdir(parents=True)
    write_sha(cache, None)  # a connector that has no SHA
    assert get_sha(cache) is None
    assert get_sha(tmp_path) is not None  # a real checkout still works


def test_github_uses_full_sha_from_zip_comment(tmp_path, monkeypatch):
    from depatlas.connectors.github import fetch_repo

    full = "a" * 40
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("acme-r-aaaaaaa/x.py", "x = 1\n")
        zf.comment = full.encode()

    class R:
        status_code = 200
        content = buf.getvalue()
        text = ""

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert get_sha(fetch_repo("acme", "r", "tok", tmp_path)) == full


def test_github_zip_without_single_top_folder_does_not_inherit_old_sha(tmp_path, monkeypatch):
    from depatlas.connectors.github import fetch_repo

    write_sha(tmp_path / "r", "stale")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("one/x.py", "x")
        zf.writestr("two/y.py", "y")

    class R:
        status_code = 200
        content = buf.getvalue()
        text = ""

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert get_sha(fetch_repo("acme", "r", "tok", tmp_path)) is None


# ---- 9. languages in snapshots ----


def test_language_set_change_does_not_flood_breaking_changes():
    sig = lambda f: {"service": "s", "name": "f", "params": [], "source_file": f, "source_line": 1}
    old = {"languages": ["java", "python"], "signatures": [sig("a.py"), sig("A.java")]}
    new = {"languages": ["java"], "signatures": [sig("A.java")]}
    o, n, skipped = comparable_signatures(old, new)
    assert [s["source_file"] for s in o] == ["A.java"]
    assert skipped == ["python"]
    # unchanged language sets, and pre-upgrade snapshots, compare everything
    assert comparable_signatures(old, old)[2] == []
    assert comparable_signatures({"signatures": old["signatures"]}, new)[0] == old["signatures"]


def test_snapshot_stores_languages_and_scan_detects_them(tmp_path):
    base, db = _scan_args(tmp_path)
    r = CliRunner().invoke(cli, base)
    assert r.exit_code == 0 and "Detected language(s): python" in r.output
    s = GraphStore(db)
    assert s.get_snapshot(s.list_snapshots()[0]["id"])["languages"] == ["python"]
    s.close()


# ---- 10. test paths and test edges ----


def test_test_path_rules_are_language_specific():
    assert not is_test_path(Path("src/main/java/com/org/test/Api.java"))
    assert not is_test_path(Path("src/main/java/Contest.java"))
    assert is_test_path(Path("src/test/java/com/org/Api.java"))
    assert is_test_path(Path("src/main/java/OrderIT.java"))
    assert is_test_path(Path("pkg/conftest.py"))
    assert is_test_path(Path("pkg/tests/helpers.py"))
    assert is_test_path(Path("web/Button.spec.tsx"))
    assert not is_test_path(Path("web/inspect.ts"))


def test_test_edges_are_ignored_by_alerts():
    teams = {"a": "x", "b": "y"}
    assert check_new_cross_team_dependencies([_edge("a", "b", from_test=True).to_dict()], teams) == []
    ups = [_edge(f"d{i}", "b", from_test=True).to_dict() for i in range(12)]
    assert check_high_fan_in("b", ups) == []
    assert check_orphaned_service("b", [], ups)  # only test edges -> still orphaned


def test_scan_tags_edges_from_test_files(tmp_path):
    repo = tmp_path / "svc"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("import auth_lib\n")
    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "svc", "auth_lib": "auth-service"}))
    db = tmp_path / "g.db"
    CliRunner().invoke(cli, ["scan", "--repo", str(repo), "--services", str(services), "--db", str(db)])
    s = GraphStore(db)
    edges = s.edges_for_service("svc")
    s.close()
    assert edges and all(e["from_test"] == 1 for e in edges)


# ---- 11. concurrency settings ----


def test_store_uses_wal(tmp_path):
    s = GraphStore(tmp_path / "g.db")
    assert s._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    s.close()


# ---- 12. team ownership ----


def test_removing_an_owner_clears_the_team_only_when_ownership_was_supplied(tmp_path):
    from depatlas.graph.models import ServiceNode

    s = GraphStore(tmp_path / "g.db")
    node = lambda team: ServiceNode(id="p", name="p", repo="/r", language="python", team=team)
    s.upsert_services([node("core")], ownership_inputs_supplied=True)
    s.upsert_services([node(None)])  # no ownership inputs: keep
    assert s.all_services()[0]["team"] == "core"
    s.upsert_services([node(None)], ownership_inputs_supplied=True)  # owner removed
    assert s.all_services()[0]["team"] is None
    s.close()
`````


## `tests/test_review_round4.py`

`````python
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
`````


## `tests/test_review_round5.py`

`````python
"""Round-5 (external) review findings."""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.fsutil import MAX_SOURCE_BYTES, iter_files
from depatlas.ownership.codeowners import OwnershipRule, resolve_owner
from depatlas.parsers.js_ts_parser import parse_repo as parse_js
from depatlas.parsers.python_parser import parse_repo as parse_python

ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# ---- 1. relative / own-module imports are never another service ----

SERVICES = {"__self__": "me", "orders": "orders"}


def test_python_relative_import_is_not_another_service(tmp_path):
    _write(tmp_path / "me" / "routes.py", "from .orders import Order\nfrom ..orders import X\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


def test_python_absolute_import_still_links(tmp_path):
    _write(tmp_path / "me" / "routes.py", "from orders.client import Order\n")
    assert [e.target_service for e in parse_python(tmp_path, SERVICES, {})] == ["orders"]


def test_python_import_of_a_module_this_repo_defines_is_its_own(tmp_path):
    _write(tmp_path / "orders.py", "x = 1\n")
    _write(tmp_path / "app.py", "import orders\nfrom orders import x\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


def test_python_own_package_under_src_is_its_own(tmp_path):
    _write(tmp_path / "src" / "orders" / "__init__.py", "")
    _write(tmp_path / "src" / "me" / "app.py", "import orders\n")
    assert parse_python(tmp_path, SERVICES, {}) == []


@pytest.mark.parametrize("spec", ["./orders", "../orders/x", ".", "..", "/orders"])
def test_js_relative_specifier_is_not_another_service(tmp_path, spec):
    _write(tmp_path / "a.js", f'import o from "{spec}";\n')
    assert parse_js(tmp_path, SERVICES, {}) == []


def test_js_bare_specifier_still_links(tmp_path):
    _write(tmp_path / "a.js", 'import o from "orders/client";\n')
    assert [e.target_service for e in parse_js(tmp_path, SERVICES, {})] == ["orders"]


# ---- 4. CODEOWNERS folder patterns ----


@pytest.mark.parametrize(
    "pattern, path, matches",
    [
        ("docs/", "docs/a.md", True),
        ("docs/", "docs/deep/a.md", True),
        ("docs/", "x/docs/a.md", True),  # unanchored folder: any depth
        ("docs/", "docsx/a.md", False),
        ("/docs/", "docs/a.md", True),
        ("/docs/", "x/docs/a.md", False),  # anchored: root only
        ("/docs", "docs/a.md", True),  # anchored name, no slash: file or folder
        ("apps/payments/", "apps/payments/s.py", True),
        ("apps/payments/", "other/apps/payments/s.py", False),
        ("apps/payments", "apps/payments/s.py", True),
        ("*.py", "a/b.py", True),
        ("*.py", "a/b.js", False),
        ("build*", "src/build.py", True),
        ("Makefile", "sub/Makefile", True),
        ("Makefile", "sub/Makefile2", False),
        ("/Makefile", "sub/Makefile", False),
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "other/a.md", False),
    ],
)
def test_codeowners_patterns(pattern, path, matches):
    owner = resolve_owner(path, Path("/repo"), [OwnershipRule(pattern, "@t")])
    assert (owner == "@t") is matches


def test_codeowners_last_matching_rule_wins():
    rules = [OwnershipRule("*", "@all"), OwnershipRule("apps/payments/", "@pay")]
    assert resolve_owner("apps/payments/s.py", Path("/r"), rules) == "@pay"
    assert resolve_owner("apps/other/s.py", Path("/r"), rules) == "@all"


# ---- 2/5/8. check ----


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True,
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


SVC = {"__self__": "pay-service", "auth_client": "auth-service"}


def _graph(tmp_path, dependents=("billing-service",), own_edges=()):
    teams = {"pay-service": "payments", "auth-service": "identity", "billing-service": "money"}
    edges = [
        {"source_service": d, "target_service": "pay-service", "type": "import",
         "source_file": "x.py", "source_line": 1, "confidence": "high", "from_test": 0}
        for d in dependents
    ] + [
        {"source_service": "pay-service", "target_service": t, "type": "import",
         "source_file": "old.py", "source_line": 1, "confidence": "high", "from_test": 0}
        for t in own_edges
    ]
    path = tmp_path / "org.json"
    path.write_text(json.dumps({"nodes": [{"id": k, "team": v} for k, v in teams.items()], "edges": edges}))
    return path


def _check(tmp_path, repo, *extra):
    sp = tmp_path / "services.json"
    sp.write_text(json.dumps(SVC))
    return CliRunner().invoke(
        cli, ["check", "--repo", str(repo), "--base", "main", "--services", str(sp), *extra]
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pay"
    _init(r)
    _write(r / "charge.py", "def charge(amount, currency):\n    return amount\n")
    _commit(r, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    return r


def test_check_ignores_files_that_are_not_committed(tmp_path, repo):
    """A generated / git-ignored file on disk exists only on the head side
    when the head is read from the working tree -- a false 'new dependency'."""
    _write(repo / ".gitignore", "generated_pb2.py\n")
    _commit(repo, "ignore generated")
    _write(repo / "generated_pb2.py", "import auth_client\n")  # e.g. built by an earlier CI step
    r = _check(tmp_path, repo, "--graph", str(_graph(tmp_path)), "--fail-on", "medium")
    assert r.exit_code == 0, r.output
    assert "NEW_CROSS_TEAM_DEPENDENCY" not in r.output


def test_export_ignore_does_not_hide_a_breaking_change(tmp_path):
    """`git archive` leaves export-ignore files out, so the base never saw
    legacy.py and its changed signature looked like a brand-new function."""
    r0 = tmp_path / "pay"
    _init(r0)
    _write(r0 / ".gitattributes", "legacy.py export-ignore\n")
    _write(r0 / "legacy.py", "def old_api(a):\n    return a\n")
    _commit(r0, "base")
    _git(r0, "checkout", "-q", "-b", "feature")
    (r0 / "legacy.py").write_text("def old_api(a, b):\n    return a\n")
    _commit(r0, "change")
    r = _check(tmp_path, r0, "--graph", str(_graph(tmp_path)), "--fail-on", "high")
    assert r.exit_code == 3, r.output
    assert "old_api" in r.output


def test_uncommitted_changes_are_reported_as_skipped_and_opt_in(tmp_path, repo):
    (repo / "charge.py").write_text("def other(x):\n    return x\n")  # removes charge(), uncommitted
    graph = _graph(tmp_path)

    default = _check(tmp_path, repo, "--graph", str(graph), "--fail-on", "high")
    assert default.exit_code == 0, default.output
    assert "uncommitted changes are not checked" in default.output

    opted_in = _check(tmp_path, repo, "--graph", str(graph), "--fail-on", "high", "--working-tree")
    assert opted_in.exit_code == 3, opted_in.output
    assert "uncommitted changes are not checked" not in opted_in.output


def test_a_new_folder_uses_the_org_graph_as_its_baseline(tmp_path):
    mono = tmp_path / "mono"
    _init(mono)
    (mono / "README.md").write_text("x")
    _commit(mono, "base")
    _git(mono, "checkout", "-q", "-b", "feature")
    svc = mono / "pay"
    svc.mkdir()
    (svc / "client.py").write_text("import auth_client\n")
    _commit(mono, "move service here")

    # The graph already knows pay-service -> auth-service: not a new dependency.
    known = _check(
        tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=(), own_edges=("auth-service",))),
        "--fail-on", "medium",
    )
    assert known.exit_code == 0, known.output
    assert "baseline" in known.output

    # A genuinely new service has no edges in the graph: still all-new.
    new = _check(tmp_path, svc, "--graph", str(_graph(tmp_path, dependents=())), "--fail-on", "medium")
    assert new.exit_code == 3, new.output


def test_fail_on_without_a_graph_is_a_usage_error(tmp_path, repo):
    r = _check(tmp_path, repo, "--fail-on", "high")
    assert r.exit_code == 2
    assert "--graph" in r.output


def test_check_without_fail_on_or_graph_still_runs(tmp_path, repo):
    r = _check(tmp_path, repo)
    assert r.exit_code == 0, r.output


# ---- 3/6/7. workflows ----


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / "examples" / "github-actions" / name).read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_nightly_refuses_to_publish_from_a_public_repo():
    steps = _steps(_workflow("depatlas-nightly.yml"))
    guard = next(i for i, s in enumerate(steps) if "isPrivate" in s.get("run", ""))
    scan = next(i for i, s in enumerate(steps) if "scan-all" in s.get("run", ""))
    assert guard < scan  # checked before any token is used
    assert "exit 1" in steps[guard]["run"]


def test_pr_check_uses_its_own_narrow_token_not_the_nightly_one():
    pr = (ROOT / "examples" / "github-actions" / "depatlas-pr-check.yml").read_text(encoding="utf-8")
    nightly = (ROOT / "examples" / "github-actions" / "depatlas-nightly.yml").read_text(encoding="utf-8")
    assert "DEPATLAS_GRAPH_TOKEN" in pr and "DEPATLAS_READ_TOKEN" not in pr.replace("# ", "").split("jobs:")[1]
    assert "DEPATLAS_READ_TOKEN" in nightly
    assert "fork" in pr.lower()  # the skipped-check caveat is explained


def test_workflows_pin_the_released_version():
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    for name in ("depatlas-nightly.yml", "depatlas-pr-check.yml"):
        installs = [s["run"] for s in _steps(_workflow(name)) if "pip install" in s.get("run", "")]
        assert installs == [f"pip install depatlas=={version}"], name


def test_grammar_packages_are_bounded():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("tree-sitter-java", "tree-sitter-javascript", "tree-sitter-typescript"):
        assert re.search(rf'"{name}>=[\d.]+,<[\d.]+"', text), name


# ---- minor: file size cap ----


def test_oversized_source_files_are_skipped_with_a_warning(tmp_path, capsys):
    (tmp_path / "small.js").write_text("x\n")
    big = tmp_path / "bundle.min.js"
    big.write_text("a" * (MAX_SOURCE_BYTES + 1))
    assert [p.name for p in iter_files(tmp_path, "*.js")] == ["small.js"]
    assert "bundle.min.js" in capsys.readouterr().err
`````


## `tests/test_scan_all.py`

`````python
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
`````
