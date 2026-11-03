"""SQLite graph store (SPEC.pdf section 5, "Graph Builder" -> "Persists to
local graph store (SQLite default)", and section 12's non-functional
requirement that storage default to SQLite).

This is intentionally a thin wrapper around the stdlib `sqlite3` module —
no ORM. For a project at this stage, a query layer you can read top to
bottom is more valuable than one that hides its SQL behind an abstraction.

Upsert model:
  - Services are upserted by `id` — rescanning the same service updates
    its row rather than duplicating it.
  - Edges are replaced per-repo on each scan: we delete every edge stored
    under the scanned repo's stable key (plus, for rows written before the
    `repo` column existed, every edge whose source is the scanned service),
    then insert the fresh set. This means a rescan reflects the current state of the code
    exactly, instead of accumulating stale edges from files that have
    since changed or been deleted.

Snapshots (SPEC.pdf section 5, "Graph Builder" -> "Versions graph", and
section 6.3's GraphSnapshot shape; they are ordered by time, not yet tagged
with a commit SHA) are additive, not
replaced: every `scan` appends a new snapshot row containing that scan's
full edge set as JSON, so the diff engine (SPEC.pdf's Intelligence Layer)
has a timeline to compare against. Unlike the `edges` table (which always
reflects only the *current* state), snapshots accumulate on purpose.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from depatlas.graph.models import DependencyEdge, ServiceNode

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
    detected_at_sha TEXT,
    repo TEXT,
    from_test INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edges_json TEXT NOT NULL,
    signatures_json TEXT NOT NULL DEFAULT '[]',
    languages TEXT NOT NULL DEFAULT ''
);
"""


