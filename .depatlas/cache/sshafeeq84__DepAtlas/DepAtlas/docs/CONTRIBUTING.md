# Contributing to DepAtlas

Thanks for looking at this project. It's still early (v1, pre-1.0), so
there's real room to shape how things develop.

## Getting set up

```bash
git clone https://github.com/sshafeeq84/DepAtlas.git
cd DepAtlas
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest tests/ -v
```

All tests should pass on a clean checkout. If they don't, that's a bug —
please open an issue rather than assuming it's your environment.

## Project layout

```
depatlas/
├── depatlas/
│   ├── cli/            # CLI entry points (click)
│   ├── connectors/      # GitHub, CodeCommit -- get code onto local disk
│   ├── parsers/          # Language parsers (Python/ast, Java/tree-sitter, JS-TS/esprima)
│   ├── graph/            # Data model, graph builder, SQLite store
│   ├── ownership/        # CODEOWNERS + depatlas.yaml manifest resolution
│   ├── intelligence/      # Diff engine, alerts
│   └── output/            # JSON / DOT exporters
├── tests/                  # Mirrors the structure above, 1:1
└── docs/
    └── SPEC.pdf             # Original v0.1 spec this project follows
```

## How this project is organized

Each parser, connector, and ownership source follows the same shape:
read some input, resolve it against a `--services` manifest, emit
`DependencyEdge`s with a confidence level (`high`/`medium`/`low`). If
you're adding a new language parser, the existing three
(`python_parser.py`, `java_parser.py`, `js_ts_parser.py`) are the best
reference — each one detects imports (high confidence), then some
form of explicit annotation or HTTP-call pattern (high or
medium/low depending on how much guessing is involved).

## Testing conventions

- Every new parser, connector, or CLI command needs tests before it's
  considered done.
- Network calls are never made in tests. Connectors (`github.py`,
  `codecommit.py`) are tested with a mocked `requests.get` or a small
  fake client object with canned responses — see
  `tests/test_connectors/` for the pattern.
- The CLI itself is tested with Click's `CliRunner`, not by shelling out
  — see `tests/test_cli/test_smoke.py`. Any new CLI command should get
  at least a `--help` smoke test added to that file's command list; this
  is what catches wiring bugs (like a duplicated option name) that unit
  tests of the underlying modules can't see, since they never actually
  import `depatlas.cli.main`.
- Fixture repos live under `tests/fixtures/` — small, hand-written
  sample services per language, used across multiple test files.

## Known scope boundaries (not bugs)

A few things are deliberately out of scope for v1, documented in the
relevant module's docstring and in the README:

- **Full TypeScript syntax** — the JS/TS parser uses `esprima`, which
  parses JavaScript but not TypeScript-specific syntax (type
  annotations, interfaces). A `.ts` file using real TS syntax is
  skipped with a warning, not crashed on. Moving the JS/TS parser to
  `tree-sitter` (as Java already uses) would fix this.
- **`boto3`/`grpc` detection in the Python parser**, and Java's
  Maven/Gradle shared-library detection — spec'd, not yet built.

If you want to tackle any of these, opening an issue first to discuss
approach is welcome but not required — a PR with tests is the fastest
path either way.

## Commit / PR conventions

This project uses a plain branch → PR → merge workflow. There's no
strict commit message format, but a short summary of *what* changed and
*why* (not just *that* something changed) is appreciated — see the
existing commit history for examples.

## License

Apache 2.0 — see `LICENSE`.
