"""Core data model for DepGraph.

These mirror the shapes defined in SPEC.md section 6. Keeping them as
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
