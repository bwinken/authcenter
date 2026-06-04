"""OIDC-compatible endpoints for OAuth2 proxy integration.

Provides standard OpenID Connect discovery, JWKS, authorize, token,
and userinfo endpoints. Reuses core auth logic from app.auth.service.
"""

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt as pyjwt
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.config import get_settings, load_registered_apps
from app.database import get_mssql_session, get_sqlite_session
from app.auth import service
from app.auth.jwt_handler import create_token, verify_token, ALGORITHM
from app.oidc.jwks import get_jwks, get_kid

router = APIRouter(tags=["oidc"])
templates: Jinja2Templates | None = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Discovery ─────────────────────────────────────────────

@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    """OpenID Connect Discovery document."""
    settings = get_settings()
    base = settings.AUTH_CENTER_BASE_URL.rstrip("/")
    return {
        "issuer": f"{base}",
        "authorization_endpoint": f"{base}/oidc/authorize",
        "token_endpoint": f"{base}/oidc/token",
        "userinfo_endpoint": f"{base}/oidc/userinfo",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "claims_supported": [
            "sub", "iss", "aud", "exp", "iat", "nonce",
            "name", "preferred_username", "org_id",
            "email", "email_verified",
        ],
        "grant_types_supported": ["authorization_code"],
    }


@router.get("/.well-known/jwks.json")
async def jwks_endpoint():
    """JSON Web Key Set — public keys for JWT verification."""
    settings = get_settings()
    jwks = get_jwks(settings.public_key)
    return JSONResponse(jwks, headers={"Cache-Control": "public, max-age=86400"})


# ─── Authorize ─────────────────────────────────────────────

@router.get("/oidc/authorize", response_class=HTMLResponse)
async def oidc_authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("openid"),
    state: str = Query(""),
    nonce: str = Query(""),
):
    """OIDC Authorization Endpoint — renders login page with OIDC context."""
    if response_type != "code":
        return JSONResponse(
            {"error": "unsupported_response_type"},
            status_code=400,
        )

    apps = load_registered_apps()
    app_info = apps.get(client_id)
    if app_info is None:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "未註冊的應用程式。",
            "app_id": client_id,
            "redirect_uri": redirect_uri,
            "app_name": "Unknown",
            "fatal": True,
        })

    if app_info["redirect_uri"] != redirect_uri:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Redirect URI 不匹配，請聯繫應用程式管理員確認設定。",
            "app_id": client_id,
            "redirect_uri": redirect_uri,
            "app_name": app_info.get("name", client_id),
            "fatal": True,
        })

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "app_id": client_id,
        "redirect_uri": redirect_uri,
        "app_name": app_info.get("name", client_id),
        "form_action": "/oidc/authorize",
        "oidc_state": state,
        "oidc_nonce": nonce,
        "oidc_scope": scope,
    })


@router.post("/oidc/authorize")
async def oidc_authorize_submit(
    request: Request,
    employee_name: str = Form(...),
    password: str = Form(...),
    app_id: str = Form(...),
    redirect_uri: str = Form(...),
    oidc_state: str = Form(""),
    oidc_nonce: str = Form(""),
    oidc_scope: str = Form("openid"),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """OIDC authorize POST — authenticate and redirect with code + state."""
    employee_name = service.normalize_employee_name(employee_name)

    apps = load_registered_apps()
    app_info = apps.get(app_id)
    app_name = app_info.get("name", app_id) if app_info else "Unknown"

    def _oidc_login_url() -> str:
        """Build the OIDC authorize URL to redirect back after registration."""
        params = urlencode({
            "response_type": "code",
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": oidc_scope,
            "state": oidc_state,
            "nonce": oidc_nonce,
        })
        return f"/oidc/authorize?{params}"

    def _error(msg: str):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": msg,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": app_name,
            "form_action": "/oidc/authorize",
            "oidc_state": oidc_state,
            "oidc_nonce": oidc_nonce,
            "oidc_scope": oidc_scope,
        })

    # Rate limit
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return _error("登入嘗試過於頻繁，請 5 分鐘後再試。")

    if app_info is None or app_info["redirect_uri"] != redirect_uri:
        return _error("應用程式驗證失敗，請從 App 重新發起登入。")

    # Authenticate
    staff, error = await service.authenticate(
        mssql_session, sqlite_session, employee_name, password
    )

    if error == "needs_registration":
        reg_token = await service.generate_registration_token(
            sqlite_session, employee_name, app_id, redirect_uri
        )
        return templates.TemplateResponse("not_registered.html", {
            "request": request,
            "employee_name": employee_name,
            "token": reg_token,
            "login_url": _oidc_login_url(),
            "success": False,
            "error": None,
        })

    if error:
        return _error(error)

    # Check app access
    allowed, reason, _scopes = await service.check_app_access(
        sqlite_session, staff, app_info
    )
    if not allowed:
        return _error(reason)

    # Generate auth code with OIDC nonce
    code = await service.generate_auth_code(
        sqlite_session, staff.employee_name, app_id, nonce=oidc_nonce
    )

    # Build redirect with code + state
    params = {"code": code}
    if oidc_state:
        params["state"] = oidc_state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=303)


