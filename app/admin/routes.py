"""Admin routes for Auth Center management (Super Admin + App Admin)."""

import hmac
import secrets

import jwt
from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, load_registered_apps, save_registered_apps
from app.database import get_sqlite_session, get_mssql_session
from app.auth import service
from loguru import logger
from app.auth.jwt_handler import create_token, verify_token

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_LEVELS = {1: "Read", 2: "Read + Write", 3: "Full Admin"}
ADMIN_TOKEN_HOURS = 2


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For behind reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_templates() -> Jinja2Templates:
    from app.auth.routes import templates
    return templates


# ─── Admin Verification ───────────────────────────────────────

def _verify_admin_cookie(admin_token: str | None) -> dict | None:
    """Verify admin JWT cookie. Returns payload with 'role' (super_admin/app_admin) or None."""
    if admin_token is None:
        return None
    try:
        settings = get_settings()
        payload = verify_token(admin_token, settings.public_key, expected_aud="auth-center-admin")
        scopes = payload.get("scopes", [])
        if "super_admin" not in scopes and "app_admin" not in scopes:
            return None
        payload["is_super"] = "super_admin" in scopes
        return payload
    except jwt.PyJWTError:
        return None
    except Exception:
        logger.exception("Unexpected error verifying admin JWT")
        return None


def _require_super(admin: dict | None) -> bool:
    """Check if admin is super admin."""
    return admin is not None and admin.get("is_super", False)


# ─── Audit Log ─────────────────────────────────────────────────

async def _log_action(
    sqlite_session: AsyncSession,
    admin_name: str,
    action: str,
    target: str = "",
    details: str = "",
) -> None:
    """Record an admin action in the audit log."""
    await sqlite_session.execute(
        text(
            "INSERT INTO admin_audit_log (admin_name, action, target, details) "
            "VALUES (:admin, :action, :target, :details)"
        ),
        {"admin": admin_name, "action": action, "target": target, "details": details},
    )
    await sqlite_session.commit()
    logger.info("Audit: %s | %s | %s | %s", admin_name, action, target, details)


# ─── App Admin Helpers ─────────────────────────────────────────

async def _get_admin_apps(sqlite_session: AsyncSession, employee_name: str) -> list[str]:
    """Get list of app_ids this employee is admin for."""
    result = await sqlite_session.execute(
        text("SELECT app_id FROM app_admins WHERE employee_name = :ename"),
        {"ename": service.normalize_employee_name(employee_name)},
    )
    return [row[0] for row in result.fetchall()]


async def _list_app_admins(sqlite_session: AsyncSession, app_id: str | None = None) -> list[dict]:
    """List all app admin assignments, optionally filtered by app_id."""
    if app_id:
        result = await sqlite_session.execute(
            text("SELECT employee_name, app_id, assigned_by, assigned_at FROM app_admins WHERE app_id = :aid ORDER BY employee_name"),
            {"aid": app_id},
        )
    else:
        result = await sqlite_session.execute(
            text("SELECT employee_name, app_id, assigned_by, assigned_at FROM app_admins ORDER BY employee_name, app_id"),
        )
    return [
        {"employee_name": r[0], "app_id": r[1], "assigned_by": r[2], "assigned_at": r[3]}
        for r in result.fetchall()
    ]


async def _fetch_available_orgs(mssql_session: AsyncSession) -> list[str]:
    """從 MSSQL 取得所有不重複的 org_id，供 App 管理頁面選擇。"""
    try:
        table = get_settings().MSSQL_TABLE
        result = await mssql_session.execute(
            text(f"SELECT DISTINCT org_id FROM {table} WHERE org_id IS NOT NULL ORDER BY org_id")
        )
        return [row[0] for row in result.fetchall()]
    except Exception:
        return []


# ─── Shared template context ──────────────────────────────────

def _base_ctx(request: Request, admin: dict, active_nav: str, **kwargs) -> dict:
    """Build base template context for admin pages."""
    ctx = {
        "request": request,
        "admin_name": admin.get("sub", ""),
        "is_super": admin.get("is_super", False),
        "active_nav": active_nav,
        "error": None,
        "success": None,
    }
    ctx.update(kwargs)
    return ctx


