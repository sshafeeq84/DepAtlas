"""Parameter kinds: Python (positional-only, keyword-only, *args, defaults)
and JavaScript/TypeScript (optional, default, rest), and the BREAKING_CHANGE
rules built on them."""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from depatlas.cli.main import cli
from depatlas.intelligence import alerts
from depatlas.intelligence.alerts import _param_problems, check_breaking_changes
from depatlas.parsers import js_ts_parser, python_parser


def _py_params(tmp_path, signature: str) -> list[dict]:
    d = tmp_path / f"py{abs(hash(signature))}"  # signatures contain characters illegal in Windows paths
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.py").write_text(f"def f({signature}): pass\n")
    return python_parser.extract_signatures(d, "svc")[0].params


def _ts_params(tmp_path, signature: str) -> list[dict]:
    d = tmp_path / f"ts{abs(hash(signature))}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.ts").write_text(f"export function f({signature}): void {{}}\n")
    return js_ts_parser.extract_signatures(d, "svc")[0].params


# ---- extraction ----


def test_python_parameter_kinds_and_defaults_are_recorded(tmp_path):
    params = _py_params(tmp_path, "a, b: int = 1, /, c=2, *args, d, e=3, **kw")
    assert [(p["name"], p["kind"], p["optional"]) for p in params] == [
        ("a", "positional_only", False),
        ("b", "positional_only", True),
        ("c", "positional_or_keyword", True),
        ("args", "var_positional", True),
        ("d", "keyword_only", False),
        ("e", "keyword_only", True),
        ("kw", "var_keyword", True),
    ]
    assert params[1]["type"] == "int"


def test_typescript_types_optionality_and_rest_are_recorded(tmp_path):
    params = _ts_params(tmp_path, "this: Foo, a: number, b?: string, c = 1, ...rest: number[]")
    assert [(p["name"], p["type"], p["kind"], p["optional"]) for p in params] == [
        ("a", "number", "positional", False),
        ("b", "string", "positional", True),
        ("c", None, "positional", True),
        ("rest", "number[]", "var_positional", True),
    ]


def test_typescript_exports_arrows_classes_and_reexports(tmp_path):
    (tmp_path / "a.ts").write_text(
        "export const g = async (user: User, opts: Opts = {}) => user;\n"
        "export const h = x => x;\n"
        "function local(p: string) {}\n"
        "export { local, local as renamed };\n"
        "export class Svc {\n"
        "  public run(job: Job, retries?: number) {}\n"
        "  private hidden(x: number) {}\n"
        "  protected prot(x: number) {}\n"
        "  #priv(x) {}\n"
        "  static make(cfg: Cfg) {}\n"
        "  constructor(a: string) {}\n"
        "}\n"
        "class NotExported { m(a) {} }\n"
        "export function over(a: string): void;\n"
        "export function over(a: number): void;\n"
        "export function over(a: any) {}\n"
    )
    sigs = js_ts_parser.extract_signatures(tmp_path, "svc")
    names = sorted(s.qualname.split("::")[1] for s in sigs)
    assert names == ["Svc.make", "Svc.run", "g", "h", "local", "over", "over", "renamed"]
    over = [s for s in sigs if s.name == "over"]
    assert {p["type"] for s in over for p in s.params} == {"string", "number"}  # not the `any` body


def test_declaration_files_and_tests_are_not_public_api(tmp_path):
    (tmp_path / "types.d.ts").write_text("export declare function f(a: string): void;\n")
    (tmp_path / "a.test.ts").write_text("export function t(a: string) {}\n")
    assert js_ts_parser.extract_signatures(tmp_path, "svc") == []


# ---- the rules, on real source ----

PYTHON_BREAKING = [
    ("a", "a, b"),                      # new required parameter
    ("a, b", "a"),                      # parameter removed
    ("a", "b"),                         # renamed: keyword callers break
    ("a, b", "b, a"),                   # reordered
    ("a, b=1", "a, b"),                 # default removed
    ("a, b", "a, *, b"),                # now keyword-only: positional callers break
    ("a, b", "a, /, b"),                # a is now positional-only: keyword callers break
    ("a, *, k", "a"),                   # keyword-only removed
    ("a", "a, *, k"),                   # new required keyword-only
    ("*, k", "*, j"),                   # keyword-only renamed
    ("*args", ""),                      # *args removed
    ("**kw", ""),                       # **kwargs removed
    ("a: int", "a: str"),               # type changed
]
PYTHON_SAFE = [
    ("a", "a, b=1"),                    # optional parameter added
    ("a", "a, *, k=1"),                 # optional keyword-only added
    ("a", "a, *args"),                  # *args added
    ("a", "a, **kw"),                   # **kwargs added
    ("a, b", "a, b=1"),                 # became optional
    ("a, /", "x, /"),                   # positional-only rename
    ("a, /", "a"),                      # positional-only became ordinary
    ("*, k", "k"),                      # keyword-only became ordinary
    ("a", "a: int"),                    # gained an annotation
    ("a, b, /", "a, *args"),            # b absorbed by *args
    ("a, b=1", "a, b=1"),               # unchanged
    ("self, x", "self, x"),
]