class GraphStore:
    """Owns a single SQLite connection for one graph.db file."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: wait for a concurrent scan (parallel CI jobs) instead of
        # failing at once with "database is locked".
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._tx_depth = 0
        self._migrate()
        self._conn.commit()

    def _commit(self) -> None:
        """Commit now, unless inside `transaction()`, which commits once at the end."""
        if self._tx_depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Group several writes so they commit together or not at all. A
        scan writes services, edges and a snapshot; a crash between them used
        to leave the database half-updated.
        """
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.rollback()
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.commit()

    def _migrate(self) -> None:
        """Lightweight, idempotent schema migrations for databases created
        before a given column existed. `CREATE TABLE IF NOT EXISTS` in
        SCHEMA only helps brand-new databases -- an existing one needs an
        explicit ALTER TABLE, so people don't have to manually delete
        their local graph.db every time the schema grows.
        """
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(snapshots)").fetchall()
        }
        edge_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(edges)").fetchall()
        }
        if "repo" not in edge_columns:
            self._conn.execute("ALTER TABLE edges ADD COLUMN repo TEXT")
        if "from_test" not in edge_columns:
            self._conn.execute("ALTER TABLE edges ADD COLUMN from_test INTEGER NOT NULL DEFAULT 0")
        if "languages" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE snapshots ADD COLUMN languages TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_service)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_service)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_repo ON edges(repo)")
        if "signatures_json" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE snapshots ADD COLUMN signatures_json TEXT NOT NULL DEFAULT '[]'"
            )

    def upsert_services(
        self, nodes: list[ServiceNode], ownership_inputs_supplied: bool = False
    ) -> None:
        """Insert or update services. `ownership_inputs_supplied` says the scan
        was given CODEOWNERS and/or an ownership manifest: then a scanned
        service's team is set to what they say, even None (an owner removed
        from CODEOWNERS clears the team). Without ownership inputs a scan
        never erases a team recorded earlier.
        """
        for node in nodes:
            # "Real" nodes (repo/language known) should never be
            # downgraded back to a placeholder by a later scan of some
            # *other* repo that merely references this service by name.
            existing = self._conn.execute(
                "SELECT repo, language FROM services WHERE id = ?", (node.id,)
            ).fetchone()

            if existing and existing[0] and not node.repo:
                continue  # keep the existing, more complete row as-is

            team_sql = (
                "excluded.team"
                if ownership_inputs_supplied and node.repo
                else "COALESCE(excluded.team, services.team)"
            )
            self._conn.execute(
                f"""
                INSERT INTO services (id, name, repo, language, team, last_analysed_sha, last_analysed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    repo=excluded.repo,
                    language=excluded.language,
                    team={team_sql},
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
        self._commit()

    def replace_edges_for_repo(
        self, repo_key: str, edges: list[DependencyEdge], self_service: str | None = None
    ) -> None:
        """Replace every edge belonging to one repo. `repo_key` is a stable
        identifier for the repo (e.g. "github:owner/name" or its resolved
        local path) -- matched exactly, so scanning /repos/pay can never
        delete /repos/payments' edges (the old LIKE-prefix delete could,
        and `_` in a path also acted as a wildcard).

        Rows written before the `repo` column existed have repo IS NULL, and
        their stored paths (absolute, often inside a connector's cache
        folder) cannot be matched against a repo key. Every edge a scan
        produces has the scanned service as its source, so those old rows
        are removed by `source_service` instead (when `self_service` is
        given).

        Placeholder services (repo == "") that no remaining edge refers to
        are removed, so a dependency that was dropped stops showing up as an
        orphaned, unowned service forever.
        """
        self._conn.execute(
            "DELETE FROM edges WHERE repo = ? OR (repo IS NULL AND source_service = ?)",
            (repo_key, self_service),
        )
        for edge in edges:
            self._conn.execute(
                """
                INSERT INTO edges
                    (source_service, target_service, type, source_file, source_line, confidence, detected_at_sha, repo, from_test)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.source_service,
                    edge.target_service,
                    edge.type,
                    edge.source_file,
                    edge.source_line,
                    edge.confidence,
                    edge.detected_at_sha,
                    repo_key,
                    int(edge.from_test),
                ),
            )
        self._conn.execute(
            "DELETE FROM services WHERE repo = '' AND id NOT IN "
            "(SELECT source_service FROM edges UNION SELECT target_service FROM edges)"
        )
        self._commit()

    def all_services(self) -> list[dict]:
        cursor = self._conn.execute("SELECT * FROM services ORDER BY id")
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def edges_for_service(self, service_name: str, direction: str = "downstream") -> list[dict]:
        """direction='downstream': what this service depends on.
        direction='upstream': what depends on this service (impact-of)."""
        column = "source_service" if direction == "downstream" else "target_service"
        cursor = self._conn.execute(
            f"SELECT * FROM edges WHERE {column} = ? ORDER BY source_file, source_line",
            (service_name,),
        )
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def save_snapshot(
        self,
        repo: str,
        edges: list[DependencyEdge],
        signatures: list | None = None,
        languages: list[str] | None = None,
    ) -> int:
        """Persist a new snapshot of this repo's edge set (and, optionally,
        its public signature set) and return its auto-generated snapshot
        id. Called once per `scan`, in addition to (not instead of)
        updating the `edges` table -- this is what gives the diff engine
        (and BREAKING_CHANGE alerting) a timeline to compare against.
        `signatures` defaults to an empty list for callers that don't
        pass any (e.g. existing tests written before signatures existed).
        """
        edges_json = json.dumps([e.to_dict() for e in edges])
        signatures_json = json.dumps([s.to_dict() for s in (signatures or [])])
        created_at = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "INSERT INTO snapshots (repo, created_at, edges_json, signatures_json, languages) "
            "VALUES (?, ?, ?, ?, ?)",
            (repo, created_at, edges_json, signatures_json, ",".join(languages or [])),
        )
        self._commit()
        return cursor.lastrowid

    def get_snapshot(self, snapshot_id: int) -> dict | None:
        """Return {"id", "repo", "created_at", "edges", "signatures"} for
        a snapshot, or None if no snapshot with that id exists.
        """
        row = self._conn.execute(
            "SELECT id, repo, created_at, edges_json, signatures_json, languages "
            "FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "repo": row[1],
            "created_at": row[2],
            "edges": json.loads(row[3]),
            "signatures": json.loads(row[4]),
            "languages": [l for l in row[5].split(",") if l],
        }

    def list_snapshots(self, repo: str | None = None, limit: int | None = None) -> list[dict]:
        """List snapshots (most recent first), optionally filtered to one
        repo. Does not include the edges themselves -- just enough to let
        a user pick snapshot ids for `diff --from/--to`.
        """
        query = "SELECT id, repo, created_at FROM snapshots"
        params: list = []
        if repo is not None:
            query += " WHERE repo = ?"
            params.append(repo)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [{"id": r[0], "repo": r[1], "created_at": r[2]} for r in rows]

    def all_edges(self) -> list[dict]:
        """Every edge currently in the live `edges` table, across every
        repo that's been scanned -- used by `depatlas export`, which
        exports the whole persisted graph rather than one repo's edges.
        """
        cursor = self._conn.execute("SELECT * FROM edges ORDER BY source_service, target_service")
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def close(self) -> None:
        self._conn.close()
