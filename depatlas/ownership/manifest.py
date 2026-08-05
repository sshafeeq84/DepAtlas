"""depatlas.yaml ownership manifest (SPEC.md section 8, priority 2:
"depatlas.yaml manifest — explicit service-to-team mapping, for repos
without CODEOWNERS").

This is deliberately much simpler than the CODEOWNERS parser: CODEOWNERS
maps file *patterns* to owners and needs per-file resolution + a
majority vote to name a single team for a service. A depatlas.yaml
manifest instead states the team directly, one service name -> one team,
so there's nothing to infer -- just a lookup.

Expected shape (SPEC.md section 8's example):

    version: 1
    services:
      - name: payments-service
        repo: org/payments
        team: payments-team
        entry_points:
          - src/main/java/com/org/payments/PaymentsApplication.java
      - name: auth-service
        repo: org/auth
        team: platform-team

Only `name` and `team` are used for ownership resolution here; `repo`
and `entry_points` are part of the spec's fuller manifest shape (service
identity / entry-point declarations) but aren't consumed by this v1
slice -- ownership is the only thing currently missing a fallback.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def parse_ownership_manifest(path: Path) -> dict[str, str]:
    """Parse a depatlas.yaml manifest into a {service_name: team} mapping.

    Entries missing a `name` or `team` are skipped rather than raising --
    a manifest is allowed to declare a service without a team yet (e.g.
    mid-migration), and that service simply won't resolve via this
    fallback, same as if the manifest didn't mention it at all.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("services") or []

    mapping: dict[str, str] = {}
    for entry in entries:
        name = entry.get("name")
        team = entry.get("team")
        if name and team:
            mapping[name] = team

    return mapping
