"""Auth Center API routes."""

from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_registered_apps, get_settings
from app.database import get_mssql_session, get_sqlite_session
from app.auth import service
from app.auth.jwt_handler import create_token, verify_token
from app.schemas import TokenRequest, ForgotPasswordRequest
from loguru import logger
from app.webhook.teams import send_forgot_password_notification, send_registration_request_notification

router = APIRouter(prefix="/auth", tags=["auth"])
templates: Jinja2Templates | None = None


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For behind reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Login Page ───────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    app_id: str = Query(...),
    redirect_uri: str = Query(...),
):
    """渲染登入頁面。

    App 端將使用者重導至此端點，帶上 app_id 與 redirect_uri 參數。
    系統會驗證 app_id 是否已註冊，以及 redirect_uri 是否與註冊資訊匹配。
    """
    apps = load_registered_apps()
    app_info = apps.get(app_id)
    if app_info is None:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "未註冊的應用程式。",
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": "Unknown",
        })

    # Validate redirect_uri matches registered app
    if app_info["redirect_uri"] != redirect_uri:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Redirect URI 不匹配。",
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": app_info.get("name", app_id),
        })

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "app_name": app_info.get("name", app_id),
    })


@router.post("/login")
async def login_submit(
    request: Request,
    employee_name: str = Form(...),
    password: str = Form(...),
    app_id: str = Form(...),
    redirect_uri: str = Form(...),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理登入表單提交。

    驗證流程：
    0. 檢查頻率限制（同一 IP 5 分鐘內最多 10 次）
    1. 重新驗證 app_id + redirect_uri（防止表單竄改）
    2. 查 MSSQL 確認員工在職
    3. 查 SQLite 確認帳號是否已註冊（未註冊則導向註冊頁）
    4. 驗證密碼是否正確
    5. 檢查該員工是否有權存取目標 App
    6. 產生 authorization code，302 重導回 App 的 redirect_uri
    """
    employee_name = service.normalize_employee_name(employee_name)

    apps = load_registered_apps()
    app_info = apps.get(app_id)
    app_name = app_info.get("name", app_id) if app_info else "Unknown"

    def _error_response(error: str):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": error,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": app_name,
        })

    # Rate limit check
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return _error_response("登入嘗試過於頻繁，請 5 分鐘後再試。")

    # Re-validate app_id and redirect_uri (hidden fields can be tampered)
    if app_info is None or app_info["redirect_uri"] != redirect_uri:
        return _error_response("應用程式驗證失敗，請從 App 重新發起登入。")

    # Authenticate
    staff, error = await service.authenticate(
        mssql_session, sqlite_session, employee_name, password
    )

    if error == "needs_registration":
        # Show confirmation page — user decides whether to register
        reg_token = await service.generate_registration_token(sqlite_session, employee_name, app_id, redirect_uri)
        return templates.TemplateResponse("not_registered.html", {
            "request": request,
            "employee_name": employee_name,
            "token": reg_token,
            "login_url": f"/auth/login?app_id={app_id}&redirect_uri={redirect_uri}",
            "success": False,
            "error": None,
        })

    if error:
        return _error_response(error)

    # Check app access permission (org check + per-user level required)
    allowed, reason, _scopes = await service.check_app_access(sqlite_session, staff, app_info)
    if not allowed:
        return _error_response(reason)

    # Generate authorization code (SQLite-backed) and redirect back to app
    code = await service.generate_auth_code(sqlite_session, staff.employee_name, app_id)
    return RedirectResponse(f"{redirect_uri}?code={code}", status_code=303)


# ─── Pre-Register (new user entry point) ─────────────────────

@router.get("/pre-register", response_class=HTMLResponse)
async def pre_register_page(
    request: Request,
    app_id: str = Query(""),
    redirect_uri: str = Query(""),
):
    """渲染預註冊頁面。

    新使用者從登入頁面點擊「還沒有帳號」後進入此頁面，
    輸入使用者名稱後系統檢查是否需要註冊。
    """
    return templates.TemplateResponse("pre_register.html", {
        "request": request,
        "error": None,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
    })


@router.post("/pre-register")
async def pre_register_submit(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(""),
    redirect_uri: str = Form(""),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理預註冊表單。

    驗證流程：
    1. 查 MSSQL 確認員工存在
    2. 查 SQLite 確認帳號尚未註冊
    3. 產生 registration token，導向身份驗證頁面
    """
    employee_name = service.normalize_employee_name(employee_name)

    ctx = {
        "request": request,
        "error": None,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
    }

    # Rate limit check
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        ctx["error"] = "請求過於頻繁，請 5 分鐘後再試。"
        return templates.TemplateResponse("pre_register.html", ctx)

    # Check staff exists in MSSQL
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        ctx["error"] = "在公司員工名單中查無此名稱，請確認輸入是否正確。"
        return templates.TemplateResponse("pre_register.html", ctx)

    # Check if already registered in SQLite
    has_account = await service.check_account_exists(sqlite_session, employee_name)
    if has_account:
        ctx["error"] = "此帳號已經註冊過了，請返回登入頁面。"
        return templates.TemplateResponse("pre_register.html", ctx)

    # Generate registration token and redirect to identity verification
    reg_token = await service.generate_registration_token(
        sqlite_session, employee_name, app_id, redirect_uri
    )
    login_url = f"/auth/login?app_id={app_id}&redirect_uri={redirect_uri}" if app_id else "/auth/dashboard"
    return templates.TemplateResponse("not_registered.html", {
        "request": request,
        "employee_name": employee_name,
        "token": reg_token,
        "login_url": login_url,
        "success": False,
        "error": None,
    })


# ─── Registration Request (webhook notification) ─────────────

@router.post("/request-register")
async def request_register_submit(
    request: Request,
    token: str = Form(...),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理註冊申請：直接發送 Teams Webhook 通知管理員。

    驗證流程：
    1. 驗證 registration token 有效
    2. 查 MSSQL 取得員工資料
    3. 發送 Teams Webhook 通知管理員
    4. Webhook 成功後作廢 token
    """
    data = await service.consume_registration_token(sqlite_session, token)
    if data is None:
        return templates.TemplateResponse("not_registered.html", {
            "request": request,
            "employee_name": "",
            "token": "",
            "login_url": "/auth/dashboard",
            "success": False,
            "error": "連結已過期或無效，請從登入頁面重新操作。",
        })

    employee_name = data["employee_name"]
    app_id = data.get("app_id", "")
    redirect_uri = data.get("redirect_uri", "")
    login_url = f"/auth/login?app_id={app_id}&redirect_uri={redirect_uri}" if app_id else "/auth/dashboard"

    # Get staff info from MSSQL
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        return templates.TemplateResponse("not_registered.html", {
            "request": request,
            "employee_name": employee_name,
            "token": "",
            "login_url": login_url,
            "success": False,
            "error": "員工資料不存在，請聯繫 IT 部門。",
        })

    # Resolve app name for webhook message
    app_name = app_id or "Dashboard"
    apps = load_registered_apps()
    app_info = apps.get(app_id, {})
    if app_info:
        app_name = app_info.get("name", app_name)

    # Extend token TTL to 48 hours so admin can see it on Dashboard
    await service.extend_registration_token(sqlite_session, token, ttl=172800)

    # Send webhook notification to admin (best-effort, admin can also check Dashboard)
    sent = await send_registration_request_notification(staff, app_name)
    if not sent:
        logger.warning("Webhook 發送失敗，但註冊請求已保留在 Dashboard: %s", employee_name)

    settings = get_settings()
    return templates.TemplateResponse("not_registered.html", {
        "request": request,
        "employee_name": employee_name,
        "token": "",
        "login_url": login_url,
        "success": True,
        "error": None,
        "super_admins": settings.SUPER_ADMIN_EMPLOYEES,
    })


# ─── Register Page (admin-generated link) ────────────────────

@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    token: str = Query(...),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """渲染註冊頁面（設定初始密碼）。

    管理員核對身份後產生此連結（含 registration token），發送至員工信箱。
    Token 有效期 24 小時。
    """
    data = await service.consume_registration_token(sqlite_session, token)
    if data is None:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "employee_name": "",
            "token": "",
            "error": "註冊連結已過期或無效，請聯繫管理員重新發送。",
            "success": False,
        })

    return templates.TemplateResponse("register.html", {
        "request": request,
        "employee_name": data["employee_name"],
        "token": token,
        "error": None,
        "success": False,
    })


@router.post("/register")
async def register_submit(
    request: Request,
    employee_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    token: str = Form(...),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理註冊表單提交（設定初始密碼）。

    驗證流程：
    1. 驗證 registration token 有效
    2. 確認兩次密碼輸入一致且長度 >= 8
    3. 查 MSSQL 確認使用者名稱存在
    4. 建立帳號（用 try/except 處理並行 race condition）
    5. 導回登入頁繼續 OAuth 流程
    """
    employee_name = service.normalize_employee_name(employee_name)

    # Validate registration token
    data = await service.consume_registration_token(sqlite_session, token)
    if data is None:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "employee_name": employee_name,
            "token": "",
            "error": "註冊連結已過期或無效，請從登入頁面重新操作。",
            "success": False,
        })

    app_id = data["app_id"]
    redirect_uri = data["redirect_uri"]

    ctx = {
        "request": request,
        "employee_name": employee_name,
        "token": token,
        "error": None,
        "success": False,
    }

    # Validate passwords match
    if password != confirm_password:
        ctx["error"] = "兩次輸入的密碼不一致。"
        return templates.TemplateResponse("register.html", ctx)

    pw_error = service.validate_password(password, employee_name)
    if pw_error:
        ctx["error"] = pw_error
        return templates.TemplateResponse("register.html", ctx)

    # Verify staff exists in MSSQL
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        ctx["error"] = "在公司員工名單中查無此名稱，請確認輸入是否正確。"
        return templates.TemplateResponse("register.html", ctx)

    # Create account — use try/except to handle race condition (#5)
    try:
        await service.register_account(sqlite_session, employee_name, password)
    except IntegrityError:
        ctx["error"] = "此帳號已經註冊過了。"
        return templates.TemplateResponse("register.html", ctx)

    # Invalidate the registration token
    await service.invalidate_registration_token(sqlite_session, token)

    # Redirect back to login to continue OAuth flow
    if app_id and redirect_uri:
        params = urlencode({"app_id": app_id, "redirect_uri": redirect_uri})
        return RedirectResponse(f"/auth/login?{params}", status_code=303)

    return RedirectResponse("/auth/dashboard", status_code=303)


