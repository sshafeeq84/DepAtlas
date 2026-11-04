"""`depatlas scan-all`: scan many repos into one graph.

The config file lists the repos (YAML):

    repos:
      - github: acme/payments
      - github: acme/orders
        ref: main                  # default: the repo's default branch
        name: orders-service       # default: inferred from the code
        team: "@acme/orders-team"  # used when CODEOWNERS gives no team
        language: [java]           # default: detected
      - path: ./services/*         # local folders; a glob is one service each
      - codecommit: billing
        region: eu-west-1
    ownership_manifest: teams.yaml # optional depatlas.yaml (name -> team)
    cache_dir: .depatlas/cache     # optional

It runs in two passes, because a repo can only be linked to the others once
all of them are known: first every repo is fetched and described (name and
import names -> the registry), then each is scanned against the registry
of all the others. One bad repo does not stop the rest; failures are
collected and reported.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from depatlas.connectors.codecommit import fetch_repo as fetch_codecommit_repo
from depatlas.connectors.codecommit import get_client as get_codecommit_client
from depatlas.connectors.github import fetch_repo as fetch_github_repo
from depatlas.connectors.github import get_token
from depatlas.discovery import (
    DiscoveredService,
    build_manifest,
    describe_service,
    find_codeowners,
)
from depatlas.ownership.manifest import parse_ownership_manifest
from depatlas.workflow import resolve_languages, run_scan


@dataclass
class OrgRepo:
    label: str  # how the user wrote it, for messages
    kind: str  # github | codecommit | path
    target: str
    ref: str = "HEAD"
    region: str = "us-east-1"
    name: str | None = None
    team: str | None = None
    languages: tuple[str, ...] = ()


@dataclass
class OrgScanReport:
    scanned: list[dict] = field(default_factory=list)  # {"name","repo","edges"}
    failures: list[tuple[str, str]] = field(default_factory=list)  # (label, reason)
    registry: list[dict] = field(default_factory=list)


def load_org_config(path: Path) -> tuple[list[OrgRepo], dict]:
    """The repos and settings in a scan-all config file."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list) or not data["repos"]:
        raise ValueError(f"{path} must have a non-empty 'repos:' list")

    repos: list[OrgRepo] = []
    base = Path(path).resolve().parent
    for index, entry in enumerate(data["repos"], start=1):
        where = f"{path}, repos entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping like '- github: owner/repo'")
        kinds = [k for k in ("github", "codecommit", "path") if k in entry]
        if len(kinds) != 1:
            raise ValueError(f"{where}: give exactly one of github, codecommit or path")
        kind = kinds[0]
        target = str(entry[kind])
        if kind == "github" and "/" not in target:
            raise ValueError(f"{where}: github must be 'owner/repo', got '{target}'")
        language = entry.get("language") or []
        if isinstance(language, str):
            language = [language]
        common = dict(
            ref=str(entry.get("ref", "HEAD")),
            region=str(entry.get("region", "us-east-1")),
            name=entry.get("name"),
            team=entry.get("team"),
            languages=tuple(language),
        )
        if kind == "path":
            pattern = target if Path(target).is_absolute() else str(base / target)
            matches = sorted(Path(m) for m in glob.glob(pattern) if Path(m).is_dir())
            if not matches:
                raise ValueError(f"{where}: no folder matches '{target}'")
            if common["name"] and len(matches) > 1:
                raise ValueError(f"{where}: 'name' cannot be used with a pattern matching several folders")
            for match in matches:
                repos.append(OrgRepo(label=f"path:{match}", kind="path", target=str(match), **common))
        else:
            repos.append(OrgRepo(label=f"{kind}:{target}", kind=kind, target=target, **common))

    settings = {
        "ownership_manifest": (
            str(base / data["ownership_manifest"]) if data.get("ownership_manifest") else None
        ),
        "cache_dir": str(base / data["cache_dir"]) if data.get("cache_dir") else None,
    }
    return repos, settings


def _fetch(repo: OrgRepo, cache_dir: Path, state: dict) -> tuple[Path, str]:
    """(local folder, stable repo key) for a repo, downloading if remote."""
    if repo.kind == "path":
        path = Path(repo.target).resolve()
        return path, str(path)
    if repo.kind == "github":
        state.setdefault("token", get_token())
        owner, name = repo.target.split("/", 1)
        return fetch_github_repo(owner, name, state["token"], cache_dir, repo.ref), f"github:{repo.target}"
    client = state.setdefault(("cc", repo.region), get_codecommit_client(repo.region))
    specifier = repo.ref if repo.ref != "HEAD" else ""
    return fetch_codecommit_repo(repo.target, client, cache_dir, specifier), f"codecommit:{repo.target}"


def scan_all(
    config_path: Path,
    db_path: Path,
    parsers: dict,
    extractors: dict,
    cache_dir: Path | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> OrgScanReport:
    repos, settings = load_org_config(config_path)
    cache = cache_dir or (Path(settings["cache_dir"]) if settings["cache_dir"] else Path(".depatlas/cache"))
    org_teams = (
        parse_ownership_manifest(Path(settings["ownership_manifest"]))
        if settings["ownership_manifest"]
        else {}
    )
    report = OrgScanReport()
    state: dict = {}

    # Pass 1: fetch and describe every repo.
    fetched: list[tuple[OrgRepo, Path, str, DiscoveredService]] = []
    used_names: dict[str, str] = {}
    for repo in repos:
        try:
            log(f"Fetching {repo.label}...")
            path, key = _fetch(repo, cache, state)
            service = describe_service(path, name=repo.name)
            service.path = key
            if service.name in used_names:
                raise ValueError(
                    f"service name '{service.name}' is already used by {used_names[service.name]}; "
                    "set 'name:' for one of them in the config"
                )
            used_names[service.name] = repo.label
            fetched.append((repo, path, key, service))
        except Exception as exc:  # one bad repo must not stop the others
            report.failures.append((repo.label, str(exc)))
            log(f"  failed: {exc}")
    report.registry = [service.to_dict() for *_, service in fetched]

    # Pass 2: scan each against all the others.
    for repo, path, key, service in fetched:
        try:
            known_services, known_endpoints, warnings = build_manifest(service.name, report.registry)
            for warning in warnings:
                log(f"warning: {service.name}: {warning}")
            teams = dict(org_teams)
            if repo.team:
                teams[service.name] = repo.team
            codeowners = find_codeowners(path)
            languages = resolve_languages(path, repo.languages, parsers, lambda m: None)
            result = run_scan(
                path,
                key,
                known_services,
                known_endpoints,
                languages,
                parsers,
                extractors,
                db_path,
                codeowners_path=codeowners,
                manifest_teams=teams or None,
                ownership_inputs_supplied=bool(codeowners or service.name in teams),
                alerts_enabled=False,
            )
            report.scanned.append({"name": service.name, "repo": key, "edges": len(result.edges)})
            log(f"Scanned {service.name}: {len(result.edges)} edge(s)")
        except Exception as exc:
            report.failures.append((repo.label, str(exc)))
            log(f"  failed to scan {repo.label}: {exc}")
    return report
