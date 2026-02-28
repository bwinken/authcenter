"""Manage per-user app permissions in the Auth Center local database.

Usage:
    python scripts/manage_permissions.py grant <employee_name> <app_id> --scopes read,write
    python scripts/manage_permissions.py revoke <employee_name> <app_id>
    python scripts/manage_permissions.py list
    python scripts/manage_permissions.py list --user <employee_name>
    python scripts/manage_permissions.py list --app <app_id>
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))

VALID_SCOPES = {"read", "write", "admin"}


async def grant(employee_name: str, app_id: str, scopes: list[str], granted_by: str, db_path: str) -> None:
    employee_name = employee_name.lower().strip()

    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        print(f"[ERROR] Invalid scopes: {invalid}. Valid scopes: {VALID_SCOPES}")
        sys.exit(1)

    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    scopes_json = json.dumps(sorted(scopes))

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO user_app_permissions (employee_name, app_id, scopes, granted_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(employee_name, app_id) DO UPDATE SET scopes = ?, granted_by = ?, granted_at = datetime('now')",
            (employee_name, app_id, scopes_json, granted_by, scopes_json, granted_by),
        )
        await db.commit()

    print(f"[OK] Permission granted: {employee_name} → {app_id}")
    print(f"     Scopes: {scopes}")
    if granted_by:
        print(f"     Granted by: {granted_by}")


async def revoke(employee_name: str, app_id: str, db_path: str) -> None:
    employee_name = employee_name.lower().strip()

    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM user_app_permissions WHERE employee_name = ? AND app_id = ?",
            (employee_name, app_id),
        )
        await db.commit()

        if cursor.rowcount == 0:
            print(f"[WARNING] No permission found for {employee_name} → {app_id}")
        else:
            print(f"[OK] Permission revoked: {employee_name} → {app_id}")


async def list_permissions(user: str | None, app: str | None, db_path: str) -> None:
    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        sys.exit(1)

    conditions = []
    params: list = []
    if user:
        conditions.append("employee_name = ?")
        params.append(user.lower().strip())
    if app:
        conditions.append("app_id = ?")
        params.append(app)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            f"SELECT employee_name, app_id, scopes, granted_by, granted_at "
            f"FROM user_app_permissions {where} ORDER BY employee_name, app_id",
            params,
        )
        rows = await cursor.fetchall()

    if not rows:
        print("[INFO] No permissions found.")
        return

    # Print table
    print(f"{'Employee':<20} {'App ID':<20} {'Scopes':<25} {'Granted By':<15} {'Granted At'}")
    print("-" * 100)
    for row in rows:
        scopes = json.loads(row[2])
        print(f"{row[0]:<20} {row[1]:<20} {', '.join(scopes):<25} {row[3]:<15} {row[4] or ''}")

    print(f"\nTotal: {len(rows)} permission(s)")


def main():
    parser = argparse.ArgumentParser(description="Manage per-user app permissions")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # grant
    grant_parser = subparsers.add_parser("grant", help="Grant permission to a user for an app")
    grant_parser.add_argument("employee_name", help="Employee name (e.g. kane.beh)")
    grant_parser.add_argument("app_id", help="App ID (e.g. ai_chat_app)")
    grant_parser.add_argument("--scopes", required=True, help="Comma-separated scopes (read,write,admin)")
    grant_parser.add_argument("--granted-by", default="", help="Admin name who granted this")
    grant_parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")

    # revoke
    revoke_parser = subparsers.add_parser("revoke", help="Revoke permission from a user for an app")
    revoke_parser.add_argument("employee_name", help="Employee name (e.g. kane.beh)")
    revoke_parser.add_argument("app_id", help="App ID (e.g. ai_chat_app)")
    revoke_parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")

    # list
    list_parser = subparsers.add_parser("list", help="List permissions")
    list_parser.add_argument("--user", help="Filter by employee name")
    list_parser.add_argument("--app", help="Filter by app ID")
    list_parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")

    args = parser.parse_args()

    if args.command == "grant":
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        asyncio.run(grant(args.employee_name, args.app_id, scopes, args.granted_by, args.db))
    elif args.command == "revoke":
        asyncio.run(revoke(args.employee_name, args.app_id, args.db))
    elif args.command == "list":
        asyncio.run(list_permissions(args.user, args.app, args.db))


if __name__ == "__main__":
    main()
