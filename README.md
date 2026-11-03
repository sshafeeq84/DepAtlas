# DepAtlas

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

*(Originally released as DepGraph — renamed to DepAtlas. The original
design doc in [`docs/SPEC.pdf`](https://github.com/sshafeeq84/DepAtlas/blob/main/docs/SPEC.pdf) predates the rename and
still uses the old name.)*

DepAtlas reads your codebase and builds a living dependency graph across
services, teams, and repositories — so blockers surface before they cost
you a sprint.

Engineering organizations above a certain size develop an invisible
coordination tax: teams block each other without knowing it, status
updates are written from memory, and nobody has a system that actually
knows what depends on what. DepAtlas reads the code itself — the one
source of truth that can't drift out of date — and turns it into a
queryable graph, automatically.

Full original design in [`docs/SPEC.pdf`](https://github.com/sshafeeq84/DepAtlas/blob/main/docs/SPEC.pdf).

<p align="center">
  <img src="https://raw.githubusercontent.com/sshafeeq84/DepAtlas/main/docs/images/example_dependency_graph.png" alt="Example dependency graph exported by depatlas, showing five services colored by owning team, generated with depatlas export --format dot" width="850">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/sshafeeq84/DepAtlas/main/docs/images/example_alerts_terminal.png" alt="Terminal output showing depatlas detecting a new cross-team dependency" width="850">
</p>

## Status

v1 core complete. Three language parsers, three
code sources, a persisted queryable graph with history and diffing, full
ownership resolution, all five spec'd alert types (including
signature-level breaking-change detection), and both JSON and DOT
export — all covered by 137 passing tests. Two things are knowingly out
of scope for now; see [Known limitations](#known-limitations).

| Area | What's there |
|---|---|
| **Languages** | Python (`ast`), Java (`tree-sitter`), JavaScript/TypeScript (`esprima`) |
| **Code sources** | Local directory, GitHub (REST API), AWS CodeCommit (`boto3`) |
| **Persistence** | SQLite-backed graph store; every scan saves an immutable snapshot |
| **Ownership** | CODEOWNERS, with a `depatlas.yaml` manifest fallback |
| **Intelligence** | Diff engine (snapshot-to-snapshot) + 5 alert types, incl. breaking-change signature detection |
| **Export** | JSON and Graphviz DOT |

## Quick start

```bash
pip install -e ".[dev]"

# Scan a local repo, resolving ownership from CODEOWNERS:
depatlas scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json \
  --codeowners tests/fixtures/sample_repo/payments/CODEOWNERS

# Query what it found:
depatlas query --service payments-service --direction downstream
depatlas query --team @payments-team

# See what changed since the last scan (run the scan above twice to
# see this produce real output; on a first scan there's nothing yet
# to compare against):
depatlas diff --last 2 --repo tests/fixtures/sample_repo/payments

# Check for cross-team dependencies, breaking changes, and more:
depatlas alerts

# Export the whole graph:
depatlas export --format dot --output graph.dot
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
| `depatlas scan` | Parse a repo (local, GitHub, or CodeCommit) and persist the resulting graph |
| `depatlas query` | Look up a service's dependencies (`--direction downstream\|upstream`) or a team's services |
| `depatlas snapshots` | List saved snapshots for a repo |
| `depatlas diff` | Compare two snapshots (`--from`/`--to` or `--last 2`) |
| `depatlas alerts` | Check the stored graph for all 5 alert types |
| `depatlas export` | Export the whole graph as JSON or DOT |

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
- **Java** (`tree-sitter`): `import` statements resolved against known
  package namespaces (high); `@FeignClient` annotations, an explicit
  dependency declaration (high); RestTemplate/WebClient-style calls
  (medium/low, same scheme as Python).
- **JavaScript/TypeScript** (`esprima`): `import`/`require(...)` (high);
  `fetch`/`axios.<method>`/`got.<method>` calls (medium/low);
  `package.json` dependencies as `shared_library` edges (high).

Tests: `tests/test_parsers/`.

### Code sources

`depatlas scan` takes exactly one of:
- **`--repo <path>`** — a local directory.
- **`--github-repo owner/repo`** — downloaded via the GitHub REST API's
  zipball endpoint (no `git clone` dependency). Requires
  `DEPATLAS_GITHUB_TOKEN`.
- **`--codecommit-repo <name>`** — needs the optional AWS extra
  (`pip install "depatlas[aws]"`); downloaded via `boto3`, walking the
  repo tree with `get_folder`/`get_file` (CodeCommit has no bulk-download
  endpoint). Uses the AWS profile in `DEPATLAS_AWS_PROFILE`, or the
  default credential chain.

Tests: `tests/test_connectors/` (all network/AWS calls mocked — no real
credentials needed to run the suite).

### Persistence and diffing

Every `scan` persists to a local SQLite store (default
`.depatlas/graph.db`) and saves an immutable snapshot of that scan's
edges. Rescanning a repo replaces its *live* edges (so the graph always
reflects current code) while snapshots accumulate, giving
`depatlas diff` a timeline to compare. Services known only as a
dependency (not yet scanned directly) get a placeholder node, filled in
once their own repo is scanned.

Tests: `tests/test_graph/`, `tests/test_intelligence/test_diff.py`.

### Ownership

Resolved in priority order: **CODEOWNERS** (majority owner across a
repo's files, last-matching-rule-wins) → **`depatlas.yaml` manifest**
fallback (explicit service→team mapping) → **unowned**.

Tests: `tests/test_ownership/`.

### Alerts

All five SPEC.pdf alert types, checked automatically after every `scan`
(`--no-alerts` to disable) or on demand via `depatlas alerts`:

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
depatlas scan \
  --repo tests/fixtures/sample_repo_java/payments \
  --services tests/fixtures/payments_services_java.json \
  --language java

# Scan a JS/TS repo:
depatlas scan \
  --repo tests/fixtures/sample_repo_js/payments \
  --services tests/fixtures/payments_services_js.json \
  --language javascript

# Scan a real GitHub repo (requires DEPATLAS_GITHUB_TOKEN):
depatlas scan \
  --github-repo sshafeeq84/DepAtlas \
  --services tests/fixtures/payments_services.json

# Scan a real AWS CodeCommit repo (requires an AWS profile):
depatlas scan \
  --codecommit-repo my-payments-repo \
  --region us-east-1 \
  --services tests/fixtures/payments_services.json

# Resolve ownership from a depatlas.yaml manifest instead of CODEOWNERS:
depatlas scan \
  --repo tests/fixtures/sample_repo/payments \
  --services tests/fixtures/payments_services.json \
  --ownership-manifest tests/fixtures/depatlas.yaml

# List and diff snapshots explicitly:
depatlas snapshots --repo tests/fixtures/sample_repo/payments
depatlas diff --from 1 --to 2

# Filter alerts by severity:
depatlas alerts --severity medium
```

## Tried on a real project

Run against the Java services of
[Spring PetClinic Microservices](https://github.com/spring-petclinic/spring-petclinic-microservices)
(one `depatlas scan` per service, with a small services/endpoints JSON):

![PetClinic graph](https://raw.githubusercontent.com/sshafeeq84/DepAtlas/main/docs/images/petclinic_graph.png)

DepAtlas found the API gateway's calls to the customers and visits
services (Spring `WebClient`) and the GenAI service's call to the vets
service. Running it on a real project also exposed bugs that the unit
tests had missed (a crash on modern Java syntax and undetected
`WebClient` calls), which are now fixed and covered by tests.

## Known limitations

A couple of things are deliberately out of scope for now, documented in
the relevant module and in [`docs/CONTRIBUTING.md`](https://github.com/sshafeeq84/DepAtlas/blob/main/docs/CONTRIBUTING.md):

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
depatlas/
├── depatlas/
│   ├── cli/            # CLI entry point (click)
│   ├── connectors/       # GitHub, AWS CodeCommit
│   ├── parsers/          # python_parser (ast), java_parser (tree-sitter),
│   │                       js_ts_parser (esprima)
│   ├── graph/             # Models, graph builder, SQLite store
│   ├── ownership/          # CODEOWNERS + depatlas.yaml manifest
│   ├── intelligence/        # Diff engine, alerts
│   └── output/                # JSON and DOT exporters
├── tests/                       # Mirrors the structure above, 1:1
├── docs/
│   ├── SPEC.pdf                  # Original v0.1 spec
│   └── CONTRIBUTING.md
└── pyproject.toml
```

## Contributing

See [`docs/CONTRIBUTING.md`](https://github.com/sshafeeq84/DepAtlas/blob/main/docs/CONTRIBUTING.md) for setup,
testing conventions, and known scope boundaries.

## License

Apache 2.0 — see [`LICENSE`](https://github.com/sshafeeq84/DepAtlas/blob/main/LICENSE).
