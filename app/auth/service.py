"""Core authentication business logic."""

import json
import logging
import secrets
import time
from collections import defaultdict

from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserAccount
from app.schemas import StaffInfo

logger = logging.getLogger(__name__)

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


def normalize_employee_name(name: str) -> str:
    """Normalize employee name to lowercase, stripped of whitespace."""
    return name.lower().strip()


# ─── Registration Tokens (SQLite-backed) ─────────────────────
REGISTRATION_TOKEN_TTL = 600           # 10 minutes (login → register-request flow)
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


SCOPE_MAP = {
    1: ["read"],
    2: ["read", "write"],
    3: ["read", "write", "admin"],
}


async def verify_staff(mysql_session: AsyncSession, employee_name: str) -> StaffInfo | None:
    """Check IT Master DB (MySQL) to confirm staff exists. Returns StaffInfo or None."""
    employee_name = normalize_employee_name(employee_name)
    result = await mysql_session.execute(
        text("SELECT staff_id, name, dept_code, level, ext FROM staff WHERE staff_id = :ename"),
        {"ename": employee_name},
    )
    row = result.fetchone()
    if row is None:
        return None
    return StaffInfo(
        employee_name=row[0], name=row[1], dept_code=row[2], level=row[3], ext=row[4] or ""
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
    mysql_session: AsyncSession,
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

    # 1. Verify staff exists in MySQL
    staff = await verify_staff(mysql_session, employee_name)
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


def map_scopes(level: int) -> list[str]:
    """Convert staff level to scope list."""
    return SCOPE_MAP.get(level, ["read"])


def _check_dept_level_access(staff: StaffInfo, app_info: dict) -> tuple[bool, str]:
    """Check if staff passes department + level rules from apps.yaml.

    Returns (allowed, reason).
    """
    allowed_depts = app_info.get("allowed_depts", []) or []
    min_level = app_info.get("min_level", 1)

    if allowed_depts and staff.dept_code not in allowed_depts:
        return False, f"您的部門 ({staff.dept_code}) 無權存取此應用程式。"

    if staff.level < min_level:
        return False, f"您的權限等級不足，此應用需要 Level {min_level} 以上。"

    return True, ""


async def check_app_access(
    sqlite_session: AsyncSession, staff: StaffInfo, app_info: dict
) -> tuple[bool, str, list[str]]:
    """Check if staff has permission to access the given app.

    Priority: per-user permission > dept/level fallback.
    Returns (allowed, reason, scopes).
    """
    app_id = app_info.get("app_id", "")

    # 1. Check per-user permission first
    perm = await get_user_app_permission(sqlite_session, staff.employee_name, app_id)
    if perm is not None:
        logger.info("Per-user permission found: %s → %s scopes=%s", staff.employee_name, app_id, perm["scopes"])
        return True, "", perm["scopes"]

    # 2. Fallback to dept/level rules
    allowed, reason = _check_dept_level_access(staff, app_info)
    if not allowed:
        logger.warning(
            "App access denied: %s (dept=%s, level=%d) tried to access %s",
            staff.employee_name, staff.dept_code, staff.level, app_id,
        )
        return False, reason, []

    return True, "", map_scopes(staff.level)


# ─── Per-User App Permissions ────────────────────────────────

async def get_user_app_permission(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> dict | None:
    """Get per-user permission for a specific app. Returns dict with scopes or None."""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text(
            "SELECT scopes, granted_by, granted_at FROM user_app_permissions "
            "WHERE employee_name = :ename AND app_id = :aid"
        ),
        {"ename": employee_name, "aid": app_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "scopes": json.loads(row[0]),
        "granted_by": row[1],
        "granted_at": row[2],
    }


async def get_user_accessible_apps(
    sqlite_session: AsyncSession,
    staff: StaffInfo,
    all_apps: dict[str, dict],
) -> list[dict]:
    """Get all apps accessible by a user (personal permissions + dept/level fallback).

    Returns list of dicts: [{app_id, name, scopes, source ("personal"/"dept_level"), redirect_uri}]
    """
    employee_name = normalize_employee_name(staff.employee_name)

    # Fetch all personal permissions
    result = await sqlite_session.execute(
        text("SELECT app_id, scopes FROM user_app_permissions WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    personal_perms = {row[0]: json.loads(row[1]) for row in result.fetchall()}

    accessible = []
    for app_id, app_info in all_apps.items():
        entry = {
            "app_id": app_id,
            "name": app_info.get("name", app_id),
            "redirect_uri": app_info.get("redirect_uri", ""),
        }

        if app_id in personal_perms:
            entry["scopes"] = personal_perms[app_id]
            entry["source"] = "personal"
            accessible.append(entry)
        else:
            allowed, _ = _check_dept_level_access(staff, app_info)
            if allowed:
                entry["scopes"] = map_scopes(staff.level)
                entry["source"] = "dept_level"
                accessible.append(entry)

    return accessible


async def grant_permission(
    sqlite_session: AsyncSession,
    employee_name: str,
    app_id: str,
    scopes: list[str],
    granted_by: str = "",
) -> None:
    """Grant or update per-user permission for an app."""
    employee_name = normalize_employee_name(employee_name)
    scopes_json = json.dumps(scopes)
    await sqlite_session.execute(
        text(
            "INSERT INTO user_app_permissions (employee_name, app_id, scopes, granted_by) "
            "VALUES (:ename, :aid, :scopes, :by) "
            "ON CONFLICT(employee_name, app_id) DO UPDATE SET scopes = :scopes, granted_by = :by, granted_at = datetime('now')"
        ),
        {"ename": employee_name, "aid": app_id, "scopes": scopes_json, "by": granted_by},
    )
    await sqlite_session.commit()
    logger.info("Permission granted: %s → %s scopes=%s by=%s", employee_name, app_id, scopes, granted_by)


async def revoke_permission(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> bool:
    """Revoke per-user permission. Returns True if a record was deleted."""
    employee_name = normalize_employee_name(employee_name)
    result = await sqlite_session.execute(
        text("DELETE FROM user_app_permissions WHERE employee_name = :ename AND app_id = :aid"),
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
        conditions.append("employee_name = :ename")
        params["ename"] = normalize_employee_name(employee_name)
    if app_id:
        conditions.append("app_id = :aid")
        params["aid"] = app_id

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await sqlite_session.execute(
        text(f"SELECT employee_name, app_id, scopes, granted_by, granted_at FROM user_app_permissions {where} ORDER BY employee_name, app_id"),
        params,
    )
    return [
        {
            "employee_name": row[0],
            "app_id": row[1],
            "scopes": json.loads(row[2]),
            "granted_by": row[3],
            "granted_at": row[4],
        }
        for row in result.fetchall()
    ]


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
