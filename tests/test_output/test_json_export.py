import json

from depatlas.graph.models import DependencyEdge
from depatlas.output.json_export import export_edges, export_graph_json


def test_export_edges_produces_valid_json(tmp_path):
    edge = DependencyEdge(
        source_service="payments-service",
        target_service="auth-service",
        type="import",
        source_file="charge.py",
        source_line=1,
        confidence="high",
    )

    text = export_edges([edge])
    parsed = json.loads(text)

    assert parsed["edges"][0]["target_service"] == "auth-service"


def test_export_edges_writes_to_file_when_given_a_path(tmp_path):
    edge = DependencyEdge(
        source_service="payments-service",
        target_service="auth-service",
        type="import",
        source_file="charge.py",
        source_line=1,
        confidence="high",
    )
    output_path = tmp_path / "out.json"

    export_edges([edge], output_path)

    assert output_path.exists()
    assert json.loads(output_path.read_text())["edges"][0]["source_service"] == "payments-service"


def test_export_graph_json_includes_both_nodes_and_edges():
    nodes = [{"id": "payments-service", "team": "payments-team"}]
    edges = [
        {
            "source_service": "payments-service",
            "target_service": "auth-service",
            "type": "import",
            "confidence": "high",
        }
    ]

    text = export_graph_json(nodes, edges)
    parsed = json.loads(text)

    assert parsed["nodes"] == nodes
    assert parsed["edges"] == edges


def test_export_graph_json_writes_to_file_when_given_a_path(tmp_path):
    output_path = tmp_path / "graph.json"

    export_graph_json([], [], output_path)

    assert output_path.exists()
    parsed = json.loads(output_path.read_text())
    assert parsed == {"nodes": [], "edges": []}
