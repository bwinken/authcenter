"""Reset a user's password in the Auth Center local database.

Usage:
    python scripts/reset_password.py <staff_id>
    python scripts/reset_password.py <staff_id> --password <new_password>

If --password is not provided, a random 12-character password will be generated.
"""

import argparse
import secrets
import string
import sqlite3
import sys
from pathlib import Path

from passlib.hash import bcrypt
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))


def generate_random_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def reset_password(staff_id: str, new_password: str | None, db_path: str) -> None:
    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if account exists
    cursor.execute("SELECT staff_id FROM user_accounts WHERE staff_id = ?", (staff_id,))
    row = cursor.fetchone()
    if row is None:
        print(f"[ERROR] Account not found: {staff_id}")
        print("This employee has not registered yet. No password to reset.")
        conn.close()
        sys.exit(1)

    # Generate or use provided password
    password = new_password or generate_random_password()
    password_hash = bcrypt.hash(password)

    cursor.execute(
        "UPDATE user_accounts SET password_hash = ?, updated_at = datetime('now') WHERE staff_id = ?",
        (password_hash, staff_id),
    )
    conn.commit()
    conn.close()

    print(f"[OK] Password reset for {staff_id}")
    print(f"     New password: {password}")
    print()
    print("Please provide this password to the employee securely.")
    print("They should change it on next login if needed.")


def main():
    parser = argparse.ArgumentParser(description="Reset a user's password in Auth Center")
    parser.add_argument("staff_id", help="Employee staff ID (e.g. EMP001)")
    parser.add_argument("--password", "-p", help="New password (random if omitted)")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    args = parser.parse_args()

    if args.password and len(args.password) < 8:
        print("[ERROR] Password must be at least 8 characters.")
        sys.exit(1)

    reset_password(args.staff_id, args.password, args.db)


if __name__ == "__main__":
    main()
