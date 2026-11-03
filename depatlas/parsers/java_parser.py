"""Java dependency parser.

v1 scope (see SPEC.pdf 7.2): detect
  1. `import` statements, resolved against a known internal package
     namespace list (same idea as the Python parser's import detection).
  2. `@FeignClient` annotations on a class/interface -- an explicit,
     direct service dependency declaration, so this is High confidence
     per SPEC.pdf 7.4's "Explicit annotation" row, no URL matching needed.
  3. RestTemplate/WebClient/HttpClient-style call sites (e.g.
     `restTemplate.getForObject(url, ...)`, `webClient.get().uri(url)`),
     resolved against an endpoints registry (medium confidence) or a
     heuristic name match (low confidence) -- the same two-tier scheme the
     Python parser uses for `requests.get/post/etc.`.

Tool: `tree-sitter` with the Java grammar. It builds a real syntax tree
(not a regex parse), supports current Java (records, text blocks, `var`,
switch expressions, ...) and is error-tolerant: a file with a syntax error
still yields a partial tree, so one odd construct no longer hides every
dependency in the file. (This replaced `javalang`, which stops at older
Java syntax and skipped any file it could not fully parse.)
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from depatlas.fsutil import is_test_path, iter_files, relative_to_repo, warn_skipped
from depatlas.graph.models import DependencyEdge, PublicSignature
from depatlas.resolution import drop_self_edges, resolve_endpoint

_PARSER = Parser(Language(tree_sitter_java.language()))

# Common Spring RestTemplate / WebClient / generic HTTP-client method
# names. This is intentionally a flat method-name allowlist rather than
# real type inference (which would need full classpath resolution) --
# a pragmatic, well-scoped heuristic for v1.
HTTP_CLIENT_METHODS = {
    "getForObject",
    "postForObject",
    "putForObject",
    "exchange",
    "get",
    "post",
    "put",
    "delete",
    # WebClient: webClient.get().uri("http://svc/...") -- the URL is the
    # argument of .uri(), not of .get().
    "uri",
}

# Method names so generic (`config.get("auth.timeout")`, `map.put(...)`)
# that a call is only treated as HTTP when its string argument looks like a
# URL. The Spring-specific names above are trusted on name alone.
GENERIC_METHODS = {"get", "post", "put", "delete", "exchange", "uri"}


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _walk(node: Node):
    """Every node in the tree, depth-first."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _parse(file_path: Path) -> Node:
    """Read and parse a file. tree-sitter never raises on bad syntax; it
    returns a tree containing ERROR nodes, so whatever parsed cleanly is
    still usable. Only an unreadable/non-UTF-8 file raises.
    """
    source = file_path.read_bytes()
    source.decode("utf-8")  # raises UnicodeDecodeError for non-UTF-8 files
    root = _PARSER.parse(source).root_node
    if root.has_error:
        warn_skipped_partial(file_path)
    return root


def warn_skipped_partial(file_path: Path) -> None:
    import sys

    print(
        f"warning: {file_path} has syntax tree-sitter could not fully parse; "
        "using the parts that parsed",
        file=sys.stderr,
    )


def _resolve_endpoint(
    url: str,
    known_endpoints: dict[str, str],
    known_services: dict[str, str],
    require_url_shape: bool = False,
):
    return resolve_endpoint(
        url, known_endpoints, known_services, require_url_shape=require_url_shape
    )


def _string_literal(node: Node | None) -> str | None:
    """The contents of a plain Java string literal node, without quotes."""
    if node is None or node.type != "string_literal":
        return None
    value = _text(node)
    if value.startswith('"""') and value.endswith('"""') and len(value) >= 6:
        return value[3:-3]
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    return None


def _string_fields(root: Node) -> dict[str, str]:
    """Fields and local variables initialised with a plain string literal,
    e.g. `private String hostname = "http://visits-service/";` -- so a call
    like `.uri(hostname + "pets")` can still be resolved. Same-file only;
    no cross-class data flow.
    """
    fields: dict[str, str] = {}
    for node in _walk(root):
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        value = _string_literal(node.child_by_field_name("value"))
        if name is not None and value is not None:
            fields[_text(name)] = value
    return fields