# ═══════════════════════════════════════════════════════════════
# LOGIN / LOGOUT
# ═══════════════════════════════════════════════════════════════

@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """渲染管理員登入頁面。"""
    templates = _get_templates()
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """處理管理員登入。

    驗證流程：
    1. 先比對 .env 中的 Super Admin 帳密（hmac.compare_digest 常數時間比對）
    2. 若非 Super Admin，嘗試員工帳密認證
    3. 認證成功後查 app_admins 表確認是否為 App Admin
    4. 簽發 admin JWT（2 小時有效），存入 admin_token cookie
    """
    templates = _get_templates()
    settings = get_settings()
    username = username.strip()

    # Rate limit check
    client_ip = _get_client_ip(request)
    service.record_attempt(client_ip)
    if not service.check_rate_limit(client_ip):
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "登入嘗試過於頻繁，請 5 分鐘後再試。",
        })

    # 1. Check Super Admin (.env)
    if (
        settings.ADMIN_PASSWORD
        and hmac.compare_digest(username, settings.ADMIN_USERNAME)
        and hmac.compare_digest(password, settings.ADMIN_PASSWORD)
    ):
        token = create_token(
            sub=username,
            org_id="",
            scopes=["super_admin"],
            aud="auth-center-admin",
            expire_hours=ADMIN_TOKEN_HOURS,
        )
        await _log_action(sqlite_session, username, "login", target="super_admin")
        response = RedirectResponse("/admin/dashboard", status_code=303)
        response.set_cookie(
            key="admin_token", value=token,
            httponly=True, samesite="lax", max_age=ADMIN_TOKEN_HOURS * 3600,
        )
        return response

    # 2. Try employee authentication (Super Admin employee / App Admin)
    staff, error = await service.authenticate(mssql_session, sqlite_session, username, password)

    if error == "needs_registration":
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "此員工尚未註冊 AuthCenter 帳號，請先完成註冊。",
        })

    if staff is None:
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "帳號或密碼錯誤。",
        })

    # 3. Check if this employee is a designated Super Admin
    if staff.employee_name in settings.SUPER_ADMIN_EMPLOYEES:
        token = create_token(
            sub=staff.employee_name,
            org_id=staff.org_id,
            scopes=["super_admin"],
            aud="auth-center-admin",
            expire_hours=ADMIN_TOKEN_HOURS,
        )
        await _log_action(sqlite_session, staff.employee_name, "login", target="super_admin(employee)")
        response = RedirectResponse("/admin/dashboard", status_code=303)
        response.set_cookie(
            key="admin_token", value=token,
            httponly=True, samesite="lax", max_age=ADMIN_TOKEN_HOURS * 3600,
        )
        return response

    # 4. Check if this employee is an app admin
    admin_apps = await _get_admin_apps(sqlite_session, staff.employee_name)
    if not admin_apps:
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "您沒有管理員權限。",
        })

    token = create_token(
        sub=staff.employee_name,
        org_id=staff.org_id,
        scopes=["app_admin"],
        aud="auth-center-admin",
        expire_hours=ADMIN_TOKEN_HOURS,
    )
    await _log_action(sqlite_session, staff.employee_name, "login", target="app_admin", details=f"apps={admin_apps}")
    response = RedirectResponse("/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_token", value=token,
        httponly=True, samesite="lax", max_age=ADMIN_TOKEN_HOURS * 3600,
    )
    return response


@router.get("/logout")
async def admin_logout():
    """管理員登出，清除 admin_token cookie 並重導至登入頁。"""
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_token")
    return response


# ═══════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════

