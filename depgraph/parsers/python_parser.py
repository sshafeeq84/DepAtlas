"""Python dependency parser.

v1 scope (see SPEC.md 7.1): detect `import` / `from X import Y` statements
and resolve them against a known internal service list. Everything else
(requests calls, boto3, grpc) comes later — this file is deliberately
narrow so it's easy to read end to end.

Approach: walk the file's AST with ast.NodeVisitor rather than regexing
the source text. This is the idiomatic way to do source analysis in
Python — it understands the language's actual grammar, so it doesn't
get confused by imports inside strings, comments, or docstrings the way
a regex would.
"""

from __future__ import annotations

import ast
from pathlib import Path

from depgraph.graph.models import DependencyEdge


class ImportVisitor(ast.NodeVisitor):
    """Walks a module's AST and records every import statement it finds.

    ast.NodeVisitor works by dispatching to a visit_<NodeType> method for
    each node type it encounters as it walks the tree. We only care about
    two node types here: `import x` (ast.Import) and
    `from x import y` (ast.ImportFrom).
    """

    def __init__(self, source_file: str):
        self.source_file = source_file
        self.imports: list[dict] = []

    def visit_Import(self, node: ast.Import) -> None:
        # e.g. `import auth_client` or `import auth_client as ac`
        for alias in node.names:
            self.imports.append(
                {
                    "module": alias.name,
                    "line": node.lineno,
                }
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # e.g. `from auth_client.session import verify_token`
        module = node.module or ""
        self.imports.append(
            {
                "module": module,
                "line": node.lineno,
            }
        )
        self.generic_visit(node)


def parse_file(file_path: Path, known_services: dict[str, str] | None = None) -> list[DependencyEdge]:
    """Parse a single Python file and return DependencyEdges for its imports.

    known_services maps a top-level module/package name -> service name,
    e.g. {"auth_client": "auth-service"}. Imports that don't resolve
    against this map are skipped for now (they're almost certainly
    stdlib or third-party packages, not internal services) — this is the
    "AST-resolved import matching known internal service" signal from
    SPEC.md 7.4, which is High confidence precisely because it isn't a
    guess: we know the source file, the line, and the exact module name.
    """
    known_services = known_services or {}
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))

    visitor = ImportVisitor(str(file_path))
    visitor.visit(tree)

    edges: list[DependencyEdge] = []
    for imp in visitor.imports:
        top_level_module = imp["module"].split(".")[0]
        target_service = known_services.get(top_level_module)
        if target_service is None:
            continue  # not a known internal service; skip for v1 slice

        edges.append(
            DependencyEdge(
                source_service=known_services.get("__self__", "unknown-service"),
                target_service=target_service,
                type="import",
                source_file=str(file_path),
                source_line=imp["line"],
                confidence="high",
            )
        )

    return edges


def parse_repo(repo_path: Path, known_services: dict[str, str]) -> list[DependencyEdge]:
    """Walk a repo directory and parse every .py file in it."""
    edges: list[DependencyEdge] = []
    for py_file in repo_path.rglob("*.py"):
        edges.extend(parse_file(py_file, known_services))
    return edges
