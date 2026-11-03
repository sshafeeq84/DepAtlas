"""JavaScript / TypeScript dependency parser.

v1 scope (see SPEC.pdf 7.3): detect
  1. `import` / `require(...)` statements, resolved against a known
     internal package namespace list (same idea as the Python and Java
     parsers' import detection).
  2. `fetch(...)` / `axios.<method>(...)` / `got.<method>(...)` call
     sites, resolved against an endpoints registry (medium confidence)
     or a heuristic name match (low confidence) -- the same two-tier
     scheme used everywhere else in this project.
  3. `package.json` `dependencies` / `devDependencies` entries, resolved
     against a known internal package list -- a "shared_library" edge,
     high confidence, since a declared dependency is an explicit fact,
     not a guess.

Tool: the `esprima` library, a pure-Python ECMAScript parser that builds
a real AST -- same rationale as Python's `ast` and Java's `tree-sitter`:
source-code truth over regex guessing.

Known limitation: esprima parses JavaScript (ES2017-ish), not
TypeScript-specific syntax (type annotations, interfaces, generics on
call sites, etc.). A `.ts`/`.tsx` file that uses only JS-compatible
syntax parses fine; one that uses real TypeScript syntax will fail to
parse and is skipped with a warning rather than crashing the whole scan.
This is a real gap -- the spec calls for `@babel/parser` or the
TypeScript compiler API, both of which require a Node.js runtime
dependency, which this project deliberately avoids for now. Full
TypeScript support is a good candidate for a future iteration once
that trade-off is worth revisiting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import esprima
from esprima.nodes import Node

from depatlas.fsutil import iter_files
from depatlas.graph.models import DependencyEdge, PublicSignature
from depatlas.resolution import resolve_endpoint

HTTP_CLIENT_OBJECTS = {"axios", "got"}
HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def _resolve_endpoint(url: str, known_endpoints: dict[str, str], known_services: dict[str, str]):
    """Same two-tier resolution used by the Python and Java parsers."""
    return resolve_endpoint(url, known_endpoints, known_services)


def _resolve_module(module_path: str, known_services: dict[str, str]) -> str | None:
    """Resolve a JS/TS module specifier against known internal packages.

    Handles both bare specifiers ("auth_client") and relative/deep paths
    ("./auth_client/session", "auth_client/session") by stripping any
    leading "./" or "../" and then checking progressively shorter
    slash-separated prefixes -- the most specific registered namespace
    wins, same idea as the Java parser's dotted-package matching.
    """
    path = module_path
    while path.startswith("./") or path.startswith("../"):
        path = path.split("/", 1)[1] if "/" in path else ""

    parts = [p for p in path.split("/") if p]
    for i in range(len(parts), 0, -1):
        prefix = "/".join(parts[:i])
        if prefix in known_services:
            return known_services[prefix]
    return None


def _walk(node, callback) -> None:
    """Recursively visit every esprima AST node, calling `callback` on
    each one. esprima doesn't ship a NodeVisitor helper the way Python's
    `ast` or `tree-sitter` do, so this is a small hand-rolled equivalent:
    every node's attributes are inspected, and any that are themselves
    Nodes (or lists of Nodes) are recursed into.
    """
    if not isinstance(node, Node):
        return

    callback(node)

    for value in vars(node).values():
        if isinstance(value, Node):
            _walk(value, callback)
        elif isinstance(value, list):
            for item in value:
                _walk(item, callback)


def _extract_string_literal(node) -> str | None:
    if isinstance(node, Node) and getattr(node, "type", None) == "Literal":
        value = getattr(node, "value", None)
        if isinstance(value, str):
            return value
    return None


def parse_file(
    file_path: Path,
    known_services: dict[str, str] | None = None,
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Parse a single .js/.jsx/.ts/.tsx file and return its DependencyEdges.

    Files with genuine TypeScript-only syntax fail to parse under esprima
    (see module docstring) and are skipped with a warning on stderr,
    rather than aborting the whole scan.
    """
    known_services = known_services or {}
    known_endpoints = known_endpoints or {}
    source_service = known_services.get("__self__", "unknown-service")

    source = file_path.read_text(encoding="utf-8")

    try:
        tree = esprima.parseModule(source, options={"loc": True, "tolerant": True})
    except Exception as exc:  # esprima raises plain Exception on syntax errors
        print(
            f"Warning: skipping {file_path} -- could not parse as JavaScript "
            f"(likely TypeScript-specific syntax): {exc}",
            file=sys.stderr,
        )
        return []

    edges: list[DependencyEdge] = []

    def visit(node: Node) -> None:
        node_type = getattr(node, "type", None)

        # 1a. `import ... from "module"`
        if node_type == "ImportDeclaration":
            module_path = getattr(node.source, "value", None)
            if module_path:
                target = _resolve_module(module_path, known_services)
                if target:
                    edges.append(
                        DependencyEdge(
                            source_service=source_service,
                            target_service=target,
                            type="import",
                            source_file=str(file_path),
                            source_line=node.loc.start.line if node.loc else 0,
                            confidence="high",
                        )
                    )

        # 1b. `require("module")`, wherever it appears (assigned to a
        # variable or called bare).
        elif node_type == "CallExpression":
            callee = node.callee
            if getattr(callee, "type", None) == "Identifier" and callee.name == "require":
                if node.arguments:
                    module_path = _extract_string_literal(node.arguments[0])
                    if module_path:
                        target = _resolve_module(module_path, known_services)
                        if target:
                            edges.append(
                                DependencyEdge(
                                    source_service=source_service,
                                    target_service=target,
                                    type="import",
                                    source_file=str(file_path),
                                    source_line=node.loc.start.line if node.loc else 0,
                                    confidence="high",
                                )
                            )

            # 2. fetch("url") / axios.<method>("url") / got.<method>("url")
            url = None
            if getattr(callee, "type", None) == "Identifier" and callee.name == "fetch":
                if node.arguments:
                    url = _extract_string_literal(node.arguments[0])
            elif getattr(callee, "type", None) == "MemberExpression":
                obj = callee.object
                prop = callee.property
                if (
                    getattr(obj, "type", None) == "Identifier"
                    and obj.name in HTTP_CLIENT_OBJECTS
                    and getattr(prop, "type", None) == "Identifier"
                    and prop.name in HTTP_METHODS
                    and node.arguments
                ):
                    url = _extract_string_literal(node.arguments[0])

            if url is not None:
                target, confidence = _resolve_endpoint(url, known_endpoints, known_services)
                if target is not None:
                    edges.append(
                        DependencyEdge(
                            source_service=source_service,
                            target_service=target,
                            type="http_call",
                            source_file=str(file_path),
                            source_line=node.loc.start.line if node.loc else 0,
                            confidence=confidence,
                        )
                    )

    _walk(tree, visit)
    return edges


