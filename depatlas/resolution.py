"""URL -> service resolution shared by the Python, Java and JS/TS parsers.

Two tiers, matching SPEC.pdf 7.4's confidence table:
  - Medium: the URL contains a host/path pattern explicitly registered in
    the endpoints manifest (e.g. "auth.internal" -> "auth-service"),
    matched at a host boundary ("auth.internal" no longer matches
    "myauth.internal.example").
  - Low: no registry entry matched, but the URL's *hostname* contains a
    known service's name as a whole word. Only the hostname is looked at,
    so https://payments.example.com/api/auth/login is not a call to
    auth-service.

Guards against false edges:
  - The scanned service itself is never a target.
  - Whole-word matching ("auth" does not match "oauth" or "author").
  - Longer service names are tried first (auth-admin before auth).
  - Very short or generic names (api, web, core, ...) are never guessed.
  - Path-only URLs have no host, so they produce no guess.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

MIN_SLUG_LENGTH = 4
GENERIC_SLUGS = {"api", "web", "app", "core", "common", "service", "server", "client", "base"}


def _host(url: str) -> str:
    """The hostname part of a URL. Scheme-less text like "auth-service/verify"
    is read as host/path; a leading "/" means there is no host.
    """
    if "://" in url:
        return (urlparse(url).hostname or "").lower()
    if url.startswith("/"):
        return ""
    return url.split("/")[0].split(":")[0].lower()


def _registry_match(pattern: str, url: str) -> bool:
    if pattern[:1].isalnum():
        regex = rf"(?<![A-Za-z0-9.\-]){re.escape(pattern)}"
    else:
        regex = re.escape(pattern)
    return re.search(regex, url) is not None


def resolve_endpoint(
    url: str,
    known_endpoints: dict[str, str],
    known_services: dict[str, str],
    *,
    require_url_shape: bool = False,
) -> tuple[str | None, str | None]:
    """Returns (service_name, confidence), or (None, None) if nothing matched.

    `require_url_shape` applies to the low-confidence guess only (it then
    needs a real "scheme://" URL): an explicit endpoints-registry match is a
    deliberate mapping, so it is trusted either way. Used for method names
    that are too generic to mean "HTTP call" by themselves.
    """
    self_service = known_services.get("__self__")

    for pattern, service in known_endpoints.items():
        if service != self_service and _registry_match(pattern, url):
            return service, "medium"

    if require_url_shape and "://" not in url:
        return None, None

    host = _host(url)
    if not host:
        return None, None

    candidates = []
    for service in set(known_services.values()):
        if service == self_service:
            continue
        slug_root = service.split("-service")[0]
        if len(slug_root) < MIN_SLUG_LENGTH or slug_root.lower() in GENERIC_SLUGS:
            continue
        candidates.append((slug_root.lower(), service))

    for slug_root, service in sorted(candidates, key=lambda c: (-len(c[0]), c[1])):
        if re.search(rf"(?<![a-z0-9]){re.escape(slug_root)}(?![a-z0-9])", host):
            return service, "low"

    return None, None


def drop_self_edges(edges: list) -> list:
    """A service importing or calling itself is not a dependency."""
    return [e for e in edges if e.source_service != e.target_service]
