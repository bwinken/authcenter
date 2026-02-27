"""JWT creation and verification using RS256."""

from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError

from app.config import get_settings

ALGORITHM = "RS256"
TOKEN_EXPIRE_HOURS = 12


def create_token(
    sub: str,
    name: str,
    dept: str,
    scopes: list[str],
    aud: str,
) -> str:
    """Sign a JWT with the private key. Returns the encoded token string."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "name": name,
        "dept": dept,
        "scopes": scopes,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.private_key, algorithm=ALGORITHM)


def verify_token(token: str, public_key: str, expected_aud: str | None = None) -> dict:
    """Verify a JWT with a public key. Returns the decoded payload.

    Raises jose.JWTError on invalid/expired tokens.
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
