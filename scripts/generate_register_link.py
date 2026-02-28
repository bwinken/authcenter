"""Generate a registration link for a verified employee.

Usage:
    python scripts/generate_register_link.py <employee_name>
    python scripts/generate_register_link.py <employee_name> --app-id <app_id> --redirect-uri <uri>

The generated link contains a 24-hour registration token.
Admin should send this link to the employee's email.
"""

import argparse
import asyncio
import secrets
import sys
import time
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))
BASE_URL = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")
TOKEN_TTL = 86400  # 24 hours


async def generate_register_link(
    employee_name: str, app_id: str, redirect_uri: str, db_path: str
) -> None:
    employee_name = employee_name.lower().strip()

    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    async with aiosqlite.connect(db_path) as db:
        # Check if already registered
        cursor = await db.execute(
            "SELECT employee_name FROM user_accounts WHERE employee_name = ?",
            (employee_name,),
        )
        if await cursor.fetchone():
            print(f"[WARNING] {employee_name} already has an account.")
            print("If they need a password reset, use: python scripts/reset_password.py")
            sys.exit(1)

        # Generate token and store in SQLite
        token = secrets.token_urlsafe(48)
        expires_at = time.time() + TOKEN_TTL

        await db.execute(
            "INSERT OR REPLACE INTO registration_tokens (token, employee_name, app_id, redirect_uri, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, employee_name, app_id, redirect_uri, expires_at),
        )
        await db.commit()

    link = f"{BASE_URL}/auth/register?token={token}"

    print(f"[OK] Registration link generated for {employee_name}")
    print(f"     Expires in 24 hours")
    print()
    print(f"     {link}")
    print()
    print("Please send this link to the employee's email.")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a registration link for a verified employee"
    )
    parser.add_argument("employee_name", help="Employee name (e.g. kane.beh)")
    parser.add_argument(
        "--app-id", default="", help="App ID the employee was trying to access"
    )
    parser.add_argument(
        "--redirect-uri", default="", help="Redirect URI for post-registration login"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})"
    )
    args = parser.parse_args()

    asyncio.run(generate_register_link(args.employee_name, args.app_id, args.redirect_uri, args.db))


if __name__ == "__main__":
    main()
