"""Alert system (SPEC.md section 10, "Alert Definitions").

Implemented -- all four are directly derivable from the data this
project already tracks (DependencyEdge, ServiceNode, and diffs between
snapshots):

  - NEW_CROSS_TEAM_DEPENDENCY (Medium): a newly-added edge (from a diff)
    whose source and target services have different, both-resolved
    teams.
  - HIGH_FAN_IN (Medium): a service's upstream (dependent) count meets
    or exceeds a configurable threshold (default 10, per SPEC.md's
    alert table).
  - ORPHANED_SERVICE (Low): a service has zero edges in either
    direction -- no dependents, no dependencies.
  - UNOWNED_SERVICE (Low): a service has no resolved team.

NOT implemented: BREAKING_CHANGE (High). SPEC.md defines its trigger as
"a public interface removed or signature changed in a service with
known dependents." That requires tracking function/method signatures
across scans -- a fundamentally different kind of analysis than the
dependency-edge model this project builds (DependencyEdge records "X
depends on Y," not "what Y's public interface looks like"). It isn't
derivable from the data this project currently collects; adding it
would mean a new signature-extraction layer in every language parser.
This is a deliberate, documented scope boundary -- see the README.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_HIGH_FAN_IN_THRESHOLD = 10


@dataclass
class Alert:
    type: str
    severity: str  # "high" | "medium" | "low"
    service: str
    message: str
    related_service: str | None = None


def check_new_cross_team_dependencies(
    added_edges: list[dict], service_teams: dict[str, str | None]
) -> list[Alert]:
    """Given the "added" half of a diff, flag any new edge whose source
    and target services belong to different, both-known teams. An edge
    involving an unowned service on either end isn't flagged here --
    that's UNOWNED_SERVICE's job, not this check's.
    """
    alerts = []
    for edge in added_edges:
        source_team = service_teams.get(edge["source_service"])
        target_team = service_teams.get(edge["target_service"])
        if source_team and target_team and source_team != target_team:
            alerts.append(
                Alert(
                    type="NEW_CROSS_TEAM_DEPENDENCY",
                    severity="medium",
                    service=edge["source_service"],
                    related_service=edge["target_service"],
                    message=(
                        f"{edge['source_service']} ({source_team}) now depends on "
                        f"{edge['target_service']} ({target_team})"
                    ),
                )
            )
    return alerts


def check_high_fan_in(
    service: str, upstream_edges: list[dict], threshold: int = DEFAULT_HIGH_FAN_IN_THRESHOLD
) -> list[Alert]:
    """Flag a service whose distinct dependent count meets or exceeds
    the threshold. Counted by distinct dependent *service*, not raw edge
    count -- a service with 3 files all importing the same dependent
    shouldn't inflate the count the way 3 different dependent services
    would.
    """
    distinct_dependents = {e["source_service"] for e in upstream_edges}
    if len(distinct_dependents) >= threshold:
        return [
            Alert(
                type="HIGH_FAN_IN",
                severity="medium",
                service=service,
                message=(
                    f"{service} has {len(distinct_dependents)} distinct dependents "
                    f"(threshold: {threshold})"
                ),
            )
        ]
    return []


def check_orphaned_service(
    service: str, downstream_edges: list[dict], upstream_edges: list[dict]
) -> list[Alert]:
    """Flag a service with no dependents and no dependencies at all --
    it's either dead code, a new service nobody's wired up yet, or a
    scan/manifest misconfiguration worth double-checking.
    """
    if not downstream_edges and not upstream_edges:
        return [
            Alert(
                type="ORPHANED_SERVICE",
                severity="low",
                service=service,
                message=f"{service} has no dependents and no dependencies",
            )
        ]
    return []


def check_unowned_service(service: str, team: str | None) -> list[Alert]:
    """Flag a service with no resolved team, from either CODEOWNERS or
    the depgraph.yaml manifest fallback.
    """
    if team is None:
        return [
            Alert(
                type="UNOWNED_SERVICE",
                severity="low",
                service=service,
                message=f"{service} has no resolvable team owner",
            )
        ]
    return []