def _url_text(node: Node | None, string_fields: dict[str, str]) -> str | None:
    """Best-effort URL text for a call argument: a literal, a same-file
    string field/variable, or a `+` concatenation of those. Unknown parts
    are dropped; returns None when nothing readable remains.
    """
    if node is None:
        return None
    literal = _string_literal(node)
    if literal is not None:
        return literal
    if node.type == "identifier":
        return string_fields.get(_text(node))
    if node.type == "parenthesized_expression" and node.named_children:
        return _url_text(node.named_children[0], string_fields)
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is not None and _text(operator) == "+":
            parts = [
                _url_text(node.child_by_field_name("left"), string_fields),
                _url_text(node.child_by_field_name("right"), string_fields),
            ]
            parts = [p for p in parts if p is not None]
            return "".join(parts) if parts else None
    return None


def _import_path(node: Node) -> str:
    """`import static a.b.C.*;` -> "a.b.C" (static keyword and wildcard dropped)."""
    for child in node.named_children:
        if child.type in ("scoped_identifier", "identifier"):
            return _text(child)
    return ""


def _feign_target(annotation: Node) -> str | None:
    """@FeignClient("name") or @FeignClient(name = "name" / value = "name")."""
    args = annotation.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        direct = _string_literal(child)
        if direct is not None:
            return direct
        if child.type == "element_value_pair":
            key = child.child_by_field_name("key")
            if key is not None and _text(key) in ("name", "value"):
                return _string_literal(child.child_by_field_name("value"))
    return None


def parse_file(
    file_path: Path,
    known_services: dict[str, str] | None = None,
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Parse a single Java file and return DependencyEdges it contains."""
    known_services = known_services or {}
    known_endpoints = known_endpoints or {}
    source_service = known_services.get("__self__", "unknown-service")

    root = _parse(file_path)
    string_fields = _string_fields(root)
    edges: list[DependencyEdge] = []

    for node in _walk(root):
        # 1. Imports -- resolve against known internal package namespaces.
        if node.type == "import_declaration":
            # A package like "com.org.auth.AuthClient" resolves against a
            # known namespace prefix like "com.org.auth" -- check every
            # prefix of the dotted path, longest first, so the most
            # specific registered namespace wins.
            parts = _import_path(node).split(".")
            target_service = None
            for i in range(len(parts), 0, -1):
                prefix = ".".join(parts[:i])
                if prefix in known_services:
                    target_service = known_services[prefix]
                    break
            if target_service is not None:
                edges.append(
                    DependencyEdge(
                        source_service=source_service,
                        target_service=target_service,
                        type="import",
                        source_file=str(file_path),
                        source_line=_line(node),
                        confidence="high",
                    )
                )

        # 2. @FeignClient annotations -- explicit, direct dependency.
        elif node.type in ("annotation", "marker_annotation"):
            name = node.child_by_field_name("name")
            if name is None or _text(name).split(".")[-1] != "FeignClient":
                continue
            target = _feign_target(node)
            if target is None:
                continue
            edges.append(
                DependencyEdge(
                    source_service=source_service,
                    target_service=target,
                    type="http_call",
                    source_file=str(file_path),
                    source_line=_line(node),
                    confidence="high",
                )
            )

        # 3. RestTemplate/WebClient/HttpClient-style call sites.
        elif node.type == "method_invocation":
            name = node.child_by_field_name("name")
            args = node.child_by_field_name("arguments")
            if name is None or args is None or _text(name) not in HTTP_CLIENT_METHODS:
                continue
            if not args.named_children:
                continue
            url = _url_text(args.named_children[0], string_fields)
            if url is None:
                continue
            target_service, confidence = _resolve_endpoint(
                url,
                known_endpoints,
                known_services,
                require_url_shape=_text(name) in GENERIC_METHODS,
            )
            if target_service is None:
                continue
            edges.append(
                DependencyEdge(
                    source_service=source_service,
                    target_service=target_service,
                    type="http_call",
                    source_file=str(file_path),
                    # The line of the method name, not of the whole
                    # chained expression, which starts at the receiver.
                    source_line=_line(name),
                    confidence=confidence,
                )
            )

    return edges


def parse_repo(
    repo_path: Path,
    known_services: dict[str, str],
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Walk a repo directory and parse every .java file in it."""
    edges: list[DependencyEdge] = []
    for java_file in iter_files(repo_path, "*.java"):
        try:
            found = parse_file(java_file, known_services, known_endpoints)
        except (UnicodeDecodeError, OSError) as exc:
            warn_skipped(java_file, exc)
            continue
        for edge in found:
            edge.source_file = relative_to_repo(edge.source_file, repo_path)
        edges.extend(found)
    return drop_self_edges(edges)


def _type_name(node: Node | None) -> str | None:
    """A parameter's base type name: generics and array dimensions are
    dropped (`List<String>` -> "List", `int[]` -> "int"), matching what
    earlier versions stored so existing snapshots keep comparing cleanly.
    """
    if node is None:
        return None
    if node.type == "generic_type":
        for child in node.named_children:
            if child.type in ("type_identifier", "scoped_type_identifier"):
                return _text(child).split(".")[-1]
    if node.type == "array_type":
        return _type_name(node.child_by_field_name("element"))
    return _text(node).split(".")[-1]


_TYPE_DECLARATIONS = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
)


