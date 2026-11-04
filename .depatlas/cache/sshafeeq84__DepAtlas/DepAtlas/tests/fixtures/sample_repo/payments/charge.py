"""Payments service: handles charge creation."""

import auth_client
from auth_client.session import verify_token
import json  # stdlib — should be ignored, not a known internal service
import requests


def create_charge(user_token: str, amount_cents: int) -> dict:
    if not verify_token(user_token):
        raise auth_client.AuthError("invalid token")

    # Explicit endpoint call — should resolve via the endpoints registry
    # at medium confidence.
    requests.get("https://auth.internal/session/validate")

    return json.dumps({"amount_cents": amount_cents, "status": "created"})


def notify_shipping(order_id: str) -> None:
    # No entry for this host in the endpoints registry, but the URL
    # contains "shipping" which happens to match a known service name —
    # should resolve at low confidence via the heuristic fallback.
    requests.post("https://internal-shipping-gateway.example.com/notify")
