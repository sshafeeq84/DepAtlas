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
