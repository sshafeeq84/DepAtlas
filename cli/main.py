"""DepGraph CLI.

v1 slice so far: `scan` (persists to a local SQLite graph store) and
`query` (reads back from it). No GitHub connector yet -- see SPEC.md
section 9 for the full planned command surface; `diff`, `watch`, `export`,
`alerts`, `init` come later.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from depgraph.parsers.python_parser import parse_repo
from depgraph.output.json_export import export_edges
from depgraph.graph.builder import build_graph
from depgraph.graph.store import GraphStore

DEFAULT_DB_PATH = Path(".depgraph/graph.db")


@click.group()
def cli() -> None:
    """DepGraph -- map service-to-service dependencies from source code."""


@cli.command()
@click.option(
    "--repo",
    "repo_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to a local repo directory to scan.",
)
@click.option(
    "--services",
    "services_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Path to a JSON file mapping importable module names to service "
        "names, e.g. {\"__self__\": \"payments-service\", \"auth_client\": \"auth-service\"}. "
        "__self__ names the service being scanned. An optional reserved "
        "\"endpoints\" key maps URL host/path patterns to service names for "
        "resolving requests.get/post/etc. call sites, e.g. "
        "{\"endpoints\": {\"auth.internal\": \"auth-service\"}}. This replaces "
        "the full depgraph.yaml manifest for now."
    ),
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Also write JSON output to this file (in addition to persisting).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite graph store.",
)
def scan(repo_path: Path, services_path: Path, output_path: Path | None, db_path: Path) -> None:
    """Scan a local repo, persist the results, and print the edges found."""
    manifest = json.loads(services_path.read_text(encoding="utf-8"))
    known_endpoints = manifest.pop("endpoints", {})
    known_services = manifest
    self_service = known_services.get("__self__", "unknown-service")

    edges = parse_repo(repo_path, known_services, known_endpoints)
    nodes = build_graph(edges, repo_path, self_service)

    store = GraphStore(db_path)
    store.upsert_services(nodes)
    store.replace_edges_for_repo(str(repo_path), edges)
    store.close()

    text = export_edges(edges, output_path)
    click.echo(text)
    click.echo(
        f"\nPersisted {len(edges)} edge(s) and {len(nodes)} service node(s) to {db_path}",
        err=True,
    )
    if output_path is not None:
        click.echo(f"Also wrote JSON to {output_path}", err=True)


@cli.command()
@click.option("--service", "service_name", required=True, help="Service name to query.")
@click.option(
    "--direction",
    type=click.Choice(["downstream", "upstream"]),
    default="downstream",
    show_default=True,
    help=(
        "downstream = what this service depends on. "
        "upstream = what depends on this service (i.e. 'impact of')."
    ),
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite graph store.",
)
def query(service_name: str, direction: str, db_path: Path) -> None:
    """Query the persisted graph for a service's dependencies."""
    store = GraphStore(db_path)
    edges = store.edges_for_service(service_name, direction)
    store.close()

    if not edges:
        click.echo(f"No {direction} dependencies found for '{service_name}'.")
        return

    if direction == "downstream":
        for edge in edges:
            click.echo(
                f"{service_name} -> {edge['target_service']}  "
                f"[{edge['type']}, {edge['confidence']}]  "
                f"({edge['source_file']}:{edge['source_line']})"
            )
    else:
        click.echo(f"Services that depend on '{service_name}':")
        for edge in edges:
            click.echo(
                f"{edge['source_service']} -> {service_name}  "
                f"[{edge['type']}, {edge['confidence']}]  "
                f"({edge['source_file']}:{edge['source_line']})"
            )


if __name__ == "__main__":
    cli()
