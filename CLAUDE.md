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
python scripts/manage_permissions.py grant <user> <app_id> --level 2
python scripts/manage_permissions.py revoke <user> <app_id>
python scripts/manage_permissions.py list [--user <user>] [--app <app_id>]
python scripts/generate_register_link.py <employee_name>
python scripts/reset_password.py <employee_name> [--password <new_pw>]

# Preflight check (verify deployment environment)
python scripts/preflight_check.py
python scripts/preflight_check.py --test-user <employee_name>
```

No test suite exists yet.

## Architecture

### Dual Database Pattern

MSSQL (IT Master DB) is **read-only** — queries employee records (`nt_account`, `org_id`, `extension`) via raw SQL through `get_mssql_session`. SQLite (Auth Local DB) is **read-write** — stores accounts, auth codes, permissions, admin config via SQLAlchemy async ORM through `get_sqlite_session`. Tables are created in-code during `lifespan` startup (not via migrations).

### App Registry

Apps are registered in `config/apps.yaml` (not in DB). `load_registered_apps()` caches by file mtime and hot-reloads on change. `save_registered_apps()` writes back to YAML (used by admin CRUD). Each app has: `app_id`, bcrypt-hashed `client_secret`, `redirect_uri`, `name`, optional `allowed_orgs`.

### Authentication Flow

`POST /auth/login` → verify staff (MSSQL) → check account (SQLite) → bcrypt password → check app access rules → generate one-time auth code (5min TTL) → 303 redirect with `?code=xxx`. App backend then calls `POST /auth/token` with `code + client_secret` → gets RS256 JWT (12h).

### OIDC Provider

Standard OIDC endpoints in `app/oidc/` for OAuth2 proxy integration. Discovery at `/.well-known/openid-configuration`, JWKS at `/.well-known/jwks.json`, authorize at `/oidc/authorize`, token at `/oidc/token` (returns `access_token` + `id_token`), userinfo at `/oidc/userinfo`. ID token uses `AUTH_CENTER_BASE_URL` as `iss` (vs `"auth-center"` for access tokens). Auth codes store `nonce` for OIDC replay protection. Supports `client_secret_post` and `client_secret_basic`.

### Permission Model (Per-User-Per-App Level)

Each user must have an explicit entry in `user_app_permissions` to access an app. No permission entry = access denied (403). The `level` integer maps to scopes automatically: 0→`[]`(denied), 1→`[read]`, 2→`[read,write]`, 3→`[read,write,admin]`. Level 0 explicitly denies access even if organization default would allow it. **Personal permission overrides org restriction**: if a user has an explicit `user_app_permissions` entry, access is determined solely by that level — `allowed_orgs` is NOT checked. Organization-based filtering via `allowed_orgs` only applies when there is no personal permission entry (fallback to org default). Default permission (`default_level`) only supports Level 1 and 2; Level 3 must be explicitly granted per user.

### Level 3 ↔ App Admin Auto-Sync

Granting Level 3 permission automatically assigns the user as App Admin (`auto_assigned=1`). Downgrading or revoking removes the auto-assigned entry but preserves manually assigned App Admin status. The `app_admins` table has an `auto_assigned` BOOLEAN column to distinguish.

### Two-Tier Admin

Super Admin supports two login methods: (1) `.env` fixed credentials (`ADMIN_USERNAME`/`ADMIN_PASSWORD`), verified with `hmac.compare_digest`; (2) employees listed in `SUPER_ADMIN_EMPLOYEES` env var (comma-separated), who authenticate with their normal password. App Admin is an employee assigned via `app_admins` table (manually or auto-assigned from Level 3) — authenticates with their normal password, then checked against the table. App Admin can edit their app's settings (allowed_orgs, default_level, token_expire_hours) but cannot create/delete apps. Both get a separate `admin_token` cookie (JWT with `aud="auth-center-admin"`, 2h TTL). Admin routes use `_verify_admin_cookie()` and `_require_super()` guards.

### Key Patterns

- **Rate limiting**: In-memory sliding window in `service.py` (per-IP, 10 attempts / 5 min)
- **Timing-attack prevention**: Dummy `bcrypt.verify` on unknown users; `hmac.compare_digest` for admin
- **Auth code consumption**: Atomic `DELETE ... RETURNING` to prevent double-spend
- **Background cleanup**: Hourly asyncio task in lifespan clears expired tokens
- **App access logging**: `app_access_log` table records each token exchange (user, app, IP, timestamp)
- **Templates**: Jinja2 initialized in `main.py`, passed to routers via `init_templates()`

## Conventions

- Language: Code in English, UI/docstrings/comments in Traditional Chinese
- All routes return Jinja2 HTML (no SPA) except `POST /auth/token` and OIDC endpoints which return JSON
- Admin routes are in `app/admin/routes.py`, user auth routes in `app/auth/routes.py`, OIDC routes in `app/oidc/routes.py`
- RSA keys in `keys/` directory (gitignored), paths configured via `.env`
- `passlib[bcrypt]` requires `bcrypt==4.0.1` (pinned — bcrypt 5.x breaks passlib)