# ─── Token Exchange ───────────────────────────────────────────

@router.post("/token")
async def exchange_token(
    request: Request,
    body: TokenRequest,
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """用 authorization code 換取 JWT Token（供 App 後端呼叫）。

    App 後端收到 callback 中的 code 後，以 POST 方式帶上
    code、app_id、client_secret 呼叫此端點。系統驗證 client 身分
    並消耗一次性 code 後，簽發包含員工資訊與 scopes 的 RS256 JWT。

    回傳格式：{ access_token, token_type, expires_in }
    """
    # Rate limit check
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    apps = load_registered_apps()
    app_info = apps.get(body.app_id)

    if app_info is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    # Verify client_secret
    if not bcrypt.verify(body.client_secret, app_info["client_secret"]):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    # Consume the authorization code (SQLite-backed)
    employee_name = await service.consume_auth_code(sqlite_session, body.code, body.app_id)
    if employee_name is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Fetch staff info to build token payload
    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        return JSONResponse({"error": "staff_not_found"}, status_code=400)

    # Resolve scopes from per-user level
    level = await service.get_user_app_level(sqlite_session, employee_name, body.app_id)
    if level is None:
        return JSONResponse({"error": "no_permission"}, status_code=403)
    scopes = service.level_to_scopes(level)
    expire_hours = app_info.get("token_expire_hours", 12)
    token = create_token(
        sub=staff.employee_name,
        org_id=staff.org_id,
        scopes=scopes,
        aud=body.app_id,
        expire_hours=expire_hours,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expire_hours * 3600,
    }


# ─── Change Password ─────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    success: str | None = None,
    access_token: str | None = Cookie(default=None),
):
    """渲染修改密碼頁面。

    使用者必須帶有有效的 JWT Cookie 才能存取此頁面。
    密碼修改成功後會帶 ?success=1 並清除 cookie，顯示成功訊息。
    """
    if success:
        return templates.TemplateResponse("change_password.html", {
            "request": request,
            "employee_name": "",
            "error": None,
            "success": True,
        })

    user = _verify_cookie(access_token)
    if user is None:
        return templates.TemplateResponse("change_password.html", {
            "request": request,
            "employee_name": "",
            "error": "請先登入後再修改密碼。",
            "success": False,
        })

    return templates.TemplateResponse("change_password.html", {
        "request": request,
        "employee_name": user["sub"],
        "error": None,
        "success": False,
    })


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    access_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理修改密碼表單。

    驗證流程：
    1. 從 Cookie 中的 JWT 取得 employee_name
    2. 確認新密碼兩次輸入一致且長度 >= 8
    3. 驗證舊密碼正確
    4. 更新為新密碼（bcrypt 雜湊）
    """
    user = _verify_cookie(access_token)
    if user is None:
        return templates.TemplateResponse("change_password.html", {
            "request": request,
            "employee_name": "",
            "error": "請先登入後再修改密碼。",
            "success": False,
        })

    employee_name = user["sub"]
    ctx = {
        "request": request,
        "employee_name": employee_name,
        "error": None,
        "success": False,
    }

    if new_password != confirm_password:
        ctx["error"] = "兩次輸入的新密碼不一致。"
        return templates.TemplateResponse("change_password.html", ctx)

    pw_error = service.validate_password(new_password, employee_name)
    if pw_error:
        ctx["error"] = pw_error
        return templates.TemplateResponse("change_password.html", ctx)

    if old_password == new_password:
        ctx["error"] = "新密碼不能與舊密碼相同。"
        return templates.TemplateResponse("change_password.html", ctx)

    error = await service.change_password(
        sqlite_session, employee_name, old_password, new_password
    )
    if error:
        ctx["error"] = error
        return templates.TemplateResponse("change_password.html", ctx)

    response = RedirectResponse("/auth/change-password?success=1", status_code=303)
    response.delete_cookie("access_token")
    return response


@router.get("/logout")
async def user_logout():
    """使用者登出，清除 access_token cookie 並重導至 Dashboard 登入頁。"""
    response = RedirectResponse("/auth/dashboard", status_code=303)
    response.delete_cookie("access_token")
    return response


def _verify_cookie(access_token: str | None) -> dict | None:
    """Verify a JWT from cookie. Returns payload or None."""
    if access_token is None:
        return None
    try:
        settings = get_settings()
        return verify_token(access_token, settings.public_key)
    except jwt.PyJWTError:
        return None  # Expected: invalid or expired token
    except Exception:
        logger.exception("Unexpected error verifying JWT cookie")
        return None


# ─── User Dashboard ──────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    access_token: str | None = Cookie(default=None),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """使用者 Dashboard：顯示有權限存取的 App 列表。

    需要有效的 JWT Cookie 登入。列出使用者所有已被授權的 App。
    """
    user = _verify_cookie(access_token)
    if user is None:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "error": None,
            "staff": None,
            "apps": [],
        })

    staff = await service.verify_staff(mssql_session, user["sub"])
    if staff is None:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "error": "員工資料不存在。",
            "staff": None,
            "apps": [],
        })

    all_apps = load_registered_apps()
    accessible = await service.get_user_accessible_apps(sqlite_session, staff, all_apps)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "error": None,
        "staff": staff,
        "apps": accessible,
    })


@router.get("/dashboard/apps")
async def dashboard_apps_api(
    request: Request,
    access_token: str | None = Cookie(default=None),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """JSON API：回傳使用者目前可存取的 App 列表，供前端輪詢即時更新。"""
    user = _verify_cookie(access_token)
    if user is None:
        return JSONResponse({"apps": []}, status_code=401)

    staff = await service.verify_staff(mssql_session, user["sub"])
    if staff is None:
        return JSONResponse({"apps": []}, status_code=401)

    all_apps = load_registered_apps()
    accessible = await service.get_user_accessible_apps(sqlite_session, staff, all_apps)

    return JSONResponse({"apps": accessible})


@router.post("/dashboard")
async def dashboard_login(
    request: Request,
    employee_name: str = Form(...),
    password: str = Form(...),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """Dashboard 登入：驗證身份後設定 Cookie 並顯示 Dashboard。"""
    employee_name = service.normalize_employee_name(employee_name)

    def _error(msg: str):
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "error": msg,
            "staff": None,
            "apps": [],
        })

    # Rate limit
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return _error("登入嘗試過於頻繁，請 5 分鐘後再試。")

    # Authenticate
    staff, error = await service.authenticate(
        mssql_session, sqlite_session, employee_name, password
    )

    if error == "needs_registration":
        reg_token = await service.generate_registration_token(
            sqlite_session, employee_name, "", ""
        )
        return templates.TemplateResponse("not_registered.html", {
            "request": request,
            "employee_name": employee_name,
            "token": reg_token,
            "login_url": "/auth/dashboard",
            "success": False,
            "error": None,
        })

    if error:
        return _error(error)

    # Issue JWT cookie for dashboard access
    token = create_token(
        sub=staff.employee_name,
        org_id=staff.org_id,
        scopes=["dashboard"],
        aud="auth-center-dashboard",
        expire_hours=12,
    )

    all_apps = load_registered_apps()
    accessible = await service.get_user_accessible_apps(sqlite_session, staff, all_apps)

    response = templates.TemplateResponse("dashboard.html", {
        "request": request,
        "error": None,
        "staff": staff,
        "apps": accessible,
    })
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, samesite="lax", max_age=12 * 3600,
    )
    return response


# ─── Forgot Password ─────────────────────────────────────────

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    """渲染忘記密碼頁面。

    提供表單讓員工輸入姓名，送出後系統將透過 Microsoft Teams
    Webhook 通知管理員協助重設密碼（不會自動重設）。
    """
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "error": None,
        "success": False,
    })


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    employee_name: str = Form(...),
    mssql_session: AsyncSession = Depends(get_mssql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理忘記密碼請求（含頻率限制）。

    查詢 MSSQL 確認員工存在後，發送 Microsoft Teams Webhook
    通知管理員。不會自動重設密碼，需由管理員手動處理。
    """
    employee_name = service.normalize_employee_name(employee_name)
    ctx = {"request": request, "error": None, "success": False}

    # Rate limit check
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        ctx["error"] = "請求過於頻繁，請 5 分鐘後再試。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    staff = await service.verify_staff(mssql_session, employee_name)
    if staff is None:
        ctx["error"] = "在公司員工名單中查無此名稱，請確認輸入是否正確。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    # 確認該員工已註冊 AuthCenter 帳號
    has_account = await service.check_account_exists(sqlite_session, employee_name)
    if not has_account:
        ctx["error"] = "此員工尚未註冊 AuthCenter 帳號，請先完成註冊。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    sent = await send_forgot_password_notification(staff)
    if not sent:
        ctx["error"] = "通知發送失敗，請聯繫 IT 部門。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    ctx["success"] = True
    return templates.TemplateResponse("forgot_password.html", ctx)
