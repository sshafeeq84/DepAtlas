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
