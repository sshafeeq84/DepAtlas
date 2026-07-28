"""SQLite graph store (SPEC.md section 5, "Graph Builder" -> "Persists to
local graph store (SQLite default)", and section 12's non-functional
requirement that storage default to SQLite).

This is intentionally a thin wrapper around the stdlib `sqlite3` module —
no ORM. For a project at this stage, a query layer you can read top to
bottom is more valuable than one that hides its SQL behind an abstraction.

Upsert model:
  - Services are upserted by `id` — rescanning the same service updates
    its row rather than duplicating it.
  - Edges are replaced per-repo on each scan: we delete every edge whose
    source_file falls under the scanned repo path, then insert the fresh
    set. This means a rescan reflects the current state of the code
    exactly, instead of accumulating stale edges from files that have
    since changed or been deleted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from depgraph.graph.models import DependencyEdge, ServiceNode

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo TEXT NOT NULL,
    language TEXT NOT NULL,
    team TEXT,
    last_analysed_sha TEXT,
    last_analysed_at TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    source_service TEXT NOT NULL,
    target_service TEXT NOT NULL,
    type TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    detected_at_sha TEXT
);
"""


class GraphStore:
    """Owns a single SQLite connection for one graph.db file."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert_services(self, nodes: list[ServiceNode]) -> None:
        for node in nodes:
            # "Real" nodes (repo/language known) should never be
            # downgraded back to a placeholder by a later scan of some
            # *other* repo that merely references this service by name.
            existing = self._conn.execute(
                "SELECT repo, language FROM services WHERE id = ?", (node.id,)
            ).fetchone()

            if existing and existing[0] and not node.repo:
                continue  # keep the existing, more complete row as-is

            self._conn.execute(
                """
                INSERT INTO services (id, name, repo, language, team, last_analysed_sha, last_analysed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    repo=excluded.repo,
                    language=excluded.language,
                    team=COALESCE(excluded.team, services.team),
                    last_analysed_sha=excluded.last_analysed_sha,
                    last_analysed_at=excluded.last_analysed_at
                """,
                (
                    node.id,
                    node.name,
                    node.repo,
                    node.language,
                    node.team,
                    node.last_analysed_sha,
                    node.last_analysed_at,
                ),
            )
        self._conn.commit()

    def replace_edges_for_repo(self, repo_path: str, edges: list[DependencyEdge]) -> None:
        self._conn.execute(
            "DELETE FROM edges WHERE source_file LIKE ?",
            (f"{repo_path}%",),
        )
        for edge in edges:
            self._conn.execute(
                """
                INSERT INTO edges
                    (source_service, target_service, type, source_file, source_line, confidence, detected_at_sha)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.source_service,
                    edge.target_service,
                    edge.type,
                    edge.source_file,
                    edge.source_line,
                    edge.confidence,
                    edge.detected_at_sha,
                ),
            )
        self._conn.commit()

    def all_services(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM services ORDER BY id").fetchall()
        columns = [c[0] for c in self._conn.execute("SELECT * FROM services").description]
        return [dict(zip(columns, row)) for row in rows]

    def edges_for_service(self, service_name: str, direction: str = "downstream") -> list[dict]:
        """direction='downstream': what this service depends on.
        direction='upstream': what depends on this service (impact-of)."""
        column = "source_service" if direction == "downstream" else "target_service"
        rows = self._conn.execute(
            f"SELECT * FROM edges WHERE {column} = ? ORDER BY source_file, source_line",
            (service_name,),
        ).fetchall()
        columns = [c[0] for c in self._conn.execute("SELECT * FROM edges").description]
        return [dict(zip(columns, row)) for row in rows]

    def close(self) -> None:
        self._conn.close()
