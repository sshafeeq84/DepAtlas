"""DepGraph CLI.

v1 slice: only `scan` is implemented, and only against a local directory
(no GitHub connector yet — see SPEC.md section 9 for the full planned
command surface). Everything else in the spec's CLI table comes later,
once there's an actual graph to query/diff/watch.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from depgraph.parsers.python_parser import parse_repo
from depgraph.output.json_export import export_edges


@click.group()
def cli() -> None:
    """DepGraph — map service-to-service dependencies from source code."""


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
        "__self__ names the service being scanned; this replaces the full "
        "depgraph.yaml manifest for now."
    ),
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write JSON output to this file instead of stdout.",
)
def scan(repo_path: Path, services_path: Path, output_path: Path | None) -> None:
    """Scan a local repo and print (or save) the dependency edges found."""
    known_services = json.loads(services_path.read_text(encoding="utf-8"))

    edges = parse_repo(repo_path, known_services)
    text = export_edges(edges, output_path)

    if output_path is not None:
        click.echo(f"Wrote {len(edges)} edge(s) to {output_path}")
    else:
        click.echo(text)


if __name__ == "__main__":
    cli()
