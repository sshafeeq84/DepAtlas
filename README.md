# DepGraph

An open-source agent that reads your codebase and builds a living dependency
graph across services, teams, and repositories — so blockers surface before
they cost you a sprint. See `docs/SPEC.md` for the full v1 specification.

## Status

This is an early, in-progress build. What works today:

- `depgraph scan --repo <path> --services <manifest.json>` walks a local
  Python codebase, parses every `.py` file with the standard library `ast`
  module, and extracts:
  - `import` / `from ... import` statements that match a known internal
    service (high confidence).
  - `requests.get/post/put/delete/patch(...)` call sites, resolved against
    an optional `endpoints` registry in the manifest (medium confidence),
    with a heuristic substring-match fallback against known service names
    (low confidence) when there's no explicit registry entry.
- Only dependencies that resolve against the `--services` manifest are
  emitted as edges — everything else (stdlib, third-party packages,
  unrecognized URLs) is correctly ignored. See
  `tests/test_parsers/test_python_parser.py` for the proof.
- Output is a JSON `DependencyEdge` list, printed to stdout or written to a
  file with `--output`.

Not yet built (see `docs/SPEC.md` for full v1 scope): boto3/grpc detection,
the GitHub/CodeCommit connectors, the graph builder + SQLite store,
CODEOWNERS ownership mapping, Java/JS parsers, the diff engine, and
alerting.

## Quick start

```bash
pip install -e ".[dev]"

# Try it against the bundled fixture repo:
depgraph scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json
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
