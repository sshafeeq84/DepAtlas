"""Python dependency parser.

v1 scope (see SPEC.md 7.1): detect
  1. `import` / `from X import Y` statements, resolved against a known
     internal service list, and
  2. `requests.get/post/put/delete/patch(...)` call sites, resolved
     against a known service endpoint registry.
boto3 and grpc detection come later — this file is deliberately narrow
so it's easy to read end to end.

Approach: walk the file's AST with ast.NodeVisitor rather than regexing
the source text. This is the idiomatic way to do source analysis in
Python — it understands the language's actual grammar, so it doesn't
get confused by imports (or URLs) sitting inside strings, comments, or
docstrings the way a regex would.
"""

from __future__ import annotations

import ast
from pathlib import Path

from depatlas.graph.models import DependencyEdge, PublicSignature

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


class DependencyVisitor(ast.NodeVisitor):
    """Walks a module's AST and records every import and requests-call it finds.

    ast.NodeVisitor works by dispatching to a visit_<NodeType> method for
    each node type it encounters as it walks the tree. We care about three
    node types here: `import x` (ast.Import), `from x import y`
    (ast.ImportFrom), and function calls in general (ast.Call) — we then
    inspect each Call to see if it looks like a `requests.<method>(...)`
    call site.
    """

    def __init__(self, source_file: str):
        self.source_file = source_file
        self.imports: list[dict] = []
        self.http_calls: list[dict] = []

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

    def visit_Call(self, node: ast.Call) -> None:
        # We're looking for calls that look like `requests.get(url)`,
        # `requests.post(url=...)`, etc. In AST terms, `requests.get` is an
        # ast.Attribute (attr="get") whose value is an ast.Name (id="requests").
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in HTTP_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id == "requests"
        ):
            url = self._extract_url(node)
            if url is not None:
                self.http_calls.append(
                    {
                        "url": url,
                        "method": func.attr,
                        "line": node.lineno,
                    }
                )
        # Keep walking — a requests call can be nested inside another call's
        # arguments, so we must not stop descending here.
        self.generic_visit(node)

    @staticmethod
    def _extract_url(node: ast.Call) -> str | None:
        """Pull a literal URL string out of a requests.<method>(...) call.

        Only handles the simple, common cases: a plain string literal as
        the first positional arg, or as a `url=` keyword arg. An f-string
        or a variable holding the URL won't resolve here — that's a known
        limitation of static AST analysis without full data-flow tracing,
        and it's fine to leave for later since the call still shows up in
        source for a human to check even if we can't auto-resolve the URL.
        """
        if node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                return first_arg.value
            return None

        for kw in node.keywords:
            if kw.arg == "url" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value

        return None


def _resolve_endpoint(url: str, known_endpoints: dict[str, str], known_services: dict[str, str]):
    """Try to match a URL string against a known service.

    Two tiers, matching SPEC.md 7.4's confidence table:
      - Medium: the URL contains a host/path pattern that's explicitly
        registered in the endpoints manifest (e.g. "auth.internal" ->
        "auth-service"). This is a deliberate, configured mapping, so
        it's more trustworthy than a guess.
      - Low: no explicit registry entry matched, but the URL happens to
        contain the name of a known service as a substring (e.g. a call
        to "https://auth-internal.example.com/verify" when we know about
        an "auth-service"). This is a heuristic string match — cheap to
        compute, easy to get wrong, so it's flagged Low rather than
        Medium.

    Returns (service_name, confidence) or (None, None) if nothing matched.
    """
    for pattern, service in known_endpoints.items():
        if pattern in url:
            return service, "medium"

    for service in set(known_services.values()):
        # Loosely check whether the service's "slug root" (e.g. "auth" out
        # of "auth-service") shows up in the URL at all.
        slug_root = service.split("-service")[0]
        if slug_root and slug_root in url:
            return service, "low"

    return None, None


