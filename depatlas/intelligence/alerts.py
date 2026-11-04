"""Alert system (SPEC.pdf section 10, "Alert Definitions").

All five alert types are now implemented:

  - NEW_CROSS_TEAM_DEPENDENCY (Medium): a newly-added edge (from a diff)
    whose source and target services have different, both-resolved
    teams.
  - HIGH_FAN_IN (Medium): a service's upstream (dependent) count meets
    or exceeds a configurable threshold (default 10, per SPEC.pdf's
    alert table).
  - ORPHANED_SERVICE (Low): a service has zero edges in either
    direction -- no dependents, no dependencies.
  - UNOWNED_SERVICE (Low): a service has no resolved team.
  - BREAKING_CHANGE (High): a service's public interface had a function
    removed, or an existing function's parameter list changed in a way
    that would break callers, between two scans -- and the service has
    at least one known dependent. Built on each language parser's
    `extract_signatures()` (SPEC.pdf's trigger explicitly says "with
    known dependents," so a signature change on a service nobody calls
    doesn't trigger this).

BREAKING_CHANGE uses full signature identity, not just function name: a
function is matched by its qualified identity (repo-relative file,
enclosing class, name; overloads are compared as a group; snapshots from
older versions fall back to the bare name). Once matched, parameters are
compared by what an existing caller could be doing -- see
`_kinded_problems` for the rules. Python and JavaScript/TypeScript record
each parameter's kind and whether it is optional, so adding an optional
parameter is not flagged but adding a required one is, and a Python
parameter that callers may pass by keyword cannot be renamed. Types are
compared only when both sides have one (Java always; Python when
annotated; TypeScript when annotated). Java parameters carry no kind, so
there a different count is breaking and a rename is not.

Edges that come from test files (`from_test`) are real dependencies but are
ignored by every alert here: a test importing another service's client must
not inflate HIGH_FAN_IN or raise NEW_CROSS_TEAM_DEPENDENCY.
"""

from __future__ import annotations

from dataclasses import dataclass

from depatlas.fsutil import language_of

DEFAULT_HIGH_FAN_IN_THRESHOLD = 10


def comparable_signatures(old_snapshot: dict, new_snapshot: dict) -> tuple[list, list, list]:
    """Signatures of the two snapshots restricted to languages scanned in
    *both*, plus the languages that were skipped. Scanning with `--language
    all` and later with `--language java` would otherwise make every Python
    and JavaScript function look removed. Snapshots saved before the language
    set was recorded are compared in full.
    """
    old_langs = set(old_snapshot.get("languages") or [])
    new_langs = set(new_snapshot.get("languages") or [])
    if not old_langs or not new_langs or old_langs == new_langs:
        return old_snapshot["signatures"], new_snapshot["signatures"], []
    shared = old_langs & new_langs
    keep = lambda sigs: [s for s in sigs if language_of(s["source_file"]) in shared]
    return keep(old_snapshot["signatures"]), keep(new_snapshot["signatures"]), sorted(old_langs ^ new_langs)


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
        if edge.get("from_test"):
            continue  # a test importing a client is not a production dependency
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
    distinct_dependents = {e["source_service"] for e in upstream_edges if not e.get("from_test")}
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
    downstream_edges = [e for e in downstream_edges if not e.get("from_test")]
    upstream_edges = [e for e in upstream_edges if not e.get("from_test")]
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


_POSITIONAL_KINDS = {"positional_only", "positional_or_keyword", "positional"}
_SELF_NAMES = {"self", "cls"}


def _has_kinds(params: list[dict]) -> bool:
    return bool(params) and all("kind" in p for p in params)


def _type_changed(old: dict, new: dict) -> bool:
    """A type change counts only when both sides have a known type."""
    return bool(old.get("type")) and bool(new.get("type")) and old["type"] != new["type"]


def _legacy_problems(old_params: list[dict], new_params: list[dict]) -> list[str]:
    """Rules for parameters without kinds (Java, and snapshots saved by
    older versions): a different count is breaking; at any shared position a
    type change is breaking only when both types are known; a rename alone is
    not.
    """
    if len(old_params) != len(new_params):
        return [f"parameter count changed from {len(old_params)} to {len(new_params)}"]
    return [
        f"param {i} ('{o['name']}'): {o.get('type')} -> {n.get('type')}"
        for i, (o, n) in enumerate(zip(old_params, new_params))
        if _type_changed(o, n)
    ]


