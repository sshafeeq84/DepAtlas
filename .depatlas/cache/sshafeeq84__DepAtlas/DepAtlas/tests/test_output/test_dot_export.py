from depatlas.output.dot_export import export_dot


def test_produces_valid_digraph_wrapper():
    text = export_dot([], [])

    assert text.startswith("digraph depatlas {")
    assert text.endswith("}")


def test_node_with_team_includes_team_in_label():
    nodes = [{"id": "payments-service", "team": "payments-team"}]

    text = export_dot(nodes, [])

    assert '"payments-service"' in text
    assert "payments-team" in text


def test_node_without_team_has_no_team_line_in_label():
    nodes = [{"id": "payments-service", "team": None}]

    text = export_dot(nodes, [])

    assert '"payments-service" [label="payments-service"];' in text


def test_edge_includes_type_and_confidence():
    edges = [
        {
            "source_service": "payments-service",
            "target_service": "auth-service",
            "type": "import",
            "confidence": "high",
        }
    ]

    text = export_dot([], edges)

    assert '"payments-service" -> "auth-service"' in text
    assert "import (high)" in text


def test_quotes_in_service_names_are_escaped():
    nodes = [{"id": 'weird"service', "team": None}]

    text = export_dot(nodes, [])

    assert 'weird\\"service' in text
