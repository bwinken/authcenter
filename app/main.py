"""Auth Center - FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import text

from app.database import sqlite_engine, mssql_engine, SQLiteSessionLocal
from app.auth.routes import router as auth_router, init_templates
from app.admin.routes import router as admin_router
from app.oidc.routes import router as oidc_router, init_templates as oidc_init_templates
from app.auth.service import cleanup_expired_tokens, cleanup_rate_limit_store
from app.config import load_registered_apps
from app.csrf import CSRFMiddleware, csrf_input


# ─── Loguru Setup ─────────────────────────────────────────────

class _InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _setup_logging() -> None:
    """Configure loguru sinks and intercept stdlib logging."""
    logger.remove()

    # Console sink (colorized)
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
        colorize=True,
    )

    # File sink (rotation + retention)
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    logger.add(
        str(logs_dir / "auth-center.log"),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="WARNING",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )

    # Intercept stdlib logging (uvicorn, sqlalchemy, etc.)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


_setup_logging()

CLEANUP_INTERVAL = 3600  # Run cleanup every hour


async def _periodic_cleanup() -> None:
    """Background task: periodically clean up expired tokens."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            async with SQLiteSessionLocal() as session:
                await cleanup_expired_tokens(session)
            cleanup_rate_limit_store()
        except Exception:
            logger.exception("Error during periodic cleanup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize SQLite tables + indexes
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_accounts (
                employee_name VARCHAR(50)  PRIMARY KEY,
                password_hash VARCHAR(255) NOT NULL,
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                code       VARCHAR(64)  PRIMARY KEY,
                employee_name VARCHAR(50) NOT NULL,
                app_id     VARCHAR(100) NOT NULL,
                expires_at REAL         NOT NULL
            )
        """))
        # Migration: add nonce column for OIDC support
        try:
            await conn.execute(text(
                "ALTER TABLE auth_codes ADD COLUMN nonce VARCHAR(255) DEFAULT ''"
            ))
        except Exception:
            pass  # Column already exists
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS registration_tokens (
                token      VARCHAR(64)  PRIMARY KEY,
                employee_name VARCHAR(50) NOT NULL,
                app_id     VARCHAR(100) DEFAULT '',
                redirect_uri TEXT       DEFAULT '',
                expires_at REAL         NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token         VARCHAR(64)  PRIMARY KEY,
                employee_name VARCHAR(50)  NOT NULL,
                expires_at    REAL         NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_reset_tokens_expires_at
            ON password_reset_tokens(expires_at)
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_app_permissions (
                employee_name VARCHAR(50)  NOT NULL,
                app_id        VARCHAR(100) NOT NULL,
                level         INTEGER      NOT NULL DEFAULT 1,
                granted_by    VARCHAR(50)  NOT NULL DEFAULT '',
                granted_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (employee_name, app_id)
            )
        """))
        # Migration: if old schema had 'scopes' column, add 'level' and migrate data
        cols_result = await conn.execute(text("PRAGMA table_info(user_app_permissions)"))
        col_names = [row[1] for row in cols_result.fetchall()]
        if "scopes" in col_names and "level" not in col_names:
            await conn.execute(text("ALTER TABLE user_app_permissions ADD COLUMN level INTEGER NOT NULL DEFAULT 1"))
            await conn.execute(text("""
                UPDATE user_app_permissions SET level = CASE
                    WHEN scopes LIKE '%admin%' THEN 3
                    WHEN scopes LIKE '%write%' THEN 2
                    ELSE 1
                END
            """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_admins (
                employee_name VARCHAR(50)  NOT NULL,
                app_id        VARCHAR(100) NOT NULL,
                assigned_by   VARCHAR(50)  NOT NULL DEFAULT '',
                assigned_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
                auto_assigned BOOLEAN      DEFAULT 0,
                PRIMARY KEY (employee_name, app_id)
            )
        """))
        # Migration: add auto_assigned column if missing (existing deployments)
        try:
            await conn.execute(text(
                "ALTER TABLE app_admins ADD COLUMN auto_assigned BOOLEAN DEFAULT 0"
            ))
        except Exception:
            pass  # Column already exists
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id            INTEGER      PRIMARY KEY AUTOINCREMENT,
                admin_name    VARCHAR(50)  NOT NULL,
                action        VARCHAR(100) NOT NULL,
                target        TEXT         DEFAULT '',
                details       TEXT         DEFAULT '',
                ip_address    VARCHAR(45)  DEFAULT '',
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_access_log (
                id              INTEGER      PRIMARY KEY AUTOINCREMENT,
                employee_name   VARCHAR(50)  NOT NULL,
                app_id          VARCHAR(100) NOT NULL,
                app_name        VARCHAR(200) DEFAULT '',
                ip_address      VARCHAR(45)  DEFAULT '',
                created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # Indexes for efficient expiry cleanup
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_auth_codes_expires_at ON auth_codes(expires_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_reg_tokens_expires_at ON registration_tokens(expires_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_access_log_created ON app_access_log(created_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_access_log_app ON app_access_log(app_id)"
        ))

    # Enable WAL mode for better concurrent read/write performance (safe for multi-worker)
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))

    # Start background cleanup task
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    logger.info("Auth Center started, background cleanup scheduled every %ds", CLEANUP_INTERVAL)

    yield

    # Shutdown: cancel cleanup and dispose engines
    cleanup_task.cancel()
    await mssql_engine.dispose()
    await sqlite_engine.dispose()


app = FastAPI(
    title="Auth Center",
    description="Central SSO authentication service for internal AI applications.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - derive allowed origins from registered apps' redirect_uri
def _get_cors_origins() -> list[str]:
    """Extract origins (scheme + host + port) from registered app redirect URIs."""
    from urllib.parse import urlparse
    origins = set()
    for info in load_registered_apps().values():
        uri = info.get("redirect_uri", "")
        if uri:
            parsed = urlparse(uri)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin and parsed.scheme:
                origins.add(origin)
    return list(origins) or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF protection (Double Submit Cookie)
app.add_middleware(CSRFMiddleware)

# Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["csrf_input"] = csrf_input
init_templates(templates)
oidc_init_templates(templates)

# Routes
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(oidc_router)


@app.get("/health")
async def health_check():
    """Health check — 驗證 SQLite 和 MSSQL 連線狀態。"""
    status = {"status": "ok", "sqlite": "ok", "mssql": "ok"}
    try:
        async with sqlite_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        status["sqlite"] = f"error: {e}"
        status["status"] = "degraded"
    try:
        async with mssql_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        status["mssql"] = f"error: {e}"
        status["status"] = "degraded"
    code = 200 if status["status"] == "ok" else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(status, status_code=code)


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    """使用者首頁 — 服務說明與常用功能導引。"""
    return templates.TemplateResponse("home.html", {"request": request})
