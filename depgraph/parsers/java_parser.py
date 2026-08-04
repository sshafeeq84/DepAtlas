"""Java dependency parser.

v1 scope (see SPEC.md 7.2): detect
  1. `import` statements, resolved against a known internal package
     namespace list (same idea as the Python parser's import detection).
  2. `@FeignClient` annotations on a class/interface -- an explicit,
     direct service dependency declaration, so this is High confidence
     per SPEC.md 7.4's "Explicit annotation" row, no URL matching needed.
  3. RestTemplate/WebClient/HttpClient-style call sites (e.g.
     `restTemplate.getForObject(url, ...)`), resolved against an
     endpoints registry (medium confidence) or a heuristic name match
     (low confidence) -- the same two-tier scheme the Python parser uses
     for `requests.get/post/etc.`.

Tool: the `javalang` library, which builds a full Java AST (not a regex
parse) -- same rationale as using Python's own `ast` module: source-code
truth over pattern guessing.
"""

from __future__ import annotations

from pathlib import Path

import javalang

from depgraph.graph.models import DependencyEdge, PublicSignature

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
}


def _resolve_endpoint(url: str, known_endpoints: dict[str, str], known_services: dict[str, str]):
    """Same two-tier resolution as the Python parser's _resolve_endpoint:
    an explicit endpoints-registry match is Medium confidence; a loose
    substring match against a known service name is Low confidence.
    """
    for pattern, service in known_endpoints.items():
        if pattern in url:
            return service, "medium"

    for service in set(known_services.values()):
        slug_root = service.split("-service")[0]
        if slug_root and slug_root in url:
            return service, "low"

    return None, None


def _extract_string_literal(node) -> str | None:
    """Pull a plain string out of a javalang Literal node's `.value`.

    javalang keeps the surrounding quote characters in `.value` (e.g.
    '"auth-service"'), so this strips them. Returns None for anything
    that isn't a simple string literal (e.g. a variable reference) --
    same limitation as the Python parser's URL extraction: no data-flow
    tracing, just what's written directly in the call.
    """
    if not isinstance(node, javalang.tree.Literal):
        return None
    value = node.value
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
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

    source = file_path.read_text(encoding="utf-8")
    tree = javalang.parse.parse(source)

    edges: list[DependencyEdge] = []

    # 1. Imports -- resolve against known internal package namespaces.
    for imp in tree.imports:
        # A package like "com.org.auth.AuthClient" resolves against a
        # known namespace prefix like "com.org.auth" -- check every
        # prefix of the dotted path, longest first, so the most specific
        # registered namespace wins.
        parts = imp.path.split(".")
        target_service = None
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in known_services:
                target_service = known_services[prefix]
                break

        if target_service is None:
            continue

        edges.append(
            DependencyEdge(
                source_service=source_service,
                target_service=target_service,
                type="import",
                source_file=str(file_path),
                source_line=imp.position.line if imp.position else 0,
                confidence="high",
            )
        )

    # 2. @FeignClient annotations -- explicit, direct dependency.
    for _path, type_decl in tree.filter(javalang.tree.TypeDeclaration):
        for ann in type_decl.annotations:
            if ann.name != "FeignClient":
                continue

            target_service = None
            # Two shapes: @FeignClient("name") (a bare Literal) or
            # @FeignClient(name = "name") (a list of ElementValuePair).
            if isinstance(ann.element, javalang.tree.Literal):
                target_service = _extract_string_literal(ann.element)
            elif isinstance(ann.element, list):
                for pair in ann.element:
                    if pair.name in ("name", "value"):
                        target_service = _extract_string_literal(pair.value)
                        break

            if target_service is None:
                continue

            edges.append(
                DependencyEdge(
                    source_service=source_service,
                    target_service=target_service,
                    type="http_call",
                    source_file=str(file_path),
                    source_line=ann.position.line if ann.position else 0,
                    confidence="high",
                )
            )

    # 3. RestTemplate/WebClient/HttpClient-style call sites.
    for _path, call in tree.filter(javalang.tree.MethodInvocation):
        if call.member not in HTTP_CLIENT_METHODS:
            continue
        if not call.arguments:
            continue

        url = _extract_string_literal(call.arguments[0])
        if url is None:
            continue

        target_service, confidence = _resolve_endpoint(url, known_endpoints, known_services)
        if target_service is None:
            continue

        edges.append(
            DependencyEdge(
                source_service=source_service,
                target_service=target_service,
                type="http_call",
                source_file=str(file_path),
                source_line=call.position.line if call.position else 0,
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
    for java_file in repo_path.rglob("*.java"):
        edges.extend(parse_file(java_file, known_services, known_endpoints))
    return edges


def extract_signatures(repo_path: Path, self_service: str) -> list[PublicSignature]:
    """Extract every public method signature across a repo's .java
    files -- the data BREAKING_CHANGE alerting compares between scans
    (SPEC.md section 10). "Public" here means javalang reports `public`
    in the method's modifier set; unlike the Python parser's
    underscore-prefix convention, Java has an explicit access modifier
    to check instead of a naming convention. Unlike Python/JS, Java
    always has a real static type for every parameter, so `type` here
    is never None.
    """
    signatures: list[PublicSignature] = []
    for java_file in repo_path.rglob("*.java"):
        source = java_file.read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)

        for _path, method in tree.filter(javalang.tree.MethodDeclaration):
            if "public" not in method.modifiers:
                continue

            signatures.append(
                PublicSignature(
                    service=self_service,
                    name=method.name,
                    params=[{"name": p.name, "type": p.type.name} for p in method.parameters],
                    source_file=str(java_file),
                    source_line=method.position.line if method.position else 0,
                )
            )

    return signatures
