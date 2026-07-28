"""Auth service: session/token verification. No internal imports of its own."""


def verify_token(token: str) -> bool:
    return bool(token)


class AuthError(Exception):
    pass
