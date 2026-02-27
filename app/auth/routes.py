"""Auth Center API routes."""

from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_registered_apps, get_settings
from app.database import get_mysql_session, get_sqlite_session
from app.auth import service
from app.auth.jwt_handler import create_token, verify_token
from app.schemas import TokenRequest, ForgotPasswordRequest
from app.webhook.teams import send_forgot_password_notification, send_registration_request_notification

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Templates = None  # initialized in main.py via init_templates()


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
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理登入表單提交。

    驗證流程：
    0. 檢查頻率限制（同一 IP 5 分鐘內最多 10 次）
    1. 重新驗證 app_id + redirect_uri（防止表單竄改）
    2. 查 MySQL 確認員工在職
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
        mysql_session, sqlite_session, employee_name, password
    )

    if error == "needs_registration":
        # Redirect to registration request page — user verifies identity, then webhook to admin
        reg_token = await service.generate_registration_token(sqlite_session, employee_name, app_id, redirect_uri)
        return RedirectResponse(f"/auth/register-request?token={reg_token}", status_code=303)

    if error:
        return _error_response(error)

    # Check app access permission
    allowed, reason = await service.check_app_access(sqlite_session, staff, app_id)
    if not allowed:
        return _error_response(reason)

    # Generate authorization code (SQLite-backed) and redirect back to app
    code = await service.generate_auth_code(sqlite_session, staff.employee_name, app_id)
    return RedirectResponse(f"{redirect_uri}?code={code}", status_code=303)


# ─── Registration Request (identity verification → webhook) ──

