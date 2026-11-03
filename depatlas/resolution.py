"""URL -> service resolution shared by the Python, Java and JS/TS parsers.

Two tiers, matching SPEC.pdf 7.4's confidence table:
  - Medium: the URL contains a host/path pattern explicitly registered in
    the endpoints manifest (e.g. "auth.internal" -> "auth-service").
  - Low: no registry entry matched, but the URL contains a known service's
    name as a whole word (e.g. "https://auth-internal.example.com/verify"
    for "auth-service").

Two guards against false edges:
  - The scanned service itself is never a target (a URL containing
    "payments" inside payments-service is not a payments -> payments
    dependency).
  - The name match is on word boundaries, so "auth" no longer matches
    "oauth" or "author".
"""

from __future__ import annotations

import re


def looks_like_url(text: str) -> bool:
    """True for something shaped like a URL or path ("http://...", "/x").
    Used to stop generic methods (`config.get("auth.timeout")`) from being
    mistaken for HTTP calls on name alone.
    """
    return "://" in text or text.startswith("/")


def resolve_endpoint(
    url: str,
    known_endpoints: dict[str, str],
    known_services: dict[str, str],
    *,
    require_url_shape: bool = False,
) -> tuple[str | None, str | None]:
    """Returns (service_name, confidence), or (None, None) if nothing matched.

    `require_url_shape` applies to the low-confidence name guess only: an
    explicit endpoints-registry match is a deliberate mapping, so it is
    trusted either way.
    """
    self_service = known_services.get("__self__")

    for pattern, service in known_endpoints.items():
        if pattern in url and service != self_service:
            return service, "medium"

    if require_url_shape and not looks_like_url(url):
        return None, None

    for service in sorted(set(known_services.values())):
        if service == self_service:
            continue
        slug_root = service.split("-service")[0]
        if not slug_root:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(slug_root)}(?![A-Za-z0-9])", url):
            return service, "low"

    return None, None