def parse_package_json(
    file_path: Path,
    known_services: dict[str, str] | None = None,
    self_service: str | None = None,
) -> list[DependencyEdge]:
    """Parse a package.json's dependencies/devDependencies into
    shared_library edges (SPEC.pdf 7.3's "package.json dependencies --
    shared library map").
    """
    known_services = known_services or {}
    source_service = self_service or known_services.get("__self__", "unknown-service")

    data = json.loads(file_path.read_text(encoding="utf-8"))
    edges: list[DependencyEdge] = []

    for section in ("dependencies", "devDependencies"):
        for package_name in data.get(section, {}):
            target = known_services.get(package_name)
            if target is None:
                continue
            edges.append(
                DependencyEdge(
                    source_service=source_service,
                    target_service=target,
                    type="shared_library",
                    source_file=str(file_path),
                    source_line=0,  # package.json entries have no meaningful line concept here
                    confidence="high",
                )
            )

    return edges


def parse_repo(
    repo_path: Path,
    known_services: dict[str, str],
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Walk a repo directory and parse every .js/.jsx/.ts/.tsx file, plus
    a top-level package.json if present.
    """
    edges: list[DependencyEdge] = []

    for pattern in ("*.js", "*.jsx", "*.ts", "*.tsx"):
        for source_file in iter_files(repo_path, pattern):
            edges.extend(parse_file(source_file, known_services, known_endpoints))

    package_json = repo_path / "package.json"
    if package_json.exists():
        edges.extend(parse_package_json(package_json, known_services))

    return edges


def _extract_signatures_from_file(
    file_path: Path, self_service: str, rel_path: str | None = None
) -> list[PublicSignature]:
    """A module's public interface, in JS/TS terms, is what it exports --
    so this looks specifically for `export function foo(...)` and
    `export default function foo(...)`, not every function in the file
    (an unexported function is a private implementation detail, same
    idea as the Python parser's underscore-prefix convention).

    Same TypeScript-syntax limitation as parse_file: a file esprima
    can't parse is skipped (already warned about during the edge-parsing
    pass, so no need to warn twice here).
    """
    source = file_path.read_text(encoding="utf-8")
    try:
        tree = esprima.parseModule(source, options={"loc": True, "tolerant": True})
    except Exception:
        return []

    signatures: list[PublicSignature] = []

    for node in tree.body:
        node_type = getattr(node, "type", None)
        declaration = None
        if node_type == "ExportNamedDeclaration":
            declaration = node.declaration
        elif node_type == "ExportDefaultDeclaration":
            declaration = node.declaration

        if declaration is None or getattr(declaration, "type", None) != "FunctionDeclaration":
            continue
        if declaration.id is None:  # anonymous `export default function (...) {}`
            continue

        signatures.append(
            PublicSignature(
                service=self_service,
                name=declaration.id.name,
                # type is always None here: esprima parses JavaScript,
                # not TypeScript's type annotations (same documented
                # limitation as parse_file above), so there's no type
                # information to capture even for .ts files that do
                # have real annotations in source.
                params=[
                    {"name": p.name, "type": None}
                    for p in declaration.params
                    if getattr(p, "type", None) == "Identifier"
                ],
                source_file=str(file_path),
                source_line=node.loc.start.line if node.loc else 0,
                qualname=f"{rel_path or file_path.name}::{declaration.id.name}",
            )
        )

    return signatures


def extract_signatures(repo_path: Path, self_service: str) -> list[PublicSignature]:
    """Extract every exported function signature across a repo's
    .js/.jsx/.ts/.tsx files -- the data BREAKING_CHANGE alerting compares
    between scans (SPEC.pdf section 10).
    """
    signatures: list[PublicSignature] = []
    for pattern in ("*.js", "*.jsx", "*.ts", "*.tsx"):
        for source_file in iter_files(repo_path, pattern):
            signatures.extend(
                _extract_signatures_from_file(
                    source_file, self_service, source_file.relative_to(repo_path).as_posix()
                )
            )
    return signatures