@router.get("/register-request", response_class=HTMLResponse)
async def register_request_page(
    request: Request,
    token: str = Query(...),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """渲染身份驗證頁面。

    員工首次登入時導向此頁面，需填寫分機號碼與部門代碼以核對身份。
    核對正確後系統發送 Teams Webhook 通知管理員，管理員再產生註冊連結。
    """
    data = await service.consume_registration_token(sqlite_session, token)
    if data is None:
        return templates.TemplateResponse("register_request.html", {
            "request": request,
            "employee_name": "",
            "token": "",
            "error": "連結已過期或無效，請從登入頁面重新操作。",
            "success": False,
        })

    return templates.TemplateResponse("register_request.html", {
        "request": request,
        "employee_name": data["employee_name"],
        "token": token,
        "error": None,
        "success": False,
    })


@router.post("/register-request")
async def register_request_submit(
    request: Request,
    employee_name: str = Form(...),
    ext: str = Form(...),
    dept_code: str = Form(...),
    token: str = Form(...),
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理身份驗證表單。

    驗證流程：
    1. 驗證 registration token 有效
    2. 查 MySQL 取得員工資料
    3. 核對分機號碼與部門代碼是否匹配
    4. 核對正確 → 發送 Teams Webhook 通知管理員
    """
    employee_name = service.normalize_employee_name(employee_name)

    data = await service.consume_registration_token(sqlite_session, token)
    if data is None:
        return templates.TemplateResponse("register_request.html", {
            "request": request,
            "employee_name": employee_name,
            "token": "",
            "error": "連結已過期或無效，請從登入頁面重新操作。",
            "success": False,
        })

    ctx = {
        "request": request,
        "employee_name": employee_name,
        "token": token,
        "error": None,
        "success": False,
    }

    # Verify staff info from MySQL
    staff = await service.verify_staff(mysql_session, employee_name)
    if staff is None:
        ctx["error"] = "使用者名稱不存在。"
        return templates.TemplateResponse("register_request.html", ctx)

    # Verify ext and dept_code match
    if staff.ext != ext.strip():
        ctx["error"] = "分機號碼不正確。"
        return templates.TemplateResponse("register_request.html", ctx)

    if staff.dept_code != dept_code.strip():
        ctx["error"] = "部門代碼不正確。"
        return templates.TemplateResponse("register_request.html", ctx)

    # Identity verified — send webhook to admin
    app_name = data.get("app_id", "Unknown")
    apps = load_registered_apps()
    app_info = apps.get(data.get("app_id", ""))
    if app_info:
        app_name = app_info.get("name", app_name)

    sent = await send_registration_request_notification(staff, app_name)
    if not sent:
        ctx["error"] = "通知發送失敗，請聯繫 IT 部門。"
        return templates.TemplateResponse("register_request.html", ctx)

    # Invalidate the token after successful submission
    await service.invalidate_registration_token(sqlite_session, token)

    ctx["success"] = True
    return templates.TemplateResponse("register_request.html", ctx)


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
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理註冊表單提交（設定初始密碼）。

    驗證流程：
    1. 驗證 registration token 有效
    2. 確認兩次密碼輸入一致且長度 >= 8
    3. 查 MySQL 確認使用者名稱存在
    4. 查 SQLite 確認帳號尚未註冊
    5. 建立帳號（bcrypt 雜湊密碼）
    6. 導回登入頁繼續 OAuth 流程
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

    if len(password) < 8:
        ctx["error"] = "密碼長度至少 8 個字元。"
        return templates.TemplateResponse("register.html", ctx)

    # Verify staff exists in MySQL
    staff = await service.verify_staff(mysql_session, employee_name)
    if staff is None:
        ctx["error"] = "使用者名稱不存在。"
        return templates.TemplateResponse("register.html", ctx)

    # Check if already registered
    exists = await service.check_account_exists(sqlite_session, employee_name)
    if exists:
        ctx["error"] = "此帳號已經註冊過了。"
        return templates.TemplateResponse("register.html", ctx)

    # Create account
    await service.register_account(sqlite_session, employee_name, password)

    # Invalidate the registration token
    await service.invalidate_registration_token(sqlite_session, token)

    # Redirect back to login to continue OAuth flow
    if app_id and redirect_uri:
        params = urlencode({"app_id": app_id, "redirect_uri": redirect_uri})
        return RedirectResponse(f"/auth/login?{params}", status_code=303)

    ctx["success"] = True
    return templates.TemplateResponse("register.html", ctx)


# ─── Token Exchange ───────────────────────────────────────────

@router.post("/token")
async def exchange_token(
    body: TokenRequest,
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """用 authorization code 換取 JWT Token（供 App 後端呼叫）。

    App 後端收到 callback 中的 code 後，以 POST 方式帶上
    code、app_id、client_secret 呼叫此端點。系統驗證 client 身分
    並消耗一次性 code 後，簽發包含員工資訊與 scopes 的 RS256 JWT。

    回傳格式：{ access_token, token_type, expires_in }
    """
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
    staff = await service.verify_staff(mysql_session, employee_name)
    if staff is None:
        return JSONResponse({"error": "staff_not_found"}, status_code=400)

    scopes = service.map_scopes(staff.level)
    token = create_token(
        sub=staff.employee_name,
        name=staff.name,
        dept=staff.dept_code,
        scopes=scopes,
        aud=body.app_id,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 43200,
    }


# ─── Change Password ─────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    access_token: str | None = Cookie(default=None),
):
    """渲染修改密碼頁面。

    使用者必須帶有有效的 JWT Cookie 才能存取此頁面。
    """
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

    if len(new_password) < 8:
        ctx["error"] = "新密碼長度至少 8 個字元。"
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

    ctx["success"] = True
    return templates.TemplateResponse("change_password.html", ctx)


def _verify_cookie(access_token: str | None) -> dict | None:
    """Verify a JWT from cookie. Returns payload or None."""
    if access_token is None:
        return None
    try:
        settings = get_settings()
        return verify_token(access_token, settings.public_key)
    except Exception:
        return None


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
    mysql_session: AsyncSession = Depends(get_mysql_session),
):
    """處理忘記密碼請求（含頻率限制）。

    查詢 MySQL 確認員工存在後，發送 Microsoft Teams Webhook
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

    staff = await service.verify_staff(mysql_session, employee_name)
    if staff is None:
        ctx["error"] = "使用者名稱不存在。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    sent = await send_forgot_password_notification(staff)
    if not sent:
        ctx["error"] = "通知發送失敗，請聯繫 IT 部門。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    ctx["success"] = True
    return templates.TemplateResponse("forgot_password.html", ctx)