def _kinded_problems(old: list[dict], new: list[dict]) -> list[str]:
    """Rules for parameters that carry a kind and an `optional` flag
    (Python, JavaScript/TypeScript). In plain terms, an existing caller
    breaks when:

    - a parameter it may pass is gone, or moved to another position;
    - a new parameter is required (a new optional one is fine);
    - an optional parameter became required;
    - a parameter's type changed (when both types are known);
    - a Python parameter that could be passed by keyword was renamed, made
      positional-only, or made keyword-only (positional callers break);
    - `*args` / `**kwargs` was removed.
    Renaming a positional-only parameter, or any JavaScript parameter, is
    safe: callers cannot refer to it by name.
    """
    problems: list[str] = []
    old_pos = [p for p in old if p["kind"] in _POSITIONAL_KINDS]
    new_pos = [p for p in new if p["kind"] in _POSITIONAL_KINDS]
    old_by_name = {p["name"]: p for p in old}
    new_by_name = {p["name"]: p for p in new}
    new_pos_names = [p["name"] for p in new_pos]
    new_var_positional = any(p["kind"] == "var_positional" for p in new)
    new_var_keyword = any(p["kind"] == "var_keyword" for p in new)

    for i, o in enumerate(old_pos):
        name = o["name"]
        if i < len(new_pos):
            n = new_pos[i]
            if name != n["name"] and name not in _SELF_NAMES:
                if o["kind"] != "positional_only" and name in new_pos_names:
                    problems.append(
                        f"parameter '{name}' moved from position {i} to {new_pos_names.index(name)}"
                    )
                elif o["kind"] == "positional_or_keyword" and name not in new_by_name:
                    problems.append(
                        f"parameter '{name}' renamed to '{n['name']}' (callers passing it by keyword break)"
                    )
            if _type_changed(o, n):
                problems.append(f"param {i} ('{name}'): {o['type']} -> {n['type']}")
            if o["kind"] == "positional_or_keyword" and n["kind"] == "positional_only":
                problems.append(f"parameter '{name}' is now positional-only")
            if o["optional"] and not n["optional"] and o["kind"] != "var_positional":
                problems.append(f"parameter '{name}' is now required")
        else:
            moved = new_by_name.get(name)
            if o["kind"] == "positional_or_keyword" and moved and moved["kind"] == "keyword_only":
                problems.append(f"parameter '{name}' is now keyword-only")
            elif not (new_var_positional and (o["kind"] != "positional_or_keyword" or new_var_keyword)):
                problems.append(f"parameter '{name}' was removed")

    for n in new_pos[len(old_pos):]:
        if n["name"] not in old_by_name and not n["optional"]:
            problems.append(f"new required parameter '{n['name']}'")

    for o in (p for p in old if p["kind"] == "keyword_only"):
        n = new_by_name.get(o["name"])
        if n is None:
            if not new_var_keyword:
                problems.append(f"keyword-only parameter '{o['name']}' was removed")
        elif n["kind"] not in ("keyword_only", "positional_or_keyword"):
            problems.append(f"parameter '{o['name']}' can no longer be passed by keyword")
        else:
            if _type_changed(o, n):
                problems.append(f"parameter '{o['name']}': {o['type']} -> {n['type']}")
            if o["optional"] and not n["optional"]:
                problems.append(f"parameter '{o['name']}' is now required")
    for n in (p for p in new if p["kind"] == "keyword_only"):
        if n["name"] not in old_by_name and not n["optional"]:
            problems.append(f"new required keyword-only parameter '{n['name']}'")

    for kind, label in (("var_positional", "*args"), ("var_keyword", "**kwargs")):
        if any(p["kind"] == kind for p in old) and not any(p["kind"] == kind for p in new):
            problems.append(f"{label} was removed")

    return list(dict.fromkeys(problems))