@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """管理後台總覽頁。

    顯示已註冊 App 數量、個人權限記錄數、App Admin 數量等統計資訊。
    Super Admin 看到所有 App，App Admin 只看到自己管理的 App。
    """
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()

    # Count permissions
    result = await sqlite_session.execute(text("SELECT COUNT(*) FROM user_app_permissions"))
    perm_count = result.scalar() or 0

    # Count app admins
    result = await sqlite_session.execute(text("SELECT COUNT(*) FROM app_admins"))
    admin_count = result.scalar() or 0

    # For App Admin, filter to their apps
    admin_apps = []
    if not admin.get("is_super"):
        admin_apps = await _get_admin_apps(sqlite_session, admin["sub"])

    # Pending registration requests
    pending = await service.get_pending_registrations(sqlite_session)
    if not admin.get("is_super") and admin_apps:
        pending = [p for p in pending if p["app_id"] in admin_apps]

    ctx = _base_ctx(request, admin, "dashboard",
                    apps=apps, perm_count=perm_count, admin_count=admin_count,
                    admin_apps=admin_apps, pending_registrations=pending)
    return templates.TemplateResponse("admin_dashboard.html", ctx)


@router.post("/generate-register-link")
async def generate_register_link(
    request: Request,
    employee_name: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """管理員產生註冊連結（24 小時有效）。

    取代 CLI 工具，讓管理員在 Dashboard 直接產生連結。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    settings = get_settings()
    employee_name = employee_name.strip().lower()

    # Clean up old pending tokens for this employee, then generate new 24-hour token
    await sqlite_session.execute(
        text("DELETE FROM registration_tokens WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    await sqlite_session.commit()
    token = await service.generate_registration_token(
        sqlite_session, employee_name, app_id="", redirect_uri="", ttl=86400
    )
    link = f"{settings.AUTH_CENTER_BASE_URL}/auth/register?token={token}"

    await _log_action(
        sqlite_session, admin["sub"], "generate_register_link",
        target=employee_name, details=f"link={link}",

    )

    templates = _get_templates()
    # Re-render dashboard with the generated link
    apps = load_registered_apps()
    result = await sqlite_session.execute(text("SELECT COUNT(*) FROM user_app_permissions"))
    perm_count = result.scalar() or 0
    result = await sqlite_session.execute(text("SELECT COUNT(*) FROM app_admins"))
    admin_count = result.scalar() or 0
    admin_apps = []
    if not admin.get("is_super"):
        admin_apps = await _get_admin_apps(sqlite_session, admin["sub"])
    pending = await service.get_pending_registrations(sqlite_session)
    if not admin.get("is_super") and admin_apps:
        pending = [p for p in pending if p["app_id"] in admin_apps]

    ctx = _base_ctx(request, admin, "dashboard",
                    apps=apps, perm_count=perm_count, admin_count=admin_count,
                    admin_apps=admin_apps, pending_registrations=pending)
    ctx["success"] = f"註冊連結已產生（24 小時有效）：{link}"
    return templates.TemplateResponse("admin_dashboard.html", ctx)


# ═══════════════════════════════════════════════════════════════
# APP MANAGEMENT (Super Admin only)
# ═══════════════════════════════════════════════════════════════

@router.get("/apps", response_class=HTMLResponse)
async def apps_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """App 管理頁面（僅 Super Admin）。

    列出所有已註冊的 App，可編輯 allowed_orgs、新增或刪除 App。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()
    available_orgs = await _fetch_available_orgs(mssql_session)
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs)
    return templates.TemplateResponse("admin_apps.html", ctx)


