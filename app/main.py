"""Auth Center - FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.database import sqlite_engine, SQLiteSessionLocal
from app.auth.routes import router as auth_router, init_templates
from app.admin.routes import router as admin_router
from app.auth.service import cleanup_expired_tokens

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = 3600  # Run cleanup every hour


async def _periodic_cleanup() -> None:
    """Background task: periodically clean up expired tokens."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            async with SQLiteSessionLocal() as session:
                await cleanup_expired_tokens(session)
        except Exception:
            logger.exception("Error during periodic token cleanup")


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
            CREATE TABLE IF NOT EXISTS user_app_permissions (
                employee_name VARCHAR(50)  NOT NULL,
                app_id        VARCHAR(100) NOT NULL,
                scopes        TEXT         NOT NULL DEFAULT '["read"]',
                granted_by    VARCHAR(50)  NOT NULL DEFAULT '',
                granted_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (employee_name, app_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_admins (
                employee_name VARCHAR(50)  NOT NULL,
                app_id        VARCHAR(100) NOT NULL,
                assigned_by   VARCHAR(50)  NOT NULL DEFAULT '',
                assigned_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (employee_name, app_id)
            )
        """))
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
        # Indexes for efficient expiry cleanup
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_auth_codes_expires_at ON auth_codes(expires_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_reg_tokens_expires_at ON registration_tokens(expires_at)"
        ))

    # Start background cleanup task
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    logger.info("Auth Center started, background cleanup scheduled every %ds", CLEANUP_INTERVAL)

    yield

    # Shutdown: cancel cleanup and dispose engines
    cleanup_task.cancel()
    await sqlite_engine.dispose()


app = FastAPI(
    title="Auth Center",
    description="Central SSO authentication service for internal AI applications.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - allow registered app origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to known app origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
init_templates(templates)

# Routes
app.include_router(auth_router)
app.include_router(admin_router)
