"""
App-side Middleware Example
===========================
This module demonstrates how an AI App should:
1. Read the JWT from an HttpOnly Cookie
2. Verify it using the Auth Center's public key (RS256)
3. Check the `aud` claim matches this app
4. Check the `scopes` claim for required permissions

Usage in a FastAPI app:
    from middleware_example.app_middleware import require_scopes

    @app.get("/protected")
    async def protected_route(user = Depends(require_scopes(["read"]))):
        return {"hello": user["name"]}
"""

from pathlib import Path
from functools import lru_cache

from fastapi import Cookie, Depends, HTTPException, status
from jose import jwt, JWTError

# === Configuration ===
# Each AI App must set these values

APP_ID = "ai_chat_app"  # Must match the app_id registered in Auth Center
PUBLIC_KEY_PATH = "./keys/public.pem"  # Path to Auth Center's public key
AUTH_CENTER_LOGIN = "http://localhost:8000/auth/login"
REDIRECT_URI = "http://localhost:8001/auth/callback"

ALGORITHM = "RS256"


@lru_cache
def _load_public_key() -> str:
    return Path(PUBLIC_KEY_PATH).read_text()


def get_current_user(access_token: str | None = Cookie(default=None)) -> dict:
    """Extract and verify the JWT from the HttpOnly cookie.

    Returns the decoded token payload.
    Raises 401 if the token is missing, invalid, or expired.
    """
    if access_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login via Auth Center.",
            headers={"Location": f"{AUTH_CENTER_LOGIN}?app_id={APP_ID}&redirect_uri={REDIRECT_URI}"},
        )

    try:
        payload = jwt.decode(
            access_token,
            _load_public_key(),
            algorithms=[ALGORITHM],
            audience=APP_ID,
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )

    return payload


def require_scopes(required: list[str]):
    """Dependency factory: ensures the user has all required scopes.

    Usage:
        @app.get("/admin")
        async def admin_route(user = Depends(require_scopes(["read", "admin"]))):
            ...
    """
    def _checker(user: dict = Depends(get_current_user)) -> dict:
        user_scopes = set(user.get("scopes", []))
        missing = set(required) - user_scopes
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Missing scopes: {missing}",
            )
        return user

    return _checker


# === Callback Route Example ===
# This is how the App handles the OAuth callback after Auth Center login.
#
# from fastapi import FastAPI, Query
# from fastapi.responses import RedirectResponse
# import httpx
#
# app = FastAPI()
#
# @app.get("/auth/callback")
# async def auth_callback(code: str = Query(...)):
#     \"\"\"Exchange the authorization code for a JWT token.\"\"\"
#     async with httpx.AsyncClient() as client:
#         resp = await client.post("http://localhost:8000/auth/token", json={
#             "code": code,
#             "app_id": APP_ID,
#             "client_secret": "chat_secret_123",  # your plain text secret
#         })
#     data = resp.json()
#
#     if "access_token" not in data:
#         raise HTTPException(400, detail="Token exchange failed")
#
#     # Store JWT in HttpOnly, Secure, SameSite cookie
#     response = RedirectResponse("/", status_code=303)
#     response.set_cookie(
#         key="access_token",
#         value=data["access_token"],
#         httponly=True,
#         secure=True,        # Set to False for local dev over HTTP
#         samesite="lax",
#         max_age=43200,      # 12 hours
#     )
#     return response