@router.post("/apps/update", response_class=HTMLResponse)
async def update_app(
    request: Request,
    app_id: str = Form(...),
    allowed_orgs: str = Form(default=""),
    default_level: int = Form(default=0),
    token_expire_hours: int = Form(default=12),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """更新 App 的存取規則（僅 Super Admin）。

    可修改 allowed_orgs、default_level、token_expire_hours。
    變更會寫回 config/apps.yaml 並記錄至 audit log。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()
    available_orgs = await _fetch_available_orgs(mssql_session)

    if app_id not in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs, error=f"App '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    # Parse allowed_orgs (comma-separated)
    orgs = [d.strip() for d in allowed_orgs.split(",") if d.strip()] if allowed_orgs.strip() else []

    old_orgs = apps[app_id].get("allowed_orgs", [])
    old_default_level = apps[app_id].get("default_level", 0)
    old_token_hours = apps[app_id].get("token_expire_hours", 12)
    token_expire_hours = max(1, min(720, token_expire_hours))
    apps[app_id]["allowed_orgs"] = orgs
    apps[app_id]["default_level"] = default_level
    apps[app_id]["token_expire_hours"] = token_expire_hours
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "update_app", target=app_id,
        details=f"allowed_orgs: {old_orgs}→{orgs}, default_level: {old_default_level}→{default_level}, token_expire_hours: {old_token_hours}→{token_expire_hours}",

    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs,
                    success=f"已更新 {apps[app_id].get('name', app_id)} 的存取規則。")
    return templates.TemplateResponse("admin_apps.html", ctx)


@router.post("/apps/create", response_class=HTMLResponse)
async def create_app(
    request: Request,
    new_app_id: str = Form(...),
    new_app_name: str = Form(...),
    new_redirect_uri: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """新增 App 到 apps.yaml（僅 Super Admin）。

    自動產生隨機 client_secret 並以 bcrypt 雜湊儲存。
    新增成功後顯示明文 secret 一次（此後無法再次查看）。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()
    available_orgs = await _fetch_available_orgs(mssql_session)

    new_app_id = new_app_id.strip().lower()
    if not new_app_id:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs, error="App ID 不可為空。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    if new_app_id in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs, error=f"App ID '{new_app_id}' 已存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    # Generate client_secret
    plain_secret = secrets.token_urlsafe(32)
    hashed_secret = bcrypt.hash(plain_secret)

    apps[new_app_id] = {
        "app_id": new_app_id,
        "client_secret": hashed_secret,
        "redirect_uri": new_redirect_uri.strip(),
        "name": new_app_name.strip(),
        "allowed_orgs": [],
        "default_level": 0,
        "token_expire_hours": 12,
    }
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "create_app", target=new_app_id,
        details=f"name={new_app_name.strip()}, redirect_uri={new_redirect_uri.strip()}",

    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps,
                    new_secret={"app_id": new_app_id, "secret": plain_secret},
                    available_orgs=available_orgs,
                    success=f"已新增 App：{new_app_name.strip()}")
    return templates.TemplateResponse("admin_apps.html", ctx)


