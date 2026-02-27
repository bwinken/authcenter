"""Core authentication business logic."""

import json
import secrets
import time

from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserAccount
from app.schemas import StaffInfo

# In-memory authorization code store: code -> {staff_id, app_id, expires_at}
_auth_codes: dict[str, dict] = {}

AUTH_CODE_TTL = 300  # 5 minutes

SCOPE_MAP = {
    1: ["read"],
    2: ["read", "write"],
    3: ["read", "write", "admin"],
}


async def verify_staff(mysql_session: AsyncSession, staff_id: str) -> StaffInfo | None:
    """Check IT Master DB (MySQL) to confirm staff exists. Returns StaffInfo or None."""
    result = await mysql_session.execute(
        text("SELECT staff_id, name, dept_code, level FROM staff WHERE staff_id = :sid"),
        {"sid": staff_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    return StaffInfo(staff_id=row[0], name=row[1], dept_code=row[2], level=row[3])


async def check_account_exists(sqlite_session: AsyncSession, staff_id: str) -> bool:
    """Check if a user account already exists in the local Auth DB."""
    result = await sqlite_session.execute(
        text("SELECT 1 FROM user_accounts WHERE staff_id = :sid"),
        {"sid": staff_id},
    )
    return result.fetchone() is not None


async def register_account(
    sqlite_session: AsyncSession, staff_id: str, password: str
) -> None:
    """Create a new user account with a bcrypt-hashed password."""
    password_hash = bcrypt.hash(password)
    await sqlite_session.execute(
        text(
            "INSERT INTO user_accounts (staff_id, password_hash) VALUES (:sid, :ph)"
        ),
        {"sid": staff_id, "ph": password_hash},
    )
    await sqlite_session.commit()


async def authenticate(
    mysql_session: AsyncSession,
    sqlite_session: AsyncSession,
    staff_id: str,
    password: str,
) -> tuple[StaffInfo | None, str]:
    """Full authentication flow.

    Returns (StaffInfo, error_message).
    - On success: (staff_info, "")
    - On failure: (None, "reason")
    - Needs registration: (staff_info, "needs_registration")
    """
    # 1. Verify staff exists in MySQL
    staff = await verify_staff(mysql_session, staff_id)
    if staff is None:
        return None, "員工編號不存在，請確認後重試。"

    # 2. Check if account exists in SQLite
    has_account = await check_account_exists(sqlite_session, staff_id)
    if not has_account:
        return staff, "needs_registration"

    # 3. Verify password
    result = await sqlite_session.execute(
        text("SELECT password_hash FROM user_accounts WHERE staff_id = :sid"),
        {"sid": staff_id},
    )
    row = result.fetchone()
    if row is None or not bcrypt.verify(password, row[0]):
        return None, "密碼錯誤，請重新輸入。"

    return staff, ""


def map_scopes(level: int) -> list[str]:
    """Convert staff level to scope list."""
    return SCOPE_MAP.get(level, ["read"])


async def check_app_access(
    sqlite_session: AsyncSession,
    staff: StaffInfo,
    app_id: str,
) -> tuple[bool, str]:
    """Check if staff has permission to access the given app.

    Returns (allowed, reason).
    """
    result = await sqlite_session.execute(
        text("SELECT allowed_depts, min_level FROM app_access_rules WHERE app_id = :aid"),
        {"aid": app_id},
    )
    row = result.fetchone()
    if row is None:
        # No rule defined = allow by default
        return True, ""

    allowed_depts = json.loads(row[0]) if row[0] else []
    min_level = row[1]

    if allowed_depts and staff.dept_code not in allowed_depts:
        return False, f"您的部門 ({staff.dept_code}) 無權存取此應用程式。"

    if staff.level < min_level:
        return False, f"您的權限等級不足，此應用需要 Level {min_level} 以上。"

    return True, ""


def generate_auth_code(staff_id: str, app_id: str) -> str:
    """Generate a one-time authorization code (stored in memory, 5-min TTL)."""
    _cleanup_expired_codes()
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "staff_id": staff_id,
        "app_id": app_id,
        "expires_at": time.time() + AUTH_CODE_TTL,
    }
    return code


def consume_auth_code(code: str, app_id: str) -> str | None:
    """Validate and consume an authorization code.

    Returns staff_id if valid, None otherwise.
    """
    _cleanup_expired_codes()
    data = _auth_codes.pop(code, None)
    if data is None:
        return None
    if data["app_id"] != app_id:
        return None
    if time.time() > data["expires_at"]:
        return None
    return data["staff_id"]


def _cleanup_expired_codes() -> None:
    """Remove expired authorization codes."""
    now = time.time()
    expired = [k for k, v in _auth_codes.items() if now > v["expires_at"]]
    for k in expired:
        del _auth_codes[k]
