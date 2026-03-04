"""CSRF protection using the Double Submit Cookie pattern.

A random token is stored in a non-HttpOnly cookie and must be echoed back
as a hidden form field on every POST request.  Because a cross-origin page
cannot read the cookie value, it cannot forge the matching form field.
"""

import secrets

from fastapi import Request
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, HTMLResponse

CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "_csrf_token"
CSRF_TOKEN_LENGTH = 32
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# API endpoints that use client_secret auth instead of cookies (no browser forms)
EXEMPT_PATHS = {"/auth/token"}


def _generate_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def _get_or_create_token(request: Request) -> str:
    """Read CSRF token from cookie, or generate a new one."""
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        return token
    return _generate_token()


def csrf_input(request: Request) -> str:
    """Jinja2 global: render hidden CSRF input field."""
    token = _get_or_create_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">')


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validate CSRF token on state-changing requests."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Always ensure a CSRF cookie exists
        token = _get_or_create_token(request)
        need_set_cookie = CSRF_COOKIE not in request.cookies

        if request.method not in SAFE_METHODS and request.url.path not in EXEMPT_PATHS:
            # Read token from form body
            try:
                form = await request.form()
                form_token = form.get(CSRF_FIELD, "")
            except Exception:
                form_token = ""

            cookie_token = request.cookies.get(CSRF_COOKIE, "")

            if not cookie_token or not secrets.compare_digest(cookie_token, form_token):
                return HTMLResponse(
                    content="<h1>403 Forbidden</h1><p>CSRF token 驗證失敗，請重新整理頁面後再試。</p>",
                    status_code=403,
                )

        response = await call_next(request)

        if need_set_cookie:
            response.set_cookie(
                key=CSRF_COOKIE,
                value=token,
                httponly=False,  # JS-readable (needed for Double Submit)
                samesite="lax",
                max_age=86400,
            )

        return response