@router.post("/apps/delete", response_class=HTMLResponse)
async def delete_app(
    request: Request,
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """從 apps.yaml 中刪除 App（僅 Super Admin）。

    刪除後該 App 的 OAuth flow 將無法使用。已存在的 per-user 權限不會自動刪除。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()
    available_orgs = await _fetch_available_orgs(mssql_session)

    if app_id not in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs, error=f"App '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    app_name = apps[app_id].get("name", app_id)
    del apps[app_id]
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "delete_app", target=app_id,
        details=f"name={app_name}",

    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, available_orgs=available_orgs,
                    success=f"已刪除 App：{app_name}")
    return templates.TemplateResponse("admin_apps.html", ctx)


# ═══════════════════════════════════════════════════════════════
# PERMISSIONS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@router.get("/permissions", response_class=HTMLResponse)
async def permissions_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    user_filter: str = Query(default=""),
    app_filter: str = Query(default=""),
):
    """權限管理頁面。

    顯示使用者的 per-app 個人權限列表，支援依使用者名稱及 App ID 篩選。
    - Super Admin：可查看所有 app 的權限。
    - App Admin：僅顯示自己管理的 app 的權限，下拉選單也僅列出管理的 app。
    """
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    user_filter = user_filter.strip()
    app_filter = app_filter.strip()

    # App Admin can only see their apps
    admin_apps = None
    if not admin.get("is_super"):
        admin_apps = await _get_admin_apps(sqlite_session, admin["sub"])
        if app_filter and app_filter not in admin_apps:
            app_filter = ""

    permissions = await service.list_permissions(
        sqlite_session,
        employee_name=user_filter or None,
        app_id=app_filter or None,
    )

    # Filter permissions for App Admin
    if admin_apps is not None:
        permissions = [p for p in permissions if p["app_id"] in admin_apps]

    apps = load_registered_apps()
    # For App Admin, only show their apps in the dropdown
    visible_apps = apps if admin.get("is_super") else {k: v for k, v in apps.items() if k in (admin_apps or [])}

    ctx = _base_ctx(request, admin, "permissions",
                    permissions=permissions, apps=visible_apps, valid_levels=VALID_LEVELS,
                    user_filter=user_filter, app_filter=app_filter)
    return templates.TemplateResponse("admin_permissions.html", ctx)


@router.post("/permissions", response_class=HTMLResponse)
async def grant_permission(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    level: int = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """授予使用者對指定 App 的存取等級。

    接收 employee_name、app_id、level（1-3），驗證 App 存在後寫入資料庫。
    App Admin 僅能授權自己管理的 app，若嘗試授權非管理的 app 會被重導回權限頁面。
    操作完成後記錄 audit log。
    """
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    employee_name = service.normalize_employee_name(employee_name)
    admin_name = admin.get("sub", "")

    # App Admin can only manage their apps
    if not admin.get("is_super"):
        admin_apps = await _get_admin_apps(sqlite_session, admin_name)
        if app_id not in admin_apps:
            return RedirectResponse("/admin/permissions", status_code=303)

    level = max(1, min(3, level))

    apps = load_registered_apps()
    if app_id not in apps:
        ctx = _base_ctx(request, admin, "permissions",
                        permissions=await service.list_permissions(sqlite_session),
                        apps=apps, valid_levels=VALID_LEVELS,
                        user_filter="", app_filter="",
                        error=f"App ID '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_permissions.html", ctx)

    await service.set_user_level(sqlite_session, employee_name, app_id, level, admin_name)
    await _log_action(
        sqlite_session, admin_name, "set_user_level", target=f"{employee_name}→{app_id}",
        details=f"level={level}",
    )

    return RedirectResponse("/admin/permissions", status_code=303)


@router.post("/permissions/revoke", response_class=HTMLResponse)
async def revoke_permission(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """撤銷使用者對指定 App 的個人權限。

    App Admin 僅能撤銷自己管理的 app 的權限。成功撤銷後記錄 audit log。
    """
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    admin_name = admin.get("sub", "")
    employee_name = service.normalize_employee_name(employee_name)

    # App Admin can only revoke their apps
    if not admin.get("is_super"):
        admin_apps = await _get_admin_apps(sqlite_session, admin_name)
        if app_id not in admin_apps:
            return RedirectResponse("/admin/permissions", status_code=303)

    deleted = await service.remove_user_permission(sqlite_session, employee_name, app_id)
    if deleted:
        await _log_action(
            sqlite_session, admin_name, "revoke_permission", target=f"{employee_name}→{app_id}",
    
        )

    return RedirectResponse("/admin/permissions", status_code=303)


# ═══════════════════════════════════════════════════════════════
# APP ADMIN MANAGEMENT (Super Admin only)
# ═══════════════════════════════════════════════════════════════

@router.get("/admins", response_class=HTMLResponse)
async def admins_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """App Admin 管理頁面（僅 Super Admin）。

    列出所有已指定的 App Admin（員工名稱、負責的 app、指定者、指定時間），
    並提供指定新 App Admin 的表單。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    app_admins = await _list_app_admins(sqlite_session)
    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "admins", app_admins=app_admins, apps=apps)
    return templates.TemplateResponse("admin_admins.html", ctx)


