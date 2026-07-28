"""Payments service: handles charge creation."""

import auth_client
from auth_client.session import verify_token
import json  # stdlib — should be ignored, not a known internal service


def create_charge(user_token: str, amount_cents: int) -> dict:
    if not verify_token(user_token):
        raise auth_client.AuthError("invalid token")

    return json.dumps({"amount_cents": amount_cents, "status": "created"})
