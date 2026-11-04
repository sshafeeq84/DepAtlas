"""Core data model for DepAtlas.

These mirror the shapes defined in SPEC.pdf section 6. Keeping them as
simple dataclasses (rather than a heavier ORM model) makes them trivial
to serialize to JSON and easy to reason about while the parser layer is
still evolving.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

EdgeType = Literal["http_call", "import", "shared_library", "config_reference"]
Confidence = Literal["high", "medium", "low"]


@dataclass
class DependencyEdge:
    """A single detected dependency: source depends on target."""

    source_service: str
    target_service: str
    type: EdgeType
    source_file: str
    source_line: int
    confidence: Confidence
    detected_at_sha: Optional[str] = None
    # True when the edge comes from a test file. Kept (a test importing a
    # client is still a real dependency) but ignored by alerts.
    from_test: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ServiceNode:
    """A service discovered in the codebase."""

    id: str
    name: str
    repo: str
    language: str
    team: Optional[str] = None
    entry_points: list[str] = field(default_factory=list)
    last_analysed_sha: Optional[str] = None
    last_analysed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PublicSignature:
    """A single public function/method belonging to a service, as of one
    scan. This is deliberately a different shape from DependencyEdge --
    it describes a fact about the service's *own* interface ("this
    service exposes a function named X taking params [a, b]"), not a
    relationship to another service. Used by the BREAKING_CHANGE alert
    (SPEC.pdf section 10) to compare a service's public interface across
    two scans.

    `params` is a list of dicts, one per parameter, in declared order:
    {"name", "type", "kind", "optional"}. `type` is None when it is not
    written in the source (an unannotated Python or plain-JavaScript
    parameter). `kind` is Python's parameter kind (positional_only,
    positional_or_keyword, keyword_only, var_positional, var_keyword) or,
    for JavaScript/TypeScript, "positional" / "var_positional". `optional`
    is True when a caller may omit the argument (a default value, `b?:`,
    `*args`, a rest parameter). Java parameters carry only name and type,
    as do snapshots saved by versions before 0.3.0. Together these let
    BREAKING_CHANGE tell adding an optional parameter (safe) from adding a
    required one, or renaming a keyword-capable parameter -- see
    depatlas/intelligence/alerts.py.
    """

    service: str
    name: str
    params: list[dict]
    source_file: str
    source_line: int
    # Unique identity within the service, e.g. "pay/charge.py::Charger.run"
    # (repo-relative file, enclosing class, method). Without it, two
    # functions that merely share a name -- `get` in two classes, or Java
    # overloads -- overwrote each other when signatures were compared.
    # None for snapshots saved by older versions.
    qualname: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
