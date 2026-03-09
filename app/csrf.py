"""CSRF protection using the Double Submit Cookie pattern.

A random token is stored in a non-HttpOnly cookie and must be echoed back
as a hidden form field on every POST request.  Because a cross-origin page
cannot read the cookie value, it cannot forge the matching form field.
"""

import secrets
from urllib.parse import parse_qs

from fastapi import Request
from markupsafe import Markup
from starlette.responses import HTMLResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "_csrf_token"
CSRF_TOKEN_LENGTH = 32
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 不需要 CSRF 保護的路由：
# - 登入表單（本身就需要輸入帳密，攻擊者無法利用）
# - 註冊流程（使用一次性 token 保護）
# - /auth/token（API 端點，用 client_secret 認證）
# 保留 CSRF 的路由：/auth/change-password、/admin/*（除了 /admin/login）
EXEMPT_PATHS = {
    "/auth/token",
    "/auth/login",
    "/auth/dashboard",
    "/auth/pre-register",
    "/auth/request-register",
    "/auth/register-request",
    "/auth/register",
    "/auth/forgot-password",
    "/admin/login",
}


def _generate_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def _get_or_create_token(request: Request) -> str:
    """Read CSRF token from cookie, or generate a new one.

    On first visit (no cookie), the generated token is cached on the request
    state so that the middleware cookie and the template hidden field use the
    same value.
    """
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        return token
    # Cache on request.state so multiple calls within the same request
    # (middleware + template) always return the same token.
    cached: str | None = getattr(request.state, "_csrf_token", None)
    if cached:
        return cached
    new_token = _generate_token()
    request.state._csrf_token = new_token
    return new_token


def csrf_input(request: Request) -> str:
    """Jinja2 global: render hidden CSRF input field."""
    token = _get_or_create_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">')


class CSRFMiddleware:
    """Pure ASGI middleware — validates CSRF token without consuming the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        method = request.method
        path = request.url.path

        # Safe methods and exempt paths skip CSRF check
        if method in SAFE_METHODS or path in EXEMPT_PATHS:
            await self._ensure_cookie(scope, receive, send, request)
            return

        # Collect request body (without consuming it for downstream)
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(body_chunks)

        # Parse form data to extract CSRF token
        content_type = request.headers.get("content-type", "")
        form_token = ""
        if "application/x-www-form-urlencoded" in content_type:
            parsed = parse_qs(body.decode("utf-8", errors="replace"))
            form_token = parsed.get(CSRF_FIELD, [""])[0]

        cookie_token = request.cookies.get(CSRF_COOKIE, "")

        if not cookie_token or not secrets.compare_digest(cookie_token, form_token):
            response = HTMLResponse(
                content="<h1>403 Forbidden</h1><p>CSRF token 驗證失敗，請重新整理頁面後再試。</p>",
                status_code=403,
            )
            await response(scope, receive, send)
            return

        # Replay the body for downstream handlers
        async def replay_receive() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        await self._ensure_cookie(scope, replay_receive, send, request)

    async def _ensure_cookie(
        self, scope: Scope, receive: Receive, send: Send, request: Request
    ) -> None:
        """Wrap send to inject CSRF cookie if not already present."""
        need_set_cookie = CSRF_COOKIE not in request.cookies
        token = _get_or_create_token(request)

        if not need_set_cookie:
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                cookie_value = (
                    f"{CSRF_COOKIE}={token}; Path=/; SameSite=Lax; Max-Age=86400"
                )
                headers.append((b"set-cookie", cookie_value.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)
