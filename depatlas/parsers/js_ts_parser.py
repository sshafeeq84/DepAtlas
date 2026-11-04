"""JavaScript / TypeScript dependency parser.

Scope (see SPEC.pdf 7.3): detect
  1. `import` / `export ... from` / `require(...)` / `import(...)`,
     resolved against a known internal package namespace list (same idea
     as the Python and Java parsers' import detection).
  2. `fetch(...)` / `axios.<method>(...)` / `got.<method>(...)` call
     sites, resolved against an endpoints registry (medium confidence)
     or a hostname match (low confidence) -- the same two-tier scheme
     used everywhere else in this project.
  3. `package.json` `dependencies` / `devDependencies` entries, resolved
     against a known internal package list -- a "shared_library" edge,
     high confidence, since a declared dependency is an explicit fact,
     not a guess.

Tool: `tree-sitter` with the JavaScript and TypeScript grammars (the
same library the Java parser uses), so real TypeScript -- type
annotations, interfaces, generics, decorators -- parses. `.ts`/`.mts`/
`.cts` use the TypeScript grammar, `.tsx` the TSX grammar, everything
else the JavaScript grammar (which includes JSX). tree-sitter recovers
from syntax errors, so a file with a mistake still contributes whatever
parsed (with a warning), instead of being skipped whole.

Public signatures (for BREAKING_CHANGE) are what a module exports:
exported functions, exported `const f = (...) => ...`, `export { f }` of
a local function, and the public methods of exported classes. Each
parameter records its TypeScript type (None in plain JavaScript), whether
it is optional (`?`, a default value, or a rest parameter) and its kind
(`positional` or `var_positional`).

Not covered: CommonJS exports (`module.exports = ...`), and functions
that are only reachable through a re-export from another module.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from depatlas.fsutil import is_test_path, iter_files, relative_to_repo, warn_skipped
from depatlas.graph.models import DependencyEdge, PublicSignature
from depatlas.resolution import drop_self_edges, resolve_endpoint

HTTP_CLIENT_OBJECTS = {"axios", "got"}
HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
FILE_PATTERNS = ("*.js", "*.jsx", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.mts", "*.cts")

_LANGUAGES = {
    "javascript": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
}


def _grammar_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".tsx":
        return "tsx"
    if suffix in (".ts", ".mts", ".cts"):
        return "typescript"
    return "javascript"


def _parse(path: Path, source: bytes) -> Node:
    return Parser(_LANGUAGES[_grammar_for(path)]).parse(source).root_node


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


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


def _walk(node: Node):
    """Every node in the tree, depth-first."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _string_value(node: Node | None) -> str | None:
    """The contents of a plain string literal (or a template literal with
    no `${...}` substitutions); None for anything computed.
    """
    if node is None:
        return None
    if node.type == "string":
        return _text(node)[1:-1]
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.children
    ):
        return _text(node)[1:-1]
    return None