def parse_file(
    file_path: Path,
    known_services: dict[str, str] | None = None,
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Parse a single Python file and return DependencyEdges it contains.

    known_services maps a top-level module/package name -> service name,
    e.g. {"auth_client": "auth-service"}. Imports that don't resolve
    against this map are skipped (they're almost certainly stdlib or
    third-party packages, not internal services) — this is the
    "AST-resolved import matching known internal service" signal from
    SPEC.md 7.4, which is High confidence precisely because it isn't a
    guess: we know the source file, the line, and the exact module name.

    known_endpoints maps a URL host/path pattern -> service name, e.g.
    {"auth.internal": "auth-service"}, used to resolve requests.get/post/
    etc. call sites found in the code.
    """
    known_services = known_services or {}
    known_endpoints = known_endpoints or {}
    source_service = known_services.get("__self__", "unknown-service")

    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))

    visitor = DependencyVisitor(str(file_path))
    visitor.visit(tree)

    edges: list[DependencyEdge] = []

    for imp in visitor.imports:
        top_level_module = imp["module"].split(".")[0]
        target_service = known_services.get(top_level_module)
        if target_service is None:
            continue  # not a known internal service; skip for v1 slice

        edges.append(
            DependencyEdge(
                source_service=source_service,
                target_service=target_service,
                type="import",
                source_file=str(file_path),
                source_line=imp["line"],
                confidence="high",
            )
        )

    for call in visitor.http_calls:
        target_service, confidence = _resolve_endpoint(call["url"], known_endpoints, known_services)
        if target_service is None:
            continue  # couldn't resolve this URL to a known service; skip

        edges.append(
            DependencyEdge(
                source_service=source_service,
                target_service=target_service,
                type="http_call",
                source_file=str(file_path),
                source_line=call["line"],
                confidence=confidence,
            )
        )

    return edges


def parse_repo(
    repo_path: Path,
    known_services: dict[str, str],
    known_endpoints: dict[str, str] | None = None,
) -> list[DependencyEdge]:
    """Walk a repo directory and parse every .py file in it."""
    edges: list[DependencyEdge] = []
    for py_file in repo_path.rglob("*.py"):
        edges.extend(parse_file(py_file, known_services, known_endpoints))
    return edges


def _param_info(arg: ast.arg) -> dict:
    """Render one function parameter as {"name", "type"}. `type` is the
    unparsed annotation expression (e.g. "int", "Optional[str]") if the
    parameter is annotated, else None -- most real-world Python code
    mixes annotated and unannotated functions, so this has to degrade
    gracefully rather than requiring full type coverage.
    """
    type_str = ast.unparse(arg.annotation) if arg.annotation is not None else None
    return {"name": arg.arg, "type": type_str}


def _collect_public_functions(node: ast.AST, results: list[dict]) -> None:
    """Collect public (non-underscore-prefixed) functions defined directly
    at module level or as class methods -- not nested inside another
    function, since a closure/helper defined inside a function body isn't
    part of the module's or class's public interface.

    Deliberately does not descend into function bodies (so a nested
    `def` inside another function is skipped entirely), but does descend
    into class bodies (so public methods are collected the same as
    module-level functions).
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not child.name.startswith("_"):
                results.append(
                    {
                        "name": child.name,
                        "params": [_param_info(a) for a in child.args.args],
                        "line": child.lineno,
                    }
                )
            # Do not descend into the function body -- anything defined
            # inside it is a private implementation detail, not part of
            # the public interface.
        elif isinstance(child, ast.ClassDef):
            _collect_public_functions(child, results)
        # Other node types (If, For, etc.) aren't descended into either
        # -- we only care about what's directly reachable at module or
        # class scope.


def extract_signatures(repo_path: Path, self_service: str) -> list[PublicSignature]:
    """Extract every public function/method signature across a repo's
    .py files -- the data BREAKING_CHANGE alerting compares between
    scans (SPEC.md section 10).
    """
    signatures: list[PublicSignature] = []
    for py_file in repo_path.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))

        results: list[dict] = []
        _collect_public_functions(tree, results)

        for r in results:
            signatures.append(
                PublicSignature(
                    service=self_service,
                    name=r["name"],
                    params=r["params"],
                    source_file=str(py_file),
                    source_line=r["line"],
                )
            )

    return signatures