TS_BREAKING = [
    ("a: number", "a: number, b: string"),
    ("a: number, b: string", "a: number"),
    ("a: number, b: string", "b: string, a: number"),
    ("a: number, b?: string", "a: number, b: string"),
    ("a: number", "a: string"),
    ("...rest: number[]", ""),
]
TS_SAFE = [
    ("a: number", "a: number, b?: string"),
    ("a: number", "a: number, b = 1"),
    ("a: number", "a: number, ...rest: number[]"),
    ("a: number", "x: number"),           # a JS caller cannot use the name
    ("a: number, b: string", "a: number, b?: string"),
    ("a", "a: number"),
]


@pytest.mark.parametrize("old,new", PYTHON_BREAKING)
def test_python_breaking(tmp_path, old, new):
    assert _param_problems(_py_params(tmp_path / "o", old), _py_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", PYTHON_SAFE)
def test_python_safe(tmp_path, old, new):
    assert not _param_problems(_py_params(tmp_path / "o", old), _py_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", TS_BREAKING)
def test_typescript_breaking(tmp_path, old, new):
    assert _param_problems(_ts_params(tmp_path / "o", old), _ts_params(tmp_path / "n", new))


@pytest.mark.parametrize("old,new", TS_SAFE)
def test_typescript_safe(tmp_path, old, new):
    assert not _param_problems(_ts_params(tmp_path / "o", old), _ts_params(tmp_path / "n", new))


def test_messages_say_what_broke():
    old = [{"name": "a", "type": None, "kind": "positional_or_keyword", "optional": False}]
    new = old + [{"name": "b", "type": None, "kind": "positional_or_keyword", "optional": False}]
    assert "new required parameter 'b'" in _param_problems(old, new)


# ---- snapshots saved before kinds existed ----


def _sig(params, qual="m.py::f"):
    return {"service": "s", "name": "f", "params": params, "source_file": "m.py",
            "source_line": 1, "qualname": qual}


def test_old_snapshot_vs_new_does_not_cry_wolf_but_still_reports_removals():
    legacy = [{"name": "self", "type": None}, {"name": "a", "type": None}]
    kinded = [
        {"name": "self", "type": None, "kind": "positional_or_keyword", "optional": False},
        {"name": "a", "type": None, "kind": "positional_or_keyword", "optional": False},
        {"name": "b", "type": None, "kind": "keyword_only", "optional": True},
    ]
    dependents = [{"source_service": "other", "target_service": "s"}]
    assert check_breaking_changes("s", [_sig(legacy)], [_sig(kinded)], dependents) == []
    removed = check_breaking_changes("s", [_sig(legacy)], [], dependents)
    assert len(removed) == 1


def test_java_style_parameters_keep_the_old_rules():
    java = lambda *types: [{"name": f"p{i}", "type": t} for i, t in enumerate(types)]
    assert _param_problems(java("int"), java("int", "String"))     # count change
    assert _param_problems(java("int"), java("String"))            # type change
    assert not _param_problems(java("int"), [{"name": "renamed", "type": "int"}])


# ---- end to end: a TypeScript service in a git repo ----


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_check_catches_a_new_required_parameter_in_a_typescript_service(tmp_path):
    repo = tmp_path / "pay"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "charge.ts").write_text("export function charge(amount: number): boolean { return true; }\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "charge.ts").write_text(
        "export function charge(amount: number, currency: string): boolean { return true; }\n"
    )
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "add param")

    services = tmp_path / "s.json"
    services.write_text(json.dumps({"__self__": "pay-service"}))
    graph = tmp_path / "g.json"
    graph.write_text(json.dumps({
        "nodes": [],
        "edges": [{"source_service": "web", "target_service": "pay-service", "type": "import",
                   "source_file": "x", "source_line": 1, "confidence": "high", "from_test": 0}],
    }))
    r = CliRunner().invoke(cli, ["check", "--repo", str(repo), "--base", "main", "--services",
                                 str(services), "--graph", str(graph), "--fail-on", "high"])
    assert r.exit_code == 3, r.output
    assert "currency" in r.output

    # An optional parameter is not a breaking change.
    (repo / "charge.ts").write_text(
        "export function charge(amount: number, currency?: string): boolean { return true; }\n"
    )
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "optional")
    r = CliRunner().invoke(cli, ["check", "--repo", str(repo), "--base", "main", "--services",
                                 str(services), "--graph", str(graph), "--fail-on", "high"])
    assert r.exit_code == 0, r.output
