# DepGraph

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

DepGraph reads your codebase and builds a living dependency graph across
services, teams, and repositories — so blockers surface before they cost
you a sprint.

Engineering organizations above a certain size develop an invisible
coordination tax: teams block each other without knowing it, status
updates are written from memory, and nobody has a system that actually
knows what depends on what. DepGraph reads the code itself — the one
source of truth that can't drift out of date — and turns it into a
queryable graph, automatically.

Full original design in [`docs/SPEC.md`](docs/SPEC.md).

## Status

Feature-complete relative to the v1 spec. Three language parsers, three
code sources, a persisted queryable graph with history and diffing, full
ownership resolution, all five spec'd alert types (including
signature-level breaking-change detection), and both JSON and DOT
export — all covered by 113 passing tests. Two things are knowingly out
of scope for now; see [Known limitations](#known-limitations).

| Area | What's there |
|---|---|
| **Languages** | Python (`ast`), Java (`javalang`), JavaScript/TypeScript (`esprima`) |
| **Code sources** | Local directory, GitHub (REST API), AWS CodeCommit (`boto3`) |
| **Persistence** | SQLite-backed graph store; every scan saves an immutable snapshot |
| **Ownership** | CODEOWNERS, with a `depgraph.yaml` manifest fallback |
| **Intelligence** | Diff engine (snapshot-to-snapshot) + 5 alert types, incl. breaking-change signature detection |
| **Export** | JSON and Graphviz DOT |

## Quick start

```bash
pip install -e ".[dev]"

# Scan a local repo, resolving ownership from CODEOWNERS:
depgraph scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json \
  --codeowners tests/fixtures/sample_repo/payments/CODEOWNERS

# Query what it found:
depgraph query --service payments-service --direction downstream
depgraph query --team @payments-team

# See what changed since the last scan (run the scan above twice to
# see this produce real output; on a first scan there's nothing yet
# to compare against):
depgraph diff --last 2 --repo tests/fixtures/sample_repo/payments

# Check for cross-team dependencies, breaking changes, and more:
depgraph alerts

# Export the whole graph:
depgraph export --format dot --output graph.dot
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

More usage examples — Java, JS/TS, GitHub, CodeCommit, ownership
manifests — are in [Full usage](#full-usage) below.

## CLI reference

| Command | Purpose |
|---|---|
| `depgraph scan` | Parse a repo (local, GitHub, or CodeCommit) and persist the resulting graph |
| `depgraph query` | Look up a service's dependencies (`--direction downstream\|upstream`) or a team's services |
| `depgraph snapshots` | List saved snapshots for a repo |
| `depgraph diff` | Compare two snapshots (`--from`/`--to` or `--last 2`) |
| `depgraph alerts` | Check the stored graph for all 5 alert types |
| `depgraph export` | Export the whole graph as JSON or DOT |

Every command supports `--help` for full option details.

## How it works

### Language parsers

Each parser walks a real AST (never regex) and extracts dependencies at
three confidence levels — **high** (an explicit, unambiguous signal),
**medium** (a configured endpoint match), and **low** (a heuristic
match) — plus public function/method signatures for breaking-change
detection.

- **Python** (`ast`): `import`/`from...import` statements (high);
  `requests.get/post/etc.` calls resolved via an endpoint registry
  (medium) or name heuristic (low).
- **Java** (`javalang`): `import` statements resolved against known
  package namespaces (high); `@FeignClient` annotations, an explicit
  dependency declaration (high); RestTemplate/WebClient-style calls
  (medium/low, same scheme as Python).
- **JavaScript/TypeScript** (`esprima`): `import`/`require(...)` (high);
  `fetch`/`axios.<method>`/`got.<method>` calls (medium/low);
  `package.json` dependencies as `shared_library` edges (high).

Tests: `tests/test_parsers/`.

### Code sources

`depgraph scan` takes exactly one of:
- **`--repo <path>`** — a local directory.
- **`--github-repo owner/repo`** — downloaded via the GitHub REST API's
  zipball endpoint (no `git clone` dependency). Requires
  `DEPGRAPH_GITHUB_TOKEN`.
- **`--codecommit-repo <name>`** — downloaded via `boto3`, walking the
  repo tree with `get_folder`/`get_file` (CodeCommit has no bulk-download
  endpoint). Uses the AWS profile in `DEPGRAPH_AWS_PROFILE`, or the
  default credential chain.

Tests: `tests/test_connectors/` (all network/AWS calls mocked — no real
credentials needed to run the suite).

### Persistence and diffing

Every `scan` persists to a local SQLite store (default
`.depgraph/graph.db`) and saves an immutable snapshot of that scan's
edges. Rescanning a repo replaces its *live* edges (so the graph always
reflects current code) while snapshots accumulate, giving
`depgraph diff` a timeline to compare. Services known only as a
dependency (not yet scanned directly) get a placeholder node, filled in
once their own repo is scanned.

Tests: `tests/test_graph/`, `tests/test_intelligence/test_diff.py`.

### Ownership

Resolved in priority order: **CODEOWNERS** (majority owner across a
repo's files, last-matching-rule-wins) → **`depgraph.yaml` manifest**
fallback (explicit service→team mapping) → **unowned**.

Tests: `tests/test_ownership/`.

### Alerts

All five SPEC.md alert types, checked automatically after every `scan`
(`--no-alerts` to disable) or on demand via `depgraph alerts`:

- **`BREAKING_CHANGE`** (high) — a public function removed, its
  parameter count changed, or a parameter's type changed at the same
  position (only when both old and new have a known type) — on a
  service with at least one known dependent. A pure parameter *rename*
  is deliberately not flagged, since most real callers invoke
  positionally.
- **`NEW_CROSS_TEAM_DEPENDENCY`** (medium) — a new edge crossing a team
  boundary.
- **`HIGH_FAN_IN`** (medium) — a service's distinct dependent count
  crosses a threshold (default 10).
- **`ORPHANED_SERVICE`** (low) — no dependents and no dependencies.
- **`UNOWNED_SERVICE`** (low) — no resolved team.

Tests: `tests/test_intelligence/test_alerts.py`.

## Full usage

```bash
# Scan a Java repo:
depgraph scan \
  --repo tests/fixtures/sample_repo_java/payments \
  --services tests/fixtures/payments_services_java.json \
  --language java

# Scan a JS/TS repo:
depgraph scan \
  --repo tests/fixtures/sample_repo_js/payments \
  --services tests/fixtures/payments_services_js.json \
  --language javascript

# Scan a real GitHub repo (requires DEPGRAPH_GITHUB_TOKEN):
depgraph scan \
  --github-repo sshafeeq84/Depgraph \
  --services tests/fixtures/payments_services.json

# Scan a real AWS CodeCommit repo (requires an AWS profile):
depgraph scan \
  --codecommit-repo my-payments-repo \
  --region us-east-1 \
  --services tests/fixtures/payments_services.json

# Resolve ownership from a depgraph.yaml manifest instead of CODEOWNERS:
depgraph scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json \
  --ownership-manifest tests/fixtures/depgraph.yaml

# List and diff snapshots explicitly:
depgraph snapshots --repo tests/fixtures/sample_repo/payments
depgraph diff --from 1 --to 2

# Filter alerts by severity:
depgraph alerts --severity medium
```

## Known limitations

A couple of things are deliberately out of scope for now, documented in
the relevant module and in [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md):

- **Full TypeScript syntax** — `esprima` parses JavaScript, not
  TypeScript-specific syntax (type annotations, interfaces). A `.ts`
  file using real TS syntax is skipped with a warning, not crashed on.
  Full support would need a Node-based parser, which this project
  avoids as a runtime dependency.
- **`boto3`/`grpc` detection** in the Python parser, and Java's
  Maven/Gradle shared-library detection — spec'd, not yet built.
- The **CodeCommit connector** is fully tested against a mocked AWS API
  but has not been exercised against a real AWS account.

## Running tests

```bash
pytest tests/ -v
```

## Repository structure

```
depgraph/
├── depgraph/
│   ├── cli/            # CLI entry point (click)
│   ├── connectors/       # GitHub, AWS CodeCommit
│   ├── parsers/          # python_parser (ast), java_parser (javalang),
│   │                       js_ts_parser (esprima)
│   ├── graph/             # Models, graph builder, SQLite store
│   ├── ownership/          # CODEOWNERS + depgraph.yaml manifest
│   ├── intelligence/        # Diff engine, alerts
│   └── output/                # JSON and DOT exporters
├── tests/                       # Mirrors the structure above, 1:1
├── docs/
│   ├── SPEC.pdf                  # Original v0.1 spec
│   └── CONTRIBUTING.md
└── pyproject.toml
```

## Contributing

See [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) for setup,
testing conventions, and known scope boundaries.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
