"""Core authentication business logic."""

import secrets
import time
from collections import defaultdict

from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    await _cleanup_expired_registration_tokens(sqlite_session)
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
    return token


async def consume_registration_token(
    sqlite_session: AsyncSession, token: str
) -> dict | None:
    """Validate and return registration token data. Does NOT delete (allows form resubmit)."""
    await _cleanup_expired_registration_tokens(sqlite_session)
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


async def _cleanup_expired_registration_tokens(sqlite_session: AsyncSession) -> None:
    await sqlite_session.execute(
        text("DELETE FROM registration_tokens WHERE expires_at < :now"),
        {"now": time.time()},
    )
    await sqlite_session.commit()

SCOPE_MAP = {
    1: ["read"],
    2: ["read", "write"],
    3: ["read", "write", "admin"],
}


async def verify_staff(mysql_session: AsyncSession, employee_name: str) -> StaffInfo | None:
    """Check IT Master DB (MySQL) to confirm staff exists. Returns StaffInfo or None."""
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
    result = await sqlite_session.execute(
        text("SELECT 1 FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    return result.fetchone() is not None


async def register_account(
    sqlite_session: AsyncSession, employee_name: str, password: str
) -> None:
    """Create a new user account with a bcrypt-hashed password."""
    password_hash = bcrypt.hash(password)
    await sqlite_session.execute(
        text(
            "INSERT INTO user_accounts (employee_name, password_hash) VALUES (:ename, :ph)"
        ),
        {"ename": employee_name, "ph": password_hash},
    )
    await sqlite_session.commit()


async def change_password(
    sqlite_session: AsyncSession,
    employee_name: str,
    old_password: str,
    new_password: str,
) -> str:
    """Change a user's password. Returns empty string on success, error message on failure."""
    result = await sqlite_session.execute(
        text("SELECT password_hash FROM user_accounts WHERE employee_name = :ename"),
        {"ename": employee_name},
    )
    row = result.fetchone()
    if row is None:
        return "帳號不存在。"

    if not bcrypt.verify(old_password, row[0]):
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
    return ""


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
    # 1. Verify staff exists in MySQL
    staff = await verify_staff(mysql_session, employee_name)
    if staff is None:
        return None, "使用者名稱不存在，請確認後重試。"

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
        return None, "密碼錯誤，請重新輸入。"

    return staff, ""


def map_scopes(level: int) -> list[str]:
    """Convert staff level to scope list."""
    return SCOPE_MAP.get(level, ["read"])


def check_app_access(staff: StaffInfo, app_info: dict) -> tuple[bool, str]:
    """Check if staff has permission to access the given app.

    Reads allowed_depts and min_level from apps.yaml config.
    Returns (allowed, reason).
    """
    allowed_depts = app_info.get("allowed_depts", []) or []
    min_level = app_info.get("min_level", 1)

    if allowed_depts and staff.dept_code not in allowed_depts:
        return False, f"您的部門 ({staff.dept_code}) 無權存取此應用程式。"

    if staff.level < min_level:
        return False, f"您的權限等級不足，此應用需要 Level {min_level} 以上。"

    return True, ""


async def generate_auth_code(
    sqlite_session: AsyncSession, employee_name: str, app_id: str
) -> str:
    """Generate a one-time authorization code (stored in SQLite, 5-min TTL)."""
    await _cleanup_expired_codes(sqlite_session)
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
    return code


async def consume_auth_code(
    sqlite_session: AsyncSession, code: str, app_id: str
) -> str | None:
    """Validate and consume an authorization code.

    Returns employee_name if valid, None otherwise.
    """
    await _cleanup_expired_codes(sqlite_session)
    result = await sqlite_session.execute(
        text("SELECT employee_name, app_id, expires_at FROM auth_codes WHERE code = :code"),
        {"code": code},
    )
    row = result.fetchone()
    if row is None:
        return None

    # Delete immediately (one-time use)
    await sqlite_session.execute(
        text("DELETE FROM auth_codes WHERE code = :code"),
        {"code": code},
    )
    await sqlite_session.commit()

    employee_name, stored_app_id, expires_at = row[0], row[1], row[2]
    if stored_app_id != app_id:
        return None
    if time.time() > expires_at:
        return None
    return employee_name


async def _cleanup_expired_codes(sqlite_session: AsyncSession) -> None:
    """Remove expired authorization codes."""
    await sqlite_session.execute(
        text("DELETE FROM auth_codes WHERE expires_at < :now"),
        {"now": time.time()},
    )
    await sqlite_session.commit()