@router.post("/admins/assign", response_class=HTMLResponse)
async def assign_app_admin(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """指定員工為某 App 的 App Admin（僅 Super Admin）。

    將 employee_name + app_id 寫入 app_admins 表。若已存在則更新 assigned_by 與時間。
    操作完成後記錄 audit log。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    employee_name = service.normalize_employee_name(employee_name)
    admin_name = admin.get("sub", "")

    apps = load_registered_apps()
    if app_id not in apps:
        templates = _get_templates()
        app_admins = await _list_app_admins(sqlite_session)
        ctx = _base_ctx(request, admin, "admins", app_admins=app_admins, apps=apps,
                        error=f"App ID '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_admins.html", ctx)

    await sqlite_session.execute(
        text(
            "INSERT INTO app_admins (employee_name, app_id, assigned_by) "
            "VALUES (:ename, :aid, :by) "
            "ON CONFLICT(employee_name, app_id) DO UPDATE SET assigned_by = :by, assigned_at = datetime('now')"
        ),
        {"ename": employee_name, "aid": app_id, "by": admin_name},
    )
    await sqlite_session.commit()

    await _log_action(
        sqlite_session, admin_name, "assign_app_admin", target=f"{employee_name}→{app_id}",

    )

    return RedirectResponse("/admin/admins", status_code=303)


@router.post("/admins/remove", response_class=HTMLResponse)
async def remove_app_admin(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """移除某員工的 App Admin 身份（僅 Super Admin）。

    從 app_admins 表刪除對應記錄，該員工將無法再以 App Admin 身份登入管理該 app。
    操作完成後記錄 audit log。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    employee_name = service.normalize_employee_name(employee_name)
    admin_name = admin.get("sub", "")

    await sqlite_session.execute(
        text("DELETE FROM app_admins WHERE employee_name = :ename AND app_id = :aid"),
        {"ename": employee_name, "aid": app_id},
    )
    await sqlite_session.commit()

    await _log_action(
        sqlite_session, admin_name, "remove_app_admin", target=f"{employee_name}→{app_id}",

    )

    return RedirectResponse("/admin/admins", status_code=303)


# ═══════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════

@router.get("/audit-log", response_class=HTMLResponse)
async def audit_log_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    page: int = Query(default=1, ge=1),
):
    """操作紀錄頁面，顯示所有管理員操作的 audit log，支援分頁（每頁 50 筆）。

    - Super Admin：查看所有操作紀錄。
    - App Admin：僅顯示與自己或自己管理的 app 相關的紀錄。
    """
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    page_size = 50
    offset = (page - 1) * page_size

    # App Admin: filter to their app-related actions
    if admin.get("is_super"):
        result = await sqlite_session.execute(
            text("SELECT COUNT(*) FROM admin_audit_log"),
        )
        total = result.scalar() or 0
        result = await sqlite_session.execute(
            text("SELECT id, admin_name, action, target, details, created_at "
                 "FROM admin_audit_log ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            {"limit": page_size, "offset": offset},
        )
    else:
        admin_apps = await _get_admin_apps(sqlite_session, admin["sub"])
        if not admin_apps:
            admin_apps = ["__none__"]
        placeholders = ", ".join(f":app{i}" for i in range(len(admin_apps)))
        params = {f"app{i}": app for i, app in enumerate(admin_apps)}
        params["ename"] = admin["sub"]

        # Show logs where target contains their app_id or their name
        like_conditions = " OR ".join(f"target LIKE :like{i}" for i in range(len(admin_apps)))
        for i, app in enumerate(admin_apps):
            params[f"like{i}"] = f"%{app}%"

        result = await sqlite_session.execute(
            text(f"SELECT COUNT(*) FROM admin_audit_log WHERE admin_name = :ename OR {like_conditions}"),
            params,
        )
        total = result.scalar() or 0
        params["limit"] = page_size
        params["offset"] = offset
        result = await sqlite_session.execute(
            text(f"SELECT id, admin_name, action, target, details, created_at "
                 f"FROM admin_audit_log WHERE admin_name = :ename OR {like_conditions} "
                 f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            params,
        )

    logs = [
        {"id": r[0], "admin_name": r[1], "action": r[2], "target": r[3],
         "details": r[4], "created_at": r[5]}
        for r in result.fetchall()
    ]

    total_pages = max(1, (total + page_size - 1) // page_size)
    ctx = _base_ctx(request, admin, "audit",
                    logs=logs, page=page, total_pages=total_pages, total=total)
    return templates.TemplateResponse("admin_audit_log.html", ctx)


# ─── User Account Management (Super Admin Only) ──────────────


@router.get("/users", response_class=HTMLResponse)
async def manage_users(
    request: Request,
    search: str = "",
    msg: str = "",
    err: str = "",
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """已註冊會員管理頁面（僅 Super Admin）。

    列出所有已註冊帳號，依組織代碼分組展開。
    支援以 employee_name 搜尋篩選。
    """
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    users = await service.list_users(sqlite_session)

    # Filter by search term
    if search.strip():
        term = search.strip().lower()
        users = [u for u in users if term in u["employee_name"]]

    # Batch query MSSQL for org_id
    org_map: dict[str, str] = {}
    if users:
        table = get_settings().MSSQL_TABLE
        names = [u["employee_name"] for u in users]
        # SQLAlchemy doesn't support IN with named params for tuples easily,
        # use a manual approach with individual params
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        params = {f"n{i}": name for i, name in enumerate(names)}
        result = await mssql_session.execute(
            text(f"SELECT nt_account, org_id FROM {table} WHERE nt_account IN ({placeholders})"),
            params,
        )
        org_map = {r[0]: r[1] for r in result.fetchall()}

    # Attach org_id and group by org
    for u in users:
        u["org_id"] = org_map.get(u["employee_name"], "未知")

    # Group by org_id
    org_groups: dict[str, list[dict]] = {}
    for u in users:
        org_groups.setdefault(u["org_id"], []).append(u)
    # Sort orgs alphabetically
    org_groups = dict(sorted(org_groups.items()))

    ctx = _base_ctx(request, admin, "users",
                    org_groups=org_groups, user_count=len(users),
                    org_count=len(org_groups), search=search,
                    reset_password=None,
                    success=msg or None, error=err or None)
    return templates.TemplateResponse("admin_users.html", ctx)


@router.post("/users/reset-password", response_class=HTMLResponse)
async def reset_user_password(
    request: Request,
    employee_name: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mssql_session: AsyncSession = Depends(get_mssql_session),
):
    """管理員強制重設使用者密碼（僅 Super Admin）。"""
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    new_password = await service.admin_reset_password(sqlite_session, employee_name)

    await _log_action(
        sqlite_session, admin["sub"], "reset_password", target=employee_name,

    )

    # Re-load users page with password displayed
    users = await service.list_users(sqlite_session)
    table = get_settings().MSSQL_TABLE
    names = [u["employee_name"] for u in users]
    org_map: dict[str, str] = {}
    if names:
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        params = {f"n{i}": name for i, name in enumerate(names)}
        result = await mssql_session.execute(
            text(f"SELECT nt_account, org_id FROM {table} WHERE nt_account IN ({placeholders})"),
            params,
        )
        org_map = {r[0]: r[1] for r in result.fetchall()}

    for u in users:
        u["org_id"] = org_map.get(u["employee_name"], "未知")
    org_groups: dict[str, list[dict]] = {}
    for u in users:
        org_groups.setdefault(u["org_id"], []).append(u)
    org_groups = dict(sorted(org_groups.items()))

    ctx = _base_ctx(
        request, admin, "users",
        org_groups=org_groups, user_count=len(users),
        org_count=len(org_groups), search="",
        reset_password={"employee_name": employee_name, "password": new_password},
        success=f"已重設 {employee_name} 的密碼。",
    )
    return templates.TemplateResponse("admin_users.html", ctx)


@router.post("/users/delete", response_class=HTMLResponse)
async def delete_user_account(
    request: Request,
    employee_name: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    """刪除使用者帳號及其所有權限（僅 Super Admin）。"""
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    deleted = await service.delete_user(sqlite_session, employee_name)

    if deleted:
        await _log_action(
            sqlite_session, admin["sub"], "delete_user", target=employee_name,
    
        )

    from urllib.parse import urlencode
    if deleted:
        qs = urlencode({"msg": f"已刪除 {employee_name} 的帳號與權限記錄。"})
    else:
        qs = urlencode({"err": f"找不到使用者 {employee_name}"})
    return RedirectResponse(f"/admin/users?{qs}", status_code=303)


# ═══════════════════════════════════════════════════════════════
# GUIDE (all admins)
# ═══════════════════════════════════════════════════════════════

@router.get("/guide", response_class=HTMLResponse)
async def guide_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
):
    """使用指南頁面，說明各項管理功能的操作方式。"""
    admin = _verify_admin_cookie(admin_token)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    ctx = _base_ctx(request, admin, "guide")
    return templates.TemplateResponse("admin_guide.html", ctx)
