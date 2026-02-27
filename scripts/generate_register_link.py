"""Generate a registration link for a verified employee.

Usage:
    python scripts/generate_register_link.py <staff_id>
    python scripts/generate_register_link.py <staff_id> --app-id <app_id> --redirect-uri <uri>

The generated link contains a 24-hour registration token.
Admin should send this link to the employee's email.
"""

import argparse
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))
BASE_URL = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")
TOKEN_TTL = 86400  # 24 hours


def generate_register_link(
    staff_id: str, app_id: str, redirect_uri: str, db_path: str
) -> None:
    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if already registered
    cursor.execute(
        "SELECT staff_id FROM user_accounts WHERE staff_id = ?", (staff_id,)
    )
    if cursor.fetchone():
        print(f"[WARNING] {staff_id} already has an account.")
        print("If they need a password reset, use: python scripts/reset_password.py")
        conn.close()
        sys.exit(1)

    # Generate token and store in SQLite
    token = secrets.token_urlsafe(48)
    expires_at = time.time() + TOKEN_TTL

    cursor.execute(
        "INSERT OR REPLACE INTO registration_tokens (token, staff_id, app_id, redirect_uri, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (token, staff_id, app_id, redirect_uri, expires_at),
    )
    conn.commit()
    conn.close()

    link = f"{BASE_URL}/auth/register?token={token}"

    print(f"[OK] Registration link generated for {staff_id}")
    print(f"     Expires in 24 hours")
    print()
    print(f"     {link}")
    print()
    print("Please send this link to the employee's email.")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a registration link for a verified employee"
    )
    parser.add_argument("staff_id", help="Employee staff ID (e.g. EMP001)")
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

    generate_register_link(args.staff_id, args.app_id, args.redirect_uri, args.db)


if __name__ == "__main__":
    main()
