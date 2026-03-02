"""JWT creation and verification using RS256."""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

ALGORITHM = "RS256"
TOKEN_EXPIRE_HOURS = 12


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
        "sub": sub,
        "name": sub,         # backward compatible (display name = employee_name)
        "dept": org_id,      # backward compatible
        "org_id": org_id,
        "scopes": scopes,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    return jwt.encode(payload, settings.private_key, algorithm=ALGORITHM)


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
        options=options,
    )