# ─── Token ─────────────────────────────────────────────────

@router.post("/oidc/token")
async def oidc_token(
    request: Request,
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """OIDC Token Endpoint — exchanges authorization code for access_token + id_token.

    Accepts application/x-www-form-urlencoded (standard) and application/json.
    Supports client_secret_post and client_secret_basic authentication.
    """
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        grant_type = form.get("grant_type", "")
        code = form.get("code", "")
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        redirect_uri = form.get("redirect_uri", "")
    else:
        body = await request.json()
        grant_type = body.get("grant_type", "")
        code = body.get("code", "")
        client_id = body.get("client_id", "")
        client_secret = body.get("client_secret", "")
        redirect_uri = body.get("redirect_uri", "")

    # Support client_secret_basic (Authorization: Basic base64(client_id:client_secret))
    if not client_id or not client_secret:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                basic_id, basic_secret = decoded.split(":", 1)
                client_id = client_id or basic_id
                client_secret = client_secret or basic_secret
            except Exception:
                pass

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    # Rate limit
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    apps = load_registered_apps()
    app_info = apps.get(client_id)
    if app_info is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    # Verify client_secret
    if not bcrypt.verify(client_secret, app_info["client_secret"]):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    # Verify redirect_uri matches (required by OIDC spec)
    if redirect_uri and app_info["redirect_uri"] != redirect_uri:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
            status_code=400,
        )

    # Consume the authorization code
    code_data = await service.consume_auth_code(sqlite_session, code, client_id)
    if code_data is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    employee_name = code_data["employee_name"]
    nonce = code_data["nonce"]

    # Fetch staff info
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        return JSONResponse({"error": "staff_not_found"}, status_code=400)

    # Resolve scopes
    allowed, reason, scopes = await service.check_app_access(
        sqlite_session, staff, app_info
    )
    if not allowed:
        return JSONResponse({"error": "no_permission"}, status_code=403)

    expire_hours = app_info.get("token_expire_hours", 12)

    # Access token (standard auth-center JWT)
    access_token = create_token(
        sub=staff.employee_name,
        org_id=staff.org_id,
        scopes=scopes,
        aud=client_id,
        expire_hours=expire_hours,
    )

    # ID token (OIDC-compliant, with issuer = base URL)
    id_token = _create_id_token(
        sub=staff.employee_name,
        aud=client_id,
        nonce=nonce,
        org_id=staff.org_id,
        expire_hours=expire_hours,
    )

    # Log app access
    app_name = app_info.get("name", client_id)
    await sqlite_session.execute(
        text(
            "INSERT INTO app_access_log (employee_name, app_id, app_name, ip_address) "
            "VALUES (:ename, :app_id, :app_name, :ip)"
        ),
        {"ename": employee_name, "app_id": client_id, "app_name": app_name, "ip": client_ip},
    )
    await sqlite_session.commit()

    return {
        "access_token": access_token,
        "id_token": id_token,
        "token_type": "bearer",
        "expires_in": expire_hours * 3600,
    }


def _build_email(nt_account: str) -> str:
    """以 nt_account + COMPANY_EMAIL_DOMAIN 組合 email（未設定域名時回傳空字串）。"""
    domain = get_settings().COMPANY_EMAIL_DOMAIN.strip().lstrip("@")
    if not domain or not nt_account:
        return ""
    return f"{nt_account}@{domain}"


def _create_id_token(
    sub: str, aud: str, nonce: str, org_id: str, expire_hours: int
) -> str:
    """Create an OIDC ID Token (RS256 JWT with kid header)."""
    settings = get_settings()
    base = settings.AUTH_CENTER_BASE_URL.rstrip("/")
    now = datetime.now(timezone.utc)

    payload = {
        "iss": base,
        "sub": sub,
        "aud": aud,
        "exp": now + timedelta(hours=expire_hours),
        "iat": now,
        "auth_time": int(now.timestamp()),
        "name": sub,
        "preferred_username": sub,
        "org_id": org_id,
    }
    # email claim（Langfuse、Grafana 等 OIDC client 以 email 作為使用者唯一識別）
    email = _build_email(sub)
    if email:
        payload["email"] = email
        payload["email_verified"] = True
    if nonce:
        payload["nonce"] = nonce

    kid = get_kid(settings.public_key)
    return pyjwt.encode(
        payload, settings.private_key, algorithm=ALGORITHM, headers={"kid": kid}
    )


# ─── UserInfo ──────────────────────────────────────────────

@router.get("/oidc/userinfo")
async def oidc_userinfo(request: Request):
    """OIDC UserInfo Endpoint — returns user claims from Bearer token."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    token = auth[7:]
    settings = get_settings()
    try:
        payload = verify_token(token, settings.public_key)
    except Exception:
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    claims = {
        "sub": payload["sub"],
        "name": payload["sub"],
        "preferred_username": payload["sub"],
        "org_id": payload.get("org_id", ""),
    }
    email = _build_email(payload["sub"])
    if email:
        claims["email"] = email
        claims["email_verified"] = True
    return claims
