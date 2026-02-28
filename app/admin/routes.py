"""Admin routes for Auth Center management (Super Admin + App Admin)."""

import hmac
import json
import logging
import secrets

import jwt
from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, load_registered_apps, save_registered_apps
from app.database import get_sqlite_session, get_mysql_session
from app.auth import service
from app.auth.jwt_handler import create_token, verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_SCOPES = ["read", "write", "admin"]
ADMIN_TOKEN_HOURS = 2


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


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Audit Log ─────────────────────────────────────────────────

async def _log_action(
    sqlite_session: AsyncSession,
    admin_name: str,
    action: str,
    target: str = "",
    details: str = "",
    ip_address: str = "",
) -> None:
    """Record an admin action in the audit log."""
    await sqlite_session.execute(
        text(
            "INSERT INTO admin_audit_log (admin_name, action, target, details, ip_address) "
            "VALUES (:admin, :action, :target, :details, :ip)"
        ),
        {"admin": admin_name, "action": action, "target": target, "details": details, "ip": ip_address},
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
    templates = _get_templates()
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
    mysql_session: AsyncSession = Depends(get_mysql_session),
):
    templates = _get_templates()
    settings = get_settings()
    username = username.strip()

    # 1. Check Super Admin (.env)
    if (
        settings.ADMIN_PASSWORD
        and hmac.compare_digest(username, settings.ADMIN_USERNAME)
        and hmac.compare_digest(password, settings.ADMIN_PASSWORD)
    ):
        token = create_token(
            sub=username,
            name="Super Admin",
            dept="",
            scopes=["super_admin"],
            aud="auth-center-admin",
            expire_hours=ADMIN_TOKEN_HOURS,
        )
        await _log_action(sqlite_session, username, "login", target="super_admin", ip_address=_get_client_ip(request))
        response = RedirectResponse("/admin/dashboard", status_code=303)
        response.set_cookie(
            key="admin_token", value=token,
            httponly=True, samesite="lax", max_age=ADMIN_TOKEN_HOURS * 3600,
        )
        return response

    # 2. Try employee authentication (App Admin)
    staff, error = await service.authenticate(mysql_session, sqlite_session, username, password)

    if error == "needs_registration":
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "此帳號尚未完成註冊。",
        })

    if staff is None:
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "帳號或密碼錯誤。",
        })

    # 3. Check if this employee is an app admin
    admin_apps = await _get_admin_apps(sqlite_session, staff.employee_name)
    if not admin_apps:
        return templates.TemplateResponse("admin_login.html", {
            "request": request, "error": "您沒有管理員權限。",
        })

    token = create_token(
        sub=staff.employee_name,
        name=staff.name,
        dept=staff.dept_code,
        scopes=["app_admin"],
        aud="auth-center-admin",
        expire_hours=ADMIN_TOKEN_HOURS,
    )
    await _log_action(sqlite_session, staff.employee_name, "login", target="app_admin", details=f"apps={admin_apps}", ip_address=_get_client_ip(request))
    response = RedirectResponse("/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_token", value=token,
        httponly=True, samesite="lax", max_age=ADMIN_TOKEN_HOURS * 3600,
    )
    return response


@router.get("/logout")
async def admin_logout():
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

    ctx = _base_ctx(request, admin, "dashboard",
                    apps=apps, perm_count=perm_count, admin_count=admin_count, admin_apps=admin_apps)
    return templates.TemplateResponse("admin_dashboard.html", ctx)


# ═══════════════════════════════════════════════════════════════
# APP MANAGEMENT (Super Admin only)
# ═══════════════════════════════════════════════════════════════

@router.get("/apps", response_class=HTMLResponse)
async def apps_page(
    request: Request,
    admin_token: str | None = Cookie(default=None),
):
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None)
    return templates.TemplateResponse("admin_apps.html", ctx)


