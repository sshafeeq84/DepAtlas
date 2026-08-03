from pathlib import Path

from depgraph.graph.builder import build_graph
from depgraph.graph.models import DependencyEdge


def _edge(source, target):
    return DependencyEdge(
        source_service=source,
        target_service=target,
        type="import",
        source_file="fake.py",
        source_line=1,
        confidence="high",
    )


def test_self_service_gets_full_node():
    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, Path("/repos/payments"), self_service="payments-service")

    self_node = next(n for n in nodes if n.id == "payments-service")
    assert self_node.repo == str(Path("/repos/payments"))
    assert self_node.language == "python"


def test_dependency_only_service_gets_placeholder_node():
    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, Path("/repos/payments"), self_service="payments-service")

    dep_node = next(n for n in nodes if n.id == "auth-service")
    assert dep_node.repo == ""
    assert dep_node.language == ""


def test_self_service_with_no_edges_still_gets_a_node():
    nodes = build_graph([], Path("/repos/leaf-service"), self_service="leaf-service")

    assert len(nodes) == 1
    assert nodes[0].id == "leaf-service"


def test_team_is_none_when_no_codeowners_rules_supplied():
    nodes = build_graph([], Path("/repos/leaf-service"), self_service="leaf-service")
    assert nodes[0].team is None


def test_team_resolved_via_codeowners_majority(tmp_path):
    from depgraph.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "b.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    nodes = build_graph([], tmp_path, self_service="payments-service", codeowners_rules=rules)
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team"


def test_dependency_placeholder_never_gets_a_team(tmp_path):
    from depgraph.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    edges = [_edge("payments-service", "auth-service")]
    nodes = build_graph(edges, tmp_path, self_service="payments-service", codeowners_rules=rules)

    dep_node = next(n for n in nodes if n.id == "auth-service")
    assert dep_node.team is None


def test_codeowners_resolves_non_python_files_too(tmp_path):
    # Regression test: _resolve_team used to hardcode rglob("*.py"), so
    # CODEOWNERS resolution silently did nothing for Java/JS repos. A
    # repo with only a .java file must still resolve a team.
    from depgraph.ownership.codeowners import parse_codeowners

    (tmp_path / "PaymentsApplication.java").write_text("// fake")
    (tmp_path / "CODEOWNERS").write_text("*.java @payments-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")

    nodes = build_graph([], tmp_path, self_service="payments-service", codeowners_rules=rules)
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team"


def test_manifest_used_when_no_codeowners_given(tmp_path):
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [], tmp_path, self_service="payments-service", manifest_teams=manifest_teams
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "payments-team-from-manifest"


def test_manifest_used_when_codeowners_resolves_nothing(tmp_path):
    from depgraph.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    # A CODEOWNERS file that matches nothing in this repo.
    (tmp_path / "CODEOWNERS").write_text("*.rb @some-other-team\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [],
        tmp_path,
        self_service="payments-service",
        codeowners_rules=rules,
        manifest_teams=manifest_teams,
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "payments-team-from-manifest"


def test_codeowners_takes_priority_over_manifest_when_both_resolve(tmp_path):
    from depgraph.ownership.codeowners import parse_codeowners

    (tmp_path / "a.py").write_text("# fake")
    (tmp_path / "CODEOWNERS").write_text("*.py @payments-team-from-codeowners\n")
    rules = parse_codeowners(tmp_path / "CODEOWNERS")
    manifest_teams = {"payments-service": "payments-team-from-manifest"}

    nodes = build_graph(
        [],
        tmp_path,
        self_service="payments-service",
        codeowners_rules=rules,
        manifest_teams=manifest_teams,
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team == "@payments-team-from-codeowners"


def test_unowned_when_neither_codeowners_nor_manifest_resolve(tmp_path):
    manifest_teams = {"some-other-service": "some-team"}

    nodes = build_graph(
        [], tmp_path, self_service="payments-service", manifest_teams=manifest_teams
    )
    self_node = next(n for n in nodes if n.id == "payments-service")

    assert self_node.team is None
