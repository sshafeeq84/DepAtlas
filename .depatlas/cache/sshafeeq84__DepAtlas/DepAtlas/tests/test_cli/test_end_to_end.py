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
