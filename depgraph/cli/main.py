"""DepGraph CLI.

v1 slice so far: `scan` (persists to a local SQLite graph store, with
optional CODEOWNERS-based team resolution, against a local directory, a
GitHub repo, or an AWS CodeCommit repo) and `query` (reads back from it,
by service or by team). SPEC.md section 9 has the full planned command
surface; `diff`, `watch`, `export`, `alerts`, `init` come later.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from depgraph.parsers.python_parser import parse_repo as parse_python_repo
from depgraph.parsers.java_parser import parse_repo as parse_java_repo
from depgraph.parsers.js_ts_parser import parse_repo as parse_js_ts_repo
from depgraph.output.json_export import export_edges
from depgraph.graph.builder import build_graph
from depgraph.graph.store import GraphStore
from depgraph.ownership.codeowners import parse_codeowners
from depgraph.intelligence.diff import compute_diff
from depgraph.connectors.github import GitHubConnectorError, fetch_repo as fetch_github_repo, get_token
from depgraph.connectors.codecommit import (
    CodeCommitConnectorError,
    fetch_repo as fetch_codecommit_repo,
    get_client as get_codecommit_client,
)

DEFAULT_DB_PATH = Path(".depgraph/graph.db")
DEFAULT_CACHE_DIR = Path(".depgraph/cache")

PARSERS = {
    "python": parse_python_repo,
    "java": parse_java_repo,
    "javascript": parse_js_ts_repo,
}


@click.group()
def cli() -> None:
    """DepGraph -- map service-to-service dependencies from source code."""


@cli.command()
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to a local repo directory to scan. Use this OR --github-repo OR --codecommit-repo.",
)
@click.option(
    "--github-repo",
    "github_repo",
    default=None,
    help=(
        "A GitHub repo to scan, as 'owner/repo' (e.g. 'sshafeeq84/Depgraph'). "
        "Downloaded via the GitHub API using the token in the "
        "DEPGRAPH_GITHUB_TOKEN environment variable."
    ),
)
@click.option(
    "--codecommit-repo",
    "codecommit_repo",
    default=None,
    help=(
        "An AWS CodeCommit repo name to scan. Fetched via boto3 using the "
        "AWS profile named in the DEPGRAPH_AWS_PROFILE environment "
        "variable (falls back to the default AWS credential chain if unset)."
    ),
)
@click.option(
    "--region",
    "region",
    default="us-east-1",
    show_default=True,
    help="AWS region (only used with --codecommit-repo).",
)
@click.option(
    "--ref",
    "ref",
    default="HEAD",
    show_default=True,
    help="Branch, tag, or commit SHA to scan (used with --github-repo or --codecommit-repo).",
)
@click.option(
    "--cache-dir",
    "cache_dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR,
    show_default=True,
    help="Where downloaded remote repos are extracted (used with --github-repo or --codecommit-repo).",
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
    "--language",
    "language",
    type=click.Choice(sorted(PARSERS.keys())),
    default="python",
    show_default=True,
    help="Which language parser to use for this repo.",
)
@click.option(
    "--codeowners",
    "codeowners_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a CODEOWNERS file. If supplied, the scanned service's "
        "team is resolved as the majority owner across its files "
        "(SPEC.md section 8, priority 1)."
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
def scan(
    repo_path: Path | None,
    github_repo: str | None,
    codecommit_repo: str | None,
    region: str,
    ref: str,
    cache_dir: Path,
    services_path: Path,
    language: str,
    codeowners_path: Path | None,
    output_path: Path | None,
    db_path: Path,
) -> None:
    """Scan a repo (local, GitHub, or CodeCommit), persist the results, and print the edges found."""
    sources_given = [s for s in (repo_path, github_repo, codecommit_repo) if s]
    if len(sources_given) == 0:
        raise click.UsageError("Provide one of --repo, --github-repo, or --codecommit-repo.")
    if len(sources_given) > 1:
        raise click.UsageError("Provide only one of --repo, --github-repo, or --codecommit-repo.")

    if github_repo:
        if "/" not in github_repo:
            raise click.UsageError("--github-repo must be in 'owner/repo' form.")
        owner, repo_name = github_repo.split("/", 1)
        try:
            token = get_token()
            click.echo(f"Downloading {github_repo}@{ref} from GitHub...", err=True)
            repo_path = fetch_github_repo(owner, repo_name, token, cache_dir, ref)
        except GitHubConnectorError as exc:
            raise click.ClickException(str(exc))

    if codecommit_repo:
        try:
            client = get_codecommit_client(region)
            click.echo(f"Downloading {codecommit_repo} from AWS CodeCommit ({region})...", err=True)
            commit_specifier = ref if ref != "HEAD" else ""
            repo_path = fetch_codecommit_repo(codecommit_repo, client, cache_dir, commit_specifier)
        except CodeCommitConnectorError as exc:
            raise click.ClickException(str(exc))

    # Normalize to an absolute path now, once -- everything downstream
    # (the edges table, the services table, and snapshots) keys off this
    # exact string, so a relative "--repo repo" and an equivalent
    # absolute "--repo /full/path/to/repo" must resolve to the same
    # value, or repeat scans and `diff --last` silently stop matching up.
    repo_path = repo_path.resolve()

    manifest = json.loads(services_path.read_text(encoding="utf-8"))
    known_endpoints = manifest.pop("endpoints", {})
    known_services = manifest
    self_service = known_services.get("__self__", "unknown-service")

    codeowners_rules = parse_codeowners(codeowners_path) if codeowners_path else None

    parse_repo = PARSERS[language]
    edges = parse_repo(repo_path, known_services, known_endpoints)
    nodes = build_graph(
        edges, repo_path, self_service, language=language, codeowners_rules=codeowners_rules
    )

    store = GraphStore(db_path)
    store.upsert_services(nodes)
    store.replace_edges_for_repo(str(repo_path), edges)
    snapshot_id = store.save_snapshot(str(repo_path), edges)
    store.close()

    text = export_edges(edges, output_path)
    click.echo(text)

    self_node = next(n for n in nodes if n.id == self_service)
    team_note = f", team={self_node.team}" if self_node.team else ", team=unowned"
    click.echo(
        f"\nPersisted {len(edges)} edge(s) and {len(nodes)} service node(s) to {db_path}"
        f"{team_note} (snapshot #{snapshot_id})",
        err=True,
    )
    if output_path is not None:
        click.echo(f"Also wrote JSON to {output_path}", err=True)


@cli.command()
@click.option("--service", "service_name", default=None, help="Service name to query.")
@click.option("--team", "team_name", default=None, help="List services owned by this team.")
@click.option(
    "--direction",
    type=click.Choice(["downstream", "upstream"]),
    default="downstream",
    show_default=True,
    help=(
        "Only used with --service. downstream = what this service "
        "depends on. upstream = what depends on this service "
        "(i.e. 'impact of')."
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
def query(service_name: str | None, team_name: str | None, direction: str, db_path: Path) -> None:
    """Query the persisted graph, either by --service or by --team."""
    if not service_name and not team_name:
        raise click.UsageError("Provide either --service or --team.")
    if service_name and team_name:
        raise click.UsageError("Provide only one of --service or --team, not both.")

    store = GraphStore(db_path)

    if team_name:
        services = [s for s in store.all_services() if s["team"] == team_name]
        store.close()
        if not services:
            click.echo(f"No services found owned by '{team_name}'.")
            return
        click.echo(f"Services owned by '{team_name}':")
        for svc in services:
            click.echo(f"  {svc['id']}  (repo: {svc['repo'] or 'unknown'})")
        return

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


@cli.command()
@click.option(
    "--repo",
    "repo_filter",
    default=None,
    help="Limit to snapshots of this repo path (as recorded at scan time).",
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=20,
    show_default=True,
    help="Maximum number of snapshots to list.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite graph store.",
)
def snapshots(repo_filter: str | None, limit: int, db_path: Path) -> None:
    """List saved snapshots (most recent first) -- use their ids with `diff --from/--to`."""
    if repo_filter is not None:
        repo_filter = str(Path(repo_filter).resolve())

    store = GraphStore(db_path)
    rows = store.list_snapshots(repo=repo_filter, limit=limit)
    store.close()

    if not rows:
        click.echo("No snapshots found.")
        return

    for row in rows:
        click.echo(f"#{row['id']}  {row['created_at']}  {row['repo']}")


@cli.command()
@click.option("--from", "from_snapshot_id", type=int, default=None)
@click.option("--to", "to_snapshot_id", type=int, default=None)
@click.option(
    "--last",
    "last_n",
    type=int,
    default=None,
    help="Compare the two most recent snapshots for --repo (i.e. --last 2).",
)
@click.option(
    "--repo",
    "repo_filter",
    default=None,
    help="Repo path to use with --last (required when using --last).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite graph store.",
)
def diff(
    from_snapshot_id: int | None,
    to_snapshot_id: int | None,
    last_n: int | None,
    repo_filter: str | None,
    db_path: Path,
) -> None:
    """Compare two snapshots and report added/removed dependencies.

    Either give explicit ids with --from/--to (see `depgraph snapshots`
    for available ids), or use --last 2 --repo <path> to compare a repo's
    two most recent scans.
    """
    store = GraphStore(db_path)

    if last_n is not None:
        if last_n != 2:
            store.close()
            raise click.UsageError("Only '--last 2' is currently supported.")
        if not repo_filter:
            store.close()
            raise click.UsageError("--last requires --repo.")
        repo_filter = str(Path(repo_filter).resolve())
        recent = store.list_snapshots(repo=repo_filter, limit=2)
        if len(recent) < 2:
            store.close()
            click.echo(f"Not enough snapshots for '{repo_filter}' to compare (need 2).")
            return
        to_snapshot_id, from_snapshot_id = recent[0]["id"], recent[1]["id"]
    elif from_snapshot_id is None or to_snapshot_id is None:
        store.close()
        raise click.UsageError("Provide either --from/--to, or --last 2 --repo <path>.")

    old_snapshot = store.get_snapshot(from_snapshot_id)
    new_snapshot = store.get_snapshot(to_snapshot_id)
    store.close()

    if old_snapshot is None:
        raise click.ClickException(f"No snapshot #{from_snapshot_id} found.")
    if new_snapshot is None:
        raise click.ClickException(f"No snapshot #{to_snapshot_id} found.")

    result = compute_diff(old_snapshot["edges"], new_snapshot["edges"])

    click.echo(
        f"Diff: #{from_snapshot_id} ({old_snapshot['created_at']}) "
        f"-> #{to_snapshot_id} ({new_snapshot['created_at']})"
    )

    if not result["added"] and not result["removed"]:
        click.echo("No changes.")
        return

    for edge in result["added"]:
        click.echo(
            f"  + {edge['source_service']} -> {edge['target_service']}  "
            f"[{edge['type']}, {edge['confidence']}]  "
            f"({edge['source_file']}:{edge['source_line']})"
        )
    for edge in result["removed"]:
        click.echo(
            f"  - {edge['source_service']} -> {edge['target_service']}  "
            f"[{edge['type']}, {edge['confidence']}]  "
            f"({edge['source_file']}:{edge['source_line']})"
        )


if __name__ == "__main__":
    cli()
