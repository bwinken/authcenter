"""JWT creation and verification using RS256."""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

ALGORITHM = "RS256"
TOKEN_EXPIRE_HOURS = 12


def _get_issuer() -> str:
    """Return the canonical issuer URL (AUTH_CENTER_BASE_URL without trailing slash)."""
    return get_settings().AUTH_CENTER_BASE_URL.rstrip("/")


def _get_kid() -> str:
    """Return the JWK kid for the current public key."""
    from app.oidc.jwks import get_kid
    return get_kid(get_settings().public_key)


def create_token(
    sub: str,
    org_id: str,
    scopes: list[str],
    aud: str,
    expire_hours: int | None = None,
) -> str:
    """Sign a JWT with the private key. Returns the encoded token string."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    hours = expire_hours if expire_hours is not None else TOKEN_EXPIRE_HOURS
    payload = {
        "iss": _get_issuer(),
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(hours=hours),
        "org_id": org_id,
        "scopes": scopes,
    }
    return jwt.encode(
        payload, settings.private_key, algorithm=ALGORITHM,
        headers={"kid": _get_kid()},
    )


def verify_token(token: str, public_key: str, expected_aud: str | None = None) -> dict:
    """Verify a JWT with a public key. Returns the decoded payload.

    Raises jwt.PyJWTError on invalid/expired tokens.
    """
    options = {}
    if expected_aud is None:
        options["verify_aud"] = False

    return jwt.decode(
        token,
        public_key,
        algorithms=[ALGORITHM],
        audience=expected_aud,
        issuer=_get_issuer(),
        options=options,
    )