def parse_file(
    file_path: Path,
    known_services: dict[str, str] | None = None,
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Parse a single JS/TS file and return its DependencyEdges. A file
    with syntax errors still contributes what parsed, with a warning.
    """
    known_services = known_services or {}
    known_endpoints = known_endpoints or {}
    source_service = known_services.get("__self__", "unknown-service")

    try:
        source = file_path.read_bytes()
        source.decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        warn_skipped(file_path, exc)
        return []

    root = _parse(file_path, source)
    if root.has_error:
        print(
            f"warning: {file_path} has syntax errors; using the parts that parsed",
            file=sys.stderr,
        )

    edges: list[DependencyEdge] = []

    def add_import(module_path: str | None, node: Node) -> None:
        if not module_path:
            return
        target = _resolve_module(module_path, known_services)
        if target:
            edges.append(
                DependencyEdge(
                    source_service=source_service,
                    target_service=target,
                    type="import",
                    source_file=str(file_path),
                    source_line=_line(node),
                    confidence="high",
                )
            )

    for node in _walk(root):
        kind = node.type

        # `import x from "m"`, `export * from "m"`, `export { a } from "m"`
        if kind in ("import_statement", "export_statement"):
            add_import(_string_value(node.child_by_field_name("source")), node)

        # `import x = require("m")` (TypeScript)
        elif kind == "import_require_clause":
            strings = [c for c in node.children if c.type == "string"]
            if strings:
                add_import(_string_value(strings[0]), node)

        elif kind == "call_expression":
            callee = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            first_arg = args.named_children[0] if args is not None and args.named_children else None
            if callee is None or first_arg is None:
                continue

            # `require("m")` and dynamic `import("m")`
            if callee.type == "import" or (callee.type == "identifier" and _text(callee) == "require"):
                add_import(_string_value(first_arg), node)
                continue

            # fetch("url") / axios.<method>("url") / got.<method>("url")
            url = None
            if callee.type == "identifier" and _text(callee) == "fetch":
                url = _string_value(first_arg)
            elif callee.type == "member_expression":
                obj = callee.child_by_field_name("object")
                prop = callee.child_by_field_name("property")
                if (
                    obj is not None
                    and prop is not None
                    and obj.type == "identifier"
                    and _text(obj) in HTTP_CLIENT_OBJECTS
                    and _text(prop) in HTTP_METHODS
                ):
                    url = _string_value(first_arg)

            if url is not None:
                target, confidence = _resolve_endpoint(url, known_endpoints, known_services)
                if target is not None:
                    edges.append(
                        DependencyEdge(
                            source_service=source_service,
                            target_service=target,
                            type="http_call",
                            source_file=str(file_path),
                            source_line=_line(node),
                            confidence=confidence,
                        )
                    )

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
    """Walk a repo directory and parse every JS/TS file, plus a top-level
    package.json if present.
    """
    edges: list[DependencyEdge] = []

    for pattern in FILE_PATTERNS:
        for source_file in iter_files(repo_path, pattern):
            found = parse_file(source_file, known_services, known_endpoints)
            for edge in found:
                edge.source_file = relative_to_repo(edge.source_file, repo_path)
            edges.extend(found)

    package_json = repo_path / "package.json"
    if package_json.exists():
        try:
            found = parse_package_json(package_json, known_services)
        except (ValueError, OSError) as exc:  # bad JSON or non-UTF-8 bytes
            warn_skipped(package_json, exc)
            found = []
        for edge in found:
            edge.source_file = relative_to_repo(edge.source_file, repo_path)
        edges.extend(found)

    return drop_self_edges(edges)


# ---- public signatures ----

_FUNCTION_DECLARATIONS = {
    "function_declaration",
    "generator_function_declaration",
    "function_signature",  # TypeScript overload / ambient signature
}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_CLASS_DECLARATIONS = {"class_declaration", "abstract_class_declaration", "class"}


def _type_text(annotation: Node | None) -> str | None:
    """A TypeScript type annotation as text, without the leading colon."""
    if annotation is None:
        return None
    text = _text(annotation).lstrip().lstrip(":").strip()
    return re.sub(r"\s+", " ", text) or None


def _param_from_pattern(
    pattern: Node, type_node: Node | None, optional: bool
) -> dict | None:
    """One parameter as {"name", "type", "kind", "optional"}."""
    kind = "positional"
    if pattern.type == "this":
        return None  # TypeScript's fake `this` parameter is not an argument
    if pattern.type == "rest_pattern":
        kind = "var_positional"
        optional = True
        inner = next((c for c in pattern.named_children), None)
        name = _text(inner) if inner is not None and inner.type == "identifier" else "..."
    elif pattern.type == "identifier":
        name = _text(pattern)
    elif pattern.type == "object_pattern":
        name = "{...}"  # fields of a destructured argument are not positional API
    elif pattern.type == "array_pattern":
        name = "[...]"
    else:
        name = _text(pattern)
    return {"name": name, "type": _type_text(type_node), "kind": kind, "optional": optional}


def _params(formal: Node | None) -> list[dict]:
    params: list[dict] = []
    if formal is None:
        return params
    for child in formal.named_children:
        param = None
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern")
            if pattern is not None:
                param = _param_from_pattern(
                    pattern,
                    child.child_by_field_name("type"),
                    optional=child.type == "optional_parameter"
                    or child.child_by_field_name("value") is not None,
                )
        elif child.type == "assignment_pattern":  # plain JS: `b = 1`
            left = child.child_by_field_name("left")
            if left is not None:
                param = _param_from_pattern(left, None, optional=True)
        elif child.type in ("identifier", "rest_pattern", "object_pattern", "array_pattern"):
            param = _param_from_pattern(child, None, optional=False)
        if param is not None:
            params.append(param)
    return params


def _function_parts(node: Node) -> tuple[Node | None, Node | None]:
    """(name node, parameters node) of a function-like node."""
    params = node.child_by_field_name("parameters")
    if params is None:
        # `x => x`: a single bare parameter
        single = node.child_by_field_name("parameter")
        return node.child_by_field_name("name"), single
    return node.child_by_field_name("name"), params


def _single_param_list(node: Node | None) -> list[dict]:
    if node is None:
        return []
    if node.type == "formal_parameters":
        return _params(node)
    param = _param_from_pattern(node, None, optional=False) if node.type == "identifier" else None
    return [param] if param else []


def _class_methods(cls: Node, class_name: str):
    """Public methods of a class: not `#private`, not TypeScript
    `private`/`protected`, not getters/setters/constructors."""
    body = cls.child_by_field_name("body")
    if body is None:
        return
    for member in body.named_children:
        if member.type != "method_definition":
            continue
        name_node = member.child_by_field_name("name")
        if name_node is None or name_node.type not in ("property_identifier", "identifier"):
            continue  # #private, computed or string-named
        name = _text(name_node)
        if name == "constructor":
            continue
        tokens = {c.type for c in member.children if not c.is_named}
        if tokens & {"get", "set"}:
            continue
        modifiers = {
            _text(c) for c in member.children if c.type == "accessibility_modifier"
        }
        if modifiers & {"private", "protected"}:
            continue
        yield name, member, _line(member)


def _collect_signatures(root: Node) -> list[tuple[str, list[dict], int]]:
    """(qualified name, params, line) of everything the module exports."""
    local_functions: dict[str, tuple[list[dict], int]] = {}
    exported: list[tuple[str, list[dict], int]] = []
    overloaded: set[str] = set()  # names declared with TypeScript overload signatures
    implementations: set[tuple[str, int]] = set()  # (name, line) of `function f() {}` bodies

    def from_declaration(decl: Node) -> list[tuple[str, list[dict], int]]:
        found = []
        if decl.type in _FUNCTION_DECLARATIONS:
            name_node, params_node = _function_parts(decl)
            if name_node is not None:
                found.append((_text(name_node), _single_param_list(params_node), _line(decl)))
                if decl.type == "function_signature":
                    overloaded.add(_text(name_node))
                else:
                    implementations.add((_text(name_node), _line(decl)))
        elif decl.type in ("lexical_declaration", "variable_declaration"):
            for declarator in decl.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier" or value is None:
                    continue
                if value.type in _FUNCTION_VALUES:
                    _, params_node = _function_parts(value)
                    found.append((_text(name_node), _single_param_list(params_node), _line(declarator)))
        elif decl.type in _CLASS_DECLARATIONS:
            name_node = decl.child_by_field_name("name")
            if name_node is not None:
                cname = _text(name_node)
                for mname, member, line in _class_methods(decl, cname):
                    found.append(
                        (f"{cname}.{mname}", _single_param_list(member.child_by_field_name("parameters")), line)
                    )
        return found

    top = list(root.named_children)
    for node in top:
        if node.type != "export_statement":
            for name, params, line in from_declaration(node):
                local_functions[name] = (params, line)

    for node in top:
        if node.type != "export_statement":
            continue
        declaration = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")  # export default <expr>
        if declaration is not None:
            exported.extend(from_declaration(declaration))
        elif value is not None and value.type in (_FUNCTION_VALUES | _CLASS_DECLARATIONS):
            exported.extend(from_declaration(value))
        elif node.child_by_field_name("source") is None:
            # `export { a, b as c }` of local functions
            for clause in node.named_children:
                if clause.type != "export_clause":
                    continue
                for spec in clause.named_children:
                    local = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    if local is not None and _text(local) in local_functions:
                        params, line = local_functions[_text(local)]
                        exported.append((_text(alias or local), params, line))

    # TypeScript overloads: the signatures are the public API; the
    # implementation signature below them is not.
    return [e for e in exported if not (e[0] in overloaded and (e[0], e[2]) in implementations)]


def _extract_signatures_from_file(
    file_path: Path, self_service: str, rel_path: str | None = None
) -> list[PublicSignature]:
    """A module's public interface, in JS/TS terms, is what it exports."""
    try:
        source = file_path.read_bytes()
        source.decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # already warned about during the edge-parsing pass

    root = _parse(file_path, source)
    rel = rel_path or file_path.name
    return [
        PublicSignature(
            service=self_service,
            name=qualified.rsplit(".", 1)[-1],
            params=params,
            source_file=rel,
            source_line=line,
            qualname=f"{rel}::{qualified}",
        )
        for qualified, params, line in _collect_signatures(root)
    ]


def extract_signatures(repo_path: Path, self_service: str) -> list[PublicSignature]:
    """Extract every exported function signature across a repo's JS/TS
    files -- the data BREAKING_CHANGE alerting compares between scans
    (SPEC.pdf section 10).
    """
    signatures: list[PublicSignature] = []
    for pattern in FILE_PATTERNS:
        for source_file in iter_files(repo_path, pattern):
            rel = source_file.relative_to(repo_path)
            if is_test_path(rel) or source_file.name.endswith(".d.ts"):
                continue  # tests and type-declaration files are not the service's own API
            signatures.extend(_extract_signatures_from_file(source_file, self_service, rel.as_posix()))
    return signatures
