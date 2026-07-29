# DepGraph

An open-source agent that reads your codebase and builds a living dependency
graph across services, teams, and repositories — so blockers surface before
they cost you a sprint. See `docs/SPEC.md` for the full v1 specification.

## Status

This is an early, in-progress build. What works today:

- `depgraph scan --repo <path> --services <manifest.json> --language python|java|javascript`
  walks a local codebase and extracts dependencies using the AST for the
  chosen language (defaults to `python`):
  - **Python** (`ast` module): `import` / `from ... import` statements
    (high confidence), and `requests.get/post/put/delete/patch(...)`
    call sites resolved via an `endpoints` registry (medium) or a
    heuristic name match (low).
  - **Java** (`javalang`): `import` statements resolved against known
    internal package namespaces (high confidence); `@FeignClient`
    annotations, an explicit direct-dependency declaration (high
    confidence, no URL matching needed); and RestTemplate/WebClient
    -style call sites (`getForObject`, `postForObject`, `exchange`,
    etc.), resolved the same medium/low way as Python's `requests`
    calls. See `tests/test_parsers/test_java_parser.py`.
  - **JavaScript/TypeScript** (`esprima`): `import`/`require(...)`
    statements resolved against known internal package names (high
    confidence); `fetch`/`axios.<method>`/`got.<method>` call sites
    resolved the same medium/low way as the other languages; and
    `package.json` `dependencies`/`devDependencies` entries, resolved as
    `shared_library` edges (high confidence — a declared dependency is
    an explicit fact, not a guess). **Known limitation:** esprima parses
    JavaScript, not TypeScript-specific syntax (type annotations,
    interfaces, etc.) — a `.ts`/`.tsx` file using real TypeScript syntax
    fails to parse and is skipped with a warning rather than crashing
    the whole scan. Full TypeScript support would need a Node-based
    parser (`@babel/parser` or the TS compiler API, per SPEC.md 7.3),
    which this project deliberately avoids as a runtime dependency for
    now. See `tests/test_parsers/test_js_ts_parser.py`.
- Only dependencies that resolve against the `--services` manifest are
  emitted as edges — everything else (stdlib/third-party imports,
  unrecognized URLs) is correctly ignored. See the parser-specific test
  files for the proof.
- Every `scan` persists its results to a local SQLite graph store
  (default: `.depgraph/graph.db`). Rescanning a repo replaces just that
  repo's edges, so the graph reflects the current state of the code
  rather than accumulating stale data. Services discovered only as a
  dependency (not yet scanned directly) get a placeholder node that
  later gets filled in once their own repo is scanned — see
  `tests/test_graph/test_store.py` and `test_builder.py`.
- `depgraph query --service <name> --direction downstream|upstream` reads
  the persisted graph: `downstream` shows what a service depends on;
  `upstream` shows what depends on it (i.e. "what would this service's
  changes affect?"). `depgraph query --team <name>` instead lists every
  service owned by that team.
- `scan --codeowners <path>` resolves the scanned service's team from a
  CODEOWNERS file (SPEC.md section 8, priority 1): every file's owner is
  resolved individually (last matching rule wins, same as GitHub's own
  CODEOWNERS behavior), and the service is attributed to whichever team
  owns the majority of its files. A service scanned without
  `--codeowners`, or with no matching rule, is left unowned. See
  `tests/test_ownership/test_codeowners.py` and the team-resolution
  tests in `test_graph/test_builder.py`.
- Output is also a JSON `DependencyEdge` list, printed to stdout or
  written to a file with `--output`.

Not yet built (see `docs/SPEC.md` for full v1 scope): boto3/grpc
detection, full TypeScript syntax support, the `depgraph.yaml` manifest
fallback for ownership, the GitHub/CodeCommit connectors, the diff
engine, and alerting.

## Quick start

```bash
pip install -e ".[dev]"

# Scan the bundled Python fixture repo (persists to .depgraph/graph.db):
depgraph scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json \
  --codeowners tests/fixtures/sample_repo/payments/CODEOWNERS

# Scan the bundled Java fixture repo:
depgraph scan \
  --repo tests/fixtures/sample_repo_java/payments \
  --services tests/fixtures/payments_services_java.json \
  --language java

# Scan the bundled JS fixture repo:
depgraph scan \
  --repo tests/fixtures/sample_repo_js/payments \
  --services tests/fixtures/payments_services_js.json \
  --language javascript

# Then query what it found:
depgraph query --service payments-service --direction downstream
depgraph query --service auth-service --direction upstream
depgraph query --team @payments-team
```

The `--services` file is a small JSON manifest mapping importable module
names to service names, plus a special `__self__` key naming the service
being scanned:

```json
{
  "__self__": "payments-service",
  "auth_client": "auth-service",
  "endpoints": {
    "auth.internal": "auth-service"
  }
}
```

The optional `endpoints` key maps a URL host/path pattern to a service
name, used to resolve `requests.get/post/etc.` call sites found in the
code. This is a stand-in for the fuller `depgraph.yaml` manifest described
in `docs/SPEC.md` section 8 — that's one of the next pieces to build.

## Running tests

```bash
pytest tests/ -v
```

## Repository structure

```
depgraph/
├── depgraph/
│   ├── cli/            # CLI entry points (click)
│   ├── parsers/         # Language parsers (ast-based)
│   ├── graph/           # Data model (DependencyEdge, ServiceNode) — graph builder/store TBD
│   └── output/          # JSON / DOT exporters
├── tests/
│   ├── fixtures/         # Sample repos used by the test suite
│   └── test_parsers/
├── docs/
│   └── SPEC.md
└── pyproject.toml
```

## License

Apache 2.0 (proposed — see `docs/SPEC.md` section 13.5).
