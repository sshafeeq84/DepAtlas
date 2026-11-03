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

    `params` is a list of {"name": str, "type": str | None} dicts, one
    per parameter, in declared order -- not just a list of names. The
    type is None when the language/parser can't determine one (e.g. an
    untyped Python parameter, or any JS/TS parameter, since esprima
    doesn't parse TypeScript's type annotations). Capturing type
    alongside position is what lets BREAKING_CHANGE tell a real type
    change apart from a harmless parameter rename -- see
    depatlas/intelligence/alerts.py's check_breaking_changes.
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