def _enclosing_types(node: Node) -> str:
    """Dotted names of the classes/interfaces enclosing `node`, outermost first."""
    names = []
    parent = node.parent
    while parent is not None:
        if parent.type in _TYPE_DECLARATIONS:
            type_name = parent.child_by_field_name("name")
            if type_name is not None:
                names.append(_text(type_name))
        parent = parent.parent
    return ".".join(reversed(names))


def _enclosing_types_are_public(method: Node) -> bool:
    """False if the method sits in a package-private (or private) class --
    its methods are not part of the service's public interface even when
    the methods themselves are marked public.
    """
    parent = method.parent
    while parent is not None:
        if parent.type in _TYPE_DECLARATIONS:
            modifiers = next((c for c in parent.children if c.type == "modifiers"), None)
            words = set(_text(modifiers).split()) if modifiers is not None else set()
            inside_interface = parent.parent is not None and parent.parent.type == "interface_body"
            if "public" not in words and not inside_interface:
                return False
        parent = parent.parent
    return True


def _is_public(method: Node) -> bool:
    modifiers = next((c for c in method.children if c.type == "modifiers"), None)
    words = set(_text(modifiers).split()) if modifiers is not None else set()
    if "public" in words:
        return True
    # Interface methods are implicitly public unless marked private.
    parent = method.parent
    return parent is not None and parent.type == "interface_body" and "private" not in words


def extract_signatures(repo_path: Path, self_service: str) -> list[PublicSignature]:
    """Extract every public method signature across a repo's .java
    files -- the data BREAKING_CHANGE alerting compares between scans
    (SPEC.pdf section 10). Interface methods count as public even without
    the keyword. Unlike Python/JS, Java always has a real static type for
    every parameter, so `type` here is never None.
    """
    signatures: list[PublicSignature] = []
    for java_file in iter_files(repo_path, "*.java"):
        if is_test_path(java_file.relative_to(repo_path)):
            continue  # tests are not the service's public interface
        try:
            root = _PARSER.parse(java_file.read_bytes()).root_node
        except OSError:
            continue

        for node in _walk(root):
            if (
                node.type != "method_declaration"
                or not _is_public(node)
                or not _enclosing_types_are_public(node)
            ):
                continue
            name = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            if name is None:
                continue
            params = []
            for p in (params_node.named_children if params_node is not None else []):
                if p.type not in ("formal_parameter", "spread_parameter"):
                    continue
                pname = p.child_by_field_name("name")
                ptype = p.child_by_field_name("type")
                if p.type == "spread_parameter":  # String... args
                    ptype = next((c for c in p.named_children if c.type.endswith("type") or c.type == "type_identifier"), None)
                    pname = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                    pname = pname.child_by_field_name("name") if pname is not None else None
                params.append(
                    {
                        "name": _text(pname) if pname is not None else "",
                        "type": _type_name(ptype),
                    }
                )
            signatures.append(
                PublicSignature(
                    service=self_service,
                    name=_text(name),
                    params=params,
                    source_file=relative_to_repo(java_file, repo_path),
                    source_line=_line(name),
                    qualname=(
                        f"{java_file.relative_to(repo_path).as_posix()}::"
                        f"{_enclosing_types(node)}.{_text(name)}"
                    ),
                )
            )

    return signatures
