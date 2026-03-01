# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start dev server (auto-reload)
fastapi dev app/main.py

# Start production server
fastapi run app/main.py

# Generate RSA key pair (first-time setup)
python generate_keys.py

# Run example AI App
fastapi dev example_app/main.py --port 8001

# CLI tools
python scripts/manage_permissions.py grant <user> <app_id> --scopes read,write
python scripts/manage_permissions.py revoke <user> <app_id>
python scripts/manage_permissions.py list [--user <user>] [--app <app_id>]
python scripts/generate_register_link.py <employee_name>
python scripts/reset_password.py <employee_name> [--password <new_pw>]
```

No test suite exists yet.

## Architecture

### Dual Database Pattern

MySQL (IT Master DB) is **read-only** — queries employee records (name, dept, level, ext) via raw SQL through `get_mysql_session`. SQLite (Auth Local DB) is **read-write** — stores accounts, auth codes, permissions, admin config via SQLAlchemy async ORM through `get_sqlite_session`. Tables are created in-code during `lifespan` startup (not via migrations).

### App Registry

Apps are registered in `config/apps.yaml` (not in DB). `load_registered_apps()` caches by file mtime and hot-reloads on change. `save_registered_apps()` writes back to YAML (used by admin CRUD). Each app has: `app_id`, bcrypt-hashed `client_secret`, `redirect_uri`, `name`, optional `allowed_depts`/`min_level`.

### Authentication Flow

`POST /auth/login` → verify staff (MySQL) → check account (SQLite) → bcrypt password → check app access rules → generate one-time auth code (5min TTL) → 303 redirect with `?code=xxx`. App backend then calls `POST /auth/token` with `code + client_secret` → gets RS256 JWT (12h).

### Permission Resolution (in service.py)

Per-user permissions (`user_app_permissions` table) take priority. If none set, falls back to `apps.yaml` dept/level rules with automatic scope mapping: level 1→`[read]`, 2→`[read,write]`, 3→`[read,write,admin]`.

### Two-Tier Admin

Super Admin credentials come from `.env` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`), verified with `hmac.compare_digest`. App Admin is an employee assigned via `app_admins` table — authenticates with their normal password, then checked against the table. Both get a separate `admin_token` cookie (JWT with `aud="auth-center-admin"`, 2h TTL). Admin routes use `_verify_admin_cookie()` and `_require_super()` guards.

### Key Patterns

- **Rate limiting**: In-memory sliding window in `service.py` (per-IP, 10 attempts / 5 min)
- **Timing-attack prevention**: Dummy `bcrypt.verify` on unknown users; `hmac.compare_digest` for admin
- **Auth code consumption**: Atomic `DELETE ... RETURNING` to prevent double-spend
- **Background cleanup**: Hourly asyncio task in lifespan clears expired tokens
- **Templates**: Jinja2 initialized in `main.py`, passed to routers via `init_templates()`

## Conventions

- Language: Code in English, UI/docstrings/comments in Traditional Chinese
- All routes return Jinja2 HTML (no SPA) except `POST /auth/token` which returns JSON
- Admin routes are in `app/admin/routes.py`, user auth routes in `app/auth/routes.py`
- RSA keys in `keys/` directory (gitignored), paths configured via `.env`
- `passlib[bcrypt]` requires `bcrypt==4.0.1` (pinned — bcrypt 5.x breaks passlib)
