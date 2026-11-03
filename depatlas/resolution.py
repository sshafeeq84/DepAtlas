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
  - The low-confidence guess needs a real "scheme://host" URL. Relative
    or scheme-less text such as fetch("orders/123") has no host, so it
    produces no guess (an explicit registry entry is still honoured).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

MIN_SLUG_LENGTH = 4
GENERIC_SLUGS = {"api", "web", "app", "core", "common", "service", "server", "client", "base"}


def _host(url: str) -> str:
    """The hostname of an absolute URL; "" when there is no "scheme://"."""
    if "://" not in url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _registry_match(pattern: str, url: str) -> bool:
    # Boundary checks on both sides when the pattern starts/ends with a
    # name character: "auth.internal" must not match "myauth.internal" or
    # "auth.internal.evil.com". Patterns starting/ending with "/" (paths)
    # need no boundary on that side.
    lead = r"(?<![A-Za-z0-9.\-])" if pattern[:1].isalnum() else ""
    trail = r"(?![A-Za-z0-9.\-])" if pattern[-1:].isalnum() else ""
    return re.search(lead + re.escape(pattern) + trail, url) is not None


def resolve_endpoint(
    url: str,
    known_endpoints: dict[str, str],
    known_services: dict[str, str],
) -> tuple[str | None, str | None]:
    """Returns (service_name, confidence), or (None, None) if nothing matched."""
    self_service = known_services.get("__self__")

    # Longest pattern first, so "/api/payments" beats "/api".
    for pattern, service in sorted(known_endpoints.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if service != self_service and _registry_match(pattern, url):
            return service, "medium"

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
