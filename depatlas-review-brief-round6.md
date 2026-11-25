# DepAtlas external review brief (round 6, v0.4.2)

**Repo:** https://github.com/sshafeeq84/DepAtlas (Apache-2.0; on PyPI as `depatlas`)
**Review this:** tag `v0.4.2` (please state the commit SHA you reviewed)
**Run it if you can:** `pip install depatlas==0.4.2` (or `pip install -e ".[dev]"` from the source), then `pytest` (323 tests; 1 skip on Windows is a symlink test).
**Last round you could only read the code. This time please run things** and tell me which findings you ran and which you only read.

## What it is
A CLI that builds a persisted service-to-service dependency graph from source (Python, Java, JS/TS) and uses it to flag risky changes in a pull request: new cross-team dependencies, and breaking changes to public functions that other services use. Flow: a nightly `scan-all` builds an org graph (JSON); each PR runs `depatlas check --base origin/main --graph org-graph.json --fail-on high`.

## What changed since your last review (v0.4.1 -> v0.4.2), please attack these
Your round-5 findings were fixed as follows. I would like each fix checked for being right, complete, and for new problems it introduces:
1. **Relative imports** (`from .x import`, `"./x"`, `"."`, `"/x"`) never resolve to another service; a Python repo's own top-level modules/packages (repo root and `src/`) are never treated as another service. `parsers/python_parser.py` (`_own_top_level_modules`), `parsers/js_ts_parser.py` (`_resolve_module`).
2. **CODEOWNERS matching** rewritten as `_globs()` in `ownership/codeowners.py` (anchoring, trailing slash, bare names, `fnmatchcase`). Compare it with GitHub's real rules: `**`, `!`, `\#`, escaped spaces, `docs/*` depth, case.
3. **`check` now reads both base and HEAD from git the same way** (`check.export_tree`): a temporary `GIT_INDEX_FILE`, `git read-tree <commit>[:<prefix>]`, then `git checkout-index -a --prefix=<dest>/` run from the repo top level. Replaces `git archive` so `export-ignore` cannot hide files. Questions: submodules, sparse checkout, `core.autocrlf`, symlinks and path safety, LFS smudge filters, Windows paths (`--prefix` with a drive letter), very large trees, `GIT_INDEX_FILE` leaking into child processes, worktrees, a repo where `HEAD` is detached, `--base` that is a SHA or tag.
4. **`--working-tree`** flag, and the default of ignoring uncommitted changes with a note.
5. **Baseline when the service folder did not exist at the base**: uses the org graph's edges for that service (`check.run_check`). Can this hide or invent alerts?
6. **`check --fail-on` without `--graph`** is a usage error.
7. **Name resolution:** `discovery.resolve_manifest` picks the service's own name by `--name`, then explicit `__self__`, then `GITHUB_REPOSITORY` matched to the registry's `github:owner/repo` path, then inference; warns when the name is not in the registry.
8. **Workflows** (`examples/github-actions/`): nightly refuses to run from a public repo (`gh repo view --json isPrivate`); PR check uses its own narrow token `DEPATLAS_GRAPH_TOKEN`, skips fork PRs, passes `github.base_ref` through an env var, pins `depatlas==0.4.2`.
9. **2 MB source-file cap** in `fsutil.iter_files`; grammar packages bounded `<0.27`.

## Still the riskiest areas (from earlier rounds)
- **False and missed edges from inference:** import-name registration, generic/stdlib guards, collisions, Java package-prefix rule (>=3 segments), `://<name>` endpoint patterns, `_name_key` (a service named `email`/`config` is matched by URL/host, never by bare import).
- **Breaking-change rules** (`intelligence/alerts.py`): Python parameter kinds, JS/TS overloads, mixed old/new snapshots.
- **Untrusted input:** `scan-all` parses third-party repos (XML/YAML/TOML/JSON, zips, symlinks, huge files).
- **Concurrency and state:** SQLite store, `scan-all --reset`, partial failures and `--allow-partial`.
- **Windows behaviour** (primary dev platform).

## Not verified live (be sceptical, and say so if you can test)
- The GitHub fetch inside `scan-all` on real GitHub (only a faked-zip test plus an earlier single-repo scan).
- The two workflows on real GitHub Actions.
- CodeCommit end to end.
Verified by me: a fresh `pip install` of 0.4.2 in a clean venv, and a scripted end-to-end run on three local git repos (`discover`, `scan-all`, `check` on a breaking PR, a no-op PR, and the `--fail-on` usage error).

## Known limitations (documented; no need to report)
CommonJS exports not recognised; newest TS syntax (`export type * from`); snapshots from before 0.3.0 skip parameter comparison once; Java packages under 3 segments not registered; Java constructors not extracted; unusual repo layouts may be mis-inferred; the graph is only as fresh as the nightly job; `scan-all` needs an explicit repo list; gateway-routed or base-URL HTTP calls need registry entries; CODEOWNERS has no `**` or negation; `*` crosses folders.

## How to report
For each finding: severity (high/medium/low), file and line, **a minimal repro (commands or a tiny repo layout)**, expected vs actual. Mark each as **confirmed (I ran it)** or **suspected (I read it)**. In earlier rounds a few suspicions did not reproduce, so a repro matters more than a long list. Also list anything you checked and found correct, so I know what was covered.

## Files provided
- `depatlas-0.4.2-source.md`: package, README, examples, pyproject (27 files)
- `depatlas-0.4.2-tests.md`: the test suite (26 files)
- `depatlas-0.4.2-source.zip`: everything, ready to run
