"""Alert system (SPEC.md section 10, "Alert Definitions").

All five alert types are now implemented:

  - NEW_CROSS_TEAM_DEPENDENCY (Medium): a newly-added edge (from a diff)
    whose source and target services have different, both-resolved
    teams.
  - HIGH_FAN_IN (Medium): a service's upstream (dependent) count meets
    or exceeds a configurable threshold (default 10, per SPEC.md's
    alert table).
  - ORPHANED_SERVICE (Low): a service has zero edges in either
    direction -- no dependents, no dependencies.
  - UNOWNED_SERVICE (Low): a service has no resolved team.
  - BREAKING_CHANGE (High): a service's public interface had a function
    removed, or an existing function's parameter list changed in a way
    that would break callers, between two scans -- and the service has
    at least one known dependent. Built on each language parser's
    `extract_signatures()` (SPEC.md's trigger explicitly says "with
    known dependents," so a signature change on a service nobody calls
    doesn't trigger this).

BREAKING_CHANGE uses full signature identity, not just function name --
see `_params_are_breaking_change` below for the exact rules, but in
short: a function is matched by name; once matched, a *parameter count*
change is always breaking, a *type* change at a shared position is
breaking only when both sides have a known type (Java always does;
Python only when annotated; JS/TS never, since esprima can't see
TypeScript's type annotations), and a pure *parameter rename* (same
position, same type-or-absence-of-type) is deliberately NOT flagged --
most real callers invoke positionally, so a rename alone doesn't break
them.
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
    the depatlas.yaml manifest fallback.
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


def _params_are_breaking_change(old_params: list[dict], new_params: list[dict]) -> bool:
    """Decide whether one function's parameter list changed in a way
    that would break existing callers, given full signature identity
    (position, count, and type where known) -- not just "is the list
    different."

    - A different parameter count is always breaking (a caller passing
      the old number of arguments would fail).
    - At any shared position, a type change is breaking *only* when
      both the old and new versions have a known type and they differ.
      If either side has no type info (untyped Python, or any JS/TS
      parameter -- esprima can't see TypeScript's annotations), there's
      nothing concrete to compare, so it's not flagged: annotating a
      previously-untyped parameter, or removing an annotation, isn't by
      itself a behavior change.
    - A parameter *name* changing alone (same position, same type or
      same "no type" state) is NOT breaking -- most real callers invoke
      positionally, so a rename alone doesn't break them. This is a
      deliberate, narrower definition of "breaking" than pure list
      inequality; see check_breaking_changes below for where this is used.
    """
    if len(old_params) != len(new_params):
        return True

    for old_param, new_param in zip(old_params, new_params):
        old_type = old_param.get("type")
        new_type = new_param.get("type")
        if old_type is not None and new_type is not None and old_type != new_type:
            return True

    return False


def _describe_param_change(old_params: list[dict], new_params: list[dict]) -> str:
    """Human-readable summary of what changed, for the alert message."""
    if len(old_params) != len(new_params):
        return f"parameter count changed from {len(old_params)} to {len(new_params)}"

    changed = []
    for i, (old_param, new_param) in enumerate(zip(old_params, new_params)):
        if old_param.get("type") != new_param.get("type"):
            changed.append(
                f"param {i} ('{old_param['name']}'): {old_param.get('type')} -> {new_param.get('type')}"
            )
    return "; ".join(changed) if changed else "parameters changed"


def check_breaking_changes(
    service: str,
    old_signatures: list[dict],
    new_signatures: list[dict],
    upstream_edges: list[dict],
) -> list[Alert]:
    """Flag a public function that was removed, or whose parameter list
    changed in a way that breaks callers, between two scans of `service`
    -- but only if the service has at least one known dependent (SPEC.md's
    trigger explicitly says "with known dependents"; a signature change
    on a service nobody calls isn't breaking anything).

    Matched by function name -- if the function itself is renamed, that
    genuinely is a removal from a caller's perspective (old callers
    calling the old name break), so it's correctly reported as a
    removal, not silently ignored. What's NOT flagged is a *parameter*
    being renamed alone -- see _params_are_breaking_change for the full
    reasoning on why that's treated as safe.
    """
    if not upstream_edges:
        return []

    distinct_dependents = {e["source_service"] for e in upstream_edges}
    old_by_name = {s["name"]: s for s in old_signatures}
    new_by_name = {s["name"]: s for s in new_signatures}

    alerts = []
    for name, old_sig in old_by_name.items():
        if name not in new_by_name:
            alerts.append(
                Alert(
                    type="BREAKING_CHANGE",
                    severity="high",
                    service=service,
                    message=(
                        f"Public function '{name}' was removed from {service} "
                        f"(has {len(distinct_dependents)} known dependent(s))"
                    ),
                )
            )
            continue

        new_sig = new_by_name[name]
        if _params_are_breaking_change(old_sig["params"], new_sig["params"]):
            alerts.append(
                Alert(
                    type="BREAKING_CHANGE",
                    severity="high",
                    service=service,
                    message=(
                        f"Signature of '{name}' changed in {service}: "
                        f"{_describe_param_change(old_sig['params'], new_sig['params'])} "
                        f"(has {len(distinct_dependents)} known dependent(s))"
                    ),
                )
            )

    return alerts