def _param_problems(old_params: list[dict], new_params: list[dict]) -> list[str]:
    """What, if anything, about a changed parameter list breaks callers."""
    old_k, new_k = _has_kinds(old_params), _has_kinds(new_params)
    if old_params and new_params and old_k != new_k:
        # One side was saved by an older version without kinds: the two
        # lists are not comparable, so judge neither (a removed function is
        # still reported). Re-scan once and later comparisons are exact.
        return []
    if old_k or new_k:
        return _kinded_problems(old_params, new_params)
    return _legacy_problems(old_params, new_params)


def _params_are_breaking_change(old_params: list[dict], new_params: list[dict]) -> bool:
    return bool(_param_problems(old_params, new_params))


def _describe_param_change(old_params: list[dict], new_params: list[dict]) -> str:
    """Human-readable summary of what changed, for the alert message."""
    return "; ".join(_param_problems(old_params, new_params)) or "parameters changed"


def check_breaking_changes(
    service: str,
    old_signatures: list[dict],
    new_signatures: list[dict],
    upstream_edges: list[dict],
) -> list[Alert]:
    """Flag a public function that was removed, or whose parameter list
    changed in a way that breaks callers, between two scans of `service`
    -- but only if the service has at least one known dependent (SPEC.pdf's
    trigger explicitly says "with known dependents"; a signature change
    on a service nobody calls isn't breaking anything).

    Matched by qualified name (file, enclosing class, function) -- so two
    methods that merely share a name are never confused, and overloads are
    compared as a group. Snapshots saved by older versions have no
    qualified name and fall back to the bare function name. If the function
    itself is renamed or moved to another file, that genuinely is a removal
    from a caller's perspective (old callers break), so it's correctly
    reported as a removal, not silently ignored. What's NOT flagged is a *parameter*
    being renamed alone -- see _params_are_breaking_change for the full
    reasoning on why that's treated as safe.
    """
    upstream_edges = [e for e in upstream_edges if not e.get("from_test")]
    if not upstream_edges:
        return []

    distinct_dependents = {e["source_service"] for e in upstream_edges}
    dependents_note = f"(has {len(distinct_dependents)} known dependent(s))"

    # Snapshots saved by older versions have no `qualname`; for those the
    # bare name is the only identity available, so fall back to it rather
    # than reporting every function as removed right after an upgrade.
    legacy = any(not s.get("qualname") for s in old_signatures + new_signatures)

    def _key(sig: dict) -> str:
        return sig["name"] if legacy else sig["qualname"]

    def _label(key: str) -> str:
        return key if legacy else key.split("::", 1)[-1]

    old_groups = _group_by(old_signatures, _key)
    new_groups = _group_by(new_signatures, _key)

    alerts = []
    for key, old_sigs in old_groups.items():
        label = _label(key)
        if key not in new_groups:
            alerts.append(
                Alert(
                    type="BREAKING_CHANGE",
                    severity="high",
                    service=service,
                    message=f"Public function '{label}' was removed from {service} {dependents_note}",
                )
            )
            continue

        new_sigs = new_groups[key]
        if len(old_sigs) == 1 and len(new_sigs) == 1:
            if _params_are_breaking_change(old_sigs[0]["params"], new_sigs[0]["params"]):
                alerts.append(
                    Alert(
                        type="BREAKING_CHANGE",
                        severity="high",
                        service=service,
                        message=(
                            f"Signature of '{label}' changed in {service}: "
                            f"{_describe_param_change(old_sigs[0]['params'], new_sigs[0]['params'])} "
                            f"{dependents_note}"
                        ),
                    )
                )
            continue

        # Overloads: an old overload is fine as long as some new overload
        # is still compatible with it. Flag only overloads that vanished.
        for old_sig in old_sigs:
            if all(_params_are_breaking_change(old_sig["params"], n["params"]) for n in new_sigs):
                alerts.append(
                    Alert(
                        type="BREAKING_CHANGE",
                        severity="high",
                        service=service,
                        message=(
                            f"Overload of '{label}' with {len(old_sig['params'])} parameter(s) "
                            f"was removed or changed in {service} {dependents_note}"
                        ),
                    )
                )

    return alerts


def _group_by(signatures: list[dict], key_fn) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for sig in signatures:
        groups.setdefault(key_fn(sig), []).append(sig)
    return groups
