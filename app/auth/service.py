"""Core authentication business logic."""

import re
import secrets
import time
from collections import defaultdict

from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from loguru import logger

from app.config import get_settings
from app.models import UserAccount
from app.schemas import StaffInfo

AUTH_CODE_TTL = 300  # 5 minutes

# ─── Rate Limiting ────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 300   # 5-minute sliding window
RATE_LIMIT_MAX_ATTEMPTS = 10


def check_rate_limit(client_ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    attempts = _rate_limit_store[client_ip]
    _rate_limit_store[client_ip] = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    return len(_rate_limit_store[client_ip]) < RATE_LIMIT_MAX_ATTEMPTS


def record_attempt(client_ip: str) -> None:
    """Record a request attempt for rate limiting."""
    _rate_limit_store[client_ip].append(time.time())


def cleanup_rate_limit_store() -> None:
    """Remove IPs with no recent attempts. Called periodically to prevent memory leak."""
    now = time.time()
    stale = [ip for ip, ts in _rate_limit_store.items() if all(now - t >= RATE_LIMIT_WINDOW for t in ts)]
    for ip in stale:
        del _rate_limit_store[ip]


def normalize_employee_name(name: str) -> str:
    """Normalize employee name to lowercase, stripped of whitespace."""
    return name.lower().strip()


# ─── Password Policy ─────────────────────────────────────────

def validate_password(password: str, employee_name: str = "") -> str:
    """Validate password strength. Returns error message or empty string on success."""
    if len(password) < 8:
        return "密碼長度至少 8 個字元。"
    if not re.search(r"[A-Z]", password):
        return "密碼須包含至少一個大寫英文字母。"
    if not re.search(r"[a-z]", password):
        return "密碼須包含至少一個小寫英文字母。"
    if not re.search(r"\d", password):
        return "密碼須包含至少一個數字。"
    if employee_name and password.lower() == employee_name.lower():
        return "密碼不可與使用者名稱相同。"
    return ""


# ─── Registration Tokens (SQLite-backed) ─────────────────────
REGISTRATION_TOKEN_TTL = 86400         # 24 hours (login → register-request flow)
ADMIN_REGISTRATION_TOKEN_TTL = 86400   # 24 hours (admin-generated link)


async def generate_registration_token(
    sqlite_session: AsyncSession,
    employee_name: str,
    app_id: str,
    redirect_uri: str,
    ttl: int = REGISTRATION_TOKEN_TTL,
) -> str:
    """Generate a short-lived token stored in SQLite."""
    employee_name = normalize_employee_name(employee_name)
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl
    await sqlite_session.execute(
        text(
            "INSERT INTO registration_tokens (token, employee_name, app_id, redirect_uri, expires_at) "
            "VALUES (:token, :ename, :aid, :uri, :exp)"
        ),
        {"token": token, "ename": employee_name, "aid": app_id, "uri": redirect_uri, "exp": expires_at},
    )
    await sqlite_session.commit()
    logger.info("Registration token generated for %s (ttl=%ds)", employee_name, ttl)
    return token


async def consume_registration_token(
    sqlite_session: AsyncSession, token: str
) -> dict | None:
    """Validate and return registration token data. Does NOT delete (allows form resubmit)."""
    result = await sqlite_session.execute(
        text(
            "SELECT employee_name, app_id, redirect_uri, expires_at "
            "FROM registration_tokens WHERE token = :token"
        ),
        {"token": token},
    )
    row = result.fetchone()
    if row is None or time.time() > row[3]:
        return None
    return {"employee_name": row[0], "app_id": row[1], "redirect_uri": row[2]}


async def invalidate_registration_token(
    sqlite_session: AsyncSession, token: str
) -> None:
    """Remove a registration token after successful use."""
    await sqlite_session.execute(
        text("DELETE FROM registration_tokens WHERE token = :token"),
        {"token": token},
    )
    await sqlite_session.commit()


async def extend_registration_token(
    sqlite_session: AsyncSession, token: str, ttl: int = 86400
) -> None:
    """延長 registration token 的有效期，讓管理員在 Dashboard 看到待處理請求。"""
    new_expires = time.time() + ttl
    await sqlite_session.execute(
        text("UPDATE registration_tokens SET expires_at = :exp WHERE token = :token"),
        {"exp": new_expires, "token": token},
    )
    await sqlite_session.commit()


# ─── Level → Scopes Mapping ─────────────────────────────────

LEVEL_SCOPE_MAP = {
    0: [],
    1: ["read"],
    2: ["read", "write"],
    3: ["read", "write", "admin"],
}


def level_to_scopes(level: int) -> list[str]:
    """Convert per-app level to scope list."""
    return LEVEL_SCOPE_MAP.get(level, ["read"])


async def verify_staff(mssql_session: AsyncSession, employee_name: str) -> StaffInfo | None:
    """Check IT Master DB (MSSQL) to confirm staff exists. Returns StaffInfo or None.

    Note: level is NOT from MSSQL. It will be set to 0 as a placeholder.
    The actual per-app level comes from user_app_permissions in SQLite.
    """
    employee_name = normalize_employee_name(employee_name)
    table = get_settings().MSSQL_TABLE
    result = await mssql_session.execute(
        text(f"SELECT nt_account, org_id, extension FROM {table} WHERE nt_account = :ename"),
        {"ename": employee_name},
    )
    row = result.fetchone()
    if row is None:
        return None
    return StaffInfo(
        employee_name=row[0], org_id=row[1], level=0, extension=row[2] or ""
    )


async def check_account_exists(sqlite_session: AsyncSession, employee_name: str) -> bool:
    """Check if a user account already exists in the local Auth DB."""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text("SELECT 1 FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    return result.fetchone() is not None


async def register_account(
    sqlite_session: AsyncSession, employee_name: str, password: str
) -> None:
    """Create a new user account with a bcrypt-hashed password."""
    employee_name = normalize_employee_name(employee_name)
    password_hash = bcrypt.hash(password)
    await sqlite_session.execute(
        text(
            "INSERT INTO user_accounts (employee_name, password_hash) VALUES (:ename, :ph)"
        ),
        {"ename": employee_name, "ph": password_hash},
    )
    await sqlite_session.commit()
    logger.info("Account created for %s", employee_name)


async def change_password(
    sqlite_session: AsyncSession,
    employee_name: str,
    old_password: str,
    new_password: str,
) -> str:
    """Change a user's password. Returns empty string on success, error message on failure."""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text("SELECT password_hash FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    row = result.fetchone()
    if row is None:
        return "帳號不存在。"

    if not bcrypt.verify(old_password, row[0]):
        logger.warning("Change password failed for %s: wrong old password", employee_name)
        return "舊密碼錯誤。"

    new_hash = bcrypt.hash(new_password)
    await sqlite_session.execute(
        text(
            "UPDATE user_accounts SET password_hash = :ph, updated_at = datetime('now') "
            "WHERE employee_name = :ename"
        ),
        {"ph": new_hash, "ename": employee_name},
    )
    await sqlite_session.commit()
    logger.info("Password changed for %s", employee_name)
    return ""


# Dummy bcrypt hash for constant-time comparison on unknown users
_DUMMY_HASH = bcrypt.hash("__dummy__")


async def authenticate(
    mssql_session: AsyncSession,
    sqlite_session: AsyncSession,
    employee_name: str,
    password: str,
) -> tuple[StaffInfo | None, str]:
    """Full authentication flow.

    Returns (StaffInfo, error_message).
    - On success: (staff_info, "")
    - On failure: (None, "reason")
    - Needs registration: (staff_info, "needs_registration")
    """
    employee_name = normalize_employee_name(employee_name)
    generic_error = "使用者名稱或密碼錯誤，請重新輸入。"

    # 1. Verify staff exists in MSSQL
    staff = await verify_staff(mssql_session, employee_name)
    if staff is None:
        # Constant-time: still run bcrypt to prevent timing-based user enumeration
        bcrypt.verify(password, _DUMMY_HASH)
        logger.warning("Login failed: unknown employee_name=%s", employee_name)
        return None, generic_error

    # 2. Check if account exists in SQLite
    has_account = await check_account_exists(sqlite_session, employee_name)
    if not has_account:
        return staff, "needs_registration"

    # 3. Verify password
    result = await sqlite_session.execute(
        text("SELECT password_hash FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    row = result.fetchone()
    if row is None or not bcrypt.verify(password, row[0]):
        logger.warning("Login failed: wrong password for employee_name=%s", employee_name)
        return None, generic_error

    logger.info("Login succeeded for %s", employee_name)
    return staff, ""


def _check_org_access(staff: StaffInfo, app_info: dict) -> tuple[bool, str]:
    """Check if staff passes organization rules from apps.yaml.

    Returns (allowed, reason).
    """
    allowed_orgs = app_info.get("allowed_orgs", []) or []

    if allowed_orgs and staff.org_id not in allowed_orgs:
        return False, f"您的組織 ({staff.org_id}) 無權存取此應用程式。"

    return True, ""


def _get_org_default_level(staff: StaffInfo, app_info: dict) -> int:
    """取得使用者因組織而獲得的預設權限等級。回傳 0 表示無預設權限。

    只在 allowed_orgs 非空且 default_level > 0 時生效。
    """
    allowed_orgs = app_info.get("allowed_orgs") or []
    default_level = app_info.get("default_level", 0) or 0
    # 預設權限不允許 level 3（需逐人手動授予）
    default_level = min(default_level, 2)
    if not allowed_orgs or not default_level:
        return 0
    if staff.org_id in allowed_orgs:
        return default_level
    return 0


async def get_user_app_level(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> int | None:
    """Get per-user level for a specific app. Returns level int or None if no permission."""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text(
            "SELECT level FROM user_app_permissions "
            "WHERE employee_name = :ename AND app_id = :aid"
        ),
        {"ename": employee_name, "aid": app_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    return row[0]


async def check_app_access(
    sqlite_session: AsyncSession, staff: StaffInfo, app_info: dict
) -> tuple[bool, str, list[str]]:
    """Check if staff has permission to access the given app.

    Access logic:
    1. Personal level 優先：有明確記錄就直接採用（包括 0=明確拒絕），跳過組織檢查
    2. 無個人記錄時 fallback：先檢查組織 → 再用 org default level

    Returns (allowed, reason, scopes).
    """
    app_id = app_info.get("app_id", "")

    # 1. Personal level 優先（None = 無記錄，0 = 明確拒絕）
    personal_level = await get_user_app_level(sqlite_session, staff.employee_name, app_id)

    if personal_level is not None:
        # 有明確個人權限，直接採用，不受組織限制
        if personal_level <= 0:
            logger.warning(
                "App access denied: %s explicitly denied for %s (personal_level=0)",
                staff.employee_name, app_id,
            )
            return False, "您尚未被授權存取此應用程式，請聯繫管理員。", []
        scopes = level_to_scopes(personal_level)
        logger.info("App access granted: %s → %s personal_level=%d scopes=%s", staff.employee_name, app_id, personal_level, scopes)
        return True, "", scopes

    # 2. 無個人記錄，fallback 到組織預設
    allowed, reason = _check_org_access(staff, app_info)
    if not allowed:
        logger.warning(
            "App access denied: %s (org=%s) tried to access %s — org not allowed",
            staff.employee_name, staff.org_id, app_id,
        )
        return False, reason, []

    effective_level = _get_org_default_level(staff, app_info)
    if effective_level <= 0:
        logger.warning(
            "App access denied: %s has no permission for %s (no personal, org_default=%d)",
            staff.employee_name, app_id, effective_level,
        )
        return False, "您尚未被授權存取此應用程式，請聯繫管理員。", []

    scopes = level_to_scopes(effective_level)
    logger.info("App access granted: %s → %s org_default_level=%d scopes=%s", staff.employee_name, app_id, effective_level, scopes)
    return True, "", scopes


# ─── Per-User App Permissions ────────────────────────────────

async def get_user_accessible_apps(
    sqlite_session: AsyncSession,
    staff: StaffInfo,
    all_apps: dict[str, dict],
) -> list[dict]:
    """Get all apps accessible by a user.

    Merges per-user permissions with org-based default level.
    Personal level 優先：有記錄用 personal（0=拒絕），無記錄 fallback org default。

    Returns list of dicts: [{app_id, name, level, scopes, redirect_uri}]
    """
    employee_name = normalize_employee_name(staff.employee_name)

    # Fetch all personal permissions (key exists with value=0 means explicit deny)
    result = await sqlite_session.execute(
        text("SELECT app_id, level FROM user_app_permissions WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    personal_perms = {row[0]: row[1] for row in result.fetchall()}

    accessible = []
    for app_id, app_info in all_apps.items():
        if app_id in personal_perms:
            # 有明確個人權限，直接採用，不受組織限制
            effective_level = personal_perms[app_id]
        else:
            # 無個人記錄，需通過組織檢查才能用 org default
            allowed, _ = _check_org_access(staff, app_info)
            if not allowed:
                continue
            effective_level = _get_org_default_level(staff, app_info)

        if effective_level <= 0:
            continue

        accessible.append({
            "app_id": app_id,
            "name": app_info.get("name", app_id),
            "redirect_uri": app_info.get("redirect_uri", ""),
            "level": effective_level,
            "scopes": level_to_scopes(effective_level),
        })

    return accessible


async def set_user_level(
    sqlite_session: AsyncSession,
    employee_name: str,
    app_id: str,
    level: int,
    granted_by: str = "",
) -> None:
    """Grant or update per-user level for an app.

    Level 3 自動同步為 App Admin（auto_assigned=1）；
    從 level 3 降級時自動移除 auto_assigned 的 App Admin 記錄。
    """
    employee_name = normalize_employee_name(employee_name)
    level = max(0, min(3, level))
    await sqlite_session.execute(
        text(
            "INSERT INTO user_app_permissions (employee_name, app_id, level, granted_by) "
            "VALUES (:ename, :aid, :level, :by) "
            "ON CONFLICT(employee_name, app_id) DO UPDATE SET level = :level, granted_by = :by, granted_at = datetime('now')"
        ),
        {"ename": employee_name, "aid": app_id, "level": level, "by": granted_by},
    )

    # Level 3 ↔ App Admin 自動同步
    if level == 3:
        await sqlite_session.execute(
            text(
                "INSERT INTO app_admins (employee_name, app_id, assigned_by, auto_assigned) "
                "VALUES (:ename, :aid, :by, 1) "
                "ON CONFLICT(employee_name, app_id) DO UPDATE SET auto_assigned = 1, assigned_by = :by, assigned_at = datetime('now')"
            ),
            {"ename": employee_name, "aid": app_id, "by": granted_by},
        )
    else:
        # 降級時僅移除自動指派的 App Admin，手動指派的保留
        await sqlite_session.execute(
            text(
                "DELETE FROM app_admins "
                "WHERE employee_name = :ename AND app_id = :aid AND auto_assigned = 1"
            ),
            {"ename": employee_name, "aid": app_id},
        )

    await sqlite_session.commit()
    logger.info("Permission granted: %s → %s level=%d by=%s", employee_name, app_id, level, granted_by)


async def remove_user_permission(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> bool:
    """Remove per-user permission. Returns True if a record was deleted.

    同時移除自動指派的 App Admin 記錄。
    """
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text("DELETE FROM user_app_permissions WHERE employee_name = :ename AND app_id = :aid"),
        {"ename": employee_name, "aid": app_id},
    )
    # 撤銷權限時，同步移除自動指派的 App Admin
    await sqlite_session.execute(
        text(
            "DELETE FROM app_admins "
            "WHERE employee_name = :ename AND app_id = :aid AND auto_assigned = 1"
        ),
        {"ename": employee_name, "aid": app_id},
    )
    await sqlite_session.commit()
    deleted = result.rowcount > 0
    if deleted:
        logger.info("Permission revoked: %s → %s", employee_name, app_id)
    return deleted


async def list_permissions(
    sqlite_session: AsyncSession,
    employee_name: str | None = None,
    app_id: str | None = None,
) -> list[dict]:
    """List per-user permissions with optional filters."""
    conditions = []
    params: dict = {}
    if employee_name:
        conditions.append("employee_name LIKE :ename")
        params["ename"] = f"%{normalize_employee_name(employee_name)}%"
    if app_id:
        conditions.append("app_id LIKE :aid")
        params["aid"] = f"%{app_id}%"

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await sqlite_session.execute(
        text(f"SELECT employee_name, app_id, level, granted_by, granted_at FROM user_app_permissions {where} ORDER BY employee_name, app_id"),
        params,
    )
    return [
        {
            "employee_name": row[0],
            "app_id": row[1],
            "level": row[2],
            "scopes": level_to_scopes(row[2]),
            "granted_by": row[3],
            "granted_at": row[4],
        }
        for row in result.fetchall()
    ]


# ─── User Account Management ─────────────────────────────────

async def list_users(sqlite_session: AsyncSession) -> list[dict]:
    """列出所有已註冊帳號。"""
    result = await sqlite_session.execute(
        text("SELECT employee_name, created_at, updated_at FROM user_accounts ORDER BY employee_name")
    )
    return [
        {"employee_name": r[0], "created_at": r[1], "updated_at": r[2]}
        for r in result.fetchall()
    ]


async def admin_reset_password(
    sqlite_session: AsyncSession, employee_name: str, new_password: str | None = None
) -> str:
    """管理員強制重設密碼（不需舊密碼）。回傳新密碼明文。"""
    employee_name = normalize_employee_name(employee_name)
    if new_password is None:
        new_password = secrets.token_urlsafe(12)
    password_hash = bcrypt.hash(new_password)
    await sqlite_session.execute(
        text("UPDATE user_accounts SET password_hash = :hash WHERE employee_name = :ename"),
        {"hash": password_hash, "ename": employee_name},
    )
    await sqlite_session.commit()
    logger.info("Password reset by admin for %s", employee_name)
    return new_password


async def delete_user(sqlite_session: AsyncSession, employee_name: str) -> bool:
    """刪除使用者帳號及其所有權限記錄。"""
    employee_name = normalize_employee_name(employee_name)
    await sqlite_session.execute(
        text("DELETE FROM user_app_permissions WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    result = await sqlite_session.execute(
        text("DELETE FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    await sqlite_session.commit()
    logger.info("User deleted by admin: %s (rows=%d)", employee_name, result.rowcount)
    return result.rowcount > 0


async def generate_auth_code(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> str:
    """Generate a one-time authorization code (stored in SQLite, 5-min TTL)."""
    employee_name = normalize_employee_name(employee_name)
    code = secrets.token_urlsafe(32)
    expires_at = time.time() + AUTH_CODE_TTL
    await sqlite_session.execute(
        text(
            "INSERT INTO auth_codes (code, employee_name, app_id, expires_at) "
            "VALUES (:code, :ename, :aid, :exp)"
        ),
        {"code": code, "ename": employee_name, "aid": app_id, "exp": expires_at},
    )
    await sqlite_session.commit()
    logger.info("Auth code generated for %s (app=%s)", employee_name, app_id)
    return code


async def consume_auth_code(
    sqlite_session: AsyncSession, code: str, app_id: str
) -> str | None:
    """Validate and consume an authorization code atomically.

    Deletes first, then validates — prevents race condition where two
    concurrent requests could both consume the same code.
    Returns employee_name if valid, None otherwise.
    """
    # Atomically delete and fetch in one step
    result = await sqlite_session.execute(
        text(
            "DELETE FROM auth_codes WHERE code = :code "
            "RETURNING employee_name, app_id, expires_at"
        ),
        {"code": code},
    )
    row = result.fetchone()
    await sqlite_session.commit()

    if row is None:
        logger.warning("Auth code consumption failed: code not found")
        return None

    employee_name, stored_app_id, expires_at = row[0], row[1], row[2]
    if stored_app_id != app_id:
        logger.warning("Auth code consumption failed: app_id mismatch (expected=%s, got=%s)", stored_app_id, app_id)
        return None
    if time.time() > expires_at:
        logger.warning("Auth code consumption failed: code expired for %s", employee_name)
        return None

    logger.info("Auth code consumed for %s (app=%s)", employee_name, app_id)
    return employee_name


async def get_pending_registrations(sqlite_session: AsyncSession) -> list[dict]:
    """查詢所有未過期的 registration_tokens（待處理的註冊請求）。"""
    result = await sqlite_session.execute(
        text(
            "SELECT employee_name, app_id, redirect_uri, expires_at "
            "FROM registration_tokens WHERE expires_at > :now "
            "ORDER BY expires_at DESC"
        ),
        {"now": time.time()},
    )
    return [
        {
            "employee_name": r[0],
            "app_id": r[1],
            "redirect_uri": r[2],
            "expires_at": r[3],
        }
        for r in result.fetchall()
    ]


async def deny_pending_registration(
    sqlite_session: AsyncSession, employee_name: str
) -> bool:
    """拒絕待處理的註冊請求，刪除該員工所有 registration tokens。回傳是否有刪除記錄。"""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text("DELETE FROM registration_tokens WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    await sqlite_session.commit()
    deleted = result.rowcount > 0
    if deleted:
        logger.info("Registration denied for %s", employee_name)
    return deleted


async def cleanup_expired_tokens(sqlite_session: AsyncSession) -> None:
    """Remove expired auth codes and registration tokens. Called by background task."""
    result1 = await sqlite_session.execute(
        text("DELETE FROM auth_codes WHERE expires_at < :now"),
        {"now": time.time()},
    )
    result2 = await sqlite_session.execute(
        text("DELETE FROM registration_tokens WHERE expires_at < :now"),
        {"now": time.time()},
    )
    await sqlite_session.commit()
    deleted = result1.rowcount + result2.rowcount
    if deleted > 0:
        logger.info("Cleaned up %d expired tokens", deleted)
