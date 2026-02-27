"""Auth Center API routes."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_registered_apps, get_settings
from app.database import get_mysql_session, get_sqlite_session
from app.auth import service
from app.auth.jwt_handler import create_token
from app.schemas import TokenRequest, ForgotPasswordRequest
from app.webhook.teams import send_forgot_password_notification

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Templates = None  # initialized in main.py via init_templates()


def init_templates(t: Jinja2Templates) -> None:
    global templates
    templates = t


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
    staff_id: str = Form(...),
    password: str = Form(...),
    app_id: str = Form(...),
    redirect_uri: str = Form(...),
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理登入表單提交。

    驗證流程：
    1. 查 MySQL 確認員工在職
    2. 查 SQLite 確認帳號是否已註冊（未註冊則導向註冊頁）
    3. 驗證密碼是否正確
    4. 檢查該員工是否有權存取目標 App
    5. 產生 authorization code，302 重導回 App 的 redirect_uri
    """
    apps = load_registered_apps()
    app_info = apps.get(app_id)
    app_name = app_info.get("name", app_id) if app_info else "Unknown"

    # Authenticate
    staff, error = await service.authenticate(
        mysql_session, sqlite_session, staff_id, password
    )

    if error == "needs_registration":
        # Redirect to registration page
        params = urlencode({
            "staff_id": staff_id,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
        })
        return RedirectResponse(f"/auth/register?{params}", status_code=303)

    if error:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": error,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": app_name,
        })

    # Check app access permission
    allowed, reason = await service.check_app_access(sqlite_session, staff, app_id)
    if not allowed:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": reason,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "app_name": app_name,
        })

    # Generate authorization code and redirect back to app
    code = service.generate_auth_code(staff.staff_id, app_id)
    return RedirectResponse(f"{redirect_uri}?code={code}", status_code=303)


# ─── Register Page ────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    staff_id: str = Query(...),
    app_id: str = Query(""),
    redirect_uri: str = Query(""),
):
    """渲染註冊頁面（設定初始密碼）。

    當員工首次登入時，系統偵測到 MySQL 有此員工但 SQLite 尚無帳號，
    自動導向此頁面讓員工設定密碼。帶入 app_id 與 redirect_uri 以便
    註冊完成後能導回原本的登入流程。
    """
    return templates.TemplateResponse("register.html", {
        "request": request,
        "staff_id": staff_id,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "error": None,
        "success": False,
    })


@router.post("/register")
async def register_submit(
    request: Request,
    staff_id: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    app_id: str = Form(""),
    redirect_uri: str = Form(""),
    mysql_session: AsyncSession = Depends(get_mysql_session),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """處理註冊表單提交（設定初始密碼）。

    驗證流程：
    1. 確認兩次密碼輸入一致且長度 >= 8
    2. 查 MySQL 確認員工編號存在
    3. 查 SQLite 確認帳號尚未註冊
    4. 建立帳號（bcrypt 雜湊密碼）
    5. 若有 app 上下文，導回登入頁繼續 OAuth 流程
    """
    ctx = {
        "request": request,
        "staff_id": staff_id,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
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
    staff = await service.verify_staff(mysql_session, staff_id)
    if staff is None:
        ctx["error"] = "員工編號不存在。"
        return templates.TemplateResponse("register.html", ctx)

    # Check if already registered
    exists = await service.check_account_exists(sqlite_session, staff_id)
    if exists:
        ctx["error"] = "此帳號已經註冊過了。"
        return templates.TemplateResponse("register.html", ctx)

    # Create account
    await service.register_account(sqlite_session, staff_id, password)

    # If we have app context, redirect back to login
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

    # Consume the authorization code
    staff_id = service.consume_auth_code(body.code, body.app_id)
    if staff_id is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Fetch staff info to build token payload
    staff = await service.verify_staff(mysql_session, staff_id)
    if staff is None:
        return JSONResponse({"error": "staff_not_found"}, status_code=400)

    scopes = service.map_scopes(staff.level)
    token = create_token(
        sub=staff.staff_id,
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


# ─── Forgot Password ─────────────────────────────────────────

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    """渲染忘記密碼頁面。

    提供表單讓員工輸入編號，送出後系統將透過 Microsoft Teams
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
    staff_id: str = Form(...),
    mysql_session: AsyncSession = Depends(get_mysql_session),
):
    """處理忘記密碼請求。

    查詢 MySQL 確認員工存在後，發送 Microsoft Teams Webhook
    通知管理員。Payload 包含員工姓名、編號、部門與權限等級。
    不會自動重設密碼，需由管理員手動處理。
    """
    ctx = {"request": request, "error": None, "success": False}

    staff = await service.verify_staff(mysql_session, staff_id)
    if staff is None:
        ctx["error"] = "員工編號不存在。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    sent = await send_forgot_password_notification(staff)
    if not sent:
        ctx["error"] = "通知發送失敗，請聯繫 IT 部門。"
        return templates.TemplateResponse("forgot_password.html", ctx)

    ctx["success"] = True
    return templates.TemplateResponse("forgot_password.html", ctx)