@router.post("/apps/update", response_class=HTMLResponse)
async def update_app(
    request: Request,
    app_id: str = Form(...),
    allowed_depts: str = Form(default=""),
    min_level: int = Form(default=1),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()

    if app_id not in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, error=f"App '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    # Parse allowed_depts (comma-separated)
    depts = [d.strip() for d in allowed_depts.split(",") if d.strip()] if allowed_depts.strip() else []
    min_level = max(1, min(3, min_level))

    old_depts = apps[app_id].get("allowed_depts", [])
    old_level = apps[app_id].get("min_level", 1)
    apps[app_id]["allowed_depts"] = depts
    apps[app_id]["min_level"] = min_level
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "update_app", target=app_id,
        details=f"allowed_depts: {old_depts}→{depts}, min_level: {old_level}→{min_level}",
        ip_address=_get_client_ip(request),
    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None,
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
):
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()

    new_app_id = new_app_id.strip().lower()
    if not new_app_id:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, error="App ID 不可為空。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    if new_app_id in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, error=f"App ID '{new_app_id}' 已存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    # Generate client_secret
    plain_secret = secrets.token_urlsafe(32)
    hashed_secret = bcrypt.hash(plain_secret)

    apps[new_app_id] = {
        "app_id": new_app_id,
        "client_secret": hashed_secret,
        "redirect_uri": new_redirect_uri.strip(),
        "name": new_app_name.strip(),
        "allowed_depts": [],
        "min_level": 1,
    }
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "create_app", target=new_app_id,
        details=f"name={new_app_name.strip()}, redirect_uri={new_redirect_uri.strip()}",
        ip_address=_get_client_ip(request),
    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps,
                    new_secret={"app_id": new_app_id, "secret": plain_secret},
                    success=f"已新增 App：{new_app_name.strip()}")
    return templates.TemplateResponse("admin_apps.html", ctx)


@router.post("/apps/delete", response_class=HTMLResponse)
async def delete_app(
    request: Request,
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
    admin = _verify_admin_cookie(admin_token)
    if not _require_super(admin):
        return RedirectResponse("/admin/login", status_code=303)

    templates = _get_templates()
    apps = load_registered_apps()

    if app_id not in apps:
        ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None, error=f"App '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_apps.html", ctx)

    app_name = apps[app_id].get("name", app_id)
    del apps[app_id]
    save_registered_apps(apps)

    await _log_action(
        sqlite_session, admin["sub"], "delete_app", target=app_id,
        details=f"name={app_name}",
        ip_address=_get_client_ip(request),
    )

    apps = load_registered_apps()
    ctx = _base_ctx(request, admin, "apps", apps=apps, new_secret=None,
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
                    permissions=permissions, apps=visible_apps, valid_scopes=VALID_SCOPES,
                    user_filter=user_filter, app_filter=app_filter)
    return templates.TemplateResponse("admin_permissions.html", ctx)


@router.post("/permissions", response_class=HTMLResponse)
async def grant_permission(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    scopes: list[str] = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
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

    valid = [s for s in scopes if s in VALID_SCOPES]
    if not valid:
        valid = ["read"]

    apps = load_registered_apps()
    if app_id not in apps:
        ctx = _base_ctx(request, admin, "permissions",
                        permissions=await service.list_permissions(sqlite_session),
                        apps=apps, valid_scopes=VALID_SCOPES,
                        user_filter="", app_filter="",
                        error=f"App ID '{app_id}' 不存在。")
        return templates.TemplateResponse("admin_permissions.html", ctx)

    await service.grant_permission(sqlite_session, employee_name, app_id, valid, admin_name)
    await _log_action(
        sqlite_session, admin_name, "grant_permission", target=f"{employee_name}→{app_id}",
        details=f"scopes={valid}", ip_address=_get_client_ip(request),
    )

    # Re-fetch with correct filtering
    return RedirectResponse("/admin/permissions", status_code=303)


@router.post("/permissions/revoke", response_class=HTMLResponse)
async def revoke_permission(
    request: Request,
    employee_name: str = Form(...),
    app_id: str = Form(...),
    admin_token: str | None = Cookie(default=None),
    sqlite_session: AsyncSession = Depends(get_sqlite_session),
):
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

    deleted = await service.revoke_permission(sqlite_session, employee_name, app_id)
    if deleted:
        await _log_action(
            sqlite_session, admin_name, "revoke_permission", target=f"{employee_name}→{app_id}",
            ip_address=_get_client_ip(request),
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
        ip_address=_get_client_ip(request),
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
        ip_address=_get_client_ip(request),
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
            text("SELECT id, admin_name, action, target, details, ip_address, created_at "
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
            text(f"SELECT id, admin_name, action, target, details, ip_address, created_at "
                 f"FROM admin_audit_log WHERE admin_name = :ename OR {like_conditions} "
                 f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            params,
        )

    logs = [
        {"id": r[0], "admin_name": r[1], "action": r[2], "target": r[3],
         "details": r[4], "ip_address": r[5], "created_at": r[6]}
        for r in result.fetchall()
    ]

    total_pages = max(1, (total + page_size - 1) // page_size)
    ctx = _base_ctx(request, admin, "audit",
                    logs=logs, page=page, total_pages=total_pages, total=total)
    return templates.TemplateResponse("admin_audit_log.html", ctx)
